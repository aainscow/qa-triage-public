"""qa_triage answers: the answers people gave for the failed jobs of the runs they
reviewed, for qa_triage run --known to learn from.

The RADOS team's wiki of reviewed runs
(https://tracker.ceph.com/projects/rados/wiki/MAIN) has a section for each run,
headed by its QA tracker, which lists the run's failed jobs with the tracker issue
each was placed in. The QA tracker names the branch the run tested, pulpito the
branch's runs, and their jobs which run each job is in.

The tracker itself has more: people link the jobs a problem failed, in its issue's
description and comments, as examples of it. With --tracker, the issues of the
projects named that were updated since a date are read, and each job they link to
in a run whose archive is kept is placed in the issue, or the one it duplicates or
backports. An issue a bot files about a test machine that failed its last jobs is
left out: it lists the jobs that ran there, not why each failed.

A run's answers are written to <directory>/<run>.yaml, as job id: issue number.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

import yaml

from .sources import PULPITO, TIMEOUT, TRACKER

SECTION = re.compile(r'h3\.\s*https://tracker\.ceph\.com/issues/(\d+)')
ISSUE = re.compile(r'https://tracker\.ceph\.com/issues/(\d+)')
# Job ids, as the wiki gives them: ['631086', '631262'], or 597567, 597303.
JOB = re.compile(r'\b(\d{6,7})\b')
# A job, as a tracker issue links to it: /a/<run>/<job>, or its pulpito or
# qa-proxy URL.
REF = re.compile(r'(?:/a/|pulpito\.ceph\.com/|/teuthology/)([\w.:%-]+-\w+)/(\d{4,8})\b')
PASSED = re.compile(r'\bpass(?:ed|es)?\b|\bgreen\b|\bsucceed', re.I)
# An issue a bot files about a test machine, which lists the last jobs that ran on
# it, whatever failed them: its jobs are not people's answers.
MACHINE = re.compile(r'^\S+: last \d+ (?:consecutive )?jobs all failed or died\b')
# The pause between reading one issue and the next, to spare the tracker.
PAUSE = 0.3


def kept_here(run):
    """True if run's archive is kept where the tool reads it: the trial lab's."""
    return run.endswith('-trial')


def wiki_text(key, project='rados', page='MAIN', url=TRACKER):
    """A tracker wiki page's text, which only a signed-in user may read."""
    req = urllib.request.Request(f'{url}/projects/{project}/wiki/{page}.json',
                                 headers={'X-Redmine-API-Key': key, 'User-Agent': 'qa_triage'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)['wiki_page']['text']


def parse(text):
    """{QA tracker number: {job id: issue number}}: in each section, the jobs named
    on a line before its first tracker link are placed in that issue."""
    sections, qa = {}, None
    for line in text.splitlines():
        heading = SECTION.match(line.strip())
        if heading:
            qa = heading.group(1)
            continue
        first = ISSUE.search(line)
        if qa and first:
            for job in JOB.findall(line[:first.start()]):
                sections.setdefault(qa, {}).setdefault(job, first.group(1))
    return sections


def branches(issue):
    """The branches a QA tracker issue's "QA Runs" field names."""
    fields = {c['name']: c.get('value') or '' for c in issue.get('custom_fields', [])}
    return str(fields.get('QA Runs', '')).split()


def gather(sections, tracker, list_runs, list_jobs, say=print):
    """{run: {job id: issue number}} for the sections' jobs, found in the runs of
    each QA tracker's branches; jobs not found in them are said, and left out."""
    found = {}
    for qa, answers in sections.items():
        left = dict(answers)
        try:
            names = branches(tracker(qa))
        except (OSError, ValueError, KeyError) as e:
            say(f'QA tracker {qa}: cannot read it ({e})')
            continue
        for branch in names:
            try:
                runs = list_runs(f'{PULPITO}/?branch={urllib.parse.quote(branch)}')
            except (OSError, ValueError) as e:
                say(f'QA tracker {qa}: cannot list the runs of {branch} ({e})')
                continue
            for run in runs:
                if not left:
                    break
                try:
                    ids = {job.id for job in list_jobs(run)}
                except (OSError, ValueError):
                    continue
                mine = {job: issue for job, issue in left.items() if job in ids}
                if mine:
                    found.setdefault(run, {}).update(mine)
                    for job in mine:
                        del left[job]
        say(f'QA tracker {qa}: {len(answers) - len(left)} of {len(answers)} jobs found in '
            f'the runs of {", ".join(names) or "no branch"}')
    return found


def sightings(issue):
    """{(run, job id)} of the jobs an issue's description and comments link to, as
    examples of it: a line that says the job passed is left out."""
    found = set()
    for text in [issue.get('description') or ''] + [j.get('notes') or ''
                                                    for j in issue.get('journals', [])]:
        for line in text.splitlines():
            if PASSED.search(line):
                continue
            found |= {(urllib.parse.unquote(run), job) for run, job in REF.findall(line)}
    return found


def harvest(issues, tracker, canonical, say=print, cache=None, archived=kept_here):
    """{run: {job id: issue number}} of the jobs the issues link to, in the runs whose
    archive is kept (archived(run)), each placed in the issue it is an example of,
    or the one that issue duplicates or backports. issues are the listed issues, with
    updated_on; tracker(number) gives one with its comments, kept in cache, a
    directory, until it is updated. A job two issues claim is left out, and said."""
    found, claims = {}, {}
    for n, listed in enumerate(issues, 1):
        number = str(listed['id'])
        issue = _kept(cache, number, listed.get('updated_on'))
        if issue is None:
            try:
                issue = tracker(number)
            except (OSError, ValueError, KeyError) as e:
                say(f'tracker {number}: cannot read it ({e})')
                continue
            _keep(cache, number, issue)
            time.sleep(PAUSE)
        if MACHINE.match(issue.get('subject') or listed.get('subject') or ''):
            continue
        jobs = {(run, job) for run, job in sightings(issue) if archived(run)}
        if jobs:
            first = canonical(number)
            for run, job in jobs:
                claims.setdefault((run, job), set()).add(first)
        if n % 100 == 0:
            say(f'{n} issues read, {len(claims)} jobs found')
    for (run, job), numbers in sorted(claims.items()):
        if len(numbers) == 1:
            found.setdefault(run, {})[job] = numbers.pop()
        else:
            say(f'{run}/{job}: claimed by {", ".join(sorted(numbers))}; left out')
    return found


def _kept(cache, number, updated):
    """The issue kept in cache, if it was kept as it was when updated."""
    path = cache and os.path.join(cache, f'{number}.json')
    if not (path and updated and os.path.exists(path)):
        return None
    with open(path) as f:
        issue = json.load(f)
    return issue if issue.get('updated_on') == updated else None


def _keep(cache, number, issue):
    if cache:
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, f'{number}.json'), 'w') as f:
            json.dump(issue, f)


def write(found, directory, source='the RADOS wiki'):
    """Write each run's answers to <directory>/<run>.yaml, and index.yaml, which
    names each run's file; the files written."""
    os.makedirs(directory, exist_ok=True)
    written, index = [], {}
    for run, answers in sorted(found.items()):
        path = os.path.join(directory, run.replace(':', '_') + '.yaml')
        with open(path, 'w') as f:
            f.write(f'# The answers {source} gives for {run}: job id: tracker issue.\n')
            yaml.safe_dump({int(job): int(issue) for job, issue in sorted(answers.items())}, f,
                           sort_keys=True)
        written.append(path)
        index[run] = os.path.basename(path)
    with open(os.path.join(directory, 'index.yaml'), 'w') as f:
        yaml.safe_dump(index, f, sort_keys=True, width=1000)
    return written
