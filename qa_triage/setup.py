"""qa_triage setup: a page to choose the settings and keys, kept in your config file.

qa_triage offers it the first time it runs with no config file, qa_triage setup
opens it any time, and o in the review opens it over the run. It shows each setting with what it is for and what it is now;
enter changes one, from a menu where there are choices, and c checks them all: that
the agents can run here, that the tracker takes its key, and that the archive host
answers. a sets up what it can automatically, from what this machine has: whether
the archive host answers, a ceph clone, a tracker key, the editors; it does so by
itself the first time, before a config file is saved. It chooses no AI agent:
that costs, and is the person's choice. qa_triage setup --auto does the same with
no page, and saves. A key you paste is kept in a file next to the config file that
only you can read, and the config file names that file: it never holds a key itself.
"""

import copy
import curses
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error

import yaml

from . import config, share
from .agents import installed, make_agent, make_judge, parse_spec
from .sources import (ARCHIVE, check_archive, github_token, github_user, https_base,
                      parse_archive, tracker_key, tracker_user)
from .tui import MOUSE_OFF, Screen, terminal, wrap

SETTINGS = (
    ('judge', 'Judge',
     'The agent asked about the groups of jobs the tree and the records could not '
     'decide, and asked to check and explain the rest. It is asked often, so a quick, '
     'cheap one is best. none: no AI, the tree and the records alone.'),
    ('analyst', 'Analyst',
     'The agent that learns from a group (l), answers a doubt (d) and talks (t): it '
     'reads the logs and changes the records and the tree, which every later run '
     'uses, so a strong one is worth what it costs. By default, the judge\'s agent, '
     'reading the evidence files.'),
    ('db', 'Library',
     'The shared library of what qa_triage has learned: the problem records, the '
     'tree\'s questions, the settled jobs and the debug scripts. A directory, '
     'or a git repository\'s URL, which is cloned into the cache and kept up to date; '
     'qa_triage share proposes what you learn to it. By default, the library/ of the '
     'repository qa_triage was cloned from.'),
    ('tracker_key', 'Tracker key',
     'Your tracker.ceph.com API key: the tracker is read as you, the RADOS wiki of '
     'reviewed runs among it, and, when you ask, from the review\'s i key, an issue is '
     'commented on or filed. Without one, the review drafts them for you to post. enter '
     'takes one, and says how to make it. $REDMINE_API_KEY, if set, comes first, then '
     'this, then ~/.redmine_key.'),
    ('github_key', 'GitHub token',
     'How GitHub is read, for the PRs a run tests and what fixed a problem, and, when '
     'you ask, from the review\'s i key, a PR commented on: gh, signed in, is used '
     'first; else $GITHUB_TOKEN, or this token. Without either, GitHub allows 60 '
     'requests an hour, and nothing is posted. enter signs gh in, or takes a token, and '
     'says how to make the smallest that does.'),
    ('tracker_project', 'Tracker project', 'The tracker project that issues are filed in.'),
    ('archive', 'Job files',
     'Where the job files are read: on a host that has the archive, over ssh, where they '
     'are searched in place, which needs the sepia VPN; or, only if you choose it, '
     'downloaded over https from qa-proxy, or from another server\'s URL, which is much '
     'slower, and needs no VPN.'),
    ('git', 'Ceph clone',
     'The ceph clone that the source a job ran is checked out from, and that the fix '
     'check asks whether a fix is in a build.'),
    ('learn_max', 'Lessons per run',
     'The most groups of jobs that a run with an analyst (qa_triage run) learns from.'),
    ('learn_budget', 'Lesson budget',
     'The most one lesson may cost, in the analyst\'s own unit, before the analyst is '
     'asked no more and the lesson is left for a person; none for no limit.'),
    ('lessons_at_once', 'Lessons at once',
     'How many lessons ask their analysts at once, or all: each change is still checked '
     'and kept one at a time, against the library as the others left it.'),
    ('judges_at_once', 'Judges at once',
     'How many groups of jobs ask the judge at once, or all: one at a time is slower, '
     'and a local model may answer only one at a time anyway.'),
    ('cited_lines', 'Lines shown',
     'The most lines of the logs a result shows for a line the judge quotes, of those '
     'that have it.'),
    ('editor', 'Editor',
     'What qa_triage open, and v in the review, open a file of a job\'s archive at a '
     'line with, to check what a result cites: a command with {file} and {line} in it, '
     'such as lnav -c \':goto {line}\' {file}. By default $VISUAL, or $EDITOR, or vim, '
     'given +{line} {file}: on the archive\'s host, the host\'s own.'),
    ('edit_on', 'Edit on',
     'Where the editor runs: on the archive\'s host, over ssh, where the files are, so '
     'that nothing is copied (archive); or here, on a copy of the file (here). With the '
     'job files from qa-proxy, it is always here.'),
    ('approve', 'Approval',
     'Before a lesson or a doubt changes the records or the tree, which every later run '
     'uses: ask you (ask), which on the review\'s full screen keeps the change for '
     'qa_triage pending; keep every change that passes the checks (always); or keep each '
     'for a person to approve later, with qa_triage pending (hold).'),
    ('approve_edits', 'Approving edits',
     'Before a change edits what the library has, a record\'s checks or a learned '
     'file\'s code, not only adds to it: as Approval says, but never always unless this '
     'says so. An addition that passes the checks adds what the library knows; an edit '
     'can lose some of it in ways the checks cannot see.'),
    ('holdout', 'Held out',
     'A directory of answered runs, as qa_triage answers writes them, held out of '
     'learning: no lesson is taught from them, the memory does not remember them, and '
     'a change that places fewer of their jobs rightly is not kept. qa_triage score on '
     'them is how the library does on runs it has not learned from.'),
    ('mouse', 'Mouse',
     'Whether the review\'s full screen starts with the mouse on: the wheel scrolls, a '
     'click chooses, and a drag selects and copies. m turns it off and on there.'),
    ('judge_on_open', 'Judge on open',
     'Whether the review asks the judge about every group not judged yet as soon as it '
     'opens, in the background, without asking first. It costs what the judge does.'),
)
TITLES = {name: title for name, title, _ in SETTINGS}
# Where a ceph clone often is, for the automatic setup to look.
CLONES = ('~/git/ceph', '~/ceph', '~/src/ceph', '~/work/ceph', '~/code/ceph')
# The editors the automatic setup looks for, besides yours.
EDITORS = ('lnav', 'vim', 'less')
# A choice that is typed instead.
OTHER = '\0other'
KEY = ('key', 'a key you paste: kept in a file only you can read')
KEY_FILE = ('file', 'a file that has it: the key alone, or a JSON object with it as '
                    'tracker.ceph.com_token')
