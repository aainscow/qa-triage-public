"""Watching several runs at once: the failures they have in common, the commonest
first, for whoever watches a suite's QA runs and its base branch's runs over a week.

Each run is triaged as qa_triage run triages it, with the tree and the records
alone, no AI. Then the jobs of every run that failed the same way are put together:
those placed in a known problem by the problem, and the others by what the tree
found. A failure seen in more runs comes first, then one with more jobs; the test
infrastructure's failures are counted apart. With no runs given, the runs are the
suite's runs of the base branch, and of the QA batches for it, scheduled between two
dates, by default the last week: those of the whole suite, not a developer's few
jobs, and begun, not still waiting for machines.
"""

import concurrent.futures
import datetime
import json
import os
import re
import urllib.parse

from . import baseline, report
from .sources import PULPITO, QA_PROJECT, QA_PROXY, issue_runs, pr_numbers

# When a run was scheduled, as its name says.
WHEN = re.compile(r'-(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})-')
# The days looked back over, the most runs taken, and the most failures listed.
DAYS, MOST, TOP = 7, 10, 10
# The fewest jobs of a run of the whole suite: a developer's run of a few of its jobs,
# on the base branch, has fewer, and would count its failures many times.
LEAST = 100
# The runs a page of pulpito lists, and the most pages read for one branch.
PAGE, PAGES = 25, 20
# A job that has not begun: its run is not ready to watch.
WAITING = ('queued', 'waiting')
# The jobs of a failure the summary names, and the report.
NAMED = 6


# Where the runs watched are kept, in the cache: those the person gave, those found
# between two dates, when they were found, and the dates: since and until, as
# YYYY-MM-DD, or None for days before until, and for now.
STATE = os.path.join('watch', 'runs.json')


def kept(cache):
    """The runs watched before: {given: [runs], found: [runs], found_at: when, or
    None}."""
    try:
        with open(os.path.join(cache, STATE)) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    return {'given': list(state.get('given') or []), 'found': list(state.get('found') or []),
            'found_at': state.get('found_at'), 'since': state.get('since'),
            'until': state.get('until'), 'days': state.get('days'),
            # The PRs whose runs are found, if those are: mine, or their numbers;
            # the teuthology users whose runs are a person's own; and {run: {PR
            # number: [how the run is known to test it]}}.
            'prs': state.get('prs'), 'users': list(state.get('users') or []),
            'tests': dict(state.get('tests') or {}),
            'titles': dict(state.get('titles') or {})}


