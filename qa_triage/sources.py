"""Read teuthology results: the job list from pulpito, and job files either
over ssh on a host that has the archive, or from qa-proxy over HTTPS."""

import gzip
import html
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from fnmatch import fnmatch

from . import archive_host

PULPITO = 'https://pulpito.ceph.com'
QA_PROXY = 'https://qa-proxy.ceph.com/teuthology'
TRACKER = 'https://tracker.ceph.com'
QA_PROJECT = 49  # "Ceph QA": one issue per batch of PRs under test
TIMEOUT = 300
# How often a dropped connection is tried, and the pause before the first retry.
RETRIES, RETRY_PAUSE = 3, 5
# Files downloaded over https are kept in the cache, each downloaded once: up to
# this size as they are, and bigger ones gzipped, unless they were gzipped already:
# those are streamed for every search.
CACHE_LIMIT = 64 << 20
# The suffix of a file kept gzipped, in the cache, as it was not on the server.
KEPT_GZIPPED = '.kept.gz'
# Where job files come from unless the config says otherwise: a host that has the
# archive, as ssh names it, and the directory the runs are in there.
ARCHIVE = 'trial200:/a'
# What to do when the archive's host does not answer over ssh.
UNREACHED = ('connect to the sepia VPN; or give --archive another host that has the '
             'archive; or --https to download the job files from qa-proxy, which is much '
             'slower')
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'archive_host.py')
# One ssh connection serves every call to a host for a minute.
SSH_OPTIONS = ('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'ControlMaster=auto',
               '-o', 'ControlPersist=60')
# The most calls one process makes at once over its connection: sshd refuses more
# than ten sessions on one, by default.
SSH_AT_ONCE = 8
_SESSIONS = threading.BoundedSemaphore(SSH_AT_ONCE)
# What a truncated .gz file raises, as a job that died can leave one.
TRUNCATED = (EOFError, gzip.BadGzipFile, zlib.error)


class ArchiveError(Exception):
    pass


@dataclass
class Job:
    run: str
    id: str
    status: str
    description: str
    reason: str

    @property
    def path(self):
        return f'{self.run}/{self.id}'


def _open(url):
    headers = {'User-Agent': 'qa_triage'}
    if _tracker_key and url.startswith(TRACKER + '/'):
        headers['X-Redmine-API-Key'] = _tracker_key
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                  timeout=TIMEOUT)


# The tracker's API key, to read it with: None to read it anonymously.
_tracker_key = None


def use_tracker_key(key):
    """Read the tracker with key from now on; with None, anonymously."""
    global _tracker_key
    _tracker_key = key


def _get_text(url, tries=RETRIES):
    """The text at url. A dropped connection is tried again, after a pause, as a
    busy server drops some; an HTTP error is not."""
    for attempt in range(tries):
        try:
            with _open(url) as r:
                return r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError) as e:
            if attempt == tries - 1:
                raise urllib.error.URLError(e) from e
            time.sleep(RETRY_PAUSE * (attempt + 1))


def run_name(arg):
    """Return the run name from a run name or any pulpito/qa-proxy URL."""
    path = urllib.parse.urlparse(arg).path if '://' in arg else arg
    parts = [p for p in urllib.parse.unquote(path).split('/')
             if p and p not in ('teuthology', 'a')]
    if not parts:
        raise ValueError(f'{arg} names no run: give a run\'s name, or its pulpito URL, or a '
                         'pulpito page that lists runs, such as a branch\'s')
    return parts[0]


# A link to a run on a pulpito page: who scheduled it and when, then its suite,
# branch and so on.
_RUN_LINK = re.compile(r'href="/([\w.+-]+-\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}-[^"/?#]+)/?"')


def list_runs(url):
    """The runs a pulpito page lists, such as a branch's
    (https://pulpito.ceph.com/?branch=<branch>), in its order: newest first."""
    return list(dict.fromkeys(urllib.parse.unquote(run)
                              for run in _RUN_LINK.findall(_get_text(url))))


def _cell(row, title):
    found = re.search(rf'data-title="{title}">(.*?)</td>', row, re.S)
    return _strip(found.group(1)) if found else ''


def list_builds(url):
    """The runs a pulpito page lists, in its order, each with what its row says: {run,
    user, suite, branch, sha1 (its build), queued, waiting, running}, the last three
    the numbers of its jobs."""
    found = []
    for row in _get_text(url).split('<tr')[1:]:
        run = re.search(r'data-title="User">\s*<a href="/([^"]+)"', row)
        sha1 = re.search(r'href="/\?sha1=([0-9a-f]{40})"', row)
        if not (run and sha1):
            continue
        counts = {name.lower(): int(_cell(row, name)) if _cell(row, name).isdigit() else 0
                  for name in ('Queued', 'Waiting', 'Running')}
        found.append({'run': urllib.parse.unquote(run.group(1)), 'user': _cell(row, 'User'),
                      'suite': _cell(row, 'Suite'), 'branch': _cell(row, 'Branch'),
                      'sha1': sha1.group(1), **counts})
    return found


_ROW = re.compile(r'<tr class="[^"]*job job_(\w+)" data-toggle="collapse" '
                  r'data-target="#(\d+)">(.*?)</tr>'
                  r'(\s*<tr class="tablesorter-childRow[^"]*">.*?</tr>)?', re.S)


