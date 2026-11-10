"""Doubts: someone questions a result, and the tree and the records learn from
the answer.

An analyst agent gets the evidence a judge would get, every record, the tree's
code, and a way to search the rest of the job's archive. It says what the right
answer is, and changes the records, or adds nodes to the tree (tree/learned.py),
to give it. The changes are kept only if, with them,

- every line the analyst quotes is in the evidence,
- the doubted job gets the analyst's answer (when a run learns, without a judge),
- every example of a record or of a node, and every job settled before
  (labels/), that a change could apply to still gets its own answer, and
- every other job triaged before (the corpus: results.json files) whose result
  the change would alter failed the same way as the doubted job or one of the
  examples of what changed: the same failure reason, with its variables masked,
  or for a node, the same explanation before it.

The doubted job is then settled: it is added to labels/, so that every later
change is checked against it as well. In interactive mode a person sees each
answer and the results of the checks, and can tell the analyst what is wrong
before accepting it.

A run learns the same way: each group of jobs that the tree and the records could
not decide by themselves is doubted, with what the judge said as the reason, so
that they decide jobs like them by themselves from then on. Each lesson is paid
for once: what it came to is kept with it (lesson.json), and a later run does not
start it again. It only resumes, once, a lesson whose analyst stopped without
answering, and asks it for its answer.
"""

import concurrent.futures
import contextlib
import datetime
import difflib
import fcntl
import getpass
import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error

import yaml

from . import db, library, pack, report, share, tree
from .agents import (KEYS, PROMPTS, JudgeError, Stopped, extract_verdict, readable,
                     report_of, schema)
from .checkout import CheckoutError, under_test, under_test_text
from .db import under
from .fingerprint import fingerprint
from .graph import FOUND, Triage, describe, fits, misses, placed, said_of
from .sources import (QA_PROJECT, SSH_AT_ONCE, ArchiveError, Job, JobFiles, pull_request,
                      search_pulls)
from .tree import learned

ROUNDS = 3              # of requests for more of the archive, and of revisions
# Held while a lesson reads the library, checks a change against it or writes one:
# the tree's nodes are the whole process's, and a check tries its change in
# them. Lessons that ask their analysts at once (learn_from_run's at_once) take
# turns at it, each checked against the library as the others left it.
LIBRARY = threading.RLock()
# How deep this process holds it, and the lock file of the library it holds it for:
# qa_triage processes on one machine share a library's clone, and take turns at it
# too (see holding).
_HELD = {'depth': 0, 'file': None}


class Collided(Exception):
    """The library changed while a change was made, others' changes with it, and
    the change no longer passes the checks: report says why."""

    def __init__(self, report):
        super().__init__('\n'.join(report))
        self.report = report


@contextlib.contextmanager
def holding(problems_dir, say=None):
    """LIBRARY, and the lock file of the library whose records are in
    problems_dir, which every qa_triage process on this machine takes in turn."""
    with LIBRARY:
        if _HELD['depth'] == 0:
            name = hashlib.sha1(os.path.realpath(problems_dir).encode()).hexdigest()[:12]
            lock = open(os.path.join(tempfile.gettempdir(), f'qa_triage-{name}.lock'), 'a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if say:
                    say('  waiting for another qa_triage on this machine, which is changing '
                        'the library')
                fcntl.flock(lock, fcntl.LOCK_EX)
            _HELD['file'] = lock
        _HELD['depth'] += 1
        try:
            yield
        finally:
            _HELD['depth'] -= 1
            if _HELD['depth'] == 0:
                fcntl.flock(_HELD['file'], fcntl.LOCK_UN)
                _HELD['file'].close()
                _HELD['file'] = None
ANSWER_NOW = ('You stopped without answering. Reply now with only the JSON object that '
              'the first prompt asks for, from what you have read so far; if that is not '
              'enough to tell, say so with assessment "unsure".')
MAX_REQUESTS = 6        # per round
MAX_FILES = 8           # per request
MAX_HITS = 200
# What learn_from_run says of a lesson whose analyst gave nothing at all.
NOT_BEGUN = 'not taught, and no more lessons asked: the analyst did not begin: '
# The most issues a tracker search found that are given to the analyst at once.
FOUND_ISSUES = 5
COPY_LIMIT = 20 << 20
LINE_LIMIT = 400
ARCHIVE = ('*', 'remote/*/*', 'remote/*/log/*', 'remote/*/log/*/*')
RELATIONS = {'copied_to': ('copied to', 'copied from'),
             'duplicates': ('duplicates', 'duplicated by'),
             'blocks': ('blocks', 'blocked by'), 'precedes': ('precedes', 'follows')}
# The source the job ran, as linked into the evidence, and what the prompt says
# of each.
SOURCES = {
    'source': '- source/: the ceph source the job ran, at {sha1}: the code of its daemons, '
              'tests and qa tasks, to read where the logs point. It is the whole tree, so '
              'read only the files you need.\n',
    'suite': '- suite/: the qa suite the job ran, at {sha1}, which differs from source/: '
             'its qa/ is the tasks and yaml the job used.\n',
}
UNDER_TEST = ('- under-test.txt: the PRs under test, as merged into the branch the job ran, '
              'and any commits made on it, newest first: the files each changes, and its '
              'changes to qa/ and to the files the job\'s failure names; source/ has the '
              'rest. A failure in a test, task or daemon that one of them changes '
              'may be that PR\'s doing, not a known problem\'s, even one whose fix is in: '
              'see "The PRs under test" below.\n')
# How much of a daemon's log, before and after where it failed, the analyst gets.
BEFORE, AFTER = 400, 50
# Where a daemon that crashed did, in its log.
CRASHED = (r'FAILED ceph_assert|Caught signal|ceph_abort', 'FAILED ceph_assert',
           'Caught signal', 'ceph_abort')
ASSESSMENTS = {'right': 'the result is right', 'wrong': 'the result is wrong',
               'unsure': 'it cannot tell', 'need-more': 'it needs more of the archive'}
# {(job path, learned.version()): the tree's Finding for the job, or None}
_FINDINGS = {}
# A change that fails this many of the other jobs triaged before is sent back
# without checking the rest.
MAX_FAILURES = 10
# Seconds between the lines that say how far the checks of an answer have got.
TICK = 5
# The most jobs the checks walk through the tree at once, as the triage does: the
# archive is asked by at most this many.
AT_ONCE = SSH_AT_ONCE
# The jobs triaged before are tried this many at a time, so that a change that
# fails too many of them already is not tried on the rest.
CHUNK = 4 * AT_ONCE
# The most jobs that passed a new or changed record's confirm checks are tried on, and
# the share of them they may all find their lines in: more, and they find what jobs
# that did not fail log too, not the failure (elastic-recheck held its queries to
# the same).
BREADTH, BREADTH_SHARE = 10, 0.1
LESSON = 'lesson.json'
OUTCOMES = {'learned': 'it learned', 'nothing': 'there was nothing to learn',
            'unsettled': 'its answer did not pass the checks, or it was unsure',
            'no-answer': 'the analyst gave no answer',
            'stopped': 'a person stopped the analyst',
            'not-begun': 'the analyst did not begin: it gave nothing at all',
            'refused': 'the analyst refused to answer, as its safety filter can: learn it '
                       'again, or choose another agent or model'}


def _label_file(directory, run):
    # Run names have colons, which some systems do not allow in file names.
    return os.path.join(directory, run.replace(':', '_') + '.yaml')


def load_labels(directory, unconfirmed=True):
    """[(Job, its problem id or None, who settled it)] for every settled job; without
    unconfirmed, but those settled for a person that none has confirmed yet."""
    labels = []
    if not os.path.isdir(directory):
        return labels
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.yaml'):
            continue
        with open(os.path.join(directory, name)) as f:
            data = yaml.safe_load(f) or {}
        for jid, e in (data.get('jobs') or {}).items():
            if not unconfirmed and e.get('confirmed') is False:
                continue
            job = Job(data['run'], str(jid), e.get('status', ''),
                      e.get('description', ''), e.get('reason', ''))
            labels.append((job, e.get('problem'), f'settled by {e.get("by", "?")}' +
                           (', not yet confirmed by a person' if e.get('confirmed') is False
                            else '')))
    return labels


def settled_by_people(directory, unconfirmed=True):
    """{<run>/<job>: problem id or None} of the jobs a person settled, not an agent:
    what they are, whatever the answers gathered from the wiki and the tracker say.
    Without unconfirmed, but those settled for a person, such as by an assistant, that
    none has confirmed yet."""
    return {job.path: (str(problem) if problem else None)
            for job, problem, where in load_labels(directory, unconfirmed)
            if 'qa_triage' not in where}


def unconfirmed_labels(directory):
    """[(Job, its problem id or None, who settled it)] of the jobs settled for a person
    that none has confirmed yet: settle confirms one, as that person."""
    return [label for label in load_labels(directory) if label[2].endswith('by a person')]


def paths_dir(labels_dir):
    """Where the library keeps the path the tree took for each of its jobs: paths/,
    beside labels/."""
    return os.path.join(os.path.dirname(os.path.abspath(labels_dir)), 'paths')


def load_paths(directory, stale=None):
    """{<run>/<job>: tree.Finding, with its path and facts} of the library's jobs,
    as the tree found them when they were last walked: a change is checked against
    the jobs it could reach, and these say which, without reading their logs. The
    files that other tree code wrote are left out, and their names added to stale."""
    found = {}
    if not os.path.isdir(directory):
        return found
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.yaml'):
            continue
        with open(os.path.join(directory, name)) as f:
            data = yaml.safe_load(f) or {}
        if data.get('code') != tree.code():
            if stale is not None:
                stale.append(name)
            continue
        for jid, e in (data.get('jobs') or {}).items():
            found[f'{data["run"]}/{jid}'] = tree.Finding(
                path=[p for p in str(e.get('tree') or '').split('/') if p],
                facts=e.get('facts') or {})
    return found


def save_paths(directory, findings, fresh=False):
    """Keep findings, {<run>/<job>: tree.Finding}, in directory: a file for each
    run, as labels/ has, with the code of the tree that found them; with fresh, in
    place of what it had, the files of other runs removed. What other tree code
    found is not kept. The files that changed."""
    runs = {}
    for path, finding in findings.items():
        run, _, jid = path.rpartition('/')
        runs.setdefault(run, {})[jid] = finding
    written = []
    for run, jobs in sorted(runs.items()):
        name = _label_file(directory, run)
        before = _read(name)
        kept = {} if fresh or before is None else yaml.safe_load(before) or {}
        entries = (kept.get('jobs') or {}) if kept.get('code') == tree.code() else {}
        for jid, finding in jobs.items():
            entries[str(jid)] = dict({'tree': finding.where},
                                     **({'facts': dict(finding.facts)} if finding.facts else {}))
        text = yaml.safe_dump({'run': run, 'code': tree.code(), 'jobs': dict(sorted(
            entries.items(), key=lambda e: (len(e[0]), e[0])))}, sort_keys=False, width=1000)
        if text != before:
            os.makedirs(directory, exist_ok=True)
            with open(name, 'w') as f:
                f.write(text)
            written.append(name)
    if fresh and os.path.isdir(directory):
        kept = {os.path.basename(_label_file(directory, run)) for run in runs}
        for name in sorted(os.listdir(directory)):
            if name.endswith('.yaml') and name not in kept:
                os.remove(os.path.join(directory, name))
                written.append(os.path.join(directory, name))
    return written


def reaches(path, top):
    """True if a change to the node at top can change the walk of a job that went to
    path: top is on the way there, or below it. The root's is on every job's way."""
    return not top or under(path, top) or under(top, path)


def reachable_examples(codes, grows):
    """[(node file, example, its path)] of the examples a change to the node files
    codes, at the paths grows, can move: those of the files it changes, and of the
    others, those whose path it can reach. The rest take theirs as they did."""
    return [(name, example, path) for name, f in sorted(learned.FILES.items())
            for example, path in f.examples.items()
            if name in codes or any(reaches(path, top) for top in grows)]


# The tool's tests, which a change to the tree's nodes must pass, and how long they
# may take; and how many of the tests that fail are told of, in how many lines each.
TESTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tests')
TESTS_TIMEOUT = 900
TESTS_SHOWN, TEST_LINES = 5, 6


def run_tests(nodes):
    """[line] of what fails when the tool's tests run on the tree's nodes in the
    directory nodes; [] if every test passes."""
    if not os.path.isdir(TESTS):
        return [f'the tool\'s tests are not in {TESTS}, so a change to the tree\'s nodes '
                'cannot be tested: run qa_triage from a clone of its repository']
    try:
        done = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', TESTS,
                               '-t', TESTS], cwd=os.path.dirname(TESTS),
                              env=dict(os.environ, QA_TRIAGE_NODES=nodes),
                              capture_output=True, text=True, timeout=TESTS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [f'the tool\'s tests did not finish in {TESTS_TIMEOUT // 60} minutes']
    if done.returncode == 0:
        return []
    # unittest's report: a line of "=" before each test that failed, then its name, a
    # line of "-", and its traceback, whose last lines say what it expected.
    failed = re.split(r'^={70}\n', done.stderr, flags=re.M)[1:]
    lines = []
    for block in failed[:TESTS_SHOWN]:
        name, trace = (re.split(r'^-{70}\n', block, flags=re.M) + ['', ''])[:2]
        said = [line for line in trace.splitlines() if line.strip()]
        # From the error, such as an AssertionError and what differed.
        error = [i for i, line in enumerate(said) if re.match(r'\w+(Error|Exception)\b', line)]
        lines.append(name.strip())
        lines += [f'  {line}' for line in (said[error[-1]:][:TEST_LINES] if error
                                           else said[-TEST_LINES:])]
    if len(failed) > TESTS_SHOWN:
        lines.append(f'... and {len(failed) - TESTS_SHOWN} more tests')
    return lines or done.stderr.strip().splitlines()[-TEST_LINES:]