# For the tracker key and the GitHub token: how to make one, and the page to.
HOW = ('\0how', 'How to make one, the smallest that does: the steps')
OPEN = ('\0open', 'Open the page that makes one, in your browser')
GH = ('\0gh', 'Sign in with gh (gh auth login): then no token is needed here')
# The pages that make them.
PAGES = {'tracker_key': 'https://tracker.ceph.com/my/account',
         'github_read': 'https://github.com/settings/personal-access-tokens/new',
         'github_key': 'https://github.com/settings/tokens/new?description=qa_triage'
                       '&scopes=public_repo'}
HOWTO = {
    'tracker_key': [
        'A tracker.ceph.com API key',
        '',
        'What qa_triage does with it: reads the tracker as you, the issues and searches '
        'your account may see, and the RADOS wiki of reviewed runs, which its memory '
        'reads; and, only when you ask, from the review\'s i key, once it has shown you '
        'what it will post, comments on an issue or files one.',
        '',
        'To make one:',
        '  1. Sign in at https://tracker.ceph.com (Register, at the top right, if you have '
        'no account).',
        f'  2. Open My account, at the top right: {PAGES["tracker_key"]}',
        '  3. In the column on the right, under API access key, choose Show, and copy '
        'the key.',
        '  4. Here, choose "a key you paste", and paste it: it is kept in a file only you '
        'can read.',
        '',
        'The smallest that does: the tracker (Redmine) gives each account one API key, '
        'with all of that account\'s rights; there is no narrower kind to make. Keep it '
        'out of files others read. If it leaks, choose Reset beside it, on the same page: '
        'the old key stops working.',
    ],
    'github_key': [
        'GitHub: gh signed in, or a token',
        '',
        'What qa_triage does with it: reads pull requests, for the PRs a run tests and '
        'what fixed a problem; and, only when you ask, from the review\'s i key, once it '
        'has shown you what it will post, comments on a PR. Without either, GitHub allows '
        'only 60 requests an hour, which one review can use up, and nothing is posted.',
        '',
        'Simplest: gh, GitHub\'s command line, signed in. Choose "Sign in with gh", or '
        'run gh auth login. qa_triage uses it first, and needs no token here.',
        '',
        'Else a personal access token, no bigger than what you want it for. On GitHub: '
        'your picture at the top right, Settings, Developer settings, Personal access '
        'tokens.',
        '  To read only: a fine-grained token, at '
        f'{PAGES["github_read"]}: Resource owner, you; Repository access, Public '
        'repositories; no permissions. It reads any public repository, ceph/ceph among '
        'them, and can change nothing.',
        '  To comment on PRs too: a classic token, at '
        f'{PAGES["github_key"]}, with the public_repo scope alone. That scope lets the '
        'token do in public repositories whatever you may, pushing included, so give it '
        'a short expiry. A fine-grained token is narrower, if the ceph organization '
        'allows them (an owner may have to approve it): Resource owner, ceph; Only '
        'select repositories, ceph/ceph; Pull requests, read and write.',
        '  Give it an expiry date, and copy it as soon as it is made: GitHub shows it only '
        'then. Here, choose "a key you paste", and paste it: it is kept in a file only '
        'you can read.',
    ],
}