def _strip(fragment):
    return html.unescape(re.sub(r'<[^>]+>', '', fragment)).strip()


def parse_run_page(run, page):
    jobs = []
    for status, jid, cells, extra in _ROW.findall(page):
        desc = re.search(r'data-title="Description">(.*?)</td>', cells, re.S)
        reason = re.search(r'<p class="code-text">(.*?)</p>', extra or '', re.S)
        jobs.append(Job(run, jid, status,
                        _strip(desc.group(1)) if desc else '',
                        ' '.join(_strip(reason.group(1)).split()) if reason else ''))
    return jobs


def list_jobs(run):
    return parse_run_page(run, _get_text(f'{PULPITO}/{urllib.parse.quote(run)}/'))


def _issues(query):
    return json.loads(_get_text(f'{TRACKER}/issues.json?{query}'))['issues']


def tracker_issues(query, page=100):
    """Every tracker issue the query lists, page by page, without their history."""
    offset = 0
    while True:
        found = json.loads(_get_text(f'{TRACKER}/issues.json?{query}&limit={page}'
                                     f'&offset={offset}'))
        yield from found['issues']
        offset += len(found['issues'])
        if not found['issues'] or offset >= found.get('total_count', 0):
            return


def job_name(arg):
    """(run, job id) from "<run>/<job>" or a pulpito or qa-proxy URL of a job."""
    path = urllib.parse.urlparse(arg).path if '://' in arg else arg
    parts = [p for p in urllib.parse.unquote(path).split('/')
             if p and p not in ('teuthology', 'a')]
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f'"{arg}" does not name a job: give <run>/<job>')
    return parts[0], parts[1]


def tracker_issue(arg):
    """The tracker issue for a number or tracker URL, with its relations and its
    history: the comments people wrote on it among them."""
    number = re.search(r'(\d+)/?$', str(arg)).group(1)
    return json.loads(_get_text(f'{TRACKER}/issues/{number}.json?include=relations,journals')
                      )['issue']


GITHUB = 'https://api.github.com'
# A GitHub token to read and post with, from the github_key setting's file, when gh is
# not signed in and $GITHUB_TOKEN is not set; None for none.
_github_key = None


def use_github_key(token):
    """Use token for GitHub from now on, where gh is not signed in and $GITHUB_TOKEN
    is not set; with None, none."""
    global _github_key
    _github_key = token


def _token():
    return os.environ.get('GITHUB_TOKEN') or _github_key


def github_token(path=None, environ=os.environ):
    """A GitHub token: $GITHUB_TOKEN, or else the file path names, which holds the
    token, or a JSON object with it as "github_token" or "github.com_token"; None if
    there is none."""
    if environ.get('GITHUB_TOKEN'):
        return environ['GITHUB_TOKEN'].strip()
    if not path:
        return None
    try:
        with open(os.path.expanduser(path)) as f:
            text = f.read().strip()
    except OSError:
        return None
    if text.startswith('{'):
        found = json.loads(text)
        return found.get('github_token') or found.get('github.com_token')
    return text or None


