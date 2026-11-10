"""Whether a job's test fails on the branch the PRs under test were made against:
in the runs of that branch's own suite, near the run's date.

A failure the base branch has too, the same way, is not the doing of the PRs under
test, whatever it is; one it does not have may be. The base branch's runs are
found on pulpito, by the run's suite and the base its testing branch names, and a
job's test is told by its suite's path and the facets that name tests, since the
others are chosen at random for each run.
"""

import datetime
import json
import os
import re
import urllib.parse

from .fingerprint import template
from .sources import PULPITO

# A run's name: <who>-<date>_<time>-<suite>-<branch>-<kernel>-<flavor>-<machine>.
RUN = re.compile(r'^.+?-(\d{4}-\d\d-\d\d)_\d\d:\d\d:\d\d-([\w:]+?)-(.+)-[\w]+-[\w]+-(\w+)$')
BASES = ('main', 'umbrella', 'tentacle', 'squid', 'reef')
# The facets that say what a job tests, and not how.
TEST = re.compile(r'\b(?:all|tasks?|workloads?|workunits?|test|tests|thrashers?)/[\w.$-]+')
# The base branch's runs looked at, and how many days either side of the run's.
RUNS, DAYS = 6, 21


def parts(run):
    """(date, suite, branch, machine) of a run's name, or None."""
    m = RUN.match(run)
    if not m:
        return None
    date, suite, rest, machine = m.groups()
    return datetime.date.fromisoformat(date), suite, rest, machine


def base_of(branch):
    """The branch a testing branch was made from: the one its name ends with, or
    main; a branch that is a base is its own."""
    for base in BASES:
        if branch == base or branch.endswith(f'-{base}') or f'-{base}-' in branch:
            return base
    return 'main'


def test_of(description):
    """What a job tests, from its description: its suite's path and the facets that
    name tests, or the whole description if none does."""
    named = sorted(set(TEST.findall(description)))
    if not named:
        return description
    return description.split('{', 1)[0] + ' ' + ' '.join(named)


def runs(run, list_runs):
    """The base branch's runs of run's suite, on the same machines, newest first,
    within DAYS of run's date: at most RUNS of them."""
    found = parts(run)
    if not found:
        return []
    date, suite, branch, machine = found
    base = base_of(branch)
    listed = list_runs(f'{PULPITO}/?branch={urllib.parse.quote(base)}'
                       f'&suite={urllib.parse.quote(suite)}')
    near = []
    for other in listed:
        theirs = parts(other)
        if other != run and theirs and theirs[3] == machine and \
                abs((theirs[0] - date).days) <= DAYS:
            near.append(other)
    return near[:RUNS]


def failures(runs_, list_jobs, cache=None):
    """{test: [(run, job id, failure reason)]} of the failed and dead jobs of runs;
    a run's jobs are kept in cache, a directory, as a finished run's do not
    change."""
    found = {}
    for run in runs_:
        path = cache and os.path.join(cache, run.replace(':', '_') + '.json')
        if path and os.path.exists(path):
            with open(path) as f:
                jobs = json.load(f)
        else:
            try:
                jobs = [[j.id, j.status, j.description, j.reason] for j in list_jobs(run)]
            except (OSError, ValueError):
                continue
            if path:
                os.makedirs(cache, exist_ok=True)
                with open(path, 'w') as f:
                    json.dump(jobs, f)
        for jid, status, description, reason in jobs:
            if status in ('fail', 'dead'):
                found.setdefault(test_of(description), []).append((run, jid, reason))
    return found


def compare(r, failed, base):
    """Note on r the base branch's jobs that ran its job's test and failed: the same
    way, if their failure reasons match its; r.baseline is [(run, job, same)]."""
    mine = template(r.job.reason or '')
    r.baseline = [(run, jid, template(reason or '') == mine)
                  for run, jid, reason in failed.get(test_of(r.job.description), [])]
    same = [f'{run}/{jid}' for run, jid, alike in r.baseline if alike]
    if same:
        r.notes.append(f'{base} failed this test the same way, so the PRs under test did '
                       f'not cause it: {same[0]}' +
                       (f' and {len(same) - 1} more' if len(same) > 1 else ''))
    elif r.baseline:
        run, jid, _ = r.baseline[0]
        r.notes.append(f'{base} failed this test too, another way: {run}/{jid}')