class Setup:
    """The settings of your config file, as they are and as you change them, and
    what checking them found."""

    def __init__(self, environ=os.environ, cwd=None):
        self.environ, self.cwd = environ, cwd or os.getcwd()
        self.path = config.user_file(environ)
        self.directory = os.path.dirname(self.path)
        self.data = self.saved()
        # The settings as the page found them.
        self.first = copy.deepcopy(self.data)
        self.changed = False
        # {setting: (True if it is fine, what checking it found)}
        self.checks = {}

    # What the page shows.

    def rows(self):
        """[(setting, title, what it is now)], an agent's key after the agents."""
        out = []
        for name, title, _ in SETTINGS:
            out.append((name, title, self.shown(name)))
            if name == 'analyst':
                out += [(f'key:{agent}', f'Key for {agent}', self.shown(f'key:{agent}'))
                        for agent in self.keyed()]
        return out

    def about(self, name):
        if name.startswith('key:'):
            agent = self.agent_named(name[4:])
            return (f'The API key that {name[4:]} needs, as ${agent.key_env}, if it is not '
                    'set in the environment: a file that has it, or one you paste.')
        return dict((n, about) for n, _, about in SETTINGS)[name]

    def shown(self, name):
        data = self.data
        if name.startswith('key:'):
            found = (data.get('agents') or {}).get(name[4:], {}).get('key_file')
            return found or 'none'
        default = {
            'judge': f'{config.DEFAULTS["judge"]} (default)',
            'analyst': 'the judge\'s agent (default)',
            'tracker_key': ('$REDMINE_API_KEY' if self.environ.get('REDMINE_API_KEY') else
                            '~/.redmine_key' if os.path.exists(
                                os.path.expanduser('~/.redmine_key')) else 'none'),
            'github_key': ('$GITHUB_TOKEN' if self.environ.get('GITHUB_TOKEN') else
                           'none: gh, if it is signed in (default)'),
            'tracker_project': f'{config.DEFAULTS["tracker_project"]} (default)',
            'db': f'{config.own_library() or "the records that come with qa_triage"} '
                  '(default)',
            'archive': f'{ARCHIVE} (default)',
            'git': 'the ceph clone you run it in (default)',
            'learn_max': f'{config.DEFAULTS["learn_max"]} (default)',
            'learn_budget': 'none (default)',
            'lessons_at_once': f'{config.DEFAULTS["lessons_at_once"]} (default)',
            'judges_at_once': f'{config.DEFAULTS["judges_at_once"]} (default)',
            'cited_lines': f'{config.DEFAULTS["cited_lines"]} (default)',
            'editor': '$VISUAL, $EDITOR or vim (default)',
            'edit_on': f'{config.DEFAULTS["edit_on"]} (default)',
            'approve': f'{config.DEFAULTS["approve"]} (default)',
            'approve_edits': 'ask, or as Approval if it is hold (default)',
            'holdout': 'none (default)',
            'mouse': 'on (default)',
            'judge_on_open': 'off (default)',
        }[name]
        value = data.get(name)
        if isinstance(value, list):
            value = ','.join(value)
        elif isinstance(value, bool):
            value = 'on' if value else 'off'
        return default if value is None else str(value)

    def choices(self, name):
        """[(value, what it is)] for a setting chosen from a menu; None for one typed.
        '' is the default; OTHER, and KEY and KEY_FILE for a key, are typed."""
        if name in ('judge', 'analyst'):
            first = (('none', 'none: no AI, the tree and the records alone') if name == 'judge'
                     else ('', 'the judge\'s agent, reading the evidence files'))
            return [first, *self.agents(), (OTHER, 'another: type an agent, with its model '
                                                   'or settings, as docs/guide.md\'s '
                                                   'Agents and config says')]
        if name == 'tracker_key':
            return [KEY, KEY_FILE, HOW, OPEN,
                    ('', 'none here: $REDMINE_API_KEY, or ~/.redmine_key')]
        if name == 'github_key':
            return ([GH] if shutil.which('gh') else []) + [
                KEY, ('file', 'a file that has it: the token alone, or a JSON object with it '
                              'as github_token'), HOW,
                (OPEN[0], 'Open GitHub\'s page for a token that can comment on PRs, in your '
                          'browser'),
                ('', 'none here: gh, if it is signed in, or $GITHUB_TOKEN')]
        if name.startswith('key:'):
            return [KEY, (KEY_FILE[0], 'a file that has it'), ('', 'none')]
        if name == 'archive':
            return [('', f'{ARCHIVE}: over ssh, where the files are; needs the sepia VPN'),
                    ('https', 'https: downloaded from qa-proxy; much slower, needs no VPN'),
                    (OTHER, 'another host that has the archive, as host:/directory, or '
                            'another server\'s https URL of the runs: type it')]
        if name == 'db':
            return [('', 'the library of the repository qa_triage was cloned from'),
                    ('none', 'none: only the records in qa_triage\'s own checkout'),
                    (OTHER, 'a directory, or a git repository\'s URL: type it')]
        if name == 'git':
            return [('', 'the ceph clone you run it in, else the one qa_triage\'s '
                         'checkout is in'),
                    (OTHER, 'one clone always: type its path'),
                    ('none', 'none: no checkouts, and the fix check goes by versions')]
        if name == 'editor':
            return [('', '$VISUAL, or $EDITOR, or vim, where it runs (default)'),
                    ("lnav -c ':goto {line}' {file}", 'lnav: a log viewer; it reads gzipped '
                                                      'logs, and colours them'),
                    ('vim -R +{line} {file}', 'vim, read-only'),
                    ('less -N +{line} {file}', 'less, with line numbers'),
                    (OTHER, 'another: type its command, with {file} and {line}')]
        if name == 'edit_on':
            return [('', 'archive: on the archive\'s host, over ssh, where the files are '
                         '(default)'),
                    ('here', 'here: on a copy of the file, made once, in the cache')]
        if name == 'approve':
            return [('', 'ask: ask you; on the review\'s full screen, keep it for qa_triage '
                         'pending (default)'),
                    ('always', 'always: keep every change that passes the checks'),
                    ('hold', 'hold: keep each for a person to approve, with qa_triage '
                             'pending')]
        if name == 'approve_edits':
            return [('', 'ask, where Approval keeps additions by themselves; as Approval '
                         'otherwise (default)'),
                    ('always', 'always: keep every edit that passes the checks too'),
                    ('hold', 'hold: keep each for a person to approve, with qa_triage '
                             'pending')]
        if name == 'holdout':
            return [('', 'none: every answered run is learned from'),
                    (OTHER, 'a directory of answered runs: type it')]
        if name == 'mouse':
            return [('', 'on: the wheel scrolls, a click chooses, a drag copies (default)'),
                    (False, 'off: the terminal selects text as usual')]
        if name == 'judge_on_open':
            return [('', 'off: the review offers to ask the judge (default)'),
                    (True, 'on: the review asks the judge as it opens, without asking you')]
        return None

    def agents(self):
        """[(agent, what it is)] to choose a judge or an analyst from: each kind
        installed here, with its own default model, and your config's agents."""
        found = [(spec, what if ':' in spec else f'{what}, with its default model')
                 for spec, what in installed()]
        for name, settings in (self.data.get('agents') or {}).items():
            if name not in dict(found):
                found.append((name, f'{name}: in your config, provider '
                                    f'{(settings or {}).get("provider")}'))
        return found

    def agent_named(self, name):
        return make_agent(name, self.data.get('agents') or {})

    def keyed(self):
        """The judge's and the analyst's agents that need a key."""
        found = []
        for name in ('judge', 'analyst'):
            for ref in str(self.data.get(name) or '').split(','):
                ref = ref.strip()
                if ref and ref != 'none' and ref not in found:
                    try:
                        if self.agent_named(ref).key_env:
                            found.append(ref)
                    except (ValueError, AttributeError):
                        pass
        return found

    # Changing them.

    def set(self, name, value):
        """Set a setting from what was chosen or typed: '' for its default."""
        value = value.strip() if isinstance(value, str) else value
        if name in ('learn_max', 'learn_budget', 'cited_lines') and value not in ('', 'none'):
            try:
                value = float(value) if name == 'learn_budget' else int(value)
            except ValueError:
                raise ValueError(f'{value} is not a number') from None
            if name == 'cited_lines' and value < 1:
                raise ValueError('at least one line is shown')
        if name in ('lessons_at_once', 'judges_at_once') and value not in ('', 'all'):
            if not str(value).isdigit() or int(value) < 1:
                raise ValueError(f'{value} is not a number from 1, or all')
            value = int(value)
        if name == 'edit_on' and value not in ('', 'archive', 'here'):
            raise ValueError('it is archive or here')
        if name == 'edit_on' and value == 'archive':
            value = ''
        if name == 'archive' and value:
            check_archive(value)
        if name in ('judge', 'analyst') and value not in ('', 'none'):
            make_judge(value, self.data.get('agents') or {})
        if name.startswith('key:'):
            # The agent, in the config's agents, where its key_file setting goes.
            agent = self.data.setdefault('agents', {}).setdefault(
                name[4:], {'provider': parse_spec(name[4:])['provider']})
            if value:
                agent['key_file'] = value
            else:
                agent.pop('key_file', None)
        elif value == '' or (name == 'learn_budget' and value == 'none'):
            self.data.pop(name, None)
        else:
            self.data[name] = value
        self.changed = True
        self.checks.pop(name, None)

    def keep_key(self, name, key):
        """Keep a key in a file only you can read, next to the config file; its path."""
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, (name[4:] if name.startswith('key:') else name)
                            .replace(os.sep, '_') +
                            ('' if name in ('tracker_key', 'github_key') else '_key'))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(key.strip() + '\n')
        os.chmod(path, 0o600)
        return path

    def saved(self):
        """The settings the config file has now."""
        try:
            with open(self.path) as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}

    def since(self):
        """{setting: its value, None for its default} for each setting saved since the
        page found the config file, its paths as config.load gives them: for what was
        running meanwhile to take up."""
        now = self.saved()
        changed = {name: now.get(name) for name in {*self.first, *now}
                   if now.get(name) != self.first.get(name)}
        return config.resolve(changed, self.directory)

    def save(self):
        os.makedirs(self.directory, exist_ok=True)
        with open(self.path, 'w') as f:
            f.write('# qa_triage\'s settings on this machine, from qa_triage setup; see\n'
                    '# examples/config.yaml in qa_triage for all of them. Keys are kept in\n'
                    '# files only you can read, never here.\n')
            if self.data:
                yaml.safe_dump(self.data, f, sort_keys=False, width=100)
        self.changed = False

    # Checking them.

    def check(self, name):
        """(True if the setting is fine, what checking it found), kept in checks."""
        self.checks[name] = found = self._check(name)
        return found

    def check_all(self):
        for name, _, _ in self.rows():
            self.check(name)

    def _check(self, name):
        agents = self.data.get('agents') or {}
        if name in ('judge', 'analyst'):
            ref = self.data.get(name, config.DEFAULTS['judge'] if name == 'judge' else None)
            if name == 'analyst' and not ref:
                judge = self.data.get('judge', config.DEFAULTS['judge'])
                ref = str(judge).split(',')[-1].strip() if judge else None
            if not ref or ref == 'none':
                return True, 'no AI'
            try:
                agent = make_judge(ref, agents)
            except ValueError as e:
                return False, str(e)
            why = getattr(agent, 'missing', lambda: None)()
            return (False, why) if why else (True, 'can run here')
        if name.startswith('key:'):
            why = self.agent_named(name[4:]).missing()
            return (False, why) if why else (True, 'found')
        if name == 'tracker_key':
            path = self.data.get('tracker_key')
            key = tracker_key(os.path.expanduser(path) if path else None, self.environ)
            if not key:
                return True, 'none: the review drafts issues for you to file'
            try:
                return True, f'the tracker takes it: it is {tracker_user(key)}\'s'
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                return False, f'the tracker does not take it: {e}'
        if name == 'github_key':
            return self._check_github()
        if name == 'archive':
            archive = self.data.get('archive') or ARCHIVE
            if https_base(archive):
                said = 'downloaded from ' + ('qa-proxy' if archive == 'https' else archive)
                try:
                    there = _ssh(parse_archive(ARCHIVE)[0], 'true').returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    there = False
                return ((False, f'{said}, though {ARCHIVE.split(":")[0]} answers over ssh, '
                                'where they are searched in place: choose it')
                        if there else (True, said))
            host, _ = parse_archive(archive)
            try:
                p = _ssh(host, 'true')
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, f'cannot reach {host}: {e}'
            if p.returncode:
                said = p.stderr.strip().splitlines()[-1:]
                return False, (f'cannot reach {host} ({said[0] if said else p.returncode}): '
                               'connect to the sepia VPN, or choose https')
            return True, f'{host} answers'
        if name == 'db':
            library = self.data.get('db') or config.own_library()
            if not library or library == 'none':
                return True, 'the records that come with qa_triage'
            if share.is_url(library):
                url, branch, _ = share.parse(library)
                p = subprocess.run(['git', 'ls-remote', '-q', url, branch or 'HEAD'],
                                   capture_output=True, text=True, timeout=60)
                return ((True, 'git can read it') if p.returncode == 0 else
                        (False, f'git cannot read it: {p.stderr.strip()[-200:]}'))
            found = os.path.isdir(os.path.join(os.path.expanduser(library), 'problems'))
            return (True, 'a library') if found else (False, 'it has no problems/ directory')
        if name == 'git':
            git = self.data.get('git')
            if git == 'none':
                return True, 'no checkouts'
            clone = (config.enclosing_repo(os.path.expanduser(git)) if git else
                     config.ceph_clone(self.cwd))
            if git:
                return (True, f'a clone: {clone}') if clone else (False, 'not a git clone')
            return True, (f'here: {clone}' if clone else
                          'not in one here: '
                          f'{config.ceph_clone(os.path.dirname(config.ROOT)) or "none"}')
        if name == 'editor':
            return self._check_editor()
        return True, ''

    def _check_github(self):
        signed_in = _gh_user()
        path = self.data.get('github_key')
        token = github_token(os.path.expanduser(path) if path else None, self.environ)
        if signed_in:
            return True, (f'gh is signed in, as {signed_in}, and is used first' +
                          ('; the token if gh fails' if token else ''))
        if not token:
            return True, ('none: GitHub allows 60 requests an hour, and no PR is commented '
                          'on; enter says how to sign in')
        try:
            login, scopes = github_user(token)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            return False, f'GitHub does not take it: {e}'
        can = ('; a fine-grained token: it can comment on PRs if it may write ceph/ceph\'s '
               'pull requests' if scopes is None else
               '; it can comment on PRs' if {'public_repo', 'repo'} & set(scopes) else
               '; read-only: it cannot comment on PRs')
        return True, f'GitHub takes it: it is {login}\'s{can}'

    def _remote(self):
        """The archive's host, if the editor runs there, else None."""
        archive = self.data.get('archive') or ARCHIVE
        if https_base(archive) or self.data.get('edit_on') == 'here':
            return None
        return parse_archive(archive)[0]

    def _check_editor(self):
        command = self.data.get('editor')
        host = self._remote()
        if host:
            program = shlex.split(command)[0] if command else \
                '${VISUAL:-${EDITOR:-vim}}'
            try:
                p = _ssh(host, f'command -v {program}')
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, f'cannot reach {host}: {e}'
            found = p.stdout.strip()
            return ((True, f'on {host}: {found}') if p.returncode == 0 and found else
                    (False, f'{host} has no {program}' if p.returncode != 255 else
                     f'cannot reach {host}'))
        program = shlex.split(command)[0] if command else (
            self.environ.get('VISUAL') or self.environ.get('EDITOR') or 'vim').split()[0]
        found = shutil.which(program)
        return (True, f'here: {found}') if found else (False, f'there is no {program} here')

    # Setting up automatically.

    def detect(self):
        """{setting: (value to set it to, or None to leave it; True if that is fine,
        False if not, None if it is for the person to choose; what was found)}, from
        what this machine has. It chooses no AI agent: that costs, and is the
        person's to choose."""
        found = {}
        # The archive's host: whether it answers, and the editors it has.
        archive = self.data.get('archive') or ARCHIVE
        on_host = None
        if not https_base(archive):
            host, directory = parse_archive(archive)
            try:
                p = _ssh(host, f'test -d {shlex.quote(directory)} && echo archive; '
                               f'command -v {" ".join(EDITORS)}; true')
                on_host = p.stdout.split() if p.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                on_host = None
            # It never chooses https: that is much slower, and the person's to choose.
            if on_host and 'archive' in on_host:
                found['archive'] = (None, True, f'{host} answers, and has {directory}')
            else:
                found['archive'] = (None, False, f'{host} does not answer over ssh: connect '
                                                 'to the sepia VPN, or choose https')
                on_host = None
        # The editor, where it runs: here, if the archive's host does not answer.
        here = self.data.get('edit_on') == 'here' or not on_host
        if not self.data.get('editor'):
            if here:
                mine = self.environ.get('VISUAL') or self.environ.get('EDITOR')
                have = [e for e in EDITORS if shutil.which(e)]
                found['editor'] = (None, bool(mine or 'vim' in have),
                                   f'here: {mine or "vim"}' +
                                   (f'; {", ".join(have)} are here too' if have else ''))
            else:
                have = [os.path.basename(path) for path in on_host if path.startswith('/')]
                found['editor'] = (None, 'vim' in have,
                                   f'on {parse_archive(archive)[0]}: its $VISUAL, $EDITOR or '
                                   'vim; it has ' +
                                   (', '.join(have) or 'none of ' + ', '.join(EDITORS)))
        # A ceph clone.
        if not self.data.get('git'):
            clone = config.ceph_clone(self.cwd)
            if clone:
                found['git'] = (None, True, f'here: {clone}')
            else:
                elsewhere = next((path for path in map(os.path.expanduser, CLONES)
                                  if config.ceph_clone(path) == path), None)
                found['git'] = ((elsewhere, True, f'found {elsewhere}') if elsewhere else
                                (None, None, 'none found: the ceph clone you run qa_triage '
                                             'in, else none'))
        # The tracker key, and how GitHub is read.
        if not self.data.get('tracker_key'):
            found['tracker_key'] = (None, *self._check('tracker_key'))
        if not self.data.get('github_key'):
            found['github_key'] = (None, *self._check('github_key'))
        # The agents: for the person to choose.
        if not self.data.get('judge') and not self.data.get('analyst'):
            have = sorted({what for spec, what in installed() if ':' not in spec})
            found['judge'] = (None, None, (f'{", ".join(have)} installed here; ' if have else
                                           'no agent installed here; ') +
                              'none chosen: AI costs, so choose one, if any, with enter')
        return found

    def auto(self):
        """Set each setting to what detect found for it; the titles of those set."""
        changed = []
        for name, (value, ok, said) in self.detect().items():
            if value is not None and self.data.get(name) != value:
                self.set(name, value)
                changed.append(TITLES[name])
            self.checks[name] = (ok, said)
        return changed