def github_user(token):
    """(whose token it is, as GitHub names them, [the scopes of a classic token], or
    None for a fine-grained token, whose permissions GitHub does not say): a check that
    GitHub takes it."""
    req = urllib.request.Request(f'{GITHUB}/user', headers={
        'User-Agent': 'qa_triage', 'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=60) as r:
        scopes = r.headers.get('X-OAuth-Scopes')
        login = json.load(r)['login']
    return login, None if scopes is None else [x.strip() for x in scopes.split(',') if x.strip()]


def _github(path):
    """An answer of GitHub's API: through gh, if it is installed and signed in, or
    else directly, with a GitHub token if there is one ($GITHUB_TOKEN, or the
    github_key setting's); without either, GitHub allows only a few requests an
    hour."""
    if shutil.which('gh'):
        p = subprocess.run(['gh', 'api', path], capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            return json.loads(p.stdout)
    headers = {'User-Agent': 'qa_triage', 'Accept': 'application/vnd.github+json'}
    if _token():
        headers['Authorization'] = f'Bearer {_token()}'
    req = urllib.request.Request(f'{GITHUB}/{path}', headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


_PRS = {}
# {number: (title, state)} of the pull requests looked up already.
_TITLES = {}


def pr_title(number, repo='ceph/ceph'):
    """(a pull request's title, its state: open, closed or merged), or None if GitHub
    cannot say."""
    if number not in _TITLES:
        try:
            pr = _github(f'repos/{repo}/pulls/{number}')
        except (urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError):
            return None
        _TITLES[number] = (pr.get('title', ''), 'merged' if pr.get('merged_at') else
                           pr.get('state', ''))
    return _TITLES[number]


def pr_of_branch(url, branch, repo='ceph/ceph'):
    """The number of the pull request whose head is branch of the fork at url (a
    github.com URL), or None."""
    owner = re.search(r'github\.com[/:]([\w.-]+)/', url or '')
    if not owner:
        return None
    key = (owner.group(1), branch)
    if key not in _PRS:
        try:
            found = _github(f'repos/{repo}/pulls?head={owner.group(1)}:{branch}'
                            '&state=all&per_page=1')
            _PRS[key] = str(found[0]['number']) if found else None
        except (OSError, ValueError, KeyError, IndexError):
            _PRS[key] = None
    return _PRS[key]


def pull_request(number, repo='ceph/ceph'):
    """A pull request: its title, state, description and merge commit, and the
    files it changed, each with the functions its changes are in, as the diff's
    hunk headers name them."""
    pr = _github(f'repos/{repo}/pulls/{number}')
    files = _github(f'repos/{repo}/pulls/{number}/files?per_page=100')
    return {'number': str(number), 'title': pr.get('title', ''), 'state': pr.get('state', ''),
            'merged': bool(pr.get('merged_at')), 'merge_commit': pr.get('merge_commit_sha'),
            'body': pr.get('body') or '',
            'files': [(f['filename'], list(dict.fromkeys(
                name.strip() for name in re.findall(r'^@@[^@]*@@ ?(.*)$', f.get('patch') or '',
                                                    re.M) if name.strip())))
                      for f in files]}


def search_pulls(words, repo='ceph/ceph', most=20):
    """[(number, title, state)] of the pull requests of repo that have all of words,
    in their title or description; merged ones say so."""
    query = urllib.parse.quote(f'repo:{repo} is:pr {words}')
    found = _github(f'search/issues?q={query}&per_page={most}')
    return [(str(item['number']), item['title'],
             'merged' if (item.get('pull_request') or {}).get('merged_at') else item['state'])
            for item in found.get('items', [])]


def _graphql(query):
    """The data of an answer of GitHub's GraphQL API, which needs a signed-in gh or
    $GITHUB_TOKEN; None if neither answers."""
    if shutil.which('gh'):
        p = subprocess.run(['gh', 'api', 'graphql', '-f', f'query={query}'],
                           capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            return json.loads(p.stdout).get('data')
    if not _token():
        return None
    req = urllib.request.Request(f'{GITHUB}/graphql', data=json.dumps({'query': query}).encode(),
                                 headers={'User-Agent': 'qa_triage',
                                          'Authorization': f'Bearer {_token()}'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r).get('data')


def github_login():
    """The GitHub login gh, or the GitHub token, is signed in as; None for none."""
    try:
        return _github('user').get('login')
    except (urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError):
        return None


def authored_prs(login, since, repo='ceph/ceph'):
    """{number: (title, updated)} of the pull requests login opened in repo that are
    open, or were closed since since, a date."""
    found = {}
    for which in ('is:open', f'is:closed updated:>={since:%Y-%m-%d}'):
        for page in range(1, 11):
            query = urllib.parse.quote(f'repo:{repo} is:pr author:{login} {which}')
            items = _github(f'search/issues?q={query}&per_page=100&page={page}')['items']
            found.update({str(item['number']): (item['title'], item['updated_at'])
                          for item in items})
            if len(items) < 100:
                break
    return found


def pr_commits(number, repo='ceph/ceph'):
    """A pull request's head: {'head': its commit, 'ref': its branch's name, 'commits':
    [(commit, author's email, subject)]}, of at most 300 commits, as GitHub lists
    them."""
    pr = _github(f'repos/{repo}/pulls/{number}')
    commits = []
    for page in range(1, 4):
        listed = _github(f'repos/{repo}/pulls/{number}/commits?per_page=100&page={page}')
        commits += [(c['sha'], c['commit']['author']['email'],
                     c['commit']['message'].split('\n', 1)[0]) for c in listed]
        if len(listed) < 100:
            break
    return {'head': pr['head']['sha'], 'ref': pr['head']['ref'], 'commits': commits}


_FILES = {}


def pr_files(number, repo='ceph/ceph'):
    """The files a pull request changes, of at most 3000; None if GitHub has no such
    pull request."""
    if number not in _FILES:
        found = []
        try:
            for page in range(1, 31):
                listed = _github(f'repos/{repo}/pulls/{number}/files?per_page=100&page={page}')
                found += [f['filename'] for f in listed]
                if len(listed) < 100:
                    break
        except (urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError):
            found = None
        _FILES[number] = found
    return _FILES[number]


# The commits asked about in one GraphQL query, and the most of a build's asked.
LINKED_AT_ONCE, LINKED_MOST = 50, 300


def linked_prs(commits, repo='ceph/ceph'):
    """{commit: [numbers]} of the pull requests GitHub links each of commits to: those
    whose branches have it, merged or open. Commits only in another repository, as a
    build's own are in ceph-ci, have none."""
    owner, name = repo.split('/')
    found = {}
    commits = commits[:LINKED_MOST]
    for at in range(0, len(commits), LINKED_AT_ONCE):
        part = commits[at:at + LINKED_AT_ONCE]
        asked = ' '.join(f'c{i}: object(oid: "{c}") {{ ... on Commit {{ associatedPullRequests'
                         f'(first: 5) {{ nodes {{ number }} }} }} }}' for i, c in enumerate(part))
        try:
            data = _graphql(f'query {{ repository(owner: "{owner}", name: "{name}") '
                            f'{{ {asked} }} }}')
        except (urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError):
            data = None
        answers = (data or {}).get('repository') or {}
        for i, commit in enumerate(part):
            nodes = ((answers.get(f'c{i}') or {}).get('associatedPullRequests') or {}).get(
                'nodes') or []
            if nodes:
                found[commit] = [str(n['number']) for n in nodes]
    return found


def tracker_key(path=None, environ=os.environ):
    """The tracker's API key, for filing issues: $REDMINE_API_KEY, or else the file
    path names, or ~/.redmine_key; None if there is none. The file holds the key, or
    is a JSON object with it as "tracker.ceph.com_token" or "redmine_api_key"."""
    if environ.get('REDMINE_API_KEY'):
        return environ['REDMINE_API_KEY'].strip()
    path = os.path.expanduser(path or os.path.join('~', '.redmine_key'))
    try:
        with open(path) as f:
            text = f.read().strip()
    except OSError:
        return None
    if text.startswith('{'):
        found = json.loads(text)
        return found.get('tracker.ceph.com_token') or found.get('redmine_api_key')
    return text or None


def tracker_user(key, url=TRACKER):
    """Whose key it is, as the tracker names them: a check that it takes it."""
    req = urllib.request.Request(f'{url}/users/current.json',
                                 headers={'X-Redmine-API-Key': key, 'User-Agent': 'qa_triage'})
    with urllib.request.urlopen(req, timeout=60) as r:
        user = json.load(r)['user']
    return ' '.join(filter(None, (user.get('firstname'), user.get('lastname')))) or \
        user.get('login', '?')


def comment_issue(key, number, notes, url=TRACKER):
    """Add notes, a comment, to the tracker's issue number."""
    body = {'issue': {'notes': notes}}
    req = urllib.request.Request(f'{url}/issues/{number}.json', data=json.dumps(body).encode(),
                                 method='PUT',
                                 headers={'Content-Type': 'application/json',
                                          'X-Redmine-API-Key': key, 'User-Agent': 'qa_triage'})
    with urllib.request.urlopen(req, timeout=TIMEOUT):
        return f'{url}/issues/{number}'


def update_issue(key, number, notes=None, status_id=None, assigned_to_id=None, url=TRACKER):
    """Change the tracker's issue number: a comment, its status, whom it is assigned
    to, each if given."""
    body = {k: v for k, v in (('notes', notes), ('status_id', status_id),
                               ('assigned_to_id', assigned_to_id)) if v is not None}
    req = urllib.request.Request(f'{url}/issues/{number}.json',
                                 data=json.dumps({'issue': body}).encode(), method='PUT',
                                 headers={'Content-Type': 'application/json',
                                          'X-Redmine-API-Key': key, 'User-Agent': 'qa_triage'})
    with urllib.request.urlopen(req, timeout=TIMEOUT):
        return f'{url}/issues/{number}'


def comment_pr(number, body, repo='ceph/ceph'):
    """Add body, a comment, to the pull request number on GitHub: through gh, if it is
    installed and signed in, or else with $GITHUB_TOKEN. The comment's URL."""
    path, data = f'repos/{repo}/issues/{number}/comments', json.dumps({'body': body})
    if shutil.which('gh'):
        p = subprocess.run(['gh', 'api', '-X', 'POST', path, '--input', '-'], input=data,
                           capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            return json.loads(p.stdout).get('html_url', f'https://github.com/{repo}/pull/{number}')
        if not _token():
            raise ValueError(f'gh did not post it: {(p.stderr or p.stdout).strip()[:300]}')
    if not _token():
        raise ValueError('posting to GitHub needs gh, signed in (gh auth login), or a '
                         'GitHub token: qa_triage setup says how to make one')
    req = urllib.request.Request(f'{GITHUB}/{path}', data=data.encode(), method='POST',
                                 headers={'User-Agent': 'qa_triage',
                                          'Accept': 'application/vnd.github+json',
                                          'Authorization': f'Bearer {_token()}'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r).get('html_url', f'https://github.com/{repo}/pull/{number}')


def file_issue(key, project, subject, description, url=TRACKER):
    """File a bug on the tracker, in project (its identifier, such as rados); its
    number."""
    body = {'issue': {'project_id': project, 'tracker_id': 1, 'subject': subject,
                      'description': description}}
    req = urllib.request.Request(f'{url}/issues.json', data=json.dumps(body).encode(),
                                 method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'X-Redmine-API-Key': key, 'User-Agent': 'qa_triage'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return str(json.load(r)['issue']['id'])


def tracker_search(words, limit=25):
    """[(number, title)] of the tracker issues that have all the words, QA
    batches left out; a title gives the issue's type and status."""
    found = json.loads(_get_text(f'{TRACKER}/search.json?issues=1&limit={limit}'
                                 f'&q={urllib.parse.quote(words)}'))['results']
    return [(str(r['id']), r['title']) for r in found
            if r.get('type') == 'issue' and not r['title'].startswith('QA Run ')]


# A tracker issue's URL; and, in an issue's text, a pulpito page of a branch's runs,
# and a link to a run, or to one of its jobs, on pulpito or qa-proxy.
_ISSUE_URL = re.compile(r'^https?://tracker\.ceph\.com/issues/(\d+)\b')
_BRANCH_LINK = re.compile(r'pulpito\.ceph\.com/\?branch=([\w.+-]+)')
_RUN_LINK_IN_TEXT = re.compile(r'(?:pulpito\.ceph\.com|qa-proxy\.ceph\.com/teuthology)/'
                               r'([\w.+-]+-\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}-'
                               r'[^\s/?#"<>|)\]]+)')


def issue_number(arg):
    """The number of the tracker issue that arg, a URL, names; None if it names none."""
    found = _ISSUE_URL.match(str(arg))
    return found.group(1) if found else None


def issue_runs(issue):
    """Where a tracker issue points at runs, the newest first: (the branches whose
    runs it is about, those its "QA Runs" field names, as a QA batch's does, first,
    then those its pulpito pages are of; the runs it links to, or to jobs of)."""
    fields = {c['name']: c.get('value') or '' for c in issue.get('custom_fields') or []}
    current = fields.get('QA Runs', '')
    current = ' '.join(current) if isinstance(current, list) else current
    texts = [urllib.parse.unquote(text) for text in
             [j.get('notes') or '' for j in reversed(issue.get('journals') or [])] +
             [issue.get('description') or '']]
    branches = current.split() + [b for text in texts for b in _BRANCH_LINK.findall(text)]
    runs = [run for text in texts for run in _RUN_LINK_IN_TEXT.findall(text)]
    return list(dict.fromkeys(branches)), list(dict.fromkeys(runs))


def find_qa_tracker(branch):
    """The QA tracker issue whose "QA Runs" field names branch, or None.

    Its subject is usually the branch, but re-runs change the date at the end
    of the branch name, so also look for the name without it.
    """
    stem = re.sub(r'-\d{8}\.\d{6}-\w+$', '', branch)
    for text in dict.fromkeys((branch, stem)):
        for issue in _issues(f'project_id={QA_PROJECT}&status_id=*&limit=100'
                             f'&subject=~{urllib.parse.quote(text)}'):
            fields = {c['name']: c.get('value') or '' for c in issue['custom_fields']}
            if branch in fields.get('QA Runs', '').split():
                return issue
    return None


_PR_LINK = re.compile(r'github\.com/ceph/ceph/pull/(\d+)')


def pr_numbers(issue):
    """The ceph PRs that a QA tracker issue lists: its latest list of them. Some put
    the latest list in the description and the one before in a comment, and some the
    other way round; so of the description and the comments that list PRs, the one
    written last: the description as it was last edited. A comment counts as a list
    if it names two PRs at least, and half as many as the description does."""
    description = set(_PR_LINK.findall(issue.get('description') or ''))
    journals = issue.get('journals') or []
    edited = [j.get('created_on') or '' for j in journals
              if any(d.get('name') == 'description' for d in j.get('details') or [])]
    lists = [(max(edited) if edited else issue.get('created_on') or '', description)] \
        if description else []
    for j in journals:
        found = set(_PR_LINK.findall(j.get('notes') or ''))
        if len(found) >= max(2, len(description) // 2):
            lists.append((j.get('created_on') or '', found))
    return max(lists, key=lambda item: item[0])[1] if lists else set()


@dataclass
class Hit:
    file: str
    line: int
    text: str
    # Where in text the regex that found the line matched, as (start, end): what a
    # person reading the line looks for. None for a line read whole, such as a tail's.
    match: tuple = field(default=None, compare=False)


def found_in(rel, n, text, regex):
    """The Hit of a line of rel that regex found."""
    m = regex.search(text)
    return Hit(rel, n, text, m.span() if m else None)


_ENTRY = re.compile(r'<a href="([^"?]+)">[^<]*</a>[ \t]+\S+[ \t]+\S+[ \t]+(\S+)')


def _marks(marks, rel, patterns):
    regexes = {name: re.compile(regex) for name, (regex, _) in patterns.items()}
    return {name: (mark['count'], [found_in(rel, n, text, regexes[name])
                                   for n, text in mark['hits']])
            for name, mark in marks.items()}


class LineReader:
    """scan, tail and between for files whose lines are read here, by lines()."""

    def _numbered(self, rel):
        found = self.glob(rel)
        return enumerate(self.lines(rel, found[0][1] if found else None), 1)

    def scan(self, rel, patterns, first=20, last=20):
        """{name: (count, [Hit])} for each of patterns, {name: (regex, [literal])},
        in rel: how many lines it found, and the first and last of them. One pass
        reads the file for all the patterns; a line is matched against a regex only
        if it has one of the plain strings in literal."""
        return _marks(archive_host.scan(self._numbered(rel), patterns, first, last), rel,
                      patterns)

    def real(self, rel):
        """The name the archive has rel by now."""
        return rel

    def tail(self, rel, count=50):
        """The last count lines of rel, as hits."""
        return [Hit(rel, n, text) for n, text in archive_host.tail(self._numbered(rel), count)]

    def between(self, rel, start, end):
        """The lines of rel numbered start to end, as hits."""
        return [Hit(rel, n, text)
                for n, text in archive_host.between(self._numbered(rel), start, end)]


class JobFiles(LineReader):
    """The archived files of one job under base, qa-proxy's URL of the runs by
    default, fetched on demand.

    Paths are relative to the job directory, e.g. "teuthology.log" or
    "remote/*/log/ceph-osd.*.log.gz"; '*' matches within one path component.
    """

    def __init__(self, job, cache_dir, base=QA_PROXY):
        self.base = f'{base}/{urllib.parse.quote(job.run)}/{job.id}/'
        self.dir = os.path.join(cache_dir, job.run, job.id)
        self._dirs = {}

    def _listdir(self, rel):
        """{name: size} for a directory; names of directories end in '/'."""
        if rel not in self._dirs:
            try:
                page = _get_text(self.base + rel)
            except urllib.error.HTTPError:
                page = ''
            self._dirs[rel] = {urllib.parse.unquote(name): size
                               for name, size in _ENTRY.findall(page)
                               if name != '../'}
        return self._dirs[rel]

    def glob(self, pattern):
        """[(path, size)] of the files matching pattern."""
        found = [('', None)]
        parts = pattern.split('/')
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            nxt = []
            for prefix, _ in found:
                for name, size in self._listdir(prefix).items():
                    is_dir = name.endswith('/')
                    names = self._listdir(prefix)
                    if is_dir != (not last) or not (
                            fnmatch(name.rstrip('/'), part) or
                            last and _gzipped_since(name, part, lambda n: n in names)):
                        continue
                    nxt.append((prefix + name, size))
            found = nxt
        return sorted(found)

    def _local(self, rel):
        return os.path.join(self.dir, rel)

    def _kept(self, rel):
        """Where the cache has rel, as it is or gzipped; None if it has not."""
        path = self._local(rel)
        return next((kept for kept in (path, path + KEPT_GZIPPED) if os.path.exists(kept)),
                    None)

    def real(self, rel):
        """The name the archive has rel by now: rel.gz if it has gzipped it since, as
        it does older runs' logs; else rel."""
        if self._kept(rel):
            return rel
        directory, _, name = rel.rpartition('/')
        names = self._listdir(directory + '/' if directory else '')
        return rel + '.gz' if name not in names and name + '.gz' in names else rel

    def _size(self, rel, size=None):
        """rel's size in bytes: size, as the listing gives it, if known."""
        if size is None:
            directory, _, name = rel.rpartition('/')
            size = self._listdir(directory + '/' if directory else '').get(name)
        return size_bytes(size)

    def _streamed(self, rel, size=None):
        """True if rel is read from the server each time, not kept: a gzipped file
        bigger than CACHE_LIMIT, such as a daemon's log of a GB, which few checks read."""
        return (rel.endswith('.gz') and not self._kept(rel) and
                self._size(rel, size) > CACHE_LIMIT)

    def fetch(self, rel, size=None):
        """Download rel into the cache, once; return where it is kept: as it is, or,
        if it is bigger than CACHE_LIMIT, gzipped, as such a file the tree reads
        (teuthology.log) is not on the server until the archive gzips older runs'.
        size is its size as the listing gives it, if known."""
        kept = self._kept(rel)
        if kept:
            return kept
        path = self._local(rel)
        squeeze = not rel.endswith('.gz') and self._size(rel, size) > CACHE_LIMIT
        kept = path + KEPT_GZIPPED if squeeze else path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = f'{kept}.{os.getpid()}.{threading.get_ident()}.part'
        try:
            with _open(self.base + rel) as r, open(part, 'wb') as f, \
                    (gzip.GzipFile(fileobj=f, mode='wb', compresslevel=1) if squeeze
                     else f) as out:
                while chunk := r.read(1 << 20):
                    out.write(chunk)
            os.rename(part, kept)
        finally:
            if os.path.exists(part):
                os.unlink(part)
        return kept

    def lines(self, rel, size=None):
        """Yield the decoded lines of rel, decompressing .gz files."""
        rel = self.real(rel)
        if self._streamed(rel, size):
            raw, gzipped = _open(self.base + rel), True
        else:
            kept = self.fetch(rel, size)
            raw, gzipped = open(kept, 'rb'), kept.endswith('.gz')
        with raw, (gzip.GzipFile(fileobj=raw) if gzipped else raw) as stream:
            try:
                for line in stream:
                    yield line.decode('utf-8', 'replace').rstrip('\n')
            except TRUNCATED:
                return

    def search(self, pattern, regex, max_hits=3, literals=()):
        """Hits for regex in the files matching pattern, at most max_hits per file.
        literals, if given, are plain strings that every line regex finds has."""
        return self.search_files(self.glob(pattern), regex, max_hits, literals)

    def search_files(self, found, regex, max_hits=3, literals=()):
        """Hits for regex in found, a list of (path, size) from glob."""
        hits = []
        for rel, size in found:
            count = 0
            for n, text in enumerate(self.lines(rel, size), 1):
                if regex.search(text):
                    hits.append(found_in(rel, n, text, regex))
                    count += 1
                    if count == max_hits:
                        break
        return hits

    def text(self, rel, limit=1 << 20):
        """The start of a small file, or '' if it does not exist."""
        try:
            kept = self.fetch(self.real(rel))
            with open(kept, 'rb') as f, \
                    (gzip.GzipFile(fileobj=f) if kept.endswith('.gz') else f) as stream:
                return stream.read(limit).decode('utf-8', 'replace')
        except (urllib.error.HTTPError, *TRUNCATED):
            return ''


class SshJobFiles:
    """The archived files of one job on a host that has the archive, read over
    ssh by archive_host.py there: they are searched where they are, and only what
    a search finds, and the files copied into the evidence, cross the network.

    ssh is the command that runs a command on host; by default ssh itself, with
    SSH_OPTIONS and, given a control_dir, a control socket. The host's answers are kept
    in the file kept, if given, and not asked for again: a job's files do not
    change once it has ended. asked and reused count them.
    """

    def __init__(self, job, host, directory, control_dir=None, ssh=None, kept=None):
        self.host = host
        self.root = posixpath.join(directory, job.run, job.id)
        # For people: the same files on qa-proxy.
        self.base = f'{QA_PROXY}/{urllib.parse.quote(job.run)}/{job.id}/'
        if ssh is None:
            ssh = ['ssh', *SSH_OPTIONS]
            socket = control_dir and control_path()
            if socket:
                ssh += ['-o', 'ControlPath=' + socket]
        self.ssh = list(ssh)
        self.kept, self.asked, self.reused = kept, 0, 0
        self._answers = None
        self._files = None
        # The files the last search could not read, as they are gone: the archive
        # has pruned them since they were listed.
        self.last_gone = []
        with open(HELPER) as f:
            self._program = f.read()

    def _argv(self):
        return self.ssh + [self.host, 'python3 -c ' + shlex.quote(self._program)]

    def _request(self, request):
        return json.dumps(dict(request, root=self.root)).encode()

    def _error(self, returncode, stderr):
        said = stderr.decode('utf-8', 'replace').strip().splitlines()
        said = said[-1] if said else f'exit {returncode}'
        if returncode == 255:
            return ArchiveError(f'cannot reach {self.host} over ssh ({said}): {UNREACHED}')
        return ArchiveError(f'{self.host}:{self.root}: {said}')

    def _load(self):
        try:
            with open(self.kept) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def siblings(self):
        """The ids of the jobs of this job's run that the archive has: it prunes most
        of those that passed sooner than those that failed."""
        return self._call({'op': 'siblings'})['jobs']

    def real(self, rel):
        """The name the archive has rel by now: rel.gz if it has gzipped it since, as
        it does older runs' logs; else rel."""
        tree = self._tree()
        return rel + '.gz' if rel not in tree and rel + '.gz' in tree else rel

    def _call(self, asked, again=True):
        request = asked
        if request.get('op') != 'list':
            if request.get('path'):
                request = dict(request, path=self.real(request['path']))
            if request.get('files'):
                request = dict(request, files=[self.real(rel) for rel in request['files']])
        key = json.dumps(request, sort_keys=True)
        if self.kept and self._answers is None:
            self._answers = self._load()
        if key in (self._answers or {}):
            self.reused += 1
            return self._answers[key]
        with _SESSIONS:
            p = subprocess.run(self._argv(), input=self._request(request), capture_output=True)
        if p.returncode:
            # A file that the kept list of the job's files has, and the archive has
            # gzipped since, as it does older runs' logs: list them again, and ask again.
            if again and request.get('op') != 'list' and \
                    b'No such file or directory' in p.stderr and self._forget_files():
                return self._call(asked, again=False)
            raise self._error(p.returncode, p.stderr)
        answer = json.loads(p.stdout)
        self.asked += 1
        if self.kept and not answer.get('missing'):
            # With what another process kept meanwhile.
            self._answers = dict(self._load(), **self._answers)
            self._answers[key] = answer
            os.makedirs(os.path.dirname(self.kept), exist_ok=True)
            part = f'{self.kept}.{os.getpid()}.{threading.get_ident()}'
            with open(part, 'w') as f:
                json.dump(self._answers, f)
            os.replace(part, self.kept)
        return answer

    def _forget_files(self):
        """Forget the list of the job's files kept from before; True if there was one."""
        key = json.dumps({'op': 'list'}, sort_keys=True)
        self._files = None
        if not self.kept or key not in (self._answers or {}):
            return False
        self._answers = {k: v for k, v in dict(self._load(), **self._answers).items()
                         if k != key}
        part = f'{self.kept}.{os.getpid()}.{threading.get_ident()}'
        with open(part, 'w') as f:
            json.dump(self._answers, f)
        os.replace(part, self.kept)
        return True

    def _tree(self):
        if self._files is None:
            answer = self._call({'op': 'list'})
            if answer.get('missing'):
                print(f'warning: {self.host} has no {self.root}', file=sys.stderr)
            self._files = answer.get('files', {})
        return self._files

    def glob(self, pattern):
        """[(path, size)] of the files matching pattern."""
        parts, tree = pattern.split('/'), self._tree()
        return sorted((rel, size) for rel, size in tree.items()
                      if len(rel.split('/')) == len(parts)
                      and all(fnmatch(a, b) for a, b in zip(rel.split('/')[:-1], parts[:-1]))
                      and (fnmatch(rel.split('/')[-1], parts[-1]) or
                           _gzipped_since(rel, parts[-1], lambda r: r in tree)))

    def search(self, pattern, regex, max_hits=3, literals=()):
        """Hits for regex in the files matching pattern, at most max_hits per file.
        literals, if given, are plain strings that every line regex finds has, which
        grep looks for first on the host."""
        return self.search_files(self.glob(pattern), regex, max_hits, literals)

    def search_files(self, found, regex, max_hits=3, literals=()):
        """Hits for regex in found, a list of (path, size) from glob; the files are
        searched in parallel on the host."""
        if not found:
            return []
        answer = self._call({'op': 'search', 'files': [rel for rel, _ in found],
                             'pattern': regex.pattern, 'flags': regex.flags,
                             'max_hits': max_hits, 'literals': list(literals)})
        self.last_gone = answer.get('gone') or []
        if self.last_gone:
            # The list of the job's files is older than the archive: list them again.
            self._forget_files()
        return [found_in(rel, n, text, regex) for rel, n, text in answer['hits']]

    def text(self, rel, limit=1 << 20):
        """The start of a small file, or '' if it does not exist."""
        return self._call({'op': 'read', 'path': rel, 'limit': limit})['text']

    def scan(self, rel, patterns, first=20, last=20):
        """As LineReader.scan, in one pass on the host."""
        return _marks(self._call({'op': 'scan', 'path': rel, 'first': first, 'last': last,
                                  'patterns': {name: [regex, list(literals)] for name,
                                               (regex, literals) in patterns.items()}}
                                 )['marks'], rel, patterns)

    def tail(self, rel, count=50):
        """The last count lines of rel, as hits."""
        return [Hit(rel, n, text)
                for n, text in self._call({'op': 'tail', 'path': rel, 'lines': count})['hits']]

    def between(self, rel, start, end):
        """The lines of rel numbered start to end, as hits."""
        return [Hit(rel, n, text) for n, text in
                self._call({'op': 'range', 'path': rel, 'start': start, 'end': end})['hits']]

    def lines(self, rel, size=None):
        """Yield the decoded lines of rel, which the host sends compressed."""
        # Before the session is taken: finding the file may ask the host too.
        real = self.real(rel)
        with _SESSIONS, subprocess.Popen(self._argv(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
            p.stdin.write(self._request({'op': 'cat', 'path': real}))
            p.stdin.close()
            finished = False
            try:
                with gzip.GzipFile(fileobj=p.stdout) as stream:
                    try:
                        for line in stream:
                            yield line.decode('utf-8', 'replace').rstrip('\n')
                    except TRUNCATED:
                        pass
                finished = True
            finally:
                if not finished:
                    p.kill()
                stderr = p.stderr.read()
        if p.returncode:
            raise self._error(p.returncode, stderr)


def _gzipped_since(path, part, there):
    """True if path is a file that part, the last part of a pattern, names as it was
    before it was gzipped, as the archive does to older runs' logs: teuthology.log
    becomes teuthology.log.gz. there(path) is True if a file is there; the file as
    it was, if it is there too, is the one that matches."""
    if not path.endswith('.gz'):
        return False
    return fnmatch(path[:-3].rsplit('/', 1)[-1], part) and not there(path[:-3])


def parse_archive(archive):
    """(host, directory) from an archive setting such as trial200:/a."""
    host, sep, directory = str(archive).partition(':')
    if not sep or not host or host.startswith('-') or not directory.startswith('/') or \
            directory.startswith('//'):
        raise ValueError(f'archive is an ssh host and the directory the runs are in there, '
                         f'such as {ARCHIVE}; or the https URL they are under, or https for '
                         f'{QA_PROXY}; not "{archive}"')
    return host, directory


def https_base(archive):
    """The URL the runs are under if archive has them downloaded over https: https
    for qa-proxy's, or a URL; None if archive is an ssh host."""
    if archive == 'https':
        return QA_PROXY
    if str(archive).startswith(('https://', 'http://')):
        return archive.rstrip('/')
    return None


def check_archive(archive):
    """Raise ValueError if archive is neither an ssh host and directory nor https."""
    if not https_base(archive):
        parse_archive(archive)


def control_path():
    """This process's ssh control socket, as ssh's ControlPath gives it: a connection
    of its own, for SSH_AT_ONCE to keep within. It is in a directory of the user's
    own in /tmp, not in the cache: a socket's path has at most 104 bytes, and ssh
    adds 17 to it as it makes it, which a cache under a long home directory passes.
    None if that directory is not the user's alone."""
    directory = f'/tmp/qa_triage-{os.getuid()}'
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        found = os.lstat(directory)
    except OSError:
        return None
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or \
            found.st_mode & 0o077:
        return None
    return os.path.join(directory, f'{os.getpid()}-%C')


def reach(archive, timeout=6):
    """(True if archive's host answers over ssh as qa_triage asks it, with no password
    or passphrase, and has the archive's directory; what was found)."""
    host, directory = parse_archive(archive)
    try:
        # Not given stdin: ssh would read it, and with it the commands piped to a review.
        p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={timeout}',
                            host, f'test -d {shlex.quote(directory)}'],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout + 20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f'{host} does not answer over ssh ({e})'
    if p.returncode == 0:
        return True, f'{host} answers over ssh, and has {directory}'
    if p.returncode != 255:
        return False, f'{host} answers over ssh, but has no {directory}'
    said = (p.stderr.strip().splitlines() or [f'exit {p.returncode}'])[-1]
    hint = ('; qa_triage\'s ssh cannot ask for a password or a passphrase, so ssh-add your '
            'key' if 'Permission denied' in said else
            f'; ssh {host} once yourself, to accept its host key'
            if 'Host key verification' in said else '')
    return False, f'{host} does not answer over ssh ({said}){hint}'


def job_files(job, archive, cache_dir):
    """A job's archived files: over ssh from archive, a host and directory such as
    trial200:/a, or downloaded into cache_dir from the URL https_base gives."""
    base = https_base(archive)
    if base:
        return JobFiles(job, cache_dir, base)
    host, directory = parse_archive(archive)
    return SshJobFiles(job, host, directory, cache_dir,
                       kept=os.path.join(cache_dir, job.run, job.id, 'archive.json'))


def size_bytes(size):
    """Bytes from an autoindex size column ("836088", "1.2M" or "-")."""
    if isinstance(size, int):
        return size
    if not size or size == '-':
        return 0
    units = {'K': 1 << 10, 'M': 1 << 20, 'G': 1 << 30}
    if size[-1] in units:
        return int(float(size[:-1]) * units[size[-1]])
    return int(size)