def next_tag(directory, prefix, suffix='.out'):
    """prefix-N for the first N with no prefix-N.out (or suffix) in directory: a
    session continued there keeps the outputs of the turns before it."""
    number = 1
    while os.path.exists(os.path.join(directory, f'{prefix}-{number}{suffix}')):
        number += 1
    return f'{prefix}-{number}'


def _read(path):
    """The text of the file at path, or None if there is none."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def save_label(directory, job, problem, by, why, summary, pr=None, confirmed=True):
    """Settle job in problem, or in none; pr is the PR under test that caused it,
    if one did. confirmed is False for a job settled for a person, such as by an
    assistant, until one confirms it."""
    path = _label_file(directory, job.run)
    data = {'run': job.run, 'jobs': {}}
    if os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or data
    data['jobs'][job.id] = {'problem': problem, 'by': by, 'why': why, 'summary': summary,
                            **({'pr': pr} if pr else {}),
                            **({} if confirmed else {'confirmed': False}),
                            'status': job.status, 'description': job.description,
                            'reason': job.reason}
    os.makedirs(directory, exist_ok=True)
    with open(path, 'w') as f:
        yaml.safe_dump(data, f, sort_keys=False, width=1000, allow_unicode=True)
    return path


def _run_of(path, e):
    """The run of e, an entry of the results.json at path: as it says, or, in one
    written before it said, the name of the directory, runs/<run>/."""
    return e.get('run') or os.path.basename(os.path.dirname(os.path.abspath(path)))


def load_corpus(paths):
    """The jobs in the results.json files of earlier triage runs."""
    jobs = {}
    for path in paths:
        with open(path) as f:
            for e in json.load(f):
                job = Job(_run_of(path, e), e['job'], e['status'], e['description'],
                          e['reason'])
                jobs[job.path] = job
    return list(jobs.values())


def load_history(paths):
    """{job path: what its triage said}, from the same files: its verdict, the
    problem a record or a judge placed it in, its path through the tree, and
    whether it was sure."""
    history = {}
    for path in paths:
        with open(path) as f:
            for e in json.load(f):
                # A path that other tree code found is found again; a triage from
                # before the code was recorded is taken as it was.
                found = e.get('tree') or {}
                if found.get('code', tree.code()) != tree.code():
                    found = {}
                history[f'{_run_of(path, e)}/{e["job"]}'] = {
                    'verdict': e.get('verdict'), 'problem': e.get('problem'),
                    'tree': found.get('path'), 'facts': found.get('facts') or {},
                    'sure': e.get('sure')}
    return history


def history_of(results):
    """The same, for results in memory."""
    return {r.job.path: {'verdict': r.verdict, 'problem': placed(r),
                         'tree': r.finding.where if r.finding else None,
                         'facts': r.finding.facts if r.finding else {}, 'sure': r.sure}
            for r in results}


# How much of what people wrote on an issue, and of its pull requests, the analyst
# is given.
COMMENTS, COMMENT_LIMIT, PULL_REQUESTS, BODY_LIMIT = 30, 3000, 3, 4000


def pull_numbers(issue):
    """The ceph pull requests an issue names: in its "Pull request ID" field, its
    description or its comments."""
    numbers = []
    for field in issue.get('custom_fields', []):
        if field.get('name') == 'Pull request ID':
            numbers += re.findall(r'\d+', str(field.get('value') or ''))
    texts = [issue.get('description') or ''] + [j.get('notes') or ''
                                                 for j in issue.get('journals', [])]
    for text in texts:
        numbers += re.findall(r'github\.com/ceph/ceph/pull/(\d+)', text)
    return list(dict.fromkeys(numbers))


def pull_text(pr):
    """A pull request as text for the analyst."""
    lines = [f'Pull request #{pr["number"]}: {pr["title"]}',
             f'state: {"merged" if pr["merged"] else pr["state"]}' +
             (f', merge commit {pr["merge_commit"]}' if pr['merged'] else ''),
             '', pr['body'][:BODY_LIMIT].strip(), '', 'Files it changed, and in what:']
    lines += [f'  {path}' + (f': {"; ".join(names[:8])}' if names else '')
              for path, names in pr['files'][:60]]
    return '\n'.join(lines) + '\n'


def write_up(issue, pulls=pull_request):
    """What people wrote about an issue after filing it: its comments, newest last,
    and its pull requests. Each is the people's answer about the problem: what they
    found the cause to be, and how they fixed it."""
    comments = [j for j in issue.get('journals', []) if (j.get('notes') or '').strip()]
    text = ''
    if comments:
        last = f', the last {COMMENTS}' if len(comments) > COMMENTS else ''
        text += f'\nComments ({len(comments)}{last}):\n'
        for j in comments[-COMMENTS:]:
            text += (f'\n--- {(j.get("user") or {}).get("name", "?")}, '
                     f'{str(j.get("created_on", ""))[:10]}:\n'
                     f'{j["notes"].strip()[:COMMENT_LIMIT]}\n')
    for number in pull_numbers(issue)[:PULL_REQUESTS]:
        try:
            text += '\n' + pull_text(pulls(number))
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            text += f'\nPull request #{number}: could not read it ({e})\n'
    return text


def tracker_text(issue, pulls=pull_request):
    """A tracker issue as text for the analyst: what it says, its fields and its
    relations, and what people wrote about it since: its comments and its pull
    requests."""
    fields = ''.join(f'{c["name"]}: {c["value"]}\n' for c in issue.get('custom_fields', [])
                     if c.get('value') and isinstance(c['value'], str))
    related = ''
    for rel in issue.get('relations', []):
        mine = rel['issue_id'] == issue.get('id')
        forward, backward = RELATIONS.get(rel['relation_type'], ('related to',) * 2)
        related += (f'{forward if mine else backward} '
                    f'#{rel["issue_to_id"] if mine else rel["issue_id"]}\n')
    return (f'{issue.get("tracker", {}).get("name", "Issue")} #{issue.get("id")}: '
            f'{issue["subject"]}\nstatus: {issue["status"]["name"]}\n'
            f'project: {issue["project"]["name"]}\n{fields}{related}\n'
            f'{(issue.get("description") or "")[:8000]}\n' + write_up(issue, pulls))


def original(issue):
    """For a backport, or an issue closed as a duplicate: (what it is, the number
    of the issue it names, or None). None for other issues."""
    me, relations = issue.get('id'), issue.get('relations', [])
    if issue.get('tracker', {}).get('name') == 'Backport':
        found = [r['issue_id'] for r in relations
                 if r['relation_type'] == 'copied_to' and r['issue_to_id'] == me]
        return 'a backport', found[0] if found else None
    if issue['status']['name'] == 'Duplicate':
        found = [r['issue_to_id'] for r in relations
                 if r['relation_type'] == 'duplicates' and r['issue_id'] == me]
        return 'a duplicate', found[0] if found else None
    return None


def canonical(number, tracker):
    """The tracker number to use for number: the issue it duplicates, or that a
    backport of it names, if the tracker says so; else number itself."""
    if not str(number).isdigit():
        return number
    try:
        other = original(tracker(number))
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return number
    return str(other[1]) if other and other[1] else number


def problem_id(value):
    """A problem id from an answer or --says, such as 76295 or PR68305; None for no
    record."""
    if value is None or str(value).strip().lower() in ('', 'new', 'none', 'null'):
        return None
    return db.record_id(value)


def _slug(title):
    return '-'.join(re.findall(r'[a-z0-9]+', title.lower())[:5])


def held_writes(held, problems_dir):
    """The files a change kept for approval writes, {path in the library: text}, as
    the checks gave them: kept with it, or, for one kept before they were, made again
    from its records and nodes (its wording aside)."""
    if held.get('writes'):
        return held['writes']
    names = {p.id: os.path.basename(p.source) for p in db.load(problems_dir)}
    writes = {}
    for change in held.get('changes') or []:
        pid, text = str(change.get('id')), str(change.get('yaml', ''))
        try:
            record = yaml.safe_load(text)
        except yaml.YAMLError:
            record = None
        title = str(record.get('title') or '') if isinstance(record, dict) else ''
        name = names.get(pid) or f'{pid}-{_slug(title)}.yaml'
        writes[f'problems/{name}'] = text.rstrip('\n') + '\n'
    for node in held.get('nodes') or []:
        name = str(node.get('file', ''))
        name = name[len('nodes/'):] if name.startswith('nodes/') else name
        writes[f'nodes/{name}'] = str(node.get('code', '')).rstrip('\n') + '\n'
    return writes


# What the analyst is told of a person's answer, when one doubts the result now and
# says what the job is (a firm doubt), and how a record's also agrees with it.
PERSON_SAYS = '''
A person doubts this result now, and doubt.yaml's expected is what they say the job
is. Unless the logs plainly show another cause, they are right: change the records,
or the tree, so that the tool places this job, and jobs like it, in their issue, and
give their issue as the problem. If their issue and the one the tool chose are the
same problem, make their issue's record the one such jobs go in, and list the other
under its `also`; do not argue that the tool's answer was right too. If the logs
show they are wrong, say so in a sentence or two, with the lines that show it, and
answer what the logs show: a person then decides.
'''
ALSO_AGREES = ('When doubt.yaml expects an issue that is the same problem as the record\n'
               '  the job is in, this is the answer: add it to the record\'s `also`, and give '
               'the\n  record as the job\'s problem; that agrees with what was expected.')
ALSO_FIRM = ('A person\'s expected issue is not met that way: the job must be placed\n'
             '  in their issue\'s record, not in one that lists it under `also`.')


class Doubt:
    """One doubted result, and the conversation with the analyst about it.

    files_for(job) and jobs_for(run) give a job's archive and a run's jobs by id;
    tracker(number) gives a tracker issue, and search(words) the [(number, title)]
    of the issues that have them. source(files, say) gives {name: directory} of
    the source the job ran (see checkout.py), which the analyst can read. corpus
    is the jobs triaged before, and history what their triage said (see
    load_history). ask and say talk to the person. Unless label_none, a job is
    settled only if a record places it. With want_sure, an answer is accepted only
    if the tree and the records then decide the job without a judge. The tree's
    nodes are kept in nodes_dir; without one, the analyst cannot change any.
    """

    def __init__(self, job, files, problems_dir, labels_dir, analyst, workdir,
                 files_for, jobs_for, tracker, expected=None, why='', by='',
                 recorded=None, corpus=(), interactive=False, write=True, ask=input,
                 say=print, search=None, label_none=True, source=None, nodes_dir=None,
                 history=None, want_sure=False, resume=None, resumed_cost=0, budget=None,
                 known=(), progress=None, session=None, said_by_people=False,
                 approve=None, library=None, publish=False, dispute=None, judged=None,
                 approve_edits=False, holdout=(), issues=(), firm=False):
        self.job, self.files = job, files
        # The numbers of the tracker issues a search found for it, which the analyst
        # is given with the evidence, rather than asking for them a turn later.
        self.issues = issues
        # The judge's answer about the job, if it was asked: the analyst is given its
        # account of the failure, to improve on.
        self.judged = judged
        # approve(doubt, answer, writes, report) asks a person before a change is
        # written (see approve.py), or None to write it once it passes the checks;
        # library, a git repository the records are in, is brought up to date before
        # the analyst starts and before the change is written, and with publish, the
        # change is pushed to its remote once written.
        self.approve, self.library, self.publish = approve, library, publish
        # The same for a change that edits what the library has, not only adds to it;
        # False to approve it as approve does.
        self.approve_edits = approve if approve_edits is False else approve_edits
        # [(Job, its right answer)] of the runs held out of learning: a change may not
        # place fewer of them rightly, and the analyst is told only how many.
        self.holdout = holdout
        # dispute(doubt, answer) keeps a change that passed the checks for an answer
        # that is not the one people expected, for a person who agrees with the
        # analyst (see approve.py): its path, in kept.
        self.dispute, self.kept = dispute, None
        # True if expected is what people said the job is, not an agent: then the
        # jobs that failed the same way, which only an agent settled, move with it.
        self.said_by_people, self.resettled = said_by_people and expected is not None, {}
        # True if a person doubts the result now and says what the job is: then only
        # a change that places the job in their issue agrees with them, not one that
        # lists their issue under another record's also, as people's earlier
        # answers, which differ between duplicate issues, may be met.
        self.firm = firm and self.said_by_people
        self.problems_dir, self.labels_dir = problems_dir, labels_dir
        self.analyst, self.workdir = analyst, workdir
        self.files_for, self.jobs_for, self.tracker = files_for, jobs_for, tracker
        self.expected, self.why, self.by, self.recorded = expected, why, by, recorded
        self.interactive, self.write = interactive, write
        self.ask, self.say = ask, say
        self.search, self.label_none, self.source = search, label_none, source
        self.labels = load_labels(labels_dir)
        # The paths the library keeps for its jobs, and those the checks found
        # anew, with the change, to keep with it: {<run>/<job>: tree.Finding}.
        self.paths_dir, self.paths, self.walked = paths_dir(labels_dir), {}, {}
        self.corpus = corpus
        self.nodes_dir, self.triaged, self.want_sure = nodes_dir, history or {}, want_sure
        # A session that stopped without answering, in this workdir, and what it
        # had cost by then.
        self.resume, self.resumed_cost = resume, resumed_cost
        # The most the analyst may cost, in its own unit, before it is asked no more.
        self.budget = budget
        # [(Job, its right answer)], such as the reviewers' for the run's jobs.
        self.known = known
        # Where the analyst's steps go as it works, for an analyst that says them.
        self.progress = progress or say
        # A session to continue, such as a discussion of the job.
        self.session, self.history, self.costs = session, [], []
        self.requests = 0
        self.answer, self.written = None, []
        # Where its change waits for a person's approval, once it does (see approve).
        self.held = None
        # pulls(number) gives a ceph pull request, and search_pulls(words) the
        # [(number, title, state)] of those that have them.
        self.pulls, self.search_pulls = pull_request, search_pulls

    def setup(self):
        """Gather the evidence for the analyst, without asking it anything, as for
        a discussion; what the tool did with the job, for its first message."""
        with self._held():
            if self.library and share.sync(self.library, self.say) and self.nodes_dir:
                learned.load(self.nodes_dir)
            self.problems = db.load(self.problems_dir)
            self.labels = load_labels(self.labels_dir)
            self._read_at = self._stamp()
            # How the job was settled when the analyst was asked, if it was.
            self._settled_given = self._settled()
            self.result = Triage(self.problems).run(self.job, self.files)
            self._prepare()
        self._source = self._link_source()
        # How to weigh the PRs under test, when there are some.
        self._under_test = ''
        if os.path.exists(os.path.join(self.workdir, 'under-test.txt')):
            with open(os.path.join(PROMPTS, 'under-test.md')) as f:
                self._under_test = f.read()
        return self._progress()

    def instructions(self):
        """What the analyst is told a doubt is, and how to answer one: the first
        prompt of a doubt."""
        with open(os.path.join(PROMPTS, 'doubt.md')) as f:
            return string.Template(f.read()).substitute(
                how=pack.HOW['files'], schema=schema('doubt'), source=self._source,
                under_test=self._under_test, tree=self._progress(),
                person=PERSON_SAYS if self.firm else '',
                also=ALSO_FIRM if self.firm else ALSO_AGREES)

    def run(self, first=None):
        """Talk to the analyst until the job is settled or given up on; returns
        True if it was settled. first, if given, is the first prompt instead of
        the doubt's instructions, for a session that has them already; or, called,
        what to say once the evidence is gathered."""
        self.setup()
        if self.resume:
            self.session = self.resume
            answer = self._ask(ANSWER_NOW)
        else:
            answer = self._ask((first() if callable(first) else first) or self.instructions())
        revisions = 0
        while True:
            answer = self._fetch_more(answer)
            with self._held():
                # Kept, if it is to be, before another lesson changes the library it
                # was checked against.
                ok, report, writes = self.check(answer)
                self._show(answer, report, writes)
                if not self.interactive and ok and (
                        self.expected is None or problem_id(self.expected) == answer['problem']
                        or not self.firm and self._also(answer, writes)):
                    try:
                        return self._accept(answer, writes, report)
                    except Collided as e:
                        # On what others changed meanwhile: for the analyst to revise.
                        ok, report = False, e.report
            if self.interactive:
                try:
                    reply = self.ask('[a]ccept, [q]uit, or tell the analyst what is '
                                     'wrong: ').strip()
                except EOFError:
                    reply = 'q'
                if reply.lower() in ('q', 'quit'):
                    return False
                if reply.lower() in ('a', 'accept'):
                    if ok:
                        # The person approved it here.
                        try:
                            return self._accept(answer, writes, report, approved=True)
                        except Collided as e:
                            self.say('The library changed meanwhile, and the change no '
                                     'longer passes the checks:', *e.report)
                    self.say('It cannot be accepted until the checks pass: tell the '
                             'analyst what to change, or quit.')
                elif reply:
                    answer = self._ask(
                        f'{self.by} replies (they can be wrong too; go by the logs):\n'
                        f'{reply}\n\nThe checks of your last answer gave:\n' +
                        '\n'.join(report) + '\n\nReconsider, and reply with a JSON '
                        'object as before.')
                continue
            if ok:
                # The analyst's answer, which passed the checks, disputes the people's.
                self.disputed = answer['problem'] or 'no record'
                self.say(f'{self.by} expected {self.expected}, the analyst says '
                         f'{self.disputed}: a person needs to decide, '
                         f'with qa_triage doubt {self.job.path} --interactive')
                changed = any(_read(self._path(name)) != text for name, text in writes.items())
                if changed and self.dispute and self.write:
                    # Its change is kept: a person who agrees keeps it by settling the job.
                    self.kept = self.dispute(self, answer)
                    self.say(f'Its change is kept, for qa_triage settle {self.job.path} '
                             f'{answer["problem"] or "new"} --why ... to keep it '
                             f'({self.kept}).')
                return False
            if answer.get('assessment') == 'unsure' or revisions == ROUNDS:
                self.say('Not settled: a person needs to look at this job.')
                return False
            revisions += 1
            answer = self._ask('Your answer did not pass the checks:\n' +
                               '\n'.join(report) + '\n\nFix the records or your answer, '
                               'and reply with a JSON object as before.')

    def settle(self, answer, rebase=None):
        """Check an answer the analyst gave without being asked here, such as one
        it wrote while a person talked with it in its own program, and keep it if
        it passes: as run does, but asking nothing. rebase(answer), if given, makes
        it again onto the library once that is up to date. (settled, the checks'
        report)"""
        self.setup()
        answer = dict(answer, problem=problem_id(answer.get('problem')))
        if rebase:
            answer = rebase(answer)
        ok, report, writes = self.check(answer)
        self._show(answer, report, writes)
        if ok:
            try:
                ok = self._accept(answer, writes, report)
            except Collided as e:
                self.say(f'{self.job.path}: the library changed meanwhile, and the change no '
                         'longer passes the checks; it is not kept.')
                ok, report = False, e.report
        return ok, report

    def _accept(self, answer, writes, report, approved=False):
        """Write an answer that passed the checks: once the library is up to date,
        and checked again if it moved; once a person approves it, if approval is
        asked; and then published to the library's remote, if it is to be, onto the
        library it was checked against: if someone pushed meanwhile, it is taken
        back and checked again with what they pushed. True if it was written; False
        if it was not approved; Collided if it no longer passes the checks."""
        for _ in range(ROUNDS):
            with self._held():
                synced = self.library and share.sync(self.library, self.say)
                if synced or self._stamp() != getattr(self, '_checked_at', None):
                    ok, report, writes = self.check(answer)
                    if not ok:
                        raise Collided(report)
                # Settling the job alone, with no change to the records or the tree,
                # asks no one.
                ask = self.approve_edits if self.edited(writes) else self.approve
                if ask and not approved and writes:
                    approved, answer, writes, report = ask(self, answer, writes, report)
                    if not approved:
                        return False
                self._apply(answer, writes)
                if not (self.publish and self.library and self.write):
                    return True
                try:
                    self.say(share.publish(self.library, f'Learned from {self.job.path}: '
                                           f'{(answer.get("summary") or "")[:100]}\n\n'
                                           f'Approved by {getpass.getuser()}; {self.by} '
                                           'doubted it.', self.say, checked=True,
                                           own=self.written))
                    return True
                except share.Moved as e:
                    self.say(f'{self.job.path}: {e}; the change is checked again with that')
        self.say(f'{self.job.path}: the library kept changing while this change was '
                 f'checked, {ROUNDS} times; it is not kept: doubt the job again later.')
        return False

    def _held(self):
        """The library, for this lesson alone: see holding."""
        return holding(self.problems_dir, self.progress)

    def _settled(self):
        """(problem, who settled it) for the job, if it is settled."""
        return next(((problem, who) for job, problem, who in self.labels
                     if job.path == self.job.path), None)

    def edited(self, writes):
        """The files of writes that edit what the library has, not only add to it:
        a node's code, a sentence of the wording, or anything of a record but
        its examples and the issues it says people also place its jobs in."""
        found = []
        for name, text in writes.items():
            before = _read(self._path(name))
            if before is None or before == text:
                continue
            if name.startswith('problems/'):
                try:
                    old, new = yaml.safe_load(before) or {}, yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    old, new = {}, None
                if isinstance(new, dict) and _grown(old, new):
                    continue
            elif name == f'nodes/{learned.WORDING}':
                try:
                    old, new = learned.wording(before), learned.wording(text)
                except learned.LearnedError:
                    old, new = {None: {}}, {}
                if all(new.get(path, {}).get(said) == sentence
                       for path, answers in old.items() for said, sentence in answers.items()):
                    continue
            found.append(name)
        return found

    def talk_live(self, prompt, proposal, run=None):
        """Tell the analyst prompt, then open its own program on its session, for a
        person to talk with it there: the answer it wrote to proposal meanwhile, if
        any."""
        reply = self.analyst.chat(self.workdir, prompt, self.session,
                                  tag=next_tag(self.workdir, 'discuss'))
        self.session = reply.get('session') or self.session
        if reply.get('text'):
            self.say(reply['text'])
        argv = self.analyst.interactive(os.path.abspath(self.workdir), self.session)
        if not argv:
            return None
        since = time.time()
        (run or subprocess.run)(argv, cwd=self.workdir, env=self.analyst.env())
        path = os.path.join(self.workdir, proposal)
        if not (os.path.exists(path) and os.path.getmtime(path) >= since):
            return None
        try:
            with open(path) as f:
                answer = json.load(f)
        except ValueError as e:
            self.say(f'Its {proposal} does not load: {e}')
            return None
        finally:
            os.replace(path, f'{path}.{int(since)}')
        return dict(answer, problem=problem_id(answer.get('problem'))) \
            if isinstance(answer, dict) else None

    def cost(self):
        """What the analyst says it cost, in its own unit, or None."""
        sessions = {}
        for session, cost in self.costs:
            if cost is not None:
                sessions.setdefault(session, []).append(cost)
        if not sessions:
            return None
        if self.analyst.cumulative_cost:
            # A resumed session's costs include what it cost before.
            return sum(costs[-1] for costs in sessions.values()) - \
                (self.resumed_cost if self.resume in sessions else 0)
        return sum(sum(costs) for costs in sessions.values())

    def _copy_library(self):
        """Give the analyst the records, in records/, and the tree's nodes, to
        change, in nodes/, as the library has them now; and keep what it was
        given, to tell later whether the library changed under it."""
        for old in ('records', 'nodes'):
            shutil.rmtree(os.path.join(self.workdir, old), ignore_errors=True)
        shutil.copytree(self.problems_dir, os.path.join(self.workdir, 'records'))
        os.makedirs(os.path.join(self.workdir, 'nodes'))
        for name in sorted(learned.FILES):
            self._write(os.path.join('nodes', name), learned.FILES[name].code)
        self._given = {f'{kind}/{name}': _read(os.path.join(directory, name))
                       for kind, directory in (('problems', self.problems_dir),
                                               ('nodes', self.nodes_dir))
                       if directory and os.path.isdir(directory)
                       for name in os.listdir(directory)}

    def _stamp(self):
        """What tells whether the library's records, nodes or settled jobs
        changed: each file's name, size and time."""
        return [(directory, e.name, e.stat().st_size, e.stat().st_mtime_ns)
                for directory in (self.problems_dir, self.nodes_dir, self.labels_dir)
                if directory and os.path.isdir(directory)
                for e in sorted(os.scandir(directory), key=lambda e: e.name) if e.is_file()]

    def _up_to_date(self):
        """Read the records, the nodes and the settled jobs again if the
        library changed since they were read: another lesson, at once with this one,
        may have changed them."""
        now = self._stamp()
        if now != getattr(self, '_read_at', now):
            self.problems = db.load(self.problems_dir)
            if self.nodes_dir:
                learned.load(self.nodes_dir)
            self.labels = load_labels(self.labels_dir)
        self._read_at = now

    def _changed_under(self, writes):
        """The files of writes that the library changed since the analyst was given
        them: writing them as the analyst wrote them would undo that."""
        given = getattr(self, '_given', None)
        if given is None:
            return []
        return [name for name, text in sorted(writes.items())
                if _read(self._path(name)) not in (given.get(name), text)]

    def _prepare(self):
        os.makedirs(self.workdir, exist_ok=True)
        # A session continued may quote what it requested.
        going_on = self.resume or self.session
        for old in ('tree',) + (() if going_on else ('requested',)):
            shutil.rmtree(os.path.join(self.workdir, old), ignore_errors=True)
        pack.gather(self.workdir, self.files, self.result, self.problems,
                    passing=self._passing())
        self._copy_library()
        # The tree's code, to read.
        shutil.copytree(os.path.dirname(tree.__file__), os.path.join(self.workdir, 'tree'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        scripts = library.scripts_of(self.problems_dir)
        if scripts:
            self._write('scripts.txt', library.listing(scripts))
        archive = sorted({found for pattern in ARCHIVE for found in self.files.glob(pattern)})
        self._write('archive.txt', ''.join(f'{rel} {size}\n' for rel, size in archive))
        self._daemon_log()
        r = self.result
        doubt = {'job': self.job.path,
                 'result': {'verdict': r.verdict, 'problem': placed(r), 'path': r.path,
                            'notes': r.notes, 'sure': r.sure},
                 'doubted_by': self.by, 'why': self.why, 'expected': self.expected}
        if r.finding:
            doubt['tree'] = {'path': r.finding.where, 'conclusive': r.finding.conclusive,
                             'grows_at': r.finding.grows_at if self.nodes_dir else ''}
        if self.recorded:
            doubt['result_in_the_run'] = self.recorded
        if pack.told(self.judged):
            j = self.judged
            self._write('judge.txt', '\n'.join(
                [f'The judge ({j.get("judge")}) said {j.get("verdict")}' +
                 (f' {db.label(db.record_id(j["problem"]))}' if j.get('problem') else '') +
                 f': {j.get("summary", "")}', '', *report.told_text(j, LINE_LIMIT)]) + '\n')
        # A session continued keeps the doubt it began with: lesson_of reads it.
        if not (going_on and os.path.exists(os.path.join(self.workdir, 'doubt.yaml'))):
            self._write('doubt.yaml', yaml.safe_dump(doubt, sort_keys=False, width=1000))
        number = problem_id(self.expected)
        for issue in dict.fromkeys(([number] if number and number.isdigit() else []) +
                                   [str(n) for n in self.issues]):
            try:
                self._write(f'tracker-{issue}.txt', tracker_text(self.tracker(issue)))
            except (urllib.error.URLError, OSError, KeyError) as e:
                self.say(f'Could not read tracker {issue}: {e}')
        if number and db.is_pr(number):
            try:
                self._write(f'pr-{number[2:]}.txt', pull_text(self.pulls(number[2:])))
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                self.say(f'Could not read {db.label(number)}: {e}')
        if going_on:
            self._write('analysis.md', f'\n# Continued: {going_on}\n\n', 'a')
        else:
            self._write('analysis.md', f'# Doubt about {self.job.path}\n\n')

    def _passing(self):
        """(job, its files) of the job of the doubted job's run that passed most like
        it, whose teuthology.log the archive has; None if there is none."""
        try:
            same = [j for j in self.jobs_for(self.job.run).values() if j.status == 'pass']
            for job in pack.alike(self.job, pack.kept(same, self.files_for))[
                    :pack.PASSING_TRIED]:
                files = self.files_for(job)
                if files.glob('teuthology.log'):
                    return job, files
        except (ArchiveError, OSError):
            pass
        return None

    def _progress(self):
        """What the tool made of the job before the analyst, for its prompt: how far
        the tree got, what each of its nodes found, the result, and why it is in
        doubt."""
        r, lines = self.result, ['What the tool did with this job before you:', '']
        if r.finding:
            lines += [r.finding.progress(), '', *r.finding.lines(LINE_LIMIT), '']
        else:
            lines += ['The tree did not look at it.', '']
        lines += [f'Its result: {describe(r)}, {"matched" if r.sure else "unmatched"}.']
        if self.why:
            lines += [f'Why you are asked: {self.why}']
        return '\n'.join(lines) + '\n'

    def _daemon_log(self):
        """Write daemon-log.txt: the log of the daemon the tree found failed, or
        logged the error, around where it did; and its lines about the object the
        error names, if any."""
        facts = self.result.finding.facts if self.result.finding else {}
        who, message = str(facts.get('reporter') or facts.get('daemon') or ''), \
            str(facts.get('message') or '') if facts.get('reporter') else ''
        if not re.fullmatch(r'[a-z]+\.\w+', who):
            return
        logs = [rel for pattern in (f'remote/*/log/ceph-{who}.log*',
                                    f'remote/*/log/*/ceph-{who}.log*')
                for rel, _ in self.files.glob(pattern)]
        regex, *literals = (re.escape(message[:120]), message[:60]) if message else CRASHED
        for rel in logs:
            found = self.files.search(rel, re.compile(regex), 1, literals)
            if not found:
                continue
            at, lines = found[0].line, []
            lines.append(f'# {rel}, lines {max(1, at - BEFORE)} to {at + AFTER}: around where '
                         f'{who} {"logged the error" if message else "failed"}')
            lines += [f'{h.line}: {h.text[:LINE_LIMIT]}'
                      for h in self.files.between(rel, max(1, at - BEFORE), at + AFTER + 1)]
            named = re.search(r'object (\S+)', message)
            if named:
                hits = self.files.search(rel, re.compile(re.escape(named.group(1))), MAX_HITS,
                                         [named.group(1)])
                lines += ['', f'# {rel}: the lines that name {named.group(1)} (at most '
                              f'{MAX_HITS})']
                lines += [f'{h.line}: {h.text[:LINE_LIMIT]}' for h in hits]
            self._write('daemon-log.txt', '\n'.join(lines) + '\n')
            return

    def _link_source(self):
        """Link the source the job ran into the evidence; the lines that tell the
        analyst about it."""
        found = self.source(self.files, self.say) if self.source else {}
        lines = []
        for name in SOURCES:
            link = os.path.join(self.workdir, name)
            if os.path.islink(link):
                os.remove(link)
            if name in found:
                os.symlink(found[name], link)
                lines.append(SOURCES[name].format(sha1=os.path.basename(found[name])))
        # The PRs under test, in the suite's history, or else the source's.
        for name in ('suite', 'source'):
            if name in found:
                try:
                    changes = under_test(found[name])
                    if changes:
                        # The diffs of qa/, and of the files the job's failure names.
                        said = said_of(self.result) if getattr(self, 'result', None) else ''
                        self._write('under-test.txt', under_test_text(
                            found[name], changes,
                            shown=lambda path: path.startswith('qa/') or
                            pack.mentioned(path, said)))
                        lines.append(UNDER_TEST)
                        break
                except CheckoutError as e:
                    self.say(f'Could not read the PRs under test in {name}/: {e}')
        return ''.join(lines)

    def _write(self, name, text, mode='w'):
        with open(os.path.join(self.workdir, name), mode) as f:
            f.write(text)

    def _ask(self, prompt):
        spent = self.cost()
        if self.budget is not None and spent is not None and spent >= self.budget:
            raise JudgeError(f'{self.analyst.name} has cost {spent:.2f}, which is over the '
                             f'budget of {self.budget} for one lesson')
        full = prompt
        if self.history and not (self.analyst.resumes and self.session):
            full = '\n\n'.join(self.history + [prompt])
        self.say(f'Asking {self.analyst.name}...')
        before, self.analyst.progress = getattr(self.analyst, 'progress', None), self.progress
        try:
            answer = self.analyst.ask(self.workdir, full, kind='doubt', session=self.session,
                                      tag=next_tag(self.workdir, 'doubt'))
        except JudgeError as e:
            self.analyst.progress = before
            if e.session:
                self.costs.append((e.session, e.cost))
                self.session = e.session
            # An agent that ran out of turns has read what it needed: ask it, once,
            # for its answer, rather than lose what it found.
            if e.refused or isinstance(e, Stopped) or \
                    not (e.session and self.analyst.resumes) or prompt == ANSWER_NOW:
                raise
            self.history += [prompt, 'You gave no answer.']
            return self._ask(ANSWER_NOW)
        self.analyst.progress = before
        session = answer.pop('session', None)
        self.costs.append((session, answer.pop('cost', None)))
        self.session = session or self.session
        answer.pop('judge', None)
        self.history += [prompt, 'Your answer was:\n' + json.dumps(answer)]
        answer['problem'] = problem_id(answer.get('problem'))
        return answer

    def _fetch_more(self, answer):
        while answer.get('assessment') == 'need-more' and answer.get('requests'):
            if self.requests == ROUNDS:
                return self._ask('You have had all your requests: answer from the files '
                                 'you have, with a JSON object as before.')
            self.requests += 1
            done = self._fulfil(answer['requests'])
            self.say('Fetched for the analyst:\n' + '\n'.join(f'  {d}' for d in done))
            answer = self._ask('The results of your requests are in requested/:\n' +
                               '\n'.join(done) +
                               '\n\nContinue, and reply with a JSON object as before.')
        return answer

    def _fulfil(self, requests):
        os.makedirs(os.path.join(self.workdir, 'requested'), exist_ok=True)
        done = []
        for i, req in enumerate(requests[:MAX_REQUESTS], 1):
            name = f'requested/{self.requests}-{i}.txt'
            if req.get('script'):
                done.append(self._run_script(req, name))
                continue
            if req.get('search') or req.get('tracker'):
                done.append(self._ask_tracker(req, name))
                continue
            if req.get('pr') or req.get('search_prs'):
                done.append(self._ask_github(req, name))
                continue
            glob, pattern = str(req.get('file', '')), req.get('pattern')
            try:
                regex = re.compile(pattern) if pattern else None
            except re.error as e:
                done.append(f'{glob}: bad pattern ({e})')
                continue
            found = self.files.glob(glob)[:MAX_FILES]
            if not found:
                done.append(f'{glob}: no such file')
                continue
            hits = self.files.search_files(found, regex, max_hits=MAX_HITS) if regex else []
            with open(os.path.join(self.workdir, name), 'w') as out:
                for rel, size in found:
                    if regex:
                        mine = [h for h in hits if h.file == rel]
                        out.write(f'# {rel}: {len(mine)} lines matching /{pattern}/ '
                                  f'(at most {MAX_HITS})\n')
                        out.writelines(f'{h.line}: {h.text[:LINE_LIMIT]}\n' for h in mine)
                        continue
                    out.write(f'# {rel}\n')
                    written = 0
                    for line in self.files.lines(rel, size):
                        written += len(line) + 1
                        if written > COPY_LIMIT:
                            out.write('[... cut by qa_triage at 20 MB: ask with a '
                                      'pattern ...]\n')
                            break
                        out.write(line + '\n')
            done.append(f'{glob}{f" /{pattern}/" if pattern else ""}: {name} '
                        f'({len(found)} files)')
        return done

    def _run_script(self, req, name):
        script = library.scripts_of(self.problems_dir).get(str(req.get('script')))
        if not script:
            return f'script {req.get("script")}: scripts.txt has no such script'
        self._write(name, library.run(script, self.files, self.workdir,
                                      str(req.get('args') or '')))
        return f'script {script.name}: {name}'

    def _ask_tracker(self, req, name):
        words, number = str(req.get('search') or ''), str(req.get('tracker') or '')
        what = f'search "{words}"' if words else f'tracker {number}'
        if words and not self.search:
            return f'{what}: searching is not available'
        if not words and not number.isdigit():
            return f'{what}: not a number'
        try:
            if words:
                hits = self.search(words)
                text = ''.join(f'#{n} {title}\n' for n, title in hits) or 'no issues\n'
                what += f' ({len(hits)} issues)'
            else:
                text = tracker_text(self.tracker(number))
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            return f'{what}: failed ({e})'
        self._write(name, text)
        return f'{what}: {name}'

    def _ask_github(self, req, name):
        words, number = str(req.get('search_prs') or ''), str(req.get('pr') or '')
        number = re.sub(r'^(?:PR)?[ #]*', '', number.strip(), flags=re.I)
        what = f'pull requests "{words}"' if words else f'PR {number}'
        if not words and not number.isdigit():
            return f'{what}: not a number'
        try:
            if words:
                hits = self.search_pulls(words)
                text = ''.join(f'PR {n} ({state}): {title}\n' for n, title, state in hits) or \
                    'no pull requests\n'
                what += f' ({len(hits)} pull requests)'
            else:
                text = pull_text(self.pulls(number))
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            return f'{what}: failed ({e})'
        self._write(name, text)
        return f'{what}: {name}'

    def check(self, answer):
        """(ok, report lines, {record file name: text}) for an answer, against the
        library as it is now."""
        with self._held():
            self._up_to_date()
            self._checked_at = self._read_at
            return self._check(answer)

    def _check(self, answer):
        report, writes = [], {}
        ok = True
        self._stop_here()
        self.progress('  checks: its answer, against the jobs it could change')
        if isinstance(self.files, JobFiles):
            self.progress('  checks: the logs they read are downloaded from qa-proxy over '
                          'https, which is slow; on the archive\'s host, over ssh, they are '
                          'searched where they are')
        self._ticked = time.monotonic()

        def result(good, line):
            nonlocal ok
            ok = ok and good
            report.append(('ok    ' if good else 'FAIL  ') + line)

        if answer.get('assessment') not in ('right', 'wrong'):
            result(False, f'no answer: {ASSESSMENTS.get(answer.get("assessment"), "?")}')
            return ok, report, writes
        settled = self._settled()
        if settled and settled != getattr(self, '_settled_given', settled) and \
                settled[0] != problem_id(answer.get('problem')):
            # Someone settled it while the analyst worked: not written over.
            result(False, f'{self.job.path} was {settled[1]}, as '
                          f'{settled[0] or "no record"}, while you worked: answer that, '
                          'unless the logs show it is wrong; then say so, as unsure, for a '
                          'person to decide')
        if answer.get('pr'):
            result(*self._blame_ok(answer))
        quotes = pack.quotes(answer)
        found = [q for q in quotes if pack.quote_found(self.workdir, q)]
        result(bool(quotes) and len(found) == len(quotes),
               f'{len(found)} of {len(quotes)} quoted lines found in the evidence')
        for q in quotes:
            if q not in found:
                report.append(f'      not found in {q.get("file")}: '
                              f'"{str(q.get("text"))[:150]}"')

        old = {p.id: p for p in self.problems}
        for change in answer.get('changes') or []:
            pid, text = str(change.get('id')), str(change.get('yaml', ''))
            try:
                p = db.parse(yaml.safe_load(text))
            except (yaml.YAMLError, ValueError, TypeError, AttributeError) as e:
                result(False, f'record {pid} does not load: {e}')
                continue
            if p.id != pid:
                result(False, f'record {pid}: its id must be its tracker issue\'s number, or '
                              'PR and the number of the pull request it is of')
                continue
            if pid in old:
                name = os.path.basename(old[pid].source)
                lost = set(old[pid].examples) - set(p.examples)
                if lost:
                    result(False, f'record {pid} loses examples: {", ".join(sorted(lost))}')
            else:
                name = f'{pid}-{_slug(p.title)}.yaml'
                result(*(self._pr_ok(pid) if db.is_pr(pid) else self._tracker_ok(pid, p)))
            writes[f'problems/{name}'] = text.rstrip('\n') + '\n'
        if answer.get('wording'):
            self._wording(answer['wording'], writes, result)
        for node in answer.get('nodes') or []:
            name = str(node.get('file', '')) if isinstance(node, dict) else '?'
            name = name[len('nodes/'):] if name.startswith('nodes/') else name
            code = str(node.get('code', '')) if isinstance(node, dict) else ''
            if not self.nodes_dir:
                result(False, f'node {name}: this doubt cannot change the tree\'s nodes')
                continue
            wrong = learned.check(name, code)
            for line in wrong:
                result(False, line)
            if not wrong:
                writes[f'nodes/{name}'] = code.rstrip('\n') + '\n'
        changed = self._changed_under(writes)
        if changed:
            # Another lesson, or a pull, changed them since: the change is made again
            # on them as they are now.
            self._copy_library()
            result(False, f'{", ".join(changed)} changed in the library while you worked '
                          '(another lesson kept a change to it): records/ and nodes/ now '
                          'have the library as it is; make your change again on that')
            return ok, report, writes

        tmp = tempfile.mkdtemp(prefix='qa_triage-doubt-')
        try:
            for name in os.listdir(self.problems_dir):
                if name.endswith('.yaml'):
                    shutil.copy(os.path.join(self.problems_dir, name), tmp)
            for name, text in writes.items():
                if name.startswith('problems/'):
                    with open(os.path.join(tmp, name[len('problems/'):]), 'w') as f:
                        f.write(text)
            try:
                new = db.load(tmp)
            except ValueError as e:
                result(False, f'the records do not load: {e}')
                return ok, report, writes
            self._regressions(answer['problem'], new, writes, old, result, report)
            for p in new:
                if f'problems/{os.path.basename(p.source)}' in writes:
                    self._breadth(p, old.get(p.id), result, report)
        finally:
            shutil.rmtree(tmp)
        if any(name.startswith('nodes/') and name.endswith('.py') for name in writes):
            self._tested(writes, result, report)
        return ok, report, writes

    def _breadth(self, p, before, result, report):
        """Try record p's confirm checks, if they are new, on jobs that passed, of the
        runs of its examples and of the doubted job, those like its jobs first: checks
        that all find their lines in more than a few such jobs find what jobs that did
        not fail log too, not the failure."""
        checks = [(c.file, c.pattern.pattern) for c in p.confirm]
        if not checks or before and checks == [(c.file, c.pattern.pattern)
                                               for c in before.confirm]:
            return
        runs = list(dict.fromkeys([self.job.run] + [e.rpartition('/')[0] for e in p.examples]))
        passed = [pack.kept([j for j in self.jobs_for(run).values() if j.status == 'pass'],
                            self.files_for) for run in runs]
        like = p.match.get('description')
        # A run at a time, so that each has a turn; the jobs like its own first.
        order = sorted((j for jobs in itertools.zip_longest(*passed) for j in jobs if j),
                       key=lambda j: not (like and like.search(j.description)))
        tried, fired = 0, []
        for job in order[:3 * BREADTH]:
            if tried == BREADTH:
                break
            files = self.files_for(job)
            try:
                if not files.glob('teuthology.log'):
                    continue
                tried += 1
                if all(files.search(c.file, c.pattern, max_hits=1) for c in p.confirm):
                    fired.append(job.path)
            except ArchiveError as e:
                report.append(f'      record {p.id}: could not try its checks on {job.path}, '
                              f'which passed: {e}')
        if not tried:
            report.append(f'      record {p.id}: no job that passed, of the runs of its '
                          'examples, was there to try its confirm checks on')
            return
        result(len(fired) <= BREADTH_SHARE * tried,
               f'record {p.id}: its confirm checks all found their lines in {len(fired)} of '
               f'{tried} jobs of its runs that passed' +
               (': they find what jobs that did not fail log too, not the failure; check '
                'for the line that shows the cause, such as the first error'
                if len(fired) > BREADTH_SHARE * tried else ''))
        report.extend(f'      passed, and has them all: {path}' for path in fired[:5])

    def _wording(self, given, writes, result):
        """Check the sentences an answer gives the tree's answers, and add them to the
        wording file, in writes."""
        if not self.nodes_dir:
            result(False, 'wording: this doubt cannot keep the tree\'s wording')
            return
        path = os.path.join(self.nodes_dir, learned.WORDING)
        try:
            kept = learned.wording(_read(path) or '')
        except learned.LearnedError as e:
            result(False, str(e))
            return
        finding = self.result.finding
        for item in given if isinstance(given, list) else [given]:
            at, said, sentence = (str(item.get(k, '')) if isinstance(item, dict) else ''
                                  for k in ('node', 'answer', 'says'))
            at = '' if at in ('root', '/') else at.strip('/')
            if at not in tree.NODES:
                result(False, f'wording: there is no node at "{at}"')
                continue
            try:
                learned.wording(yaml.safe_dump({at: {said: sentence}}))
            except learned.LearnedError as e:
                result(False, str(e))
                continue
            for step in (finding.steps if finding else []):
                if step.node == at and step.answer == said:
                    missing = [n for n in tree.core.FIELD.findall(sentence)
                               if n and n not in finding.facts and n != 'says']
                    result(not missing, f'wording of "{at}" {said}: ' + (
                        f'this job has no fact {", ".join(missing)}' if missing else
                        'it reads, for this job: ' + tree.core.FIELD.sub(
                            lambda m: str(dict(finding.facts, says=step.says)[m.group(1)])
                            if m.group(1) else m.group(0)[0], sentence)))
            kept.setdefault(at, {})[said] = sentence
        writes[f'nodes/{learned.WORDING}'] = (
            '# How the tree\'s answers read to a person, in place of the nodes\' own\n'
            '# sentences: by the node\'s path (the root\'s is \'\'), then its answer. {name} is\n'
            '# a fact the tree found by then; {says} is the node\'s own sentence.\n' +
            yaml.safe_dump(kept, sort_keys=True, width=100, allow_unicode=True))

    def _blame_ok(self, answer):
        """(ok, line) of the checks of an answer that says a PR under test caused the
        failure: one under-test.txt names, with the job placed in no record, which
        it adds none for."""
        pr = str(answer['pr']).strip()
        listed = _read(os.path.join(self.workdir, 'under-test.txt')) or ''
        number = re.search(r'\d{3,}', pr)
        if not listed:
            return False, f'{pr} caused it: but no PRs under test were found for this job'
        if not (pr in listed or (number and re.search(rf'#{number.group(0)}\b', listed))):
            return False, f'{pr} caused it: but under-test.txt names no such PR'
        if answer.get('problem') is not None:
            return False, (f'{pr} caused it: then the job is new, the PR\'s: give "problem" '
                           'null')
        for change in answer.get('changes') or []:
            try:
                examples = (yaml.safe_load(str(change.get('yaml', ''))) or {}).get('examples')
            except (yaml.YAMLError, AttributeError):
                continue
            if self.job.path in [str(e) for e in examples or []]:
                return False, (f'{pr} caused it: then it needs no record, which would claim '
                               'jobs once the PR is gone; give it no example of this job')
        return True, f'{pr}, under test, caused it, as under-test.txt names it'

    def _also(self, answer, writes):
        """True if the record the answer places the job in, as the answer leaves it,
        says people place its failures in the expected issue too."""
        want, expected = str(answer.get('problem')), problem_id(self.expected)
        for name, text in writes.items():
            if name.startswith('problems/'):
                data = yaml.safe_load(text) or {}
                if str(data.get('id')) == want:
                    return expected in [str(a) for a in data.get('also') or []]
        return any(p.id == want and expected in p.also for p in self.problems)

    def _moves_with(self, where, after, doubted, want):
        """True if a job, which an agent settled, or which is only a record's
        example, may take want with the doubted job: people said what the doubted
        job is, and the change gives the job want, explaining its failure as it does
        the doubted job's (after and doubted are their results with the change). An
        agent's earlier answer gives way to people's; a person's never does."""
        if not (self.said_by_people and want and placed(after) == want):
            return False
        if not (where.startswith('settled by qa_triage') or where.startswith('example of ')):
            return False
        return after.signature == doubted.signature

    def _drop_example(self, pid, path, new, writes):
        """Take path out of record pid's examples, in writes."""
        record = next((p for p in new if p.id == pid), None)
        if not record:
            return
        name = f'problems/{os.path.basename(record.source)}'
        text = writes.get(name) or _read(record.source) or ''
        text = re.sub(rf'^[ \t]*- [\'"]?{re.escape(path)}[\'"]?[ \t]*\n', '', text, flags=re.M)
        # With its last example gone, the record has none.
        writes[name] = re.sub(r'^examples:[ \t]*\n(?![ \t]+-)', '', text + '\n',
                              flags=re.M).rstrip('\n') + '\n'

    def _stop_here(self):
        """Stop the checks, as the analyst stops, when a person stops it."""
        if getattr(self.analyst, 'stopped', False):
            raise Stopped(f'{self.analyst.name}: stopped')

    def _at_once(self, work, items, what, before=0, total=None):
        """[work(item) for item in items], AT_ONCE at a time, as the triage asks
        the archive: a check reads the logs of each job it walks, on the archive's
        host or fetched from qa-proxy, which can take a while. It says now and then
        how far it has got, as before more of total, and stops when a person stops
        the analyst."""
        items = list(items)
        found = [None] * len(items)

        def do(i):
            self._stop_here()
            found[i] = work(items[i])

        with concurrent.futures.ThreadPoolExecutor(AT_ONCE) as pool:
            futures = [pool.submit(do, i) for i in range(len(items))]
            try:
                for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                    future.result()
                    if time.monotonic() - self._ticked >= TICK:
                        self._ticked = time.monotonic()
                        self.progress(f'  checks: {before + done} of '
                                      f'{total or len(items)} {what}')
            except BaseException:
                pool.shutdown(cancel_futures=True)
                raise
        return found

    def _triage(self, job, problems):
        """Triage(problems)'s result for job, or None if its archive is gone."""
        files = self.files_for(job)
        return Triage(problems).run(job, files) if files.glob('teuthology.log') else None

    def _explain(self, job):
        """What the tree finds for job now, or None if its archive is gone."""
        files = self.files_for(job)
        return tree.explain(job, files) if files.glob('teuthology.log') else None

    def _found(self, job):
        """True if where the tree went for job is known without walking it."""
        return job.path in self.paths or \
            (self.triaged.get(job.path) or {}).get('tree') is not None or \
            (job.path, learned.version()) in _FINDINGS

    def _regressions(self, want, new, writes, old, result, report):
        # The settled jobs that move with the doubted job: {path: Job}.
        self.resettled = {}
        codes = {name[len('nodes/'):]: text for name, text in writes.items()
                 if name.startswith('nodes/') and name.endswith('.py')}
        # Where the tree changes: the paths of the changed nodes, before and after.
        grows, examples = set(), {}
        if codes:
            kept = {name: dict(learned.FILES[name].examples) for name in codes
                    if name in learned.FILES}
            try:
                with learned.trying(codes) as files:
                    grows = {path for f in files for path in f.paths}
                    examples = {e: path for f in files for e, path in f.examples.items()}
                    for f in files:
                        self._kept_examples(f, kept.get(f.name, {}), result)
            except learned.LearnedError as e:
                result(False, f'the nodes do not load: {e}')
                return
            grows |= {path for name in codes if name in learned.FILES
                      for path in learned.FILES[name].paths}
            result(self.job.path in examples, f'{self.job.path} is one of the changed nodes\' '
                                              'EXAMPLES')
        changed = [p for p in new if f'problems/{os.path.basename(p.source)}' in writes]
        # A job either version of a changed record could apply to may change.
        touched = changed + [old[p.id] for p in changed if p.id in old]
        changed_ids = {p.id for p in changed}
        ids = {p.id for p in new}
        if want is not None and want not in ids:
            result(False, f'there is no record {want}: add one in "changes"')

        def affected(job):
            return any(self._might_apply(p, job) for p in touched) or \
                self._reaches(job, grows)

        stale = []
        self.paths, self.walked = load_paths(self.paths_dir, stale), {}
        library = list(self.labels)
        # Every example's job, from its run's jobs: pulpito lists a run slowly, so the
        # runs are listed several at once first.
        self._list_runs({e.rpartition('/')[0] for p in new for e in p.examples})
        for p in new:
            for example in p.examples:
                library.append((self._job(example), p.id, f'example of {p.id}'))
        if grows or any(p.trees for p in touched):
            # Where the tree went for each job the change could reach, which the
            # paths kept and the triages before do not say: walked AT_ONCE at a time.
            if stale:
                self.progress(f'  checks: the paths kept for {len(stale)} runs of the library '
                              'were found by other tree code, so their jobs are walked again '
                              '(qa_triage paths walks them all)')
            jobs = {job.path: job for job, _, _ in library}
            jobs.update((job.path, job) for job, _ in list(self.known) + list(self.holdout))
            jobs.update((job.path, job) for job in self.corpus)
            self._at_once(self._finding, [job for path, job in jobs.items()
                                          if path != self.job.path and not self._found(job)],
                          'jobs walked through the tree, to see if the change could reach them')
        settled = [(job, expected, where) for job, expected, where in library
                   if job.path != self.job.path and (expected in changed_ids or affected(job))]
        # The library's jobs that were walked, which the change cannot reach: they
        # take the same path after it.
        tried = {job.path for job, _, _ in settled}
        for job, _, _ in library:
            finding = _FINDINGS.get((job.path, learned.version()))
            if finding and job.path not in self.paths and job.path not in tried:
                self.walked[job.path] = finding
        # The jobs whose answers are known: a change may not give one that was right
        # a wrong answer, nor one that was wrong another wrong answer.
        known = [(job, answer) for job, answer in self.known
                 if job.path != self.job.path and (answer in changed_ids or affected(job))]
        known = [(job, answer, before) for (job, answer), before in zip(known, self._at_once(
            lambda k: self._triage(k[0], self.problems), known, 'jobs with known answers triaged'))
                 if before is not None]
        # The held-out jobs it could change, and whether each was placed rightly.
        held = [(job, answer) for job, answer in self.holdout
                if job.path != self.job.path and (answer in changed_ids or affected(job))]
        held = [(job, answer, fits(before, answer)) for (job, answer), before in zip(
            held, self._at_once(lambda h: self._triage(h[0], self.problems), held,
                                'held-out jobs triaged')) if before is not None]

        # Nobody has settled the other jobs, so a change to their results is
        # accepted only for jobs that failed the way the changed records' or
        # nodes' examples did. What the tree said of them before is found first.
        same = {fingerprint(self.job)} | {fingerprint(job) for job, expected, _ in settled
                                          if expected in changed_ids}
        explained = {self.result.signature}
        others = [self._job(example) for example in examples if example != self.job.path]
        for job, finding in zip(others, self._at_once(
                self._explain, others, 'examples of the changed nodes walked as they were')):
            if finding:
                explained.add(finding.signature(job))
        skip = {job.path for job, _, _ in settled + known} | {self.job.path}

        def triaged(job):
            files = self.files_for(job)
            if files.glob('teuthology.log'):
                # A node changes the tree, so its old answers are found first;
                # a record leaves the tree as it is, so they share its findings.
                return job, files, Triage(self.problems).run(job, files) if codes else None
            return None

        candidates = [job for job in self.corpus if job.path not in skip and affected(job)]
        corpus = [c for c in self._at_once(triaged, candidates, 'jobs triaged before, '
                                                               'triaged again as they were') if c]
        shared = {}

        with learned.trying(codes):
            r = Triage(new).run(self.job, self.files)
            good = placed(r) == want and (r.sure or not self.want_sure)
            result(good, f'{self.job.path} (doubted): {describe(r)}, '
                         f'{"matched" if r.sure else "unmatched"}; should be {want or "no record"}' +
                         (', and matched without a judge' if self.want_sure else ''))
            if placed(r) != want and want in ids:
                record = next(p for p in new if p.id == want)
                report.extend(f'      {line}' for line in misses(record, r))
            if not good and r.finding and r.finding.conclusive and r.candidates:
                report.append('      The tree explains it, but records nearly fit it: the '
                              'prefilter chose ' + ', '.join(p.id for p in r.candidates) +
                              ', and their checks failed. Place the job in one, or keep '
                              'them off it (with tree, facts or an exclude check).')
            if not good and r.finding:
                report.append('      What the tree now says of it:')
                report.extend(f'      {line}' for line in r.finding.lines(200))
            if r.finding:
                self.walked[self.job.path] = r.finding
            if codes:
                every = reachable_examples(codes, grows)
                self._list_runs({example.rpartition('/')[0] for _, example, _ in every})
                jobs = {example: self._job(example) for _, example, _ in every}
                found = self._at_once(
                    lambda e: r.finding if e[1] == self.job.path else self._explain(jobs[e[1]]),
                    every, 'examples of the nodes walked with the change')
                for (name, example, path), finding in zip(every, found):
                    self._example(example, path, name, finding, result, cite=name in codes)
                    if finding:
                        self.walked[example] = finding
            afters = self._at_once(lambda e: self._triage(e[0], new), settled,
                                   'jobs settled before triaged with the change')
            for (job, expected, where), after in zip(settled, afters):
                if after is None:
                    report.append(f'skip  {job.path} ({where}): its archive is gone')
                    continue
                if after.finding:
                    self.walked[job.path] = after.finding
                if not fits(after, expected) and self._moves_with(where, after, r, want):
                    self.resettled[job.path] = job
                    if where.startswith('example of '):
                        self._drop_example(where[len('example of '):], job.path, new, writes)
                    report.append(f'ok    {job.path} ({where}): {describe(after)}; it failed '
                                  f'the way this job did, and moves with it, since people '
                                  f'said what this job is and no person settled that one')
                    continue
                # Right in a record that lists what people said under also, as the
                # score counts it: a duplicate issue's jobs may go in the original's.
                result(fits(after, expected), f'{job.path} ({where}): {describe(after)}; '
                                              f'should be {expected or "no record"}' +
                       (', or a record that lists it under also' if expected else ''))
            afters = self._at_once(lambda k: Triage(new).run(k[0], self.files_for(k[0])), known,
                                   'jobs with known answers triaged with the change')
            for (job, answer, before), after in zip(known, afters):
                result(fits(after, answer) or placed(after) == placed(before),
                       f'{job.path} (known: {answer}): {placed(before) or "no record"} -> '
                       f'{placed(after) or "no record"}')
            if held:
                # Only how many: what they are, and which, the analyst is not told.
                afters = self._at_once(lambda h: Triage(new).run(h[0], self.files_for(h[0])),
                                       held, 'held-out jobs triaged with the change')
                right = sum(fits(after, answer) for (_, answer, _), after in zip(held, afters))
                was = sum(rightly for _, _, rightly in held)
                result(right >= was, f'the held-out jobs it could change, which you are not '
                                     f'shown: {was} of {len(held)} placed rightly before, '
                                     f'{right} after')
            def again(c):
                job, files, before = c
                if before is None:
                    old_triage = Triage(self.problems)
                    old_triage.findings = shared
                    before = old_triage.run(job, files)
                new_triage = Triage(new)
                if not codes:
                    new_triage.findings = shared
                return before, new_triage.run(job, files)

            def tried():
                for at in range(0, len(corpus), CHUNK):
                    chunk = corpus[at:at + CHUNK]
                    yield from zip(chunk, self._at_once(
                        again, chunk, 'jobs triaged before triaged with the change', at,
                        len(corpus)))

            moved, sure, failures, checked = 0, 0, 0, 0
            for (job, files, _), (before, after) in tried():
                if failures == MAX_FAILURES:
                    report.append(f'      ... and {len(corpus) - checked} more of the jobs '
                                  f'triaged before were not checked: the change failed '
                                  f'{failures} already')
                    break
                checked += 1
                if placed(before) == placed(after) and before.sure == after.sure:
                    continue
                moved += 1
                alike = fingerprint(job) in same or before.signature in explained
                prior = (self.triaged.get(job.path) or {}).get('problem')
                if placed(before) != placed(after):
                    change = f'{placed(before) or "no record"} -> {placed(after) or "no record"}'
                else:
                    change = (f'{"matched" if before.sure else "unmatched"} -> '
                              f'{"matched" if after.sure else "unmatched"} {after.verdict} '
                              f'({after.finding.where if after.finding else "-"})')
                line = f'{job.path} (not settled): {change}'
                if before.sure and not after.sure:
                    good = False
                    line += ': it is no longer matched' + \
                        (f', because {", ".join(p.id for p in after.candidates)} nearly '
                         'fit it' if after.candidates else '')
                elif placed(before) == placed(after) and prior and prior != placed(after):
                    good = False
                    line += f', but its triage placed it in {prior}'
                else:
                    # A change that only stops a record nearly fitting a job gives it
                    # back the certainty the record took from it.
                    released = not placed(after) and after.sure and not before.sure and \
                        {p.id for p in before.candidates} & {p.id for p in touched} and \
                        not {p.id for p in after.candidates} & {p.id for p in touched}
                    good = alike or bool(released)
                    sure += good and after.sure and not before.sure
                    if released:
                        line += ', since a changed record no longer nearly fits it'
                    elif not alike:
                        line += f', but it failed differently: "{job.reason[:160]}"' + \
                            (f', and the tree explained it as "{before.signature[:160]}"'
                             if codes else '')
                failures += not good
                result(good, line)
        if self.corpus:
            report.append(f'      {moved} of the {len(self.corpus)} jobs triaged before '
                          f'change result' +
                          (f'; {sure} of them are now matched without a judge' if sure else ''))

    def _kept_examples(self, f, before, result):
        """Check that the node file f keeps the examples it had, before, each with its
        path: only the doubted job's path may change, as people said what it is."""
        lost = sorted(e for e in before if e not in f.examples)
        moved = sorted(e for e in before if e in f.examples and f.examples[e] != before[e]
                       and e != self.job.path)
        if lost:
            result(False, f'nodes/{f.name} loses examples: {", ".join(lost)}: keep every '
                          'example, with its path')
        for e in moved:
            result(False, f'nodes/{f.name} moves its example {e} from {before[e]} to '
                          f'{f.examples[e]}: only this job\'s path may change; doubt that '
                          'job to move it')

    def _tested(self, writes, result, report):
        """Run the tool's tests on the tree's nodes as the change leaves them."""
        self.progress('  checks: the tool\'s tests, on the tree with the change')
        nodes = tempfile.mkdtemp(prefix='qa_triage-nodes-')
        try:
            for name in os.listdir(self.nodes_dir) if os.path.isdir(self.nodes_dir) else ():
                if name.endswith('.py'):
                    shutil.copy(os.path.join(self.nodes_dir, name), nodes)
            for name, text in writes.items():
                if name.startswith('nodes/') and name.endswith('.py'):
                    with open(os.path.join(nodes, name[len('nodes/'):]), 'w') as f:
                        f.write(text)
            failed = run_tests(nodes)
        finally:
            shutil.rmtree(nodes)
        result(not failed, 'the tool\'s tests pass with the change' if not failed else
               'the tool\'s tests fail with the change: make the nodes pass them, or, if a '
               'test expects what is wrong, say which in "tool_suggestions"')
        report.extend(f'      {line}' for line in failed)

    def _job(self, example):
        run, _, jid = example.rpartition('/')
        return self.jobs_for(run).get(jid) or Job(run, jid, '', '', '')

    def _list_runs(self, runs):
        """List the jobs of runs, as jobs_for does, AT_ONCE at a time."""
        with concurrent.futures.ThreadPoolExecutor(AT_ONCE) as pool:
            list(pool.map(self.jobs_for, sorted(runs)))

    def _finding(self, job):
        """What the tree found for job: as the library keeps it, as its triage
        recorded it, or found now; None if its archive is gone."""
        if job.path in self.paths:
            return self.paths[job.path]
        entry = self.triaged.get(job.path) or {}
        if entry.get('tree') is not None:
            return tree.Finding(path=[p for p in entry['tree'].split('/') if p],
                                facts=entry.get('facts') or {})
        key = (job.path, learned.version())
        if key not in _FINDINGS:
            files = self.files_for(job)
            _FINDINGS[key] = tree.explain(job, files) if files.glob('teuthology.log') else None
        return _FINDINGS[key]

    def _might_apply(self, p, job):
        """True if record p could apply to job: its prefilter, given what the tree
        found; or, for a record that hangs below where the tree stopped then, its
        match keys alone, since a deeper tree may reach it."""
        if not p.trees:
            return p.prefilter(job)
        finding = self._finding(job)
        if finding is None:
            return False
        if not p.hangs(finding.where) and any(under(top, finding.where) for top in p.trees):
            return all(regex.search(getattr(job, key)) for key, regex in p.match.items())
        return p.prefilter(job, finding)

    def _reaches(self, job, grows):
        """True if the walk through the tree for job could reach one of the paths
        in grows: it went there, or stopped on the way."""
        if not grows:
            return False
        finding = self._finding(job)
        path = finding.where if finding else ''
        return bool(path) and any(reaches(path, top) for top in grows)

    def _example(self, example, path, name, finding, result, cite=False):
        """Check that a node file's example still takes its path, given what the
        tree finds for it now (None if its archive is gone); and with cite, that the
        file's nodes cite the lines they decide it by."""
        if finding is None:
            return
        result(finding.conclusive and under(finding.where, path),
               f'{example} (example of nodes/{name}): {finding.where or "-"}, '
               f'{"conclusive" if finding.conclusive else "inconclusive"}; should be {path}, '
               'conclusive')
        mine = set(learned.FILES[name].paths) if cite else set()
        for step in finding.uncited():
            if step.node in mine:
                result(False, f'{example} (example of nodes/{name}): the node at '
                              f'"{step.node}" answered {step.answer} and cited no line: give '
                              f'{step.kind} the lines that show it, which people are shown '
                              'as its evidence')

    def _pr_ok(self, pid):
        """(ok, line) for a new record of a pull request: the PR must exist."""
        try:
            pr = self.pulls(pid[2:])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, f'{db.label(pid)} does not exist'
            return True, f'{db.label(pid)} could not be read ({e}): check it by hand'
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            return True, f'{db.label(pid)} could not be read ({e}): check it by hand'
        return True, (f'{db.label(pid)}: {pr["title"]} '
                      f'({"merged" if pr["merged"] else pr["state"]})')

    def _tracker_ok(self, number, record):
        try:
            issue = self.tracker(number)
        except urllib.error.HTTPError:
            return False, f'tracker {number} does not exist'
        except (urllib.error.URLError, OSError) as e:
            return True, f'tracker {number} could not be read ({e}): check it by hand'
        if issue['project']['id'] == QA_PROJECT:
            return False, f'tracker {number} lists a QA batch; it is not a problem'
        line = f'tracker {number}: {issue["subject"]} ({issue["status"]["name"]})'
        other = original(issue)
        if other:
            return False, (f'{line}: it is {other[0]}; give the record the number of the '
                           f'issue it names' + (f' (#{other[1]})' if other[1] else ''))
        fields = {c['name']: c.get('value') for c in issue.get('custom_fields', [])}
        # The fix check needs the merge's version, so that a problem seen again
        # after its fix is escalated instead of called a duplicate.
        fixed_in = fields.get('Fixed In')
        if fixed_in and fixed_in not in [f.fixed_in for f in record.fixes]:
            return False, f'{line}; it was fixed in {fixed_in} (PR ' \
                          f'{fields.get("Pull request ID")}), which the record\'s fixes ' \
                          f'must give as fixed_in'
        return True, line

    def _show(self, answer, report, writes):
        a = self.answer = answer
        lines = ['', f'{self.analyst.name}: {ASSESSMENTS.get(a.get("assessment"), "?")}. '
                     f'{a.get("summary", "")}',
                 f'Its answer: {a["problem"] or "no record"}. The result was '
                 f'{describe(self.result)}.', '', str(a.get('explanation', '')).strip()]
        for n, step in enumerate(pack.steps(a), 1):
            lines += ([''] if n == 1 else []) + [
                f'{n}. {step.get("title") or ""}: {step.get("do") or ""} '
                f'{step.get("finds") or ""}'.strip()]
        quotes = pack.quotes(a)
        if quotes:
            lines += ['', 'Quoted:'] + [f'  {q.get("file")}: {q.get("text")}' for q in quotes]
        for name, text in writes.items():
            path = self._path(name)
            before = []
            if os.path.exists(path):
                with open(path) as f:
                    before = f.read().splitlines()
            lines += [''] + list(difflib.unified_diff(
                before, text.splitlines(), f'a/{name}', f'b/{name}', lineterm=''))
        if a.get('tool_suggestions'):
            lines += ['', f'Suggestions for the tool: {a["tool_suggestions"]}']
        lines += ['', 'Checks:'] + report
        text = '\n'.join(lines)
        self.say(text)
        self._write('analysis.md', text + '\n', 'a')

    def _label(self, answer):
        """(problem, by, why) of the label an accepted answer settles the job with.
        What people said the job is stays theirs, though the record that places it
        lists it under also; an answer no person gave is the analyst's, though a
        person doubted the result."""
        if self.said_by_people:
            return problem_id(self.expected), self.by, self.why
        if self.by and not str(self.by).startswith('qa_triage'):
            analyst = getattr(self.analyst, 'name', None) or 'an analyst'
            return (answer['problem'], f'qa_triage ({analyst})',
                    f'{self.by} doubted it' + (f': {self.why}' if self.why else '') +
                    '; this is the analyst\'s answer, not theirs')
        return answer['problem'], self.by, self.why

    def _path(self, name):
        """Where a change, such as problems/<file> or nodes/<file>, is kept."""
        kind, _, base = name.partition('/')
        return os.path.join(self.problems_dir if kind == 'problems' else self.nodes_dir, base)

    def _apply(self, answer, writes):
        if not self.write:
            self.say('Nothing written (--dry-run).')
            return
        # A file the answer gives as it is now is not written, or said to be; the
        # others say who learned what they say, from which job.
        writes = {name: text for name, text in writes.items() if _read(self._path(name)) != text}
        analyst = getattr(self.analyst, 'name', None)
        learnt = (f'{datetime.date.today()} {readable(analyst) if analyst else "an analyst"}, '
                  f'from {self.job.path}' + (f', as {self.by} said' if self.by else ''))
        writes = {name: stamped(name, text, learnt, _read(self._path(name)))
                  for name, text in writes.items()}
        for name, text in writes.items():
            os.makedirs(os.path.dirname(self._path(name)), exist_ok=True)
            with open(self._path(name), 'w') as f:
                f.write(text)
        if any(name.startswith('nodes/') for name in writes):
            learned.load(self.nodes_dir)
        self.written = list(writes)
        # Where the tree takes the jobs the checks walked, for the next change's checks.
        save_paths(self.paths_dir, self.walked)
        pr = answer.get('pr')
        if pr:
            self.say(f'{self.job.path}: {self.analyst.name} found that {pr}, under test, '
                     f'caused it: {answer.get("summary", "")}')
        if answer['problem'] is None and not self.label_none:
            self.say(f'{self.job.path}: no record should place it' +
                     (f'. Wrote {", ".join(self.written)}' if self.written else ''))
            return
        problem, by, why = self._label(answer)
        label = save_label(self.labels_dir, self.job, problem, by, why,
                           answer.get('summary', ''), pr)
        self.written.append(os.path.relpath(label, os.path.dirname(self.labels_dir)))
        for job in self.resettled.values():
            label = save_label(self.labels_dir, job, problem, by,
                               f'It failed the way {self.job.path} did, which people placed '
                               f'in {problem}.', answer.get('summary', ''))
            name = os.path.relpath(label, os.path.dirname(self.labels_dir))
            if name not in self.written:
                self.written.append(name)
        self.say(f'Settled {self.job.path} as {problem or "no record"}. Wrote ' +
                 ', '.join(self.written))


# Who learned what a record or a node says: its "learned" list, or its
# "# learned:" comments, one for each change, the oldest first.
LEARNED_KEY = re.compile(r'^learned:.*\n?(?:[ \t]*-.*\n?)*', re.M)
LEARNED_COMMENT = '# learned: '


def _learned(name, text):
    """What text, of the library's file name, says of who learned it: [entry]."""
    if name.startswith('problems/'):
        try:
            said = (yaml.safe_load(text or '') or {}).get('learned') or []
        except (yaml.YAMLError, AttributeError):
            return []
        return [str(s) for s in (said if isinstance(said, list) else [said])]
    return [line[len(LEARNED_COMMENT):] for line in (text or '').splitlines()
            if line.startswith(LEARNED_COMMENT)]


def stamped(name, text, learnt, before=None):
    """text, of the library's file name, saying who learned it: what before, the file
    as it was, and text said of that, then learnt."""
    said = list(dict.fromkeys(_learned(name, before) + _learned(name, text) + [learnt]))
    if name.startswith('problems/'):
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            return text
        return LEARNED_KEY.sub('', text).rstrip('\n') + '\nlearned:\n' + \
            ''.join(f'- {json.dumps(s)}\n' for s in said)
    if name.startswith('nodes/') and name.endswith('.py'):
        code = '\n'.join(line for line in text.splitlines()
                         if not line.startswith(LEARNED_COMMENT)).rstrip('\n')
        return code + '\n' + ''.join(f'{LEARNED_COMMENT}{s}\n' for s in said)
    return text


def _grown(old, new):
    """True if the record new is old with only examples, or issues people also place
    its jobs in, added: who learned it aside."""
    grows = ('examples', 'also')
    if any(not {str(e) for e in old.get(key) or []} <= {str(e) for e in new.get(key) or []}
           for key in grows):
        return False
    return {k: v for k, v in old.items() if k not in grows + ('learned',)} == \
        {k: v for k, v in new.items() if k not in grows + ('learned',)}


def recorded(r):
    """A triage result as doubt.yaml gives it."""
    entry = {'verdict': r.verdict, 'problem': placed(r), 'path': r.path, 'notes': r.notes}
    if r.finding:
        entry['explanation'] = r.finding.lines()
    if r.agent:
        entry['judge'] = {k: r.agent.get(k) for k in ('judge', 'verdict', 'problem', 'summary')}
    return entry


def grows(r):
    """Where the tree could learn to explain r's job, or ''."""
    return r.finding.grows_at if r.finding and not r.finding.conclusive else ''


def lessons(results, known=None):
    """What a run's results can teach the tree and the records: [(result, expected
    problem id or None for any, why, group size)] for one job of each group that
    they could not decide by themselves, the likeliest to teach something first.
    known gives the right answers for some jobs, {job id: problem id}: first come
    the jobs whose result differs, one of each group that failed the same way."""
    found, seen = [], set()
    known = known or {}
    wrong = [r for r in results if r.job.id in known and not fits(r, known[r.job.id])]
    for r in wrong:
        # Jobs that need the same lesson: the same answer, the same wrong result,
        # and the same explanation.
        label = known[r.job.id]
        key = (label, placed(r), r.signature)
        if key in seen:
            continue
        seen.add(key)
        size = sum(1 for o in wrong if (known[o.job.id], placed(o), o.signature) == key)
        said = f'the tool said {describe(r)}, {"matched" if r.sure else "unmatched"}'
        if r.agent:
            said += f', after its judge ({r.agent.get("judge")}) said ' \
                    f'{r.agent.get("verdict")} {r.agent.get("problem") or ""}'
        found.append((-1, r, label, f'The QA reviewers placed this job in {label}, but '
                      f'{said}. Find out whether the reviewers are right. If they are, '
                      'change the records, or the tree (mend a node on its path that '
                      'answered it wrongly, or add one where it stopped), so that the tool '
                      f'places jobs like this one in {label} by itself, with no judge.', size))
    for r in results:
        if r.job.id in known:
            continue
        o = r.opinion
        if o and o.get('agrees') is False and r.signature not in seen:
            # The tree and the records decided it, and the judge, asked to check that,
            # disagrees.
            seen.add(r.signature)
            size = sum(1 for other in results if other.opinion is o)
            found.append((0, r, None, f'The tool decided this job by itself: '
                          f'{describe(r)}. Its judge ({o.get("judge")}), asked to check '
                          f'that, disagrees: it says {o.get("verdict")} '
                          f'{o.get("problem") or ""}: {o.get("summary", "")}. Find out from '
                          'the logs which is right. If the judge is, change the records, or '
                          'the tree (mend a node on its path that answered it wrongly, or '
                          'add one where it stopped), so that the tool gets jobs like this one '
                          'right by itself; if the tool is, say why.', size))
            continue
        if 'memory' in r.path and r.problem and r.signature not in seen:
            # No record's checks placed it: the memory of what people answered for the
            # same failure did. The records are to place it themselves.
            seen.add(r.signature)
            size = sum(1 for o in results if 'memory' in o.path and o.signature == r.signature)
            pid = r.problem.id
            mend = (f'add a record for {pid}, which has none yet'
                    if r.problem.source == 'memory' else
                    f'change record {pid}: where one of its checks finds no line in this '
                    'job, make it find what this job logs instead, without losing its '
                    'examples')
            found.append((0, r, pid, 'No record\'s checks placed this job, but people placed '
                          f'jobs that failed the same way in {pid} before, so the tool\'s '
                          'memory of their answers placed it there. If they are right, '
                          f'{mend}, so that the checks place jobs like this one by '
                          'themselves. If they are wrong, say what it is.', size))
            continue
        if r.sure and not r.agent:
            # The tree and the records decided it: if new, nobody has looked for its
            # cause. The test infrastructure's failures are left out.
            if r.verdict != 'new' or not r.finding or r.finding.path[:1] == ['infra'] or \
                    r.signature in seen:
                continue
            seen.add(r.signature)
            size = sum(1 for o in results if o.verdict == 'new' and o.sure and not o.agent
                       and o.signature == r.signature)
            says = r.finding.steps[-1].says if r.finding.steps else r.job.reason
            found.append((3, r, None, f'No record placed this job, and the tree explained it '
                          f'conclusively, so the tool calls it new: {says}. Nobody has looked '
                          'for its cause. Find it, beyond the symptom where the tree stopped: '
                          'read the logs around the failure, the daemons\' logs and the source. '
                          'Search the tracker: if an issue describes this failure, add a record '
                          'for it. If none does, leave "problem" null, and say in the summary '
                          'what failed and why, for a person to file. If the explanation stops '
                          'at a symptom, teach the tree the cause, with a node at '
                          f'"{r.finding.grows_at}": it is asked after the tree\'s answer, and '
                          'answers done with the cause, or unknown to leave the explanation as '
                          'it is.', size))
            continue
        key = r.group or r.signature
        if key in seen:
            continue
        size = sum(1 for o in results if (o.group or o.signature) == key)
        if r.verdict == 'ambiguous':
            found.append((1, r, None, f'The checks of more than one record matched this '
                                      f'job ({"; ".join(r.notes)}). Decide which problem it '
                                      'is, and change the records so that only its '
                                      'record matches.', size))
        elif r.agent and r.group == r.job.id:
            said = (f'the judge ({r.agent.get("judge")}) said {r.agent.get("verdict")} '
                    f'{r.agent.get("problem") or ""}: {r.agent.get("summary", "")}')
            if r.problem:
                found.append((0, r, r.problem.id, f'No record\'s log checks placed this job, '
                              f'but {said}. If the judge is right, change the records so '
                              'that the checks place it.', size))
            elif grows(r):
                asked = f' It asked: "{r.finding.question}"' if r.finding.question else ''
                found.append((2, r, None, f'No record placed this job, and {said}. The tree '
                              f'could not explain it by itself: it stopped at "{grows(r)}".'
                              f'{asked} Teach it to explain failures like this one without a '
                              f'judge: add a node at "{grows(r)}", or change the '
                              'file that has one there, so that the tree explains '
                              'this job conclusively. If a tracker issue describes this '
                              'failure, add a record for it as well.', size))
            else:
                found.append((2, r, None, f'No record placed this job, and {said}. If a '
                              'tracker issue already describes this failure, find it and '
                              'add a record for it, so that the checks place jobs like this '
                              'one. If none does, say so: a person will file one.', size))
        else:
            continue
        seen.add(key)
    found.sort(key=lambda lesson: (lesson[0], -lesson[4]))
    return [lesson[1:] for lesson in found]


def answer_of(directory, analyst):
    """The last answer the analyst gave in a lesson's directory (a doubt's workdir),
    as it printed it; None if it gave none. analyst reads what it printed."""
    outs = sorted(glob.glob(os.path.join(directory, 'doubt-*.out')),
                  key=lambda path: int(re.search(r'doubt-(\d+)', path).group(1)))
    for path in reversed(outs):
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        answer = extract_verdict(analyst.answer_text(text) if analyst else text,
                                 KEYS['doubt'])
        if answer:
            answer['problem'] = problem_id(answer.get('problem'))
            return answer
    return None


def lesson_of(directory, analyst):
    """What the lesson in directory (a doubt's workdir) came to, or None: its
    lesson.json, or for one stopped before it wrote that, what it left."""
    try:
        with open(os.path.join(directory, 'doubt.yaml')) as f:
            doubt = yaml.safe_load(f)
        job, expected = doubt['job'], problem_id(doubt.get('expected'))
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return None
    try:
        with open(os.path.join(directory, LESSON)) as f:
            return dict({'expected': expected}, **json.load(f), workdir=directory)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(directory, 'analysis.md')) as f:
            answered = '\nChecks:' in f.read()
    except OSError:
        return None
    outs = sorted(glob.glob(os.path.join(directory, 'doubt-*.out')),
                  key=lambda path: int(re.search(r'doubt-(\d+)', path).group(1)))
    if not outs:
        return None
    with open(outs[-1]) as f:
        report = report_of(f.read())
    found = {}
    for field, get in (('session', analyst.session), ('cost', analyst.cost)):
        try:
            found[field] = get(report)
        except (AttributeError, KeyError, TypeError, ValueError):
            found[field] = None
    return {'job': job, 'signature': None, 'expected': expected,
            'outcome': 'unsettled' if answered else 'no-answer',
            'summary': '', 'resumed': False, 'session': found['session'],
            'cost': found['cost'], 'session_cost': found['cost'], 'workdir': directory}


def earlier_lessons(directories, analyst):
    """{(job path or signature, the answer it expected): what the latest lesson
    about it came to}, from the doubts' workdirs of earlier runs' lessons
    (<out>/learn/<job>/). A lesson that expects another answer is another lesson."""
    found = {}
    for directory in sorted(directories, key=os.path.getmtime):
        lesson = lesson_of(directory, analyst)
        if lesson:
            for what in (lesson['job'], lesson.get('signature')):
                if what:
                    found[(what, lesson.get('expected'))] = lesson
    return found


def keep_account(results, r, answer, analyst, files=None, workdir=None, held=None):
    """Give r and the jobs that failed the same way the analyst's answer about r's
    job, whose account of the failure the report and the review show in place of
    the judge's: with "analyst", and "job", whose archive its quotes are from; and,
    given files, that job's archive, and workdir, the evidence the analyst read, where
    each quote is in the logs, and the lines that have it. held is where its change
    waits for a person's approval, if it does: the answer passed the checks."""
    if not pack.told(answer):
        return
    kept = dict(answer, analyst=analyst, job=r.job.id, **({'held': held} if held else {}))
    if files is not None and workdir:
        pack.locate(workdir, kept, files)
    for o in results:
        if o is r or (o.group and o.group == r.group) or o.signature == r.signature:
            o.analysis = kept


def learn_from_run(results, files_for, jobs_for, problems_dir, labels_dir, analyst, workdir,
                   tracker, search, corpus, limit, say=print, source=None, nodes_dir=None,
                   history=None, earlier=(), relearn=False, known=None, budget=None,
                   show=None, progress=None, approving=None, holdout=(), at_once=1):
    """Doubt up to limit of a run's lessons, one after the other, so that each
    starts from the tree and the records the ones before it changed; or at_once of
    them at a time (all: every one), each with its own conversation with the
    analyst, and each change checked and kept in turn, against the library as the
    lessons before it left it (see LIBRARY). Returns a line for each.

    For a new problem, the tracker is first searched with the judge's searches,
    and the analyst is told what they found. If they find nothing and the tree
    cannot learn to explain it either, there is nothing to learn yet, and the
    analyst is not asked.

    A group that a lesson of an earlier run (in earlier, its workdirs) was about is
    not taught again, unless relearn: only a lesson whose analyst stopped without
    answering is resumed, once, in its own workdir.

    show, if given, gets each lesson's analysis as it is written: the analyst's
    answer, its changes and the checks'; progress gets the analyst's steps (by
    default, say)."""
    lines, left, asked, todo = [], 0, 0, []
    tried = {} if relearn else earlier_lessons(earlier, analyst)
    # Held while the results are marked with what a lesson found.
    marking = threading.Lock()

    def note(r, summary):
        """Keep what the analyst found with the results of the jobs it is about."""
        for o in results:
            if (o.group and o.group == r.group) or o.signature == r.signature:
                if FOUND + summary not in o.notes:
                    o.notes.append(FOUND + summary)

    def blame(r, pr, summary):
        """Give the jobs a lesson was about the PR under test it found caused them."""
        for o in results:
            if ((o.group and o.group == r.group) or o.signature == r.signature) and \
                    pr not in (b[0] for b in o.blamed):
                o.blamed.append((pr, f'the analyst ({analyst.name})', summary))

    for r, expected, why, size in lessons(results, known):
        jobs = r.job.id + (f' and {size - 1} like it' if size > 1 else '')
        before = tried.get((r.signature, expected)) or tried.get((r.job.path, expected))
        if before and before['outcome'] in ('stopped', 'not-begun'):
            # A person stopped it, or it never began: not taught.
            before = None
        resume = before and before['outcome'] == 'no-answer' and before.get('session') and \
            analyst.resumes and not before.get('resumed') and os.path.isdir(before['workdir'])
        if before and not resume:
            cost = f', for {before["cost"]:.2f}' if before.get('cost') is not None else ''
            line = (f'{jobs}: taught before{cost}: {OUTCOMES[before["outcome"]]}' +
                    (f': {before["summary"]}' if before.get('summary') else '') +
                    f'; see {os.path.join(before["workdir"], "analysis.md")}' +
                    ('' if before['outcome'] == 'learned' else
                     '. Left for a person (--relearn to teach it again)'))
            say(line)
            lines.append(line)
            if before.get('summary'):
                note(r, before['summary'])
            continue
        searches = [str(s) for s in (r.agent or {}).get('searches') or [] if str(s).strip()]
        hits = {}
        if not resume and r.verdict != 'ambiguous' and expected is None and search and \
                searches:
            hits, failed = {}, False
            for words in searches[:3]:
                try:
                    hits.update(search(words))
                except (urllib.error.URLError, OSError, ValueError, KeyError):
                    failed = True
            if not hits and not failed and not (nodes_dir and grows(r)):
                line = (f'{jobs}: no tracker issue found by searching for ' +
                        ', '.join(f'"{w}"' for w in searches[:3]) + '; a person should '
                        'file one')
                say(line)
                lines.append(line)
                continue
            if not hits and not failed:
                why += (' Searching the tracker for ' +
                        ', '.join(f'"{w}"' for w in searches[:3]) + ' found nothing.')
            if hits:
                why += (' Searching the tracker for ' +
                        ', '.join(f'"{w}"' for w in searches[:3]) + ' found: ' +
                        '; '.join(f'#{n} {title}' for n, title in list(hits.items())[:10]) +
                        '. ' + ('It is' if len(hits) == 1 else
                                f'The first {min(len(hits), FOUND_ISSUES)} are') +
                        ' in tracker-<number>.txt.')
        if asked == limit:
            left += 1
            continue
        asked += 1
        # Its line, once it is taught, goes where it was found.
        todo.append((len(lines), r, expected, why, jobs, before, resume, hits))
        lines.append(None)
    at_once = len(todo) if at_once == 'all' else at_once or 1
    # Set when the analyst gave nothing at all, as one out of credit does: the
    # lessons not begun yet are left as not taught.
    gave_nothing = threading.Event()

    def teach(at, r, expected, why, jobs, before, resume, hits):
        if gave_nothing.is_set():
            return
        heard = []

        def tagged(text):
            # Of lessons at once, each line says whose it is.
            return text if at_once == 1 else '\n'.join(
                f'{r.job.id}: {line}' for line in str(text).split('\n'))

        def hear(line):
            heard.append(line)
            if show:
                show(tagged(line))

        say(f'{jobs}: asking {analyst.name}; a lesson usually takes 2 to 10 minutes, '
            'sometimes half an hour')
        job = r.job
        if resume and before['job'] != r.job.path:
            # The session was about another job that failed the same way.
            run, _, jid = before['job'].rpartition('/')
            job = jobs_for(run).get(jid) or r.job
        right = [(o.job, (known or {})[o.job.id]) for o in results if o.job.id in (known or {})]
        doubt = Doubt(job, files_for(job), problems_dir, labels_dir, analyst,
                      before['workdir'] if resume else os.path.join(workdir, r.job.id),
                      files_for, jobs_for, tracker, expected=expected, why=why,
                      # What the reviewers answered stands as theirs; an agent's only
                      # until people say otherwise.
                      by=(f'the QA reviewers, via qa_triage ({analyst.name})' if expected
                          else f'qa_triage ({analyst.name})'),
                      recorded=recorded(r), corpus=corpus,
                      search=search, label_none=False, say=hear, source=source,
                      nodes_dir=nodes_dir, history=history,
                      want_sure=bool(expected or (nodes_dir and grows(r))),
                      resume=before['session'] if resume else None,
                      resumed_cost=(before.get('session_cost') or 0) if resume else 0,
                      budget=budget, known=right,
                      progress=lambda line: (progress or say)(tagged(line)),
                      said_by_people=True, holdout=holdout,
                      issues=list(hits)[:FOUND_ISSUES], **(approving or {}))
        refused = False
        try:
            settled = doubt.run()
        except Stopped:
            # Left as if not taught, for a person to ask again.
            with open(os.path.join(doubt.workdir, LESSON), 'w') as f:
                json.dump({'job': job.path, 'signature': r.signature, 'expected': expected,
                           'outcome': 'stopped', 'analyst': analyst.name,
                           'cost': doubt.cost()}, f, indent=1)
            raise
        except JudgeError as e:
            if not (e.session or e.refused or doubt.costs):
                # It gave nothing at all, as an agent out of credit does: nor will it
                # for the lessons after this one, which are left as not taught.
                with open(os.path.join(doubt.workdir, LESSON), 'w') as f:
                    json.dump({'job': job.path, 'signature': r.signature,
                               'expected': expected, 'outcome': 'not-begun',
                               'analyst': analyst.name}, f, indent=1)
                lines[at] = f'{jobs}: {NOT_BEGUN}{e}'
                say(lines[at])
                gave_nothing.set()
                return
            settled, heard, refused = False, [f'not settled: {e}'], e.refused
        outcome = 'learned' if doubt.written else 'nothing' if settled else \
            'unsettled' if doubt.answer else 'refused' if refused else 'no-answer'
        pr = (doubt.answer or {}).get('pr') if settled else None
        with marking:
            if (doubt.answer or {}).get('summary'):
                note(r, doubt.answer['summary'])
            if settled or doubt.held:
                keep_account(results, r, doubt.answer, analyst.name, doubt.files,
                             doubt.workdir, None if settled else doubt.held)
            if pr:
                blame(r, pr, doubt.answer.get('summary', ''))
        sessions = [s for s, _ in doubt.costs if s]
        with open(os.path.join(doubt.workdir, LESSON), 'w') as f:
            json.dump({'job': job.path, 'signature': r.signature, 'expected': expected,
                       'outcome': outcome,
                       'summary': (doubt.answer or {}).get('summary', ''),
                       'written': doubt.written, 'analyst': analyst.name,
                       'resumed': bool(resume), 'session': sessions[-1] if sessions else None,
                       'cost': doubt.cost(),
                       'session_cost': (doubt.cost() or 0) + doubt.resumed_cost
                       if analyst.cumulative_cost else None}, f, indent=1)
        if doubt.written:
            outcome = f'learned {doubt.answer["problem"] or "a new problem"}, ' \
                      f'wrote {", ".join(doubt.written)}'
        else:
            outcome = 'no record should place it' if settled else \
                heard[-1].strip().splitlines()[-1] if heard else 'no answer'
            if doubt.answer and doubt.answer.get('summary'):
                outcome += f': {doubt.answer["summary"]}'
        if pr:
            outcome += f'; {pr}, under test, caused it'
        cost = doubt.cost()
        lines[at] = f'{jobs}: {outcome}' + (f' (cost {cost:.2f})' if cost is not None
                                             else '') + \
            f'; see {os.path.join(doubt.workdir, "analysis.md")}'
        say(lines[at])

    if at_once == 1:
        for item in todo:
            teach(*item)
    elif todo:
        say(f'{len(todo)} lessons, {min(at_once, len(todo))} at once; each line of one '
            'begins with its job')
        with concurrent.futures.ThreadPoolExecutor(at_once) as pool:
            done = [pool.submit(teach, *item) for item in todo]
        # Stopped, as a person stops them all; else what went wrong first.
        failed = [f.exception() for f in done if f.exception()]
        if failed:
            raise next((e for e in failed if isinstance(e, Stopped)), failed[0])
    lines = [line for line in lines if line is not None]
    if left:
        lines.append(f'{left} more groups left for later runs (--learn-max)')
    return lines