def keep(cache, state):
    path = os.path.join(cache, STATE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.new', 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(path + '.new', path)


def watched(state):
    """The runs a state watches, the newest first."""
    return sorted(dict.fromkeys(state['given'] + state['found']), key=when, reverse=True)


def short_name(run):
    """A run in a few words, for watch's pane: its date, then the QA batch's name, as
    bharath9 for wip-bharath9-testing-..., or else its branch and who ran it."""
    found = baseline.parts(run)
    if not found:
        return run[:40]
    date, _, branch, _ = found
    batch = re.match(r'wip-([\w.]+?)-testing', branch)
    return f'{date:%m-%d} ' + (batch.group(1) if batch else
                               f'{branch[:24]} {run.split("-", 1)[0]}')


def when(run):
    """When run was scheduled, from its name."""
    m = WHEN.search(run)
    return datetime.datetime.strptime(m.group(1), '%Y-%m-%d_%H:%M:%S') if m else \
        datetime.datetime.min


def branch_of(run):
    found = baseline.parts(run)
    return found[2] if found else ''


def suite_of(run):
    found = baseline.parts(run)
    return found[1] if found else ''


def day(text, today=None):
    """The date text names: YYYY-MM-DD, or MM-DD of this year; None for none."""
    text = (text or '').strip()
    if not text:
        return None
    today = today or datetime.date.today()
    try:
        return datetime.date.fromisoformat(text if text.count('-') == 2 else
                                           f'{today.year}-{text}')
    except ValueError:
        raise ValueError(f'{text} is not a date: give it as 2026-10-29, or 10-29') from None


def window(since=None, until=None, days=DAYS, now=None):
    """(from, to) of the runs watched: from the start of since, a date, or else days
    before to; to the end of until, a date, or else now."""
    now = now or datetime.datetime.now()
    end = datetime.datetime.combine(until, datetime.time.max) if until else now
    start = datetime.datetime.combine(since, datetime.time.min) if since else \
        end - datetime.timedelta(days=days)
    if start > end:
        raise ValueError(f'{start:%Y-%m-%d} is after {end:%Y-%m-%d}')
    return start, end


def span(state, now=None):
    """The window of the runs a state watches, as window gives it."""
    return window(day(state.get('since')), day(state.get('until')),
                  state.get('days') or DAYS, now)


def said(state):
    """The window of the runs a state watches, in words, and the PRs whose runs they
    are, if they are."""
    since, until = day(state.get('since')), day(state.get('until'))
    prs = following(state)
    prs = f'{prs}, ' if prs else ''
    if since:
        return f'{prs}{since:%m-%d} to {f"{until:%m-%d}" if until else "now"}'
    days = state.get('days') or DAYS
    return f'{prs}the {days} days to {f"{until:%m-%d}" if until else "now"}'


def following(state):
    """The PRs whose runs a state watches, in words; '' if it watches a suite's."""
    prs = state.get('prs')
    if not prs:
        return ''
    if prs == 'mine':
        return 'your PRs'
    return f'PR{"" if len(prs) == 1 else "s"} ' + ', '.join(f'#{n}' for n in prs)


def recent(list_runs, issues, jobs_of, start, end, base='main', suite='rados', most=MOST,
           least=LEAST):
    """(the runs a watcher of base looks at, the newest first, at most most; [(run,
    why it is left out)]): the suite's runs of base, and of the branches of the QA
    batches for base whose QA tracker was updated since start, scheduled from start to
    end, of at least least jobs, all begun. list_runs(url) gives the runs a pulpito
    page lists, issues(query) the tracker issues a query finds, and jobs_of(run) a
    run's jobs."""
    branches = [base]
    # A QA tracker is updated as its batch's runs are: one updated since start may
    # have runs in the window, though it was updated again after.
    for issue in issues(f'project_id={QA_PROJECT}&status_id=*'
                        f'&updated_on=%3E%3D{start:%Y-%m-%d}'):
        named, _ = issue_runs(issue)
        branches += [b for b in named if b != base and baseline.base_of(b) == base]

    def runs_of(branch):
        # pulpito lists so many runs a page, of the dates asked for.
        found, page = [], 1
        while page <= PAGES:
            listed = list_runs(f'{PULPITO}/?branch={urllib.parse.quote(branch)}'
                               f'&suite={urllib.parse.quote(suite)}&date={start:%Y-%m-%d}'
                               f'&to_date={end:%Y-%m-%d}' + (f'&page={page}' if page > 1 else ''))
            found += [run for run in listed if run not in found]
            if len(listed) < PAGE:
                return found
            page += 1
        return found

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        listed = list(pool.map(runs_of, dict.fromkeys(branches)))
    found = sorted({run for runs in listed for run in runs
                    if start <= when(run) <= end and suite_of(run) == suite}, key=when,
                   reverse=True)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        jobs = dict(zip(found, pool.map(jobs_of, found)))
    taken, left = [], []
    for run in found:
        waiting = sum(j.status in WAITING for j in jobs[run])
        if waiting:
            left.append((run, f'{waiting} of its {len(jobs[run])} jobs '
                                f'{"has" if waiting == 1 else "have"} not begun'))
        elif len(jobs[run]) < least:
            left.append((run, f'it has {len(jobs[run])} jobs, a part of the suite'))
        elif len(taken) < most:
            taken.append(run)
    return taken, left


# The fewest letters of a PR's branch name, without wip-, that a run's branch must
# have in it to be looked at as one of its; and the most builds looked into, the
# newest first.
STEM, BUILDS = 8, 40


def pr_runs(prs, builds, issues, read, start, end, users=(), most=MOST):
    """(the runs of prs, the newest first, at most most; [(run, why it is left out)];
    {run: {PR number: [how it is known to test it]}}), of those scheduled from start
    to end:

    - the runs of the QA batches whose QA tracker, updated since start, lists them;
    - the runs of a PR's own build, its head as it is;
    - and the runs of users, a person's own, and those of a branch named as a PR's is,
      each as its build shows it to test them: read(sha1s) gives {sha1: ({PR number:
      [how]}, [(author's email, subject)] of its commits)}, or None for a build it
      could not read; a commit of the same author and subject as one of a PR's
      counts too, as one rebased does.

    prs is {number: {'head': its commit, 'ref': its branch's name, 'commits':
    [(commit, email, subject)]}}; builds the runs pulpito lists from start to end, as
    sources.list_builds gives them; issues(query) the tracker issues a query finds."""
    rows = [row for row in builds if start <= when(row['run']) <= end]
    tests = {}

    def note(run, number, how):
        known = tests.setdefault(run, {}).setdefault(number, [])
        if how not in known:
            known.append(how)

    for issue in issues(f'project_id={QA_PROJECT}&status_id=*'
                        f'&updated_on=%3E%3D{start:%Y-%m-%d}'):
        listed = sorted(pr_numbers(issue) & set(prs), key=int)
        branches = set(issue_runs(issue)[0]) if listed else set()
        for row in rows:
            if row['branch'] in branches:
                for number in listed:
                    note(row['run'], number, f'its QA tracker, #{issue["id"]}, lists it')
    heads = {pr['head']: number for number, pr in prs.items()}
    stems = {number: pr['ref'][4:] if pr['ref'].startswith('wip-') else pr['ref']
             for number, pr in prs.items()}
    stems = {number: stem for number, stem in stems.items() if len(stem) >= STEM}
    named = {}
    for row in rows:
        if row['sha1'] in heads:
            note(row['run'], heads[row['sha1']], 'it is a build of the PR\'s head')
        for number, stem in stems.items():
            if stem in row['branch']:
                named.setdefault(row['run'], []).append(number)
    own = [row for row in rows if row['user'] in users or row['run'] in named]
    wanted = list(dict.fromkeys(row['sha1'] for row in sorted(own, key=lambda row: when(
        row['run']), reverse=True)))
    found = read(wanted[:BUILDS]) if wanted else {}
    titles = {number: {(email, subject) for _, email, subject in pr['commits']}
              for number, pr in prs.items()}
    left = []
    for row in own:
        if found.get(row['sha1']) is None:
            if row['sha1'] in wanted[BUILDS:]:
                left.append((row['run'], f'its build is not one of the newest {BUILDS} '
                                         'looked into'))
            elif row['sha1'] in found:
                left.append((row['run'], 'its build could not be read'))
            for number in named.get(row['run'], []):
                note(row['run'], number, 'its branch is named as the PR\'s is')
            continue
        named_prs, commits = found[row['sha1']]
        for number in prs:
            for how in named_prs.get(number, []):
                note(row['run'], number, how)
            same = len(titles[number] & set(map(tuple, commits)))
            if same and number not in named_prs:
                note(row['run'], number, f'{same} of its {len(titles[number])} commits are '
                                         'in the build, by their titles')
        for number in named.get(row['run'], []):
            if number not in tests.get(row['run'], {}):
                note(row['run'], number, 'its branch is named as the PR\'s is')
    waiting = {row['run']: row['queued'] + row['waiting'] for row in rows}
    taken = []
    for run in sorted(tests, key=when, reverse=True):
        if waiting.get(run):
            left.append((run, f'{waiting[run]} of its jobs '
                              f'{"has" if waiting[run] == 1 else "have"} not begun'))
        elif len(taken) < most:
            taken.append(run)
        else:
            left.append((run, f'only the newest {most} runs are taken'))
    return taken, left, {run: tests[run] for run in taken}


def pr_lines(tests, titles=None):
    """Lines for each PR, of the runs that test it, the newest first, and how each is
    known to test it; titles is {number: title}."""
    by_pr = {}
    for run in sorted(tests, key=when, reverse=True):
        for number, how in tests[run].items():
            by_pr.setdefault(number, []).append((run, how))
    lines = []
    for number in sorted(by_pr, key=int):
        runs = by_pr[number]
        title = (titles or {}).get(number)
        lines.append(f'#{number}' + (f' {title}' if title else '') +
                     f': {len(runs)} run{"" if len(runs) == 1 else "s"}')
        for run, how in runs:
            lines.append(f'  {short_name(run)}: {"; ".join(how)}   ({run})')
    return lines


class Failure:
    """The jobs of several runs that failed the same way."""

    def __init__(self, key):
        self.key, self.results = key, []

    @property
    def first(self):
        return self.results[0]

    @property
    def runs(self):
        """The runs it failed in, the newest first."""
        return sorted({r.job.run for r in self.results}, key=when, reverse=True)

    @property
    def problem(self):
        return self.key[1] if self.key[0] == 'problem' else None

    @property
    def kind(self):
        """seen-after-fix, known, new (the tree explains it, and it needs a tracker
        filed) or unexplained (for a person to look into)."""
        if any(r.verdict == 'seen-after-fix' for r in self.results):
            return 'seen-after-fix'
        if self.problem:
            return 'known'
        if all(r.verdict == 'new' and r.sure and not r.agent for r in self.results):
            return 'new'
        return 'unexplained'

    @property
    def infra(self):
        """True if the test infrastructure failed, not the test or the product."""
        r = self.first
        if self.problem:
            return r.problem.kind == 'infra'
        return bool(r.finding and r.finding.path[:1] == ['infra'])

    @property
    def label(self):
        return self.first.problem.label if self.problem else self.kind

    @property
    def title(self):
        r = self.first
        return r.problem.title if self.problem else report.what(r)

    def on(self, branch):
        """How many of its runs are of branch."""
        return sum(branch_of(run) == branch for run in self.runs)


def key_of(r):
    """What puts r with the results of other runs that failed the same way: its known
    problem, if a record or the judge placed it in one; else what the tree found."""
    if r.problem and r.verdict in ('duplicate', 'probable-duplicate', 'seen-after-fix'):
        return ('problem', r.problem.id)
    return ('failure', r.signature)


def combine(results):
    """[Failure] of results, of any runs, the commonest first: those of more runs,
    then of more jobs."""
    found = {}
    for r in results:
        key = key_of(r)
        found.setdefault(key, Failure(key)).results.append(r)
    return sorted(found.values(), key=lambda f: (-len(f.runs), -len(f.results), f.label))


KINDS = {'seen-after-fix': 'a known problem seen again after its fix',
         'known': 'a known problem',
         'new': 'new: the tree explains it, and no record has it, so it needs a tracker '
                'filed',
         'unexplained': 'for a person to look into: neither the tree nor the records '
                        'decided it'}


def _runs(n):
    return f'{n} run{"" if n == 1 else "s"}'


def _jobs(n):
    return f'{n} job{"" if n == 1 else "s"}'


def where(f, base):
    """Of which runs a failure is, in words."""
    on = f.on(base)
    return (f'on {base} too ({on} of its runs)' if on and on < len(f.runs) else
            f'only on {base}' if on else f'only in QA batches')


def summary(failures, runs, base, top=TOP):
    """The commonest failures of runs, for the console."""
    listed = [f for f in failures if not f.infra]
    lines = [f'The commonest failures of {_runs(len(runs))}, of '
             f'{_jobs(sum(len(f.results) for f in failures))} that failed or died:', '']
    for n, f in enumerate(listed[:top], 1):
        lines += [f'{n:>3}. {f.label:<14} {_runs(len(f.runs)):>7}, {_jobs(len(f.results)):>8}  '
                  f'{report.short(f.title, 100)}',
                  f'{"":19}{KINDS[f.kind]}; {where(f, base)}']
    if len(listed) > top:
        lines.append(f'     ... and {len(listed) - top} more, in the report')
    infra = [f for f in failures if f.infra]
    if infra:
        lines += ['', f'The test infrastructure failed {_jobs(sum(len(f.results) for f in infra))}'
                      f' in {_runs(len({run for f in infra for run in f.runs}))}, not listed: ' +
                  ', '.join(f'{report.short(f.title, 60)} ({len(f.results)})' for f in infra[:3]) +
                  (', ...' if len(infra) > 3 else '')]
    return lines


def _job_links(f, run):
    ids = [r.job.id for r in f.results if r.job.run == run]
    shown = ', '.join(f'[{jid}]({PULPITO}/{run}/{jid}/)' for jid in ids[:NAMED])
    return shown + (f' and {len(ids) - NAMED} more' if len(ids) > NAMED else '')


def markdown(failures, runs, base, counts):
    """The report of the runs: the runs, then every failure, the commonest first, each
    with its runs and jobs. counts is {run: how many of its jobs failed or died}."""
    lines = [f'# The commonest failures of {_runs(len(runs))}', '',
             'Triaged by the tree and the records, with no AI.', '',
             '| Run | Failed or dead |', '|---|---|']
    lines += [f'| [{run}]({PULPITO}/{run}/) | {counts.get(run, 0)} |' for run in runs]
    lines += ['', '| # | Failure | Runs | Jobs | What |', '|---|---|---|---|---|']
    numbered = [f for f in failures if not f.infra] + [f for f in failures if f.infra]
    for n, f in enumerate(numbered, 1):
        title = report.short(f.title, 120).replace('|', '\\|')
        lines.append(f'| {n} | {f.label}{" (infra)" if f.infra else ""} | {len(f.runs)} | '
                     f'{len(f.results)} | {title} |')
    for n, f in enumerate(numbered, 1):
        r = f.first
        heading = f'{f.label}: {f.title}' if f.problem else f.label
        lines += ['', f'## {n}. {report.short(heading, 160)}', '',
                  f'{KINDS[f.kind][0].upper()}{KINDS[f.kind][1:]}; {where(f, base)}.' +
                  (f' Tracker: {r.problem.tracker}' if f.problem else ''), '']
        if not f.problem:
            lines += [report.short(report.what(r), 600), '']
        lines += [f'- {run}: {_job_links(f, run)}' for run in f.runs]
        lines += ['', f'Its logs: {QA_PROXY}/{r.job.run}/{r.job.id}/']
    return '\n'.join(lines) + '\n'