def _gh_user():
    """The GitHub login gh is signed in as; None without gh, or signed out."""
    if not shutil.which('gh'):
        return None
    try:
        p = subprocess.run(['gh', 'api', 'user', '--jq', '.login'], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def open_page(url, environ=os.environ):
    """Open url in a browser here, if there is one to open it in: True if one was
    started. Over ssh, with no display, there is none."""
    if sys.platform == 'darwin':
        argv = ['open', url]
    elif (environ.get('DISPLAY') or environ.get('WAYLAND_DISPLAY')) and shutil.which('xdg-open'):
        argv = ['xdg-open', url]
    else:
        return False
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def _ssh(host, command):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
                           command], capture_output=True, text=True, timeout=30)


class SetupScreen(Screen):
    """The setup page, on the review's screen's parts."""

    def __init__(self, stdscr, setup):
        super().__init__(stdscr, None)
        self.setup, self.selected = setup, 0
        self.set_mouse(True)

    def draw(self, busy=None, live=None):
        self.clear()
        height, width = self.stdscr.getmaxyx()
        setup = self.setup
        self.put(0, 0, f' qa_triage setup  {setup.path} '.ljust(width),
                 self.colour('head') | curses.A_BOLD)
        self.put(1, 1, 'Each setting, what it is now, and what checking it found.')
        rows = setup.rows()
        self.selected = max(0, min(self.selected, len(rows) - 1))
        for i, (name, title, shown) in enumerate(rows):
            ok, found = setup.checks.get(name, (None, ''))
            line = f' {title:<17} {shown[:38]:<38} {found}'
            if i == self.selected:
                self.put(3 + i, 0, f'>{line}'.ljust(width), curses.A_REVERSE)
            else:
                self.put(3 + i, 0, f' {line}', self.colour(
                    'sure' if ok else 'unsure' if ok is False else 'section'))
        y = 4 + len(rows)
        self.put(y, 0, '─' * width, self.colour('section'))
        name = rows[self.selected][0]
        ok, found = setup.checks.get(name, (None, ''))
        lines = [setup.about(name)] + (['', f'Checked: {found}'] if found else [])
        for i, line in enumerate(wrap(lines, width - 2)[:max(0, height - y - 3)]):
            self.put(y + 1 + i, 1, line)
        foot = busy or self.flash or ('↑↓ choose  enter change  a set up automatically  '
                                      'c check them all  s save  q leave' +
                                      ('   (not saved)' if setup.changed else ''))
        self.put(height - 1, 0, f' {foot} '.ljust(width), self.colour('foot'))
        self.refresh()

    def change(self, name, title):
        setup = self.setup
        choices = setup.choices(name)
        if choices is None:
            value = self.ask(f'{title}, now {setup.shown(name)} (empty keeps it, "default" '
                             'for the default):')
            if not value:
                return
            setup.set(name, '' if value.strip() == 'default' else value)
            return
        values = [value for value, _ in choices]
        now = setup.data.get(name, '') if not name.startswith('key:') else ''
        while True:
            picked = self.choose(title, [what for _, what in choices],
                                 values.index(now) if now in values else 0)
            if picked is None:
                return
            value = values[picked]
            if value != HOW[0]:
                break
            self.pager(f'{title}: how to make one', HOWTO[name])
            now = HOW[0]
        if value == OPEN[0]:
            url = PAGES[name]
            self.flash = (f'Opened {url} in your browser.' if open_page(url) else
                          f'Open {url} in your browser: there is none here to open it.')
            return
        if value == GH[0]:
            self.outside(['gh', 'auth', 'login'], said='Signing gh in to GitHub, with gh auth '
                                                        'login: answer its questions.',
                         pause=True)
            self.draw(busy='Checking it...')
            ok, found = setup.check(name)
            self.flash = f'{title}: {found}.'
            return
        if value == OTHER:
            value = self.ask(f'{title}:')
            if not value:
                return
        elif value == KEY[0]:
            key = self.ask(f'{title}: paste it (it is shown as dots):', secret=True)
            if not key:
                return
            value = setup.keep_key(name, key)
        elif value == KEY_FILE[0]:
            value = self.ask(f'{title}: the file that has it:')
            if not value:
                return
            if not os.path.exists(os.path.expanduser(value)):
                raise ValueError(f'there is no file {value}')
        setup.set(name, value)
        if name in ('tracker_key', 'github_key') and value:
            # Whose it is, and what it can do, at once.
            self.draw(busy='Checking it...')
            ok, found = setup.check(name)
            self.flash = f'{title}: {found}. s saves.'
            return
        self.flash = f'{title}: {setup.shown(name)}. c checks it, s saves.'

    def auto(self):
        self.draw(busy='Finding what this machine has: the archive\'s host can take a few '
                       'seconds...')
        changed = self.setup.auto()
        self.flash = ((f'Set {", ".join(changed)}, from what this machine has: ' if changed else
                       'Nothing to change, from what this machine has: ') +
                      'what it found is beside each. s saves.')

    def run(self):
        """Until the person leaves: True if they saved."""
        saved = False
        if not os.path.exists(self.setup.path):
            # The first time, it sets up what it can by itself.
            self.auto()
        else:
            self.flash = 'Checking what can be checked here...'
            self.draw()
            for name in ('judge', 'analyst', 'git'):
                self.setup.check(name)
            self.flash = None
        while True:
            self.draw()
            key = self.key()
            self.flash = None
            rows = self.setup.rows()
            if key in (curses.KEY_DOWN, curses.KEY_UP):
                self.selected += 1 if key == curses.KEY_DOWN else -1
            elif key == curses.KEY_MOUSE:
                what, y = self.mouse()
                if what in ('click', 'double') and 3 <= y < 3 + len(rows):
                    self.selected = y - 3
            elif key in (10, 13, curses.KEY_ENTER):
                name, title, _ = rows[self.selected]
                try:
                    self.change(name, title)
                except ValueError as e:
                    self.flash = f'Not changed: {e}'
            elif key == ord('c'):
                self.draw(busy='Checking: the tracker and the archive host can take a few '
                               'seconds...')
                self.setup.check_all()
            elif key == ord('a'):
                self.auto()
            elif key == ord('s'):
                self.setup.save()
                saved = True
                self.flash = f'Saved to {self.setup.path}.'
            elif key in (ord('q'), 27):
                if not self.setup.changed or self.confirm('Leave without saving the changes?'):
                    return saved


def automatically(environ=os.environ, say=print):
    """Set up what can be set up from what this machine has, with no page, and save:
    say what was found, and what was set."""
    s = Setup(environ)
    changed = s.auto()
    titles = {name: title for name, title, _ in s.rows()}
    for name, (ok, found) in s.checks.items():
        say(f'{titles.get(name, name)}: {s.shown(name)}' + (f' ({found})' if found else ''))
    if changed or not os.path.exists(s.path):
        s.save()
        say(f'Set {", ".join(changed) or "nothing"}; saved to {s.path}. To see every setting, '
            'and choose an agent, if any: qa_triage setup.')
    else:
        say(f'Nothing to change in {s.path}.')
    return s


def run(environ=os.environ, page=None):
    """Show the setup page, of page's settings, until the person leaves: True if they
    saved."""
    os.environ.setdefault('ESCDELAY', '25')
    try:
        return curses.wrapper(lambda stdscr: SetupScreen(stdscr, page or Setup(environ)).run())
    finally:
        terminal(MOUSE_OFF)
