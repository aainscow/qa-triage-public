"""qa_triage: find the known problems among a teuthology run's failures.

qa_triage <a run's pulpito URL> reviews the run on a full screen: the tree and the
records triage its failed and dead jobs while it opens, then the judge and the
analyst are asked only when you say. The output is kept in the cache."""

import argparse
import concurrent.futures
import contextlib
import datetime
import getpass
import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
from collections import defaultdict
from types import SimpleNamespace

import yaml

from . import (answers, approve, baseline, config, db, editor, hosts, mcp, memory, report,
               score, setup, share, tui, watch)
from .agents import (PROVIDERS, SPECS, JudgeError, each_at_once, make_agent, make_judge,
                     units)
from .checkout import (CheckoutError, added, build_prs, check_out, fetch_all, versions,
                       worktree)
from .db import under
from .graph import FOUND, Triage, describe, fits, misses, placed
from .learn import (Doubt, canonical, history_of, learn_from_run, load_corpus,
                    load_history, load_labels, load_paths, original, paths_dir, problem_id,
                    pull_numbers, save_label, save_paths, settled_by_people,
                    unconfirmed_labels)
from .review import Review
from .sources import (PULPITO, QA_PROXY, SSH_AT_ONCE, TRACKER, UNREACHED, ArchiveError, Job,
                      authored_prs, check_archive, find_qa_tracker, github_login, https_base,
                      issue_number, issue_runs, job_files, job_name, linked_prs, list_builds,
                      list_jobs, list_runs, parse_archive, pr_commits, pr_files, pr_numbers,
                      pr_of_branch, pr_title, pull_request, reach, run_name, tracker_issue,
                      tracker_issues, tracker_key, tracker_search, use_tracker_key,
                      github_token, use_github_key)
from .tree import NODES, explain, learned
from .tree.common import short


def files_of(args, job):
    return job_files(job, args.archive, args.cache)


def source_of(args):
    """What gives the analyst the source a job ran, from a worktree of the clone."""
    return lambda files, say: check_out(files, args.git, args.worktrees, say)


def search_originals(words, search=tracker_search, tracker=tracker_issue):
    """search's [(number, title)], each backport or duplicate given as the issue it
    is of, since that is the problem a job is placed in."""
    found = {}
    for number, title in search(words):
        first = canonical(number, tracker)
        if first != number:
            try:
                issue = tracker(first)
                title = (f'{issue["tracker"]["name"]} #{first} ({issue["status"]["name"]}): '
                         f'{issue["subject"]}')
            except (urllib.error.URLError, OSError, KeyError, ValueError):
                first = number
        found.setdefault(first, title)
    return list(found.items())


def err(line):
    print(line, file=sys.stderr, flush=True)


def quiet(line):
    pass


# How long a step that waits on another machine may take before it says so.
SLOW = 2


@contextlib.contextmanager
def slowly(what, say=err, after=SLOW):
    """Say what is being waited for if what is done inside takes longer than after
    seconds: so that waiting on a host, such as one only the VPN reaches, does not
    look like a hang."""
    timer = threading.Timer(after, say, [what])
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def batch_prs(args, jobs):
    """The PRs under test, from the run's QA tracker; and a line for the report."""
    if args.qa_tracker == 'none' or not jobs:
        return set(), []
    try:
        if args.qa_tracker:
            issue = tracker_issue(args.qa_tracker)
        else:
            config = files_of(args, jobs[0]).text('orig.config.yaml')
            branch = (yaml.safe_load(config) or {}).get('branch', '')
            issue = find_qa_tracker(branch)
            if not issue:
                print(f'No QA tracker found for {branch}; name it with --qa-tracker',
                      file=sys.stderr)
                return set(), []
            # With its comments: its latest list of PRs may be in one.
            issue = tracker_issue(issue['id'])
    except urllib.error.URLError as e:
        print(f'Could not read the QA tracker: {e}', file=sys.stderr)
        return set(), []
    prs = pr_numbers(issue)
    line = (f'QA tracker {TRACKER}/issues/{issue["id"]}, PRs under test: ' +
            ', '.join(f'#{n}' for n in sorted(prs, key=int)))
    (quiet if args.quiet else err)(line)
    lines = [line]
    if prs and not args.settings.get('git'):
        lines.append(NO_CLONE)
        (quiet if args.quiet else err)(NO_CLONE)
    return prs, lines + ['']


# Said where the PRs under test cannot be blamed, for want of a ceph clone.
NO_CLONE = ('PR blame is off: qa_triage is not in a ceph clone, and the git setting names '
            'none, so the PRs under test cannot be checked against what each job ran. Run it '
            'from a ceph clone, as ceph\'s src/script/qa_triage/qa_triage.sh does, or set git '
            'to one.')


def judge_refs(args):
    """The agents of the judge's cascade, as given."""
    spec, agents = args.judge, args.settings['agents']
    if not spec or spec == 'none':
        return []
    if isinstance(spec, list):
        return spec
    return [spec] if spec.startswith('cmd:') or spec in agents else spec.split(',')


def make_analyst(args, fallback=False):
    """The analyst, which reads the evidence files itself: --analyst, or the
    config's; with fallback, else the last of the judge's agents. None for none."""
    ref = args.analyst or (fallback and (judge_refs(args) or [None])[-1])
    if not ref or ref == 'none':
        return None
    if ',' in ref and not ref.startswith('cmd:') and ref not in args.settings['agents']:
        raise ValueError('the analyst is one agent, not a cascade')
    return make_agent(ref, args.settings['agents'], files=True)


def judge_unplaced(triage, results, files, judge, say=err, at_once=1):
    """Judge one job of each group of jobs that the tree and the records could
    not decide, grouped by what the tree found; at_once groups at a time."""
    groups = defaultdict(list)
    for r in results:
        if r.path[-1] == 'agent':
            groups[r.signature].append(r)
    if not judge:
        for group in groups.values():
            for r in group:
                r.group = group[0].job.id
        return

    def judge_one(group):
        judging = triage.judging(judge)
        try:
            judging.judge_group(group, files[group[0].job.id])
        finally:
            triage.spent.extend(judging.spent)
        say(f'{",".join(r.job.id for r in group)} judged {group[0].verdict} '
            f'{group[0].problem.id if group[0].problem else ""}')

    each_at_once(judge_one, groups.values(), at_once)


def check_decided(triage, results, files, judge, say=err, at_once=1):
    """Ask the judge to check each group that the tree and the records decided, as
    the review's judge all does, at_once groups at a time: its answer is each job's
    opinion, and the verdicts stand."""
    def check_one(rs):
        judging = triage.judging(judge)
        try:
            judging.check_group(rs, files[rs[0].job.id])
        finally:
            triage.spent.extend(judging.spent)
        o = rs[0].opinion
        said = ('gave no answer' if not o else
                {True: 'agrees', None: 'is not sure'}.get(
                    o.get('agrees'), f'disagrees: it says {o.get("verdict")} '
                                     f'{o.get("problem") or ""}'.rstrip()))
        say(f'{",".join(r.job.id for r in rs)} {describe(rs[0])}: the judge {said}')

    each_at_once(check_one, [rs for _, rs in report.groups(results)
                             if rs[0].sure and not rs[0].agent], at_once)


def retriage(triage, results, files, judge, say=err, at_once=1):
    """Triage the jobs again with the tree and the records a run learned. The
    jobs they decide now get their new results; the others keep the judge's."""
    triage.findings = {}
    again, unplaced = [], []
    redone = triage_all(triage, [old.job for old in results], lambda job: files[job.id])
    for old, (_, _, r) in zip(results, redone):
        r.notes += [n for n in old.notes if n.startswith(FOUND)]
        r.analysis = r.analysis or old.analysis
        if r.path[-1] != 'agent':
            if 'agent' in old.path:
                r.notes.append('decided by what the tree and the records learned from '
                               'this run')
            again.append(r)
        elif 'agent' in old.path:
            again.append(old)
        else:
            again.append(r)
            unplaced.append(r)
    judge_unplaced(triage, unplaced, files, judge, say, at_once)
    return again


def load_known(path):
    """{job id: problem id} from a file of known answers, each a duplicate's or a
    backport's number replaced by the issue it names; {} for no file."""
    if not path:
        return {}
    with open(path) as f:
        known = {str(j): str(p) for j, p in (yaml.safe_load(f) or {}).items()}
    numbers = {p: canonical(p, tracker_issue) for p in set(known.values())}
    return {j: numbers[p] for j, p in known.items()}


# A run's name: who scheduled it and when, then its suite, branch and so on.
RUN_NAME = re.compile(r'[\w.+-]+-\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}-')


def the_run(arg, choose=None, say=err):
    """The run arg names: a run's name or URL, a branch, a pulpito page that lists
    runs, such as a branch's (https://pulpito.ceph.com/?branch=<branch>), or a
    tracker issue that points at runs. choose(runs) picks one of several, or gives
    None."""
    if issue_number(arg):
        return run_of_issue(issue_number(arg), choose, say)
    branch = None
    if '://' not in arg and not RUN_NAME.match(run_name(arg)):
        # Not a run's name: a branch, whose runs pulpito lists.
        branch, arg = arg, f'{PULPITO}/?branch={urllib.parse.quote(arg)}'
    page = urllib.parse.urlparse(arg) if '://' in arg else None
    if not (page and page.query and not page.path.strip('/')):
        return run_name(arg)
    try:
        runs = list_runs(arg)
    except urllib.error.URLError as e:
        raise ValueError(f'cannot read {arg}: {e}') from e
    if len(runs) == 1:
        if branch:
            say(f'{branch}: {runs[0]}')
        return runs[0]
    chosen = choose(runs) if runs and choose else None
    if not chosen and branch and not runs:
        raise ValueError(f'{branch} is not a run\'s name, and pulpito has no runs of a branch '
                         'of that name: give a run\'s name or URL, a branch, or a tracker '
                         'issue\'s URL')
    if not chosen:
        raise ValueError((f'branch {branch} has ' if branch else f'{arg} lists ') +
                         (f'{len(runs)} runs; give one of them:\n  ' + '\n  '.join(runs)
                          if runs else 'no runs'))
    return chosen


def run_of_issue(number, choose=None, say=err):
    """The run tracker issue number is about: a QA batch's, the run of its branch,
    or, if that has none yet, of the branch before it; another issue's, the run it
    links to. choose(runs) picks one of several, or gives None."""
    try:
        issue = tracker_issue(number)
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise ValueError(f'cannot read tracker issue {number}: {e}') from e
    branches, linked = issue_runs(issue)
    for n, branch in enumerate(branches):
        try:
            runs = list_runs(f'{PULPITO}/?branch={urllib.parse.quote(branch)}')
        except urllib.error.URLError as e:
            raise ValueError(f'cannot read the runs of {branch} on pulpito: {e}') from e
        if runs:
            where = (f'its branch {branch}' if n == 0 else
                     f'{branch}, as {branches[0]} has no runs yet')
            break
    else:
        runs, where = linked, 'the runs it links to'
    if not runs:
        raise ValueError(f'tracker issue {number} points at no run: it names no branch with '
                         'runs on pulpito, and links to no run')
    run = runs[0] if len(runs) == 1 else choose(runs) if choose else None
    if not run:
        raise ValueError(f'tracker issue {number} points at {len(runs)} runs, of {where}; '
                         'give one of them:\n  ' + '\n  '.join(runs))
    say(f'Tracker issue {number} ({issue.get("subject", "")}): {run}, of {where}.')
    return run


def choose_run(runs):
    """One of runs, which the person chooses at the terminal; None with no terminal."""
    if not sys.stdin.isatty():
        return None
    print('There are several runs, newest first:', file=sys.stderr)
    for n, run in enumerate(runs, 1):
        print(f'  {n}. {run}', file=sys.stderr)
    answer = input('Which one (its number; enter for the first)? ').strip() or '1'
    return runs[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(runs) else None


# The most jobs of a run triaged at once, unless the at_once setting says otherwise.
AT_ONCE = SSH_AT_ONCE


# Said when the job files are downloaded over https.
HTTPS_SLOW = ('The job files are downloaded over https, which is slow: each log the tree '
              'reads is downloaded first, and a lesson\'s checks may download hundreds. '
              'Over ssh to a host that has the archive (on the sepia VPN, %s by default), '
              'they are searched where they are.' % config.DEFAULTS['archive'])


# The commands that read job files: they say at once if the archive's host does not
# answer, rather than wait on it for every job.
READS_ARCHIVE = {'run', 'review', 'verify', 'open', 'files', 'doubt', 'settle', 'checkout',
                 'teach', 'paths', 'watch'}


def unreachable(args, say=err):
    """Why the job files cannot be read as args.archive says, or None: whether its
    host answers over ssh. The setting https is never chosen for the person."""
    if https_base(args.archive):
        return None
    host = parse_archive(args.archive)[0]
    with slowly(f'Waiting for {host} to answer over ssh, to read the job files there '
                '(is the sepia VPN connected?)', say):
        ok, found = reach(args.archive)
    return None if ok else f'{found}: {UNREACHED}'


def hint_ssh(args, say):
    """If a config, not the command line, has the job files downloaded over https,
    and the default archive's host answers over ssh, say so, on a thread of its own:
    the setup of earlier versions chose https by itself when it did not answer."""
    archive = config.DEFAULTS['archive']
    if not https_base(args.archive) or args.archive != args.settings.get('archive'):
        return

    def look():
        ok, found = reach(archive)
        if ok:
            say(f'The archive setting is {args.archive}, so the job files are downloaded over '
                f'https; but {found}, where they are searched in place, which is much '
                f'faster. Unless you chose https, take archive out of your config '
                f'(qa_triage setup, Job files): earlier versions of the setup chose https '
                f'by themselves when {archive.split(":")[0]} did not answer.')
    threading.Thread(target=look, daemon=True).start()


def runs_dir(args):
    """Where each run's output is, unless --out says otherwise: in the cache."""
    return os.path.join(args.cache, 'runs')


def unready(*agents):
    """Why the first of agents that cannot run here cannot, or None."""
    for agent in agents:
        why = agent and getattr(agent, 'missing', lambda: None)()
        if why:
            return why
    return None


def compare_with_base(triage, run, cache, say=err):
    """Give triage the failures of the base branch's runs near run, to compare each
    job with; a line for the report, or none."""
    try:
        near = baseline.runs(run, list_runs)
        failed = baseline.failures(near, list_jobs, os.path.join(cache, 'baseline'))
    except (urllib.error.URLError, OSError, ValueError) as e:
        say(f'Could not read the base branch\'s runs: {e}')
        return []
    if not near:
        return []
    triage.base = baseline.base_of(baseline.parts(run)[2])
    triage.base_failures = failed
    line = (f'Compared with {len(near)} runs of {triage.base} from near its date, which '
            f'failed {sum(len(jobs) for jobs in failed.values())} jobs: ' + ', '.join(near))
    say(line)
    return [line, '']


def triage_all(triage, jobs, files_for, at_once=AT_ONCE, in_order=True, stop=None):
    """Yield (job, its files, its Result) for each of jobs, the tree and the records
    triaging at_once of them at a time: most of the time goes on waiting for the
    archive. In their order, or, unless in_order, each as soon as it is triaged.

    Once stop, an Event, is set, or the caller stops early, it ends at once: the jobs
    not begun are left, and those being triaged are given up, on threads that never
    hold up the process's exit, as a download that seems stuck may."""
    todo = queue.Queue()
    for n, job in enumerate(jobs):
        todo.put((n, job))
    done, ended = queue.Queue(), threading.Event()

    def work():
        mine = triage.for_another_job()
        while not ended.is_set():
            try:
                n, job = todo.get_nowait()
            except queue.Empty:
                return
            try:
                files = files_for(job)
                done.put((n, (job, files, mine.run(job, files)), None))
            except BaseException as e:
                done.put((n, None, e))
    for _ in range(min(at_once, len(jobs))):
        threading.Thread(target=work, daemon=True).start()
    kept, first = {}, 0
    try:
        for _ in jobs:
            while True:
                if stop and stop.is_set():
                    return
                try:
                    n, triaged, error = done.get(timeout=0.2)
                    break
                except queue.Empty:
                    pass
            if error:
                raise error
            if not in_order:
                yield triaged
                continue
            kept[n] = triaged
            while first in kept:
                yield kept.pop(first)
                first += 1
    finally:
        ended.set()


def start(args, problems, say):
    """Triage a run's failed jobs with the tree and the records alone: no AI."""
    session = opened(args)
    prepare(args, problems, say, session)
    triage_jobs(args, session, say)
    return session


def opened(args):
    """The session of the run args name, before its jobs are found: what a screen that
    opens at once shows, as prepare and triage_jobs fill it in."""
    run = run_name(args.run)
    return SimpleNamespace(run=run, jobs=[], out=args.out or os.path.join(runs_dir(args), run),
                           header=[], known={}, triage=None, results=[], files={},
                           triaged=False, stop=threading.Event(), skipped=[])


def prepare(args, problems, say, session):
    """Find the session's failed jobs, and make the triage that decides them."""
    run = session.run
    statuses = args.status.split(',')
    every = list_jobs(run)
    jobs = [j for j in every if j.status in statuses]
    if args.jobs:
        wanted = set(args.jobs.split(','))
        jobs = [j for j in jobs if j.id in wanted]
    say(f'{len(jobs)} {" or ".join(statuses)} jobs in {run}')
    if https_base(args.archive):
        say(HTTPS_SLOW)
        hint_ssh(args, say)
    out = session.out
    prs, header = batch_prs(args, jobs)
    triage = Triage(problems, deep=args.deep, git_dir=args.git,
                    workdir=os.path.join(out, 'judge'), batch_prs=prs,
                    verdicts=os.path.join(args.cache, 'verdicts'), search=search_originals)
    triage.blame, triage.pr_of = True, pr_of_branch
    triage.cited_lines = args.settings.get('cited_lines', triage.cited_lines)
    # The judge's evidence leaves out what a job of the run that passed logs too.
    triage.passing = [j for j in every if j.status == 'pass']
    triage.files_for = lambda job: files_of(args, job)
    # What people answered for the runs before this one: it must not grade itself,
    # nor know what was learned after it.
    triage.memory = memory.load(memory_dir(args), leave_out=(run,), before=run)
    if triage.memory.stale:
        say(f'The memory of {len(triage.memory.stale)} runs was kept with another way of '
            'making signatures, so it is left out: qa_triage remember writes it again.')
    header += compare_with_base(triage, run, args.cache, say)
    session.header[:0] = header
    session.jobs, session.known, session.triage = jobs, load_known(args.known), triage


def triage_jobs(args, session, say):
    """Triage the session's jobs, each result added to its results as soon as it is
    decided, its files first; then in the jobs' order. Once session.stop is set, the
    jobs not triaged by then are left, in session.skipped."""
    files, results = session.files, session.results
    for job, found, r in triage_all(session.triage, session.jobs,
                                    lambda job: files_of(args, job),
                                    args.settings.get('at_once', AT_ONCE), in_order=False,
                                    stop=session.stop):
        files[job.id] = found
        say(f'{job.id} {r.verdict:<18} {r.problem.label if r.problem else ""}')
        for line in report.decisions(r):
            say(line)
        results.append(r)
    order = {job.id: n for n, job in enumerate(session.jobs)}
    results.sort(key=lambda r: order[r.job.id])
    session.skipped = [job.id for job in session.jobs if job.id not in files]
    if session.skipped:
        say(f'Triage stopped: {len(session.skipped)} jobs not triaged: '
            f'{", ".join(session.skipped)}. Starting the review again triages them, and '
            f'the rest from what the cache kept of them.')
    placed_on = {job.id: hosts.machines(files[job.id]) for job in session.jobs
                 if job.id in files}
    session.header += hosts.note(hosts.crowded(results,
                                               lambda r: placed_on.get(r.job.id, [])))
    asked = sum(getattr(f, 'asked', 0) for f in files.values())
    reused = sum(getattr(f, 'reused', 0) for f in files.values())
    if asked or reused:
        say(f'The archive was asked {asked} questions; {reused} more were answered by what '
            f'it said before, kept in {args.cache}.')
    session.triaged = True


# What a job ends as: a run all of whose jobs did has finished, and its list of jobs
# does not change.
ENDED = ('pass', 'fail', 'dead')
# The runs' jobs listed already, by this process: {run: {job id: Job}}.
_RUNS_JOBS = {}


def jobs_of(run, cache):
    """{job id: Job} of run, as pulpito lists them: kept in cache once the run has
    finished, since pulpito lists a big run slowly, and the checks of one change
    list the runs of hundreds of examples. {} if pulpito cannot be read."""
    if run in _RUNS_JOBS:
        return _RUNS_JOBS[run]
    path = os.path.join(cache, 'jobs', run.replace(':', '_') + '.json')
    try:
        with open(path) as f:
            jobs = [Job(**job) for job in json.load(f)]
    except (OSError, ValueError, TypeError):
        try:
            jobs = list_jobs(run)
        except urllib.error.URLError:
            return {}
        if not (jobs and all(j.status in ENDED for j in jobs)):
            # Not finished: listed again when asked again.
            return {j.id: j for j in jobs}
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(f'{path}.{os.getpid()}.{threading.get_ident()}', 'w') as f:
                json.dump([vars(j) for j in jobs], f)
            os.replace(f.name, path)
    _RUNS_JOBS[run] = {j.id: j for j in jobs}
    return _RUNS_JOBS[run]


def learning(args, session):
    """What learn_from_run needs, besides the results and the analyst, to learn
    from the session's run."""
    run, out = session.run, session.out
    runs = {run: {j.id: j for j in session.jobs}}

    def files_for(job):
        if job.run == run and job.id in session.files:
            return session.files[job.id]
        return files_of(args, job)

    def jobs_for(name):
        if name not in runs:
            runs[name] = jobs_of(name, args.cache)
        return runs[name]

    patterns = args.corpus or [os.path.join(os.path.dirname(out) or '.', '*', 'results.json')]
    earlier = sorted({path for pattern in patterns for path in glob.glob(pattern)})
    corpus = [j for j in load_corpus(earlier) if j.run != run] + session.jobs
    return dict(files_for=files_for, jobs_for=jobs_for, problems_dir=args.problems,
                labels_dir=args.labels, workdir=os.path.join(out, 'learn'),
                tracker=tracker_issue, search=tracker_search,
                corpus=corpus, holdout=held_out(args, corpus)[0],
                limit=args.learn_max, at_once=args.lessons_at_once,
                source=source_of(args), nodes_dir=args.nodes,
                history=dict(load_history(earlier), **history_of(session.results)),
                relearn=args.relearn, known=session.known, budget=args.learn_budget,
                earlier=glob.glob(os.path.join(os.path.dirname(out) or '.', '*', 'learn', '*',
                                               '')),
                approving=approving(args))


def cmd_run(args, problems):
    say = quiet if args.quiet else err
    try:
        judge = make_judge(args.judge, args.settings['agents'])
        analyst = make_analyst(args)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    if unready(judge, analyst):
        print(f'error: {unready(judge, analyst)}', file=sys.stderr)
        return 2
    session = start(args, problems, say)
    run, out, header, known = session.run, session.out, session.header, session.known
    triage, results, files = session.triage, session.results, session.files

    judge_unplaced(triage, results, files, judge, say, args.judges_at_once)
    if args.check and judge:
        triage.blind = args.check == 'blind'
        check_decided(triage, results, files, judge, say, args.judges_at_once)
    if known:
        header += report.compared(results, known, f'Known answers ({args.known}): as '
                                                  'triaged' + (', before learning'
                                                               if analyst else ''))
        if args.check and judge:
            header += report.checked(results, known)
    lessons = []
    if analyst:
        lessons = learn_from_run(results, analyst=analyst, say=say,
                                 **learning(args, session))
        if lessons:
            header += [f'What the tree and the records learned from this run '
                       f'({analyst.name}); the changes are in {args.nodes}, '
                       f'{args.problems} and {args.labels}:', '',
                       *[f'- {line}' for line in lessons], '']
            spent = report.judge_costs(results)
            if spent:
                header += ['Before they learned, the judge was asked about every group they '
                           'could not decide: ' +
                           ', '.join(f'{name} {cost:.4f} ({calls} calls)'
                                     for name, (cost, calls) in spent.items()) +
                           '. The costs below are for the groups they still cannot.', '']
            triage.problems = db.load(args.problems)
            results = retriage(triage, results, files, judge, say, args.judges_at_once)
            if known:
                header += report.compared(results, known, 'After learning')
    report.write(out, run, results, header=header)
    print('\n'.join(report.summary(run, results, header, lessons, triage.spent,
                                   units(judge, analyst), analyst and analyst.name)))
    print(f'Report: {out}/report.md\nDrafts: {out}/drafts/')
    return 0


def reviewing(args, problems, judge, analyst):
    """A review of the run args names, and load(say), which finds the run's jobs and
    triages them into it: in the background, as the review's screen opens at once."""
    session = opened(args)
    review = Review(session, judge, analyst, learning=lambda: learning(args, session),
                    retriage=lambda results: retriage(session.triage, results,
                                                      session.files, None, quiet),
                    units=units(judge, analyst), args=args,
                    agents=lambda: (make_judge(args.judge, args.settings['agents']),
                                    make_analyst(args, fallback=True)))

    def load(say):
        prepare(args, problems, say, session)
        triage_jobs(args, session, say)
        judge_unplaced(session.triage, session.results, session.files, None)
        review.restore(say)
        # The run is one that later changes are checked against, from now on.
        review.write()

    return review, load


def cmd_review(args, problems):
    """Work through a run at a prompt, or on the full screen (see review.py)."""
    try:
        judge = make_judge(args.judge, args.settings['agents'])
        # It is asked only when the person says, so it can be the judge's agent.
        analyst = make_analyst(args, fallback=True)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    why = unready(judge, analyst)
    if why:
        print(f'error: {why}', file=sys.stderr)
        return 2
    review, load = reviewing(args, problems, judge, analyst)
    if args.plain or not sys.stdout.isatty():
        load(quiet if args.quiet else (lambda line: line.startswith('  ') or err(line)))
        review.do_summary('')
        review.cmdloop()
    else:
        # The screen opens at once, and the jobs come into it as they are triaged.
        print(tui.run(review, load))
    return 0


# How many runs watch triages at once, without its screen; and the most runs left
# out for one reason that it names, rather than counts.
RUNS_AT_ONCE, LEFT_NAMED = 3, 3


def run_args(args, run):
    """watch's args, for one of its runs, as run and review take them."""
    return argparse.Namespace(**dict(
        vars(args), run=run, out=None, jobs=None, known=None, quiet=True, plain=False,
        relearn=False, check=None,
        # The base branch's runs are no QA batch's: they have no QA tracker.
        qa_tracker='none' if watch.branch_of(run) == args.base else None))


def _kept_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _keep_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.new', 'w') as f:
        json.dump(data, f)
    os.replace(path + '.new', path)


def watched_prs(args, state, start):
    """{number: {title, head, ref, commits}} of the PRs whose runs state follows: the
    person's own (mine), open or closed since start, or those it names. What GitHub
    said of each is kept in the cache, and asked again once the PR is updated."""
    path = os.path.join(args.cache, 'watch', 'prs.json')
    kept = _kept_json(path)
    if state['prs'] == 'mine':
        login = github_login()
        if not login:
            raise ValueError('GitHub does not say who you are: sign in with gh auth login, '
                             'or give a GitHub token on the setup page (qa_triage setup)')
        listed = authored_prs(login, start)
    else:
        listed = {number: ((pr_title(number) or ('',))[0], None) for number in state['prs']}

    def ask(number):
        title, updated = listed[number]
        known = kept.get(number)
        if known and updated and known.get('updated') == updated:
            return number, known
        try:
            return number, dict(pr_commits(number), title=title, updated=updated)
        except (urllib.error.URLError, OSError, ValueError, KeyError,
                subprocess.SubprocessError):
            return number, known

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        found = {number: pr for number, pr in pool.map(ask, listed) if pr}
    kept.update(found)
    _keep_json(path, kept)
    return found


def build_reader(args):
    """read(sha1s), as watch.pr_runs takes it: {sha1: ({PR number: [how]}, [(author's
    email, subject)] of its commits)} of each build, fetched from ceph-ci into the
    ceph clone if it does not have it, or None for one it could not read. What each
    build tests is kept in the cache, since a build does not change."""
    path = os.path.join(args.cache, 'watch', 'builds.json')

    def read(sha1s):
        kept = _kept_json(path)
        missing = [sha1 for sha1 in sha1s if sha1 not in kept]
        if missing and not args.git:
            return {sha1: kept.get(sha1) for sha1 in sha1s}
        failed = set(fetch_all(args.git, missing)) if missing else set()
        for sha1 in missing:
            if sha1 in failed:
                continue
            try:
                commits = added(args.git, sha1)
            except CheckoutError:
                continue
            kept[sha1] = [build_prs(commits, pr_files, linked_prs, pr_of_branch),
                          [(email, message.split('\n', 1)[0])
                           for _, parents, email, message, _ in commits if len(parents) == 1]]
        if missing:
            _keep_json(path, kept)
        return {sha1: kept.get(sha1) for sha1 in sha1s}

    return read


def watch_users(state):
    """The teuthology users whose runs are the person's own: as state names them, or
    else their GitHub login and their user name here."""
    return state['users'] or list(dict.fromkeys(
        name for name in (github_login(), getpass.getuser()) if name))


def follow_prs(given, state):
    """Have the watch's state follow the PRs --prs gives, for this watch alone:
    without it, the suite's runs, whatever the watch before followed. The runs found
    before are dropped when that changes."""
    prs = None
    if given:
        prs = [p.strip().lstrip('#') for p in given.split(',') if p.strip()]
        if prs in (['mine'], ['none']):
            prs = None if prs == ['none'] else 'mine'
        elif not all(p.isdigit() for p in prs):
            raise ValueError(f'--prs {given}: give mine, or PR numbers, as 73070,73100')
    if prs != state['prs']:
        state.update(prs=prs, found=[], found_at=None, tests={})


def cmd_watch(args, problems):
    """Triage several runs with the tree and the records, no AI, and list the
    failures they have in common, the commonest first (see watch.py): on a screen
    with a pane of the runs, to go to any of them, or, with no terminal or with
    --plain, printed. With --prs, the runs are those of the PRs."""
    say = quiet if args.quiet else err
    state = watch.kept(args.cache)
    try:
        if args.since or args.until or args.days:
            state.update(since=args.since and str(watch.day(args.since)),
                         until=args.until and str(watch.day(args.until)), days=args.days)
        follow_prs(args.prs, state)
        if args.user:
            state['users'] = [u.strip() for u in args.user.split(',') if u.strip()]
        # Not start: that triages a run.
        first, last = watch.span(state)
        given = list(dict.fromkeys(the_run(arg, lambda found: next(
            (r for r in found if watch.suite_of(r) == args.suite), found[0]), say)
            for arg in args.runs))
    except (ValueError, urllib.error.URLError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2

    def find():
        """The runs between the state's dates, as they are when it is asked: the
        suite's, or the PRs' that the state follows."""
        since, until = watch.span(state)
        if not state['prs']:
            return watch.recent(list_runs, tracker_issues, list_jobs, since, until,
                                args.base, args.suite, args.most, args.least)
        prs = watched_prs(args, state, since)
        builds = list_builds(f'{PULPITO}/?date={since:%Y-%m-%d}&to_date={until:%Y-%m-%d}')
        runs, left, tests = watch.pr_runs(prs, builds, tracker_issues, build_reader(args),
                                          since, until, watch_users(state), args.most)
        state['tests'] = tests
        state['titles'] = {number: pr['title'] for number, pr in prs.items()}
        return runs, left

    if sys.stdin.isatty() and sys.stdout.isatty() and not args.plain:
        if given:
            state.update(given=given, found=[], found_at=None)
        watch.keep(args.cache, state)
        try:
            make_judge(args.judge, args.settings['agents'])
            make_analyst(args, fallback=True)
        except ValueError as e:
            print(f'error: {e}', file=sys.stderr)
            return 2

        # One limit for the agents of every run: so many turns of the judge, and of the
        # analyst, as the settings say a review has.
        turns = {kind: threading.BoundedSemaphore(n) for kind, n in
                 (('judge', args.judges_at_once), ('analyst', args.lessons_at_once))
                 if isinstance(n, int)}

        def review_of(run):
            one = run_args(args, run)
            review, load = reviewing(one, problems,
                                     make_judge(one.judge, one.settings['agents']),
                                     make_analyst(one, fallback=True))
            review.turns = turns
            return review, load

        print(tui.watch_runs(state, review_of, find, args.cache, args.base))
        return 0

    runs = given
    whose = (f'runs of {watch.following(state)}' if state['prs'] else
             f'{args.suite} runs of {args.base}, and of its QA batches,')
    if not runs:
        try:
            with slowly(f'Finding the {whose} from {first:%Y-%m-%d} to {last:%Y-%m-%d}...'):
                runs, left = find()
        except (urllib.error.URLError, ValueError) as e:
            print(f'error: {e}', file=sys.stderr)
            return 2
        reasons = defaultdict(list)
        for run, why in left:
            reasons[why].append(run)
        for why, those in reasons.items():
            if len(those) > LEFT_NAMED:
                say(f'Left out {len(those)} runs: {why}')
            else:
                for run in those:
                    say(f'Left out {run}: {why}')
        if state['prs']:
            print('\n'.join(watch.pr_lines(state['tests'], state.get('titles'))))
    if not runs:
        print(f'error: pulpito has no {whose} from {first:%Y-%m-%d} to {last:%Y-%m-%d}: name '
              'the runs', file=sys.stderr)
        return 2
    say(f'{len(runs)} runs, triaged by the tree and the records, with no AI:')
    for run in runs:
        say(f'  {run}')

    def triage(run):
        try:
            session = start(run_args(args, run), problems, quiet)
        except (urllib.error.URLError, ArchiveError) as e:
            say(f'{run}: not triaged: {e}')
            return None
        report.write(session.out, run, session.results, header=session.header)
        say(f'{run}: {len(session.results)} failed or dead jobs; its report is '
            f'{session.out}/report.md')
        return session

    with concurrent.futures.ThreadPoolExecutor(RUNS_AT_ONCE) as pool:
        sessions = [s for s in pool.map(triage, runs) if s]
    runs = [s.run for s in sessions]
    failures = watch.combine([r for s in sessions for r in s.results])
    out = args.out or os.path.join(args.cache, 'watch',
                                   datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S'))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'watch.md'), 'w') as f:
        f.write(watch.markdown(failures, runs, args.base,
                               {s.run: len(s.results) for s in sessions}))
    print('\n'.join(watch.summary(failures, runs, args.base, args.top)))
    print(f'Report: {out}/watch.md')
    return 0


def cmd_answers(args, problems):
    """Write the answers people gave for the jobs of the runs on the RADOS wiki, or
    with --tracker, those the tracker's issues give by linking to jobs."""
    if args.tracker:
        return tracker_answers(args)
    key = tracker_key(args.settings.get('tracker_key'))
    if not key:
        print('error: reading the wiki needs a tracker API key: set REDMINE_API_KEY, or '
              'the tracker_key setting (qa_triage setup)', file=sys.stderr)
        return 2
    try:
        sections = answers.parse(answers.wiki_text(key, args.project, args.page))
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f'error: cannot read the wiki page {args.project}/{args.page}: {e}',
              file=sys.stderr)
        return 2
    print(f'{sum(len(jobs) for jobs in sections.values())} jobs in {len(sections)} sections '
          f'of {args.project}/{args.page}')
    found = answers.gather(sections, tracker_issue, list_runs, list_jobs)
    directory = args.out or os.path.join(args.cache, 'answers')
    written = answers.write(found, directory)
    print(f'{sum(len(a) for a in found.values())} jobs in {len(written)} runs: {directory}; '
          'qa_triage run <run> --known <its file> learns from them')
    return 0


def held_out(args, corpus, canonical_of=str):
    """[(Job, its answer)] of the jobs of corpus that the holdout setting's runs
    answer: held out of learning, so that a change may not place fewer of them
    rightly; and the runs, {run: {job id: answer}}."""
    runs = score.held_out(args.settings.get('holdout'))
    return [(job, canonical_of(runs[job.run][job.id])) for job in corpus
            if job.id in runs.get(job.run, {})], runs


def approving(args, interactive=None):
    """What Doubt needs to bring the library up to date, ask before changing it and
    publish the change: {approve, library, publish}. interactive is False where no
    one can be asked now, as on the review's screen."""
    return approve.doubting(args.settings, args.cache, getattr(args, 'library', None),
                            interactive)


def cmd_pending(args, problems):
    """List the changes that wait for a person's approval, each with the command
    that shows it, checks it again against the library as it is, and asks."""
    def changes(held, path=None):
        what = ', '.join(c.get('id', '?') for c in held.get('changes') or []) or 'no record'
        nodes = ', '.join(n.get('file', '?') for n in held.get('nodes') or [])
        made = path and approve.commit_of(path, held, args.problems)
        return f'  changes record {what}' + (f'; nodes {nodes}' if nodes else '') + \
            (f'\n  to inspect it: git -C {made[0]} show {made[1][:12]}' if made else '')

    waiting = approve.held_in(approve.pending(args.cache))
    edits = approve.held_in(approve.edits(args.cache))
    disputed = approve.held_in(approve.disputed(args.cache))
    labels = unconfirmed_labels(args.labels)
    if not waiting and not edits and not disputed and not labels:
        print('No change waits for approval.')
        return 0
    if waiting or edits:
        print('The review\'s A key asks about each of these, or, here:\n')
    for path, held in waiting:
        print(f'{held.get("job")}: {held.get("summary", "")[:120]}\n{changes(held, path)}'
              f'\n  qa_triage doubt {held.get("job")} --proposal {path}')
    if edits:
        print('\nChanges that edit records or nodes, not only add to them, which '
              'a person approves even where additions are kept by themselves:')
    for path, held in edits:
        print(f'{held.get("job")}: {held.get("summary", "")[:120]}\n{changes(held, path)}'
              f'\n  qa_triage doubt {held.get("job")} --proposal {path}')
    if disputed:
        print('\nChanges for answers that dispute the people\'s, kept for a person who '
              'agrees with the analyst:')
    for path, held in disputed:
        people = held.get('disputes') or {}
        print(f'{held.get("job")}: {held.get("summary", "")[:120]}\n{changes(held)}\n'
              f'  {people.get("by")} expected {people.get("expected")}, the analyst says '
              f'{held.get("expected")}\n'
              f'  qa_triage settle {held.get("job")} {held.get("expected")} --why ...')
    if labels:
        print(f'\nJobs settled for a person, which none has confirmed yet ({len(labels)}): '
              'settling one again, as you, confirms it; score says what they change.')
    for job, problem, where in labels:
        print(f'  {job.path}: {problem or "new"}, {where}\n'
              f'    qa_triage settle {job.path} {problem or "new"} --why ...')
    return 0


def memory_dir(args):
    """The library's memory: beside its settled jobs."""
    return os.path.join(os.path.dirname(os.path.abspath(args.labels)), 'memory')


def precision_file(args):
    """Where the library keeps how often the jobs people answered were placed rightly
    in each problem (score.precision): beside its settled jobs."""
    return os.path.join(os.path.dirname(os.path.abspath(args.labels)), 'precision.yaml')


def cmd_remember(args, problems):
    """Write the library's memory: what people placed each way a job failed in, from
    the runs triaged with their answers."""
    answers_dir = args.answers or os.path.join(args.cache, 'answers')
    first, issues = {}, {}

    def issue_of(number):
        if number not in issues:
            issues[number] = tracker_issue(number)
        return issues[number]

    def canonical_of(number):
        if number not in first:
            first[number] = canonical(number, issue_of)
        return first[number]

    def title_of(number):
        try:
            return issue_of(number).get('subject')
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            return None

    # The memory remembers nothing of the runs held out of learning.
    held = score.held_out(args.settings.get('holdout'))
    triaged = [found for found in score.runs(answers_dir, args.runs or runs_dir(args),
                                             settled_by_people(args.labels))
               if found[0] not in held]
    found = memory.build(triaged, canonical_of)
    named = {i for signatures in found.values() for answers in signatures.values()
             for i in answers}
    written = memory.write(memory_dir(args), found,
                           {i: title_of(i) for i in sorted(named) if str(i).isdigit()})
    forgotten = memory.forget(memory_dir(args), held)
    print(f'{sum(len(s) for s in found.values())} ways of failing, from {len(written)} runs, '
          f'in {memory_dir(args)}' +
          (f'; the memory of {len(forgotten)} runs held out of learning removed'
           if forgotten else ''))
    # And how often each problem's placements were right, for people to weigh them by.
    with open(precision_file(args), 'w') as f:
        f.write('# How often the jobs people answered that qa_triage placed in each problem '
                'were placed\n# rightly, from qa_triage remember: report.PRECISION.\n')
        yaml.safe_dump(score.precision(score.score(triaged, problems, canonical_of)), f,
                       sort_keys=True, width=1000)
    return 0


def cmd_score(args, problems):
    """How often the tree and the records gave the answers people gave, for the runs
    triaged before."""
    answers_dir = args.answers or os.path.join(args.cache, 'answers')
    runs_ = args.runs or runs_dir(args)
    settled = settled_by_people(args.labels)
    try:
        triaged = score.runs(answers_dir, runs_, settled)
        as_given = score.runs(answers_dir, runs_)
    except OSError as e:
        print(f'error: cannot read the answers in {answers_dir}: {e}', file=sys.stderr)
        return 2
    first = {}

    def canonical_of(number):
        if number not in first:
            first[number] = canonical(number, tracker_issue)
        return first[number]

    print(f'{len(triaged)} runs of {answers_dir} were triaged in {runs_}.')
    out = score.lines(score.score(triaged, problems, canonical_of), args.most)
    # The answers as people gave them, apart from those settled since, which may be
    # an assistant's, and which a lesson's dispute can bring nearer the tool's.
    otherwise = sum(1 for run, answers, _ in as_given for job, answer in answers.items()
                    if f'{run}/{job}' in settled and settled[f'{run}/{job}'] != answer)
    if otherwise:
        given = score.score(as_given, problems, canonical_of)['total']
        waiting = len(unconfirmed_labels(args.labels))
        out.insert(1, f'Against the answers as the wiki and the tracker gave them, before '
                      f'{otherwise} of them were settled otherwise: {given["right"]} of '
                      f'{given["jobs"]} ({100 * given["right"] // max(1, given["jobs"])}%)' +
                      (f'; {waiting} jobs were settled for a person, and await one to confirm '
                       'them (qa_triage pending)' if waiting else '') + '.')
    print('\n'.join(out))
    return 0


def tracker_answers(args):
    """Write the answers the tracker's issues give, by linking to the jobs that are
    examples of them, for the projects and the time --projects and --since say."""
    found, read = {}, 0
    for project in args.projects.split(','):
        query = (f'project_id={urllib.parse.quote(project)}&status_id=*'
                 f'&updated_on=%3E%3D{args.since}&sort=updated_on:desc')
        try:
            issues = list(tracker_issues(query))
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            print(f'error: cannot list the issues of {project}: {e}', file=sys.stderr)
            return 2
        read += len(issues)
        print(f'{project}: {len(issues)} issues updated since {args.since}')
        for run, jobs in answers.harvest(
                issues, tracker_issue, lambda n: canonical(n, tracker_issue), say=err,
                cache=os.path.join(args.cache, 'tracker')).items():
            found.setdefault(run, {}).update(jobs)
    directory = args.out or os.path.join(args.cache, 'answers-tracker')
    written = answers.write(found, directory, 'the tracker')
    print(f'{sum(len(a) for a in found.values())} jobs in {len(written)} runs, from {read} '
          f'issues: {directory}; qa_triage run <run> --known <its file> learns from them')
    return 0


# What the analyst is asked about a record whose tracker issue has news.
REFRESH = ('People have written about this record\'s tracker issue since it was made: '
           'tracker-{n}.txt has the issue, its comments and its pull requests. What they '
           'wrote is the answer to learn from: what the problem really was, and how it was '
           'fixed. Bring the record in line with it: its notes should say the cause they '
           'found, and its confirm checks find that cause, not only its symptom; if it was '
           'fixed, its fixes should give the pull request and fixed_in, as the tracker '
           'does; if the issue duplicates another, the jobs belong to that one. If the tree '
           'stops at a symptom of it, teach the tree the cause. If the record says all that '
           'already, say that the result is right, and change nothing.')


def cmd_outcomes(args, problems):
    """Recheck the records whose tracker issues people have written on since they
    were last checked, and with --learn, have the analyst learn from what they
    wrote."""
    state = os.path.join(args.cache, 'outcomes.json')
    try:
        with open(state) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    analyst = None
    if args.learn:
        try:
            analyst = make_analyst(args, fallback=True)
        except ValueError as e:
            print(f'error: {e}', file=sys.stderr)
            return 2
        if not analyst or unready(analyst):
            print(f'error: {unready(analyst) if analyst else "no analyst"}', file=sys.stderr)
            return 2
    news = []
    for p in problems:
        try:
            issue = tracker_issue(p.id)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            print(f'{p.id}: cannot read its issue ({e})')
            continue
        before = '' if args.all else seen.get(p.id, '')
        if issue.get('updated_on', '') <= before:
            continue
        comments = [j for j in issue.get('journals', [])
                    if (j.get('notes') or '').strip() and j.get('created_on', '') > before]
        fields = {c['name']: c.get('value') for c in issue.get('custom_fields', [])}
        pulls = pull_numbers(issue)
        print(f'{p.id} {issue["status"]["name"]}: {len(comments)} comment'
              f'{"" if len(comments) == 1 else "s"}' +
              (f', fixed in {fields["Fixed In"]}' if fields.get('Fixed In') else '') +
              (f', pull request {", ".join(pulls)}' if pulls else '') + f': {p.title}')
        news.append((p, issue))
    if not args.learn:
        print(f'{len(news)} records have news; qa_triage outcomes --learn asks the analyst '
              'to learn from them')
        return 0
    runs, archives = {}, {}

    def jobs_for(run):
        if run not in runs:
            try:
                runs[run] = {j.id: j for j in list_jobs(run)}
            except urllib.error.URLError:
                runs[run] = {}
        return runs[run]

    def files_for(job):
        if job.path not in archives:
            archives[job.path] = files_of(args, job)
        return archives[job.path]

    patterns = args.corpus or [os.path.join(runs_dir(args), '*', 'results.json')]
    earlier = sorted({path for pattern in patterns for path in glob.glob(pattern)})
    corpus, history = load_corpus(earlier), load_history(earlier)
    for p, issue in news[:args.max]:
        job = None
        for example in p.examples:
            run, _, jid = example.rpartition('/')
            found = jobs_for(run).get(jid)
            if found and files_for(found).glob('teuthology.log'):
                job = found
                break
        if not job:
            print(f'{p.id}: none of its example jobs can be read: left as it is')
            continue
        target = canonical(p.id, tracker_issue)
        doubt = Doubt(job, files_for(job), args.problems, args.labels, analyst,
                      os.path.join(args.cache, 'outcomes', p.id), files_for, jobs_for,
                      tracker_issue, expected=target, why=REFRESH.format(n=target),
                      by=f'qa_triage ({analyst.name})', label_none=False, corpus=corpus,
                      search=tracker_search, source=source_of(args), nodes_dir=args.nodes,
                      history=history, budget=args.learn_budget, **approving(args))
        try:
            settled = doubt.run()
        except JudgeError as e:
            print(f'{p.id}: not settled: {e}')
            continue
        print(f'{p.id}: ' + (f'kept, wrote {", ".join(doubt.written)}' if doubt.written else
                             'nothing to change' if settled else 'not settled') +
              f' (cost {doubt.cost() or 0:.2f}); see {doubt.workdir}/analysis.md')
        seen[p.id] = issue.get('updated_on', '')
        os.makedirs(os.path.dirname(state), exist_ok=True)
        with open(state, 'w') as f:
            json.dump(seen, f, indent=1)
    return 0


def cmd_share(args, problems):
    """Propose what was learned to the shared library: see share.py."""
    directory = (share.local(args.settings['db'], args.cache) if args.settings.get('db')
                 else os.path.dirname(os.path.abspath(args.problems)))
    if not os.path.isdir(os.path.join(directory, '.git')):
        print(f'error: {directory} is not a git repository: the db setting names the shared '
              'library', file=sys.stderr)
        return 2
    found = share.changes(directory)
    if not found:
        print('Nothing to share: the library has no changes.')
        return 0
    print(f'{directory}: {share.summary(found)}:')
    print('\n'.join(f'  {path}' for paths in found.values() for path in paths))
    if not args.yes and sys.stdin.isatty():
        if input('Share them, for everyone to have once they are reviewed? [y/N] '
                 ).strip().lower()[:1] != 'y':
            print('Nothing shared.')
            return 0
    try:
        print(share.share(directory, args.message))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    return 0


def cmd_verify(args, problems):
    """Check that each record still matches every one of its example jobs, and
    that the tree still explains each node's examples. An example whose archive is
    gone is skipped."""
    failed = 0
    runs = {}

    def found(example):
        """(the example's Job, its files), or None if its archive is gone."""
        run, _, jid = example.rpartition('/')
        job = jobs_of(run, args.cache).get(jid, Job(run, jid, '', '', ''))
        try:
            files = files_of(args, job)
            return (job, files) if files.glob('teuthology.log') else None
        except ArchiveError:
            return None

    for p in problems:
        if args.ids and p.id not in args.ids:
            continue
        if not p.examples:
            print(f'{p.id}: no examples to verify')
        for example in p.examples:
            got = found(example)
            if not got:
                print(f'{p.id} {example}: skipped, its archive is gone')
                continue
            job, files = got
            r = Triage([p], deep=args.deep).run(job, files)
            ok = r.problem is p
            failed += not ok
            print(f'{p.id} {example}: {"ok" if ok else "FAILED"} ({" -> ".join(r.path)})')
            if not ok:
                print(''.join(f'    {line}\n' for line in misses(p, r)), end='')
    for job, expected, who in load_labels(args.labels):
        if args.ids and expected not in args.ids:
            continue
        r = Triage(problems, deep=args.deep).run(job, files_of(args, job))
        ok = placed(r) == expected
        failed += not ok
        print(f'{expected or "no record"} {job.path} ({who}): {"ok" if ok else "FAILED"} '
              f'({describe(r)})')
    for name, f in sorted(learned.FILES.items()):
        if args.ids and name not in args.ids:
            continue
        for example, path in f.examples.items():
            got = found(example)
            if not got:
                print(f'nodes/{name} {example}: skipped, its archive is gone')
                continue
            job, files = got
            finding = explain(job, files)
            uncited = [s for s in finding.uncited() if s.node in f.paths]
            ok = finding.conclusive and under(finding.where, path) and not uncited
            failed += not ok
            print(f'nodes/{name} {example}: {"ok" if ok else "FAILED"} ({finding.where or "-"}'
                  f', {"conclusive" if finding.conclusive else "inconclusive"}' +
                  ''.join(f'; at "{s.node}" it answered {s.answer} and cited no line'
                          for s in uncited) + ')')
    return 1 if failed else 0


def cmd_open(args, problems):
    """Open a file of a job's archive at a line, such as the report cites as
    file:line, in the editor (the editor setting): on the archive's host, unless
    edit_on is here. 1 if the file is not in the archive."""
    run, jid = job_name(args.job)
    job = Job(run, jid, '', '', '')
    files = files_of(args, job)
    rel, line = args.where, 1
    found = re.fullmatch(r'(.+):(\d+)', args.where)
    try:
        if found and not files.glob(args.where):
            rel, line = found.group(1), int(found.group(2))
        if not files.glob(rel):
            print(f'{rel}: not in the archive of {run}/{jid}; its files: '
                  f'qa_triage files {run}/{jid}', file=sys.stderr)
            return 1
        argv = editor.argv(files, job, rel, line, args.settings.get('editor'),
                           args.settings.get('edit_on') == 'here', args.cache)
    except ArchiveError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    try:
        return subprocess.run(argv).returncode
    except OSError as e:
        print(f'error: {argv[0]}: {e}', file=sys.stderr)
        return 2


# What files lists without a glob: the top of the archive and the daemons' logs.
LISTED = ('*', 'remote/*/log/*', 'remote/*/log/*/*')


def cmd_files(args, problems):
    """The files of a job's archive that a glob matches, with their sizes."""
    run, jid = job_name(args.job)
    files = files_of(args, Job(run, jid, '', '', ''))
    try:
        for pattern in [args.glob] if args.glob else LISTED:
            for rel, size in files.glob(pattern):
                print(f'{rel}  {size}')
    except ArchiveError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    return 0


# The most times --relearn teaches a lesson, each time with another of its jobs.
TRIES = 3


def cmd_teach(args, problems):
    """Teach the analyst what the tree and the records get wrong most often, against
    the answers people gave: one lesson for each kind of mistake, the commonest
    first, on one of its jobs, with the people's answer. A lesson's change is checked
    against every job triaged before, so each teaches all the jobs of its kind."""
    answers_dir = args.answers or os.path.join(args.cache, 'answers')
    runs_ = args.runs or runs_dir(args)
    try:
        analyst = make_analyst(args, fallback=True)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    if not analyst or unready(analyst):
        print(f'error: {unready(analyst) if analyst else "no analyst: give --analyst"}',
              file=sys.stderr)
        return 2
    first = {}

    def canonical_of(number):
        if number not in first:
            first[number] = canonical(number, tracker_issue)
        return first[number]

    earlier = sorted(glob.glob(os.path.join(runs_, '*', 'results.json')))
    corpus, history = load_corpus(earlier), load_history(earlier)
    # No lesson is taught from the runs held out of learning.
    holdout, held = held_out(args, corpus, canonical_of)
    triaged = [found for found in score.runs(answers_dir, runs_, settled_by_people(args.labels))
               if found[0] not in held]
    known = {p.id for p in problems} | {a for p in problems for a in p.also}
    lessons = score.curriculum(score.score(triaged, problems, canonical_of), known)
    # What a person, or another agent, would have the analyst know for a lesson:
    # {lesson: text}; a lesson with a hint is taught again, if it was taught before.
    hints = {}
    if args.hints:
        with open(args.hints) as f:
            hints = {str(k): str(v) for k, v in (yaml.safe_load(f) or {}).items()}
    # Several can teach at once, each its share of the lessons (--shard k/N), each
    # keeping what it taught in a file of its own, and skipping what any taught.
    shard, shards = (int(n) for n in (args.shard or '0/1').split('/'))
    state_file = os.path.join(args.cache, 'taught.json' if shards == 1 else
                              f'taught-{shard}.json')
    taught, before = {}, {}
    for path in glob.glob(os.path.join(args.cache, 'taught*.json')):
        with open(path) as f:
            (taught if path == state_file else before).update(json.load(f))
    runs, archives = {}, {}

    def jobs_for(run):
        if run not in runs:
            try:
                runs[run] = {j.id: j for j in list_jobs(run)}
            except urllib.error.URLError:
                runs[run] = {}
        return runs[run]

    def files_for(job):
        if job.path not in archives:
            archives[job.path] = files_of(args, job)
        return archives[job.path]

    print(f'{len(lessons)} kinds of mistake in {len(triaged)} runs; teaching at most '
          f'{args.most}, the commonest first.' +
          (f' {len(held)} runs are held out of learning.' if held else ''))
    done = 0
    for number, (said, answer, examples) in enumerate(lessons):
        key = f'{said or "new"}->{answer}'
        if done == args.most:
            break
        last = {**before, **taught}.get(key)
        if number % shards != shard or (last and key not in hints and (
                not args.relearn or last.get('tries', 1) >= TRIES)):
            continue
        # Taught again, it is taught with another of its jobs; and with the next one
        # whose logs the archive still has.
        tries = (last or {}).get('tries', 1 if last else 0)
        job = None
        for run, entry in examples[tries % len(examples):] + examples[:tries % len(examples)]:
            job = jobs_for(run).get(entry['job'])
            if job and files_for(job).glob('teuthology.log'):
                break
            job = None
        if not job:
            print(f'{key}: none of its {len(examples)} jobs has its logs in the archive')
            continue
        # A lesson before may have taught these jobs already: the records now say.
        if fits(Triage(db.load(args.problems)).run(job, files_for(job)), answer):
            print(f'{key}: learned already, by an earlier lesson')
            taught[key] = {'job': job.path, 'settled': True, 'jobs': len(examples),
                           'cost': 0, 'by': 'an earlier lesson'}
            continue
        done += 1
        print(f'{key}: {len(examples)} jobs; teaching with {run}/{job.id}')
        recorded = {k: entry.get(k) for k in ('verdict', 'problem', 'path', 'notes')}
        others = sorted({f'{r}/{e["job"]}' for r, e in examples} - {job.path})
        why = (f'People placed {len(examples)} jobs that the tool placed in '
               f'{said or "no known problem"} in {answer}, this one among them.'
               if answer in known else
               f'People placed {len(examples)} jobs in {answer}, this one among them, and no '
               'record names it yet: the tool placed them in ' +
               ', '.join(sorted({f'{answer}, by the memory of what people answered alone'
                                 if e.get('problem') == answer else
                                 e.get('problem') or 'no known problem' for _, e in examples})) +
               '. A record for it should place them all; others are ' +
               ', '.join(others[:5]) + '.')
        if key in hints:
            why += f' Doubts about how this was taught before, to weigh: {hints[key]}'
        doubt = Doubt(job, files_for(job), args.problems, args.labels, analyst,
                      os.path.join(args.cache, 'teach', key.replace('>', '')),
                      files_for, jobs_for, tracker_issue, expected=answer,
                      why=why,
                      by=f'{args.source}, via qa_triage ({analyst.name})', recorded=recorded,
                      corpus=corpus, search=tracker_search, label_none=False,
                      source=source_of(args), nodes_dir=args.nodes, history=history,
                      budget=args.learn_budget, want_sure=True, said_by_people=True,
                      holdout=holdout, **approving(args))
        try:
            settled = doubt.run()
        except JudgeError as e:
            settled = None
            print(f'{key}: {e}')
        held, disputed = getattr(doubt, 'held', None), getattr(doubt, 'disputed', None)
        taught[key] = {'job': job.path, 'settled': bool(settled), 'jobs': len(examples),
                       'cost': doubt.cost(), 'tries': tries + 1,
                       **({'held': held} if held else {}),
                       **({'disputed': disputed} if disputed else {}),
                       **({'kept': doubt.kept} if getattr(doubt, 'kept', None) else {})}
        with open(state_file, 'w') as f:
            json.dump(taught, f, indent=1)
        print(f'{key}: {"learned" if settled else "held for approval" if held else "not settled"}'
              f'; cost {doubt.cost()}')
    disputes = {k: t for k, t in taught.items() if t.get('disputed') and not t.get('settled')}
    if disputes:
        print(f'\nThe analyst disputes what people answered for {len(disputes)} lessons, with '
              'evidence that passed the checks; a person decides each, with qa_triage settle '
              '<job> <id> --why ..., which keeps the analyst\'s change if they agree, or '
              'discusses it with qa_triage doubt <job> --interactive:')
        for k, t in sorted(disputes.items()):
            print(f'  {k}: the analyst says {t["disputed"]}' +
                  (', and changed the library for it' if t.get('kept') else '') +
                  f': {t["job"]}')
    print('Triage the runs again, then qa_triage score, to see what was learned.')
    return 0


def cmd_doubt(args, problems):
    """Question the result for a job: an agent finds the cause, and fixes the
    records if the result was wrong."""
    args.corpus = args.corpus or [os.path.join(runs_dir(args), '*', 'results.json')]
    try:
        run, jid = job_name(args.job)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    runs, archives = {}, {}

    def jobs_for(run):
        if run not in runs:
            try:
                runs[run] = {j.id: j for j in list_jobs(run)}
            except urllib.error.URLError:
                runs[run] = {}
        return runs[run]

    def files_for(job):
        if job.path not in archives:
            archives[job.path] = files_of(args, job)
        return archives[job.path]

    job = jobs_for(run).get(jid)
    if not job:
        print(f'error: pulpito has no job {jid} in {run}', file=sys.stderr)
        return 2
    recorded = None
    results = args.results or os.path.join(runs_dir(args), run, 'results.json')
    if os.path.exists(results):
        with open(results) as f:
            entry = next((e for e in json.load(f) if e['job'] == jid), None)
        if entry:
            recorded = {k: entry[k] for k in ('verdict', 'problem', 'path', 'notes')}
            if entry.get('judge'):
                recorded['judge'] = {k: entry['judge'].get(k)
                                     for k in ('judge', 'verdict', 'problem', 'summary')}
    try:
        analyst = make_analyst(args, fallback=True)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    if not analyst:
        print('error: no analyst: give --analyst, or set analyst in a config file (see '
              'examples/)', file=sys.stderr)
        return 2
    if unready(analyst):
        print(f'error: {unready(analyst)}', file=sys.stderr)
        return 2
    earlier = sorted({path for pattern in args.corpus for path in glob.glob(pattern)})
    held = {}
    if args.proposal:
        # A change kept for approval is checked where it was made: its quotes are of
        # what the analyst asked for there, and who doubted the job, and why, stand.
        with open(args.proposal) as f:
            held = json.load(f)
        args.out = args.out or held.get('workdir')
        args.says = args.says or held.get('expected')
        args.why = args.why or held.get('why') or ''
        if held.get('by'):
            args.by = held['by']
    doubt = Doubt(job, files_for(job), args.problems, args.labels, analyst,
                  args.out or os.path.join(runs_dir(args), run, f'doubt-{jid}'),
                  files_for, jobs_for, tracker_issue, expected=args.says, why=args.why,
                  by=args.by, recorded=recorded, corpus=load_corpus(earlier),
                  interactive=args.interactive, write=not args.dry_run,
                  search=tracker_search, source=source_of(args), nodes_dir=args.nodes,
                  history=load_history(earlier),
                  said_by_people=not str(args.by).startswith('qa_triage'), firm=True,
                  holdout=held_out(args, load_corpus(earlier))[0], **approving(args))
    try:
        if args.proposal:
            # A change kept for approval: checked again, and asked about.
            # Its session, so that what the analyst asked for there is kept.
            doubt.session = held.get('session') or doubt.session
            settled, _ = doubt.settle(held)
            if settled:
                approve.approved(args.proposal, held)
        else:
            settled = doubt.run()
    except JudgeError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    if doubt.cost() is not None:
        print(f'Cost: analyst {analyst.name} {doubt.cost():.2f} {analyst.unit} in '
              f'{len(doubt.costs)} turn{"" if len(doubt.costs) == 1 else "s"}.')
    print(f'Analysis: {doubt.workdir}/analysis.md')
    return 0 if settled else 1


def cmd_settle(args, problems):
    """Say, as a person, what a job is: kept with the library's settled jobs, which no
    change may move, and which score, teach and remember take instead of the answers
    gathered from the wiki and the tracker. The records must place the job so already:
    if they do not, doubt it, for the analyst to change them."""
    try:
        run, jid = job_name(args.job)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    job = {j.id: j for j in list_jobs(run)}.get(jid)
    if not job:
        print(f'error: pulpito has no job {jid} in {run}', file=sys.stderr)
        return 2
    want = problem_id(args.problem)
    r = Triage(problems).run(job, files_of(args, job))
    # The change the analyst made when it disputed the people's answer, if it did.
    path = approve.kept(approve.disputed(args.cache), job) \
        if getattr(args, 'cache', None) else ''
    held = None
    if path and os.path.exists(path):
        with open(path) as f:
            held = json.load(f)
    if placed(r) != want and held and problem_id(held.get('problem')) == want:
        # The person agrees with the analyst: its change is checked again and kept.
        if args.dry_run:
            print(f'{job.path}: would keep the analyst\'s change ({path}) and settle it in '
                  f'{want or "no record"} by {args.by} (not written: --dry-run)')
            return 0
        print(f'{job.path}: keeping the analyst\'s change, which placed it in '
              f'{want or "no record"}')
        if cmd_doubt(argparse.Namespace(**dict(
                vars(args), proposal=path, says=args.problem, interactive=False,
                analyst=None, results=None, corpus=getattr(args, 'corpus', None),
                out=None)), problems):
            return 1
        if args.nodes:
            learned.load(args.nodes)
        r = Triage(db.load(args.problems)).run(job, files_of(args, job))
    if placed(r) != want:
        print(f'error: the records place {job.path} in {placed(r) or "no record"}, not in '
              f'{want or "no record"}: doubt it, with --says, for the analyst to change them',
              file=sys.stderr)
        return 1
    if not args.dry_run:
        save_label(args.labels, job, want, args.by, args.why, describe(r),
                   confirmed=not getattr(args, 'unconfirmed', False))
        if path and os.path.exists(path):
            # The analyst's change answered otherwise: the person decided against it.
            os.remove(path)
    print(f'{job.path}: settled in {want or "no record"} by {args.by}' +
          (', for a person to confirm' if getattr(args, 'unconfirmed', False) else '') +
          (' (not written: --dry-run)' if args.dry_run else ''))
    return 0


# The tracker's statuses of an issue that is no longer worked on.
CLOSED = ('Resolved', 'Closed', 'Rejected', "Won't Fix", "Can't reproduce", 'Duplicate')


def cmd_stale(args, problems):
    """The records that may have had their day, for a person to retire, or to keep
    to catch the problem if it comes back; nothing is changed. A record is stale if
    its tracker issue is closed, or its PR merged, and it placed no job of the runs
    triaged in the last --days days; one whose issue duplicates another should be the
    other's; and one that placed no job of them at all, and has no example, has never
    been seen to work."""
    patterns = args.corpus or [os.path.join(runs_dir(args), '*', 'results.json')]
    seen = {}
    for path in sorted({p for pattern in patterns for p in glob.glob(pattern)}):
        with open(path) as f:
            for e in json.load(f):
                day = memory.run_date(e.get('run') or os.path.basename(os.path.dirname(path)))
                if e.get('problem') and day:
                    seen[e['problem']] = max(day, seen.get(e['problem'], day))
    today, found = datetime.date.today(), []
    for p in sorted(problems, key=lambda p: p.id):
        last = seen.get(p.id)
        since = f'last placed a job of a run of {last}' if last else 'placed no job'
        try:
            if db.is_pr(p.id):
                pr = pull_request(p.id[2:])
                state = 'merged' if pr['merged'] else pr['state']
                done = pr['merged'] or pr['state'] == 'closed'
            else:
                issue = tracker_issue(p.id)
                state, done = issue['status']['name'], issue['status']['name'] in CLOSED
                other = original(issue)
                if other:
                    found.append((0, p, f'its issue is {other[0]} of {other[1] or "another"}: '
                                        'the record should be of that one'))
                    continue
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            print(f'{p.label}: could not be read ({e})', file=sys.stderr)
            continue
        if done and (not last or (today - last).days > args.days):
            found.append((1, p, f'{state} on the tracker, and it {since}: retire it, or keep '
                                'it to catch the problem if it comes back'))
        elif not last and not p.examples:
            found.append((2, p, f'{state}; it placed no job, and has no example'))
    if not found:
        print(f'No record of the {len(problems)} looks stale.')
        return 0
    print(f'{len(found)} of the {len(problems)} records may have had their day (their '
          f'tracker issues, and the runs triaged in {", ".join(patterns)}):')
    for _, p, why in sorted(found, key=lambda item: (item[0], item[1].id)):
        print(f'  {p.label}: {short(p.title, 80)}\n      {why}')
    return 0


def cmd_checkout(args, problems):
    """Check out the source a job ran, or the jobs of a run, in worktrees of the
    ceph clone; print where."""
    if not args.git:
        err('error: no ceph clone to check out from: set git in a config file')
        return 2
    try:
        run, jid = job_name(args.job)
        jobs = [Job(run, jid, '', '', '')]
    except ValueError:
        jobs = list_jobs(run_name(args.job))
    for job in jobs:
        found = versions(files_of(args, job))
        if found:
            break
    else:
        err(f'error: no config of {args.job} names the commit it ran')
        return 1
    for name, (sha1, url) in found.items():
        try:
            print(f'{name}: {worktree(args.git, sha1, url, args.worktrees, err)}')
        except CheckoutError as e:
            err(f'error: {e}')
            return 1
    return 0


def cmd_paths(args, problems):
    """Walk each job of the library through the tree, and keep where it went in
    paths/, beside labels/: the settled jobs, and the examples of the records and of
    the nodes. A lesson's checks read there which jobs its change could reach,
    rather than walking them all; each lesson keeps the paths of those it walks, and
    this writes them all again, as after a change to the tree's engine."""
    jobs = {job.path: job for job in load_corpus(glob.glob(os.path.join(runs_dir(args), '*',
                                                                       'results.json')))}
    library = {job.path: jobs.get(job.path, job) for job, _, _ in load_labels(args.labels)}
    runs = {}
    for example in [e for p in problems for e in p.examples] + \
            [e for f in learned.FILES.values() for e in f.examples]:
        if example in library:
            continue
        run, _, jid = example.rpartition('/')
        if example not in jobs and run not in runs:
            try:
                runs[run] = {j.id: j for j in list_jobs(run)}
            except urllib.error.URLError:
                runs[run] = {}
        library[example] = jobs.get(example) or runs.get(run, {}).get(jid) or \
            Job(run, jid, '', '', '')
    if https_base(args.archive):
        print(HTTPS_SLOW)
    print(f'Walking the {len(library)} jobs of the library through the tree...', flush=True)

    def walk(job):
        try:
            files = files_of(args, job)
            return explain(job, files) if files.glob('teuthology.log') else None
        except ArchiveError:
            return None

    found, gone = {}, 0
    with concurrent.futures.ThreadPoolExecutor(AT_ONCE) as pool:
        for n, (path, finding) in enumerate(zip(library, pool.map(walk, library.values())), 1):
            if finding is None:
                gone += 1
            else:
                found[path] = finding
            if n % 50 == 0:
                print(f'  {n} of {len(library)}', flush=True)
    directory = paths_dir(args.labels)
    written = save_paths(directory, found, fresh=True)
    print(f'{len(found)} paths in {directory}, {len(written)} files changed' +
          (f'; {gone} jobs left out, their archives gone' if gone else ''))


def cmd_lint(args, problems):
    """Check the library, as a change to it is checked before it is merged: the
    records, the tree's nodes, the settled jobs and the memory. 1 if any of
    them is wrong."""
    # A job may be settled in a record's issue, or in one a record lists under also,
    # which a job placed in the record is right for too.
    wrong, ids = [], {p.id for p in problems} | {str(a) for p in problems for a in p.also}
    print(f'{len(problems)} problem records in {args.problems} are valid')
    names = sorted(n for n in os.listdir(args.nodes) if n.endswith('.py')) \
        if args.nodes and os.path.isdir(args.nodes) else []
    for name in names:
        with open(os.path.join(args.nodes, name)) as f:
            code = f.read()
        try:
            with learned.trying({name: code}):
                pass
        except learned.LearnedError as e:
            wrong.append(f'nodes/{e}' if str(e).startswith(name) else f'nodes/{name}: {e}')
    settled = 0
    for name, data in _yaml_files(args.labels, wrong, 'labels'):
        if not isinstance(data.get('run'), str) or not isinstance(data.get('jobs'), dict):
            wrong.append(f'labels/{name}: give run: <run> and jobs: {{<job>: {{problem: ...}}}}')
            continue
        for jid, e in data['jobs'].items():
            settled += 1
            problem = e.get('problem') if isinstance(e, dict) else None
            if not isinstance(e, dict) or (problem is not None and str(problem) not in ids):
                wrong.append(f'labels/{name}: job {jid} is settled in {problem}, which no '
                             'record is, or lists under also' if isinstance(e, dict) else
                             f'labels/{name}: job {jid}: not a mapping')
    remembered = 0
    for name, data in _yaml_files(memory_dir(args), wrong, 'memory'):
        signatures = data.get('signatures')
        if not isinstance(data.get('run'), str) or not isinstance(signatures, dict) or not all(
                isinstance(a, dict) and all(isinstance(n, int) for n in a.values())
                for a in signatures.values()):
            wrong.append(f'memory/{name}: give run: <run> and signatures: '
                         '{<signature>: {<issue>: <jobs>}}')
            continue
        remembered += 1
    print(f'{len(names)} node files, {settled} settled jobs and the memory of {remembered} '
          'runs: ' + (f'{len(wrong)} wrong:' if wrong else 'valid'))
    print(''.join(f'  {line}\n' for line in wrong), end='')
    # Not wrong, but slow: a lesson walks the jobs again, as it would without them.
    stale = []
    if args.labels:
        load_paths(paths_dir(args.labels), stale)
    if stale:
        print(f'The tree paths of {len(stale)} runs were found by other tree code: '
              'qa_triage paths walks their jobs again')
    return 1 if wrong else 0


def _yaml_files(directory, wrong, part):
    """(name, data) of each YAML file in directory that loads as a mapping; what does
    not is added to wrong."""
    if not directory or not os.path.isdir(directory):
        return
    for name in sorted(n for n in os.listdir(directory) if n.endswith('.yaml')):
        try:
            with open(os.path.join(directory, name)) as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            wrong.append(f'{part}/{name}: {e}')
            continue
        if isinstance(data, dict):
            yield name, data
        else:
            wrong.append(f'{part}/{name}: not a mapping')


def cmd_config(args, problems):
    """Show the settings, where they came from, and the agents they give."""
    print('# config files read: ' + (', '.join(args.config_files) or 'none'))
    # As the options leave them.
    settings = dict(args.settings, **{key: getattr(args, key) for key in OPTIONS
                                      if getattr(args, key, None) is not None})
    print(yaml.safe_dump(settings, sort_keys=False, width=1000), end='')
    try:
        judge = make_judge(args.judge, args.settings['agents'])
        analyst = make_analyst(args)
    except ValueError as e:
        print(f'# error: {e}')
        return 2
    print(f'# judge: {judge.name if judge else "none"}')
    print(f'# analyst: {analyst.name if analyst else "none"}')
    print(f'# providers: {", ".join(PROVIDERS)}')
    return 0


def cmd_mcp(args, problems):
    mcp.serve(args.directory)
    return 0


# Settings that options of the same name override; None means not given.
# The options of qa_triage itself that take a value, before the command.
VALUED = ('--config', '--set', '--problems', '--nodes', '--archive', '--cache', '--labels')


def with_command(argv, commands, valued=()):
    """argv, with review put after qa_triage's own options when what follows is not
    a command but a run's URL, with review's options before it or after: so that
    qa_triage <a run's pulpito URL> reviews the run. valued are review's options
    that take a value."""
    i = 0
    while i < len(argv) and argv[i] in VALUED:
        i += 2
    if i == len(argv) or argv[i] in commands or argv[i] in ('-h', '--help'):
        return argv
    j = i
    while j < len(argv) and argv[j].startswith('-'):
        j += 2 if argv[j] in valued else 1
    return argv[:i] + ['review'] + argv[i:] if j < len(argv) else argv


OPTIONS = ('problems', 'nodes', 'labels', 'archive', 'cache', 'git', 'worktrees', 'deep',
           'corpus', 'judge', 'analyst', 'learn_max', 'learn_budget', 'lessons_at_once',
           'judges_at_once')


def at_once(value):
    """How many at once, as --lessons-at-once and --judges-at-once give it: a number
    from 1, or all."""
    if value == 'all':
        return value
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f'a number from 1, or all, not {value!r}')
    return int(value)


ARCHIVE_HELP = ('where the job files are: an ssh host that has the archive and the '
                'directory of the runs there (default: %s); or https to download them from '
                'qa-proxy, or the https URL of another server\'s copy of the runs' %
                config.DEFAULTS['archive'])


def run_options(p):
    """The options that run and review share."""
    p.add_argument('run', help='run name, or a pulpito or qa-proxy URL of the run, or a '
                               'branch, or a tracker issue\'s URL, of a QA batch or one that '
                               'links to a run')
    p.add_argument('--jobs', help='comma-separated job ids (default: all failed and dead)')
    p.add_argument('--status', default='fail,dead', help='job statuses to triage')
    p.add_argument('--judge',
                   help='the agent for the jobs the tree and the records left unmatched, or '
                        'several separated by commas, each asked only if the one before is '
                        'unsure; none for no AI (default: %s)' % config.DEFAULTS['judge'])
    p.add_argument('--analyst',
                   help='the agent that then looks at the jobs the tree and the records '
                        'could not decide, and teaches the tree, or changes the records, so '
                        'that they decide jobs like those by themselves (default: none, or '
                        'the config\'s analyst)')
    p.add_argument('--learn-max', type=int,
                   help='the most groups of jobs to learn from in one run (default: 5)')
    p.add_argument('--known',
                   help='a YAML file of the right answers for some of the run\'s jobs, '
                        'such as the reviewers\': job id to problem id. The report compares '
                        'the results with them, and the analyst learns first from the jobs '
                        'the tool gets wrong')
    p.add_argument('--check', nargs='?', const='shown', choices=('shown', 'blind'),
                   help='also ask the judge to check every group the tree and the records '
                        'decided, as the review\'s judge all does: shown what they decided, '
                        'or, blind, not told, so that it decides each job afresh; with '
                        '--known, the report says how many of their wrong answers it caught')
    p.add_argument('--learn-budget', type=float,
                   help='the most one lesson may cost, in the analyst\'s own unit: then it is '
                        'asked no more, and the lesson is left for a person (default: none)')
    p.add_argument('--lessons-at-once', type=at_once,
                   help='how many lessons ask their analysts at once, or all; each change is '
                        'still checked and kept one at a time (default: 1)')
    p.add_argument('--judges-at-once', type=at_once,
                   help='how many groups of jobs ask the judge at once, or all (default: '
                        '%s)' % config.DEFAULTS['judges_at_once'])
    p.add_argument('--relearn', action='store_true',
                   help='teach again what an earlier run\'s lessons (next to --out) '
                        'already tried')
    p.add_argument('--deep', action='store_true', default=None,
                   help='also run the records\' deep checks (big daemon logs)')
    p.add_argument('--git', help='a ceph git clone, to tell whether fixes are in a build')
    p.add_argument('--archive', default=argparse.SUPPRESS, help=ARCHIVE_HELP)
    p.add_argument('--https', dest='archive', action='store_const', const='https',
                   default=argparse.SUPPRESS,
                   help=f'download the job files from qa-proxy ({QA_PROXY}) over https, '
                        'which is much slower than searching them where they are, over ssh: '
                        'for when the archive\'s host cannot be reached, as off the sepia VPN')
    p.add_argument('--qa-tracker',
                   help='the QA tracker issue (number or URL) that lists the PRs under '
                        'test, or "none" (default: look it up by the run\'s branch)')
    p.add_argument('--corpus', action='append',
                   help='as for doubt (default: the results.json files next to --out)')
    p.add_argument('--out', help='output directory (default: runs/<run> in the cache, '
                                 '~/.cache/qa_triage)')
    p.add_argument('-q', '--quiet', action='store_true',
                   help='print only the summary, not what the tree, the records, the judge '
                        'and the analyst decided about each job')


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='qa_triage', usage='%(prog)s [options] <run URL>, or %(prog)s [options] '
                                '<command> ...', description=__doc__,
        epilog='Settings come from config files (see examples/), and options override '
               'them. Agents: an agent named in the config, or ' + SPECS + '.')
    parser.add_argument('--config', action='append',
                        help='a config file to read instead of ~/.config/qa_triage/'
                             'config.yaml and ./qa_triage.yaml; can be given more than once '
                             '(default: $QA_TRIAGE_CONFIG, if set)')
    parser.add_argument('--set', action='append', default=[], metavar='KEY=VALUE',
                        help='override a setting, e.g. --set agents.gemma.url=http://h:11434 '
                             'or --set "judge=[gemma, haiku]"; the value is YAML')
    parser.add_argument('--problems', help='the problem database directory')
    parser.add_argument('--nodes', help='the directory of the tree\'s nodes')
    parser.add_argument('--archive', help=ARCHIVE_HELP)
    parser.add_argument('--cache', help='where downloaded job files are kept')
    parser.add_argument('--labels', help='the settled jobs, which every change to the '
                                         'records is checked against')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('run', help='triage the failed jobs of a run')
    run_options(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser('review', help='work through a run on a full screen: the tree and '
                                      'the records triage it, and the judge and the analyst '
                                      'are asked only when you say')
    run_options(p)
    p.add_argument('--plain', action='store_true',
                   help='a prompt to type commands at, instead of the full screen')
    p.set_defaults(func=cmd_review)
    # Its options that take a value, which may come before the run's URL.
    review_valued = {option for action in p._actions if action.option_strings and
                     action.nargs != 0 for option in action.option_strings}

    p = sub.add_parser('watch', help='triage several runs with the tree and the records, '
                                     'no AI, and list the failures they have in common, '
                                     'the commonest first: by default, the suite\'s runs of '
                                     'the base branch, and of its QA batches, of the last days')
    p.add_argument('runs', nargs='*',
                   help='the runs: their names or pulpito URLs, branches, or tracker issues '
                        '(default: the suite\'s runs of --base, and of the QA batches for it, '
                        'between the dates)')
    p.add_argument('--since', help='the first day of the runs to find, as 2026-10-29 or '
                                   '10-29 (default: --days before --until)')
    p.add_argument('--until', help='the last day of the runs to find (default: today)')
    p.add_argument('--days', type=int,
                   help='how many days back to find runs, with no --since (default: %s); '
                        'the screen keeps the dates, and D changes them' % watch.DAYS)
    p.add_argument('--base', default='main', help='the base branch (default: %(default)s)')
    p.add_argument('--suite', default='rados', help='the suite (default: %(default)s)')
    p.add_argument('--prs', help='watch the runs of PRs instead, of any suite: mine, the '
                                 'PRs you opened, or their numbers, as 73070,73100, for '
                                 'this watch: without it, watch takes the suite\'s runs. '
                                 'Their runs are those of the QA batches whose QA '
                                 'tracker lists them, and the runs of yours, or of a '
                                 'branch named as a PR\'s, whose build has their commits, '
                                 'or commits that name them')
    p.add_argument('--user', help='with --prs, your teuthology user names, comma '
                                  'separated, whose runs are yours (default: your GitHub '
                                  'login and your user name here); the screen keeps them')
    p.add_argument('--most', type=int, default=watch.MOST,
                   help='the most runs to take, the newest first (default: %(default)s)')
    p.add_argument('--least', type=int, default=watch.LEAST,
                   help='the fewest jobs of a run it takes by itself: one with fewer is a '
                        'part of the suite (default: %(default)s)')
    p.add_argument('--top', type=int, default=watch.TOP,
                   help='how many failures to list (default: %(default)s); the report has '
                        'them all')
    p.add_argument('--status', default='fail,dead', help='job statuses to triage')
    p.add_argument('--deep', action='store_true', default=None,
                   help='also run the records\' deep checks (big daemon logs)')
    p.add_argument('--out', help='where to write the report (default: watch/<when> in the '
                                 'cache)')
    p.add_argument('--plain', action='store_true',
                   help='print the failures, instead of showing the screen')
    p.add_argument('-q', '--quiet', action='store_true',
                   help='with --plain, or no terminal, print only the failures, not how far '
                        'it has got')
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser('verify', help='check records and nodes against their '
                                      'example jobs, and the settled jobs')
    p.add_argument('ids', nargs='*', help='problem ids or node files (default: all)')
    p.add_argument('--deep', action='store_true', default=None,
                   help='also run the deep checks')
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('open', help='open a file of a job\'s archive at a line the report '
                                    'cites, such as teuthology.log:1234, in your editor (the '
                                    'editor setting): to check a result, at no cost')
    p.add_argument('job', help='<run>/<job>, or a pulpito or qa-proxy URL of the job')
    p.add_argument('where', nargs='?', default='teuthology.log',
                   help='a file of the archive, or file:line (default: teuthology.log)')
    p.set_defaults(func=cmd_open)

    p = sub.add_parser('files', help='the files of a job\'s archive, with their sizes')
    p.add_argument('job', help='<run>/<job>, or a pulpito or qa-proxy URL of the job')
    p.add_argument('glob', nargs='?',
                   help='which files, such as remote/*/log/ceph-osd.*: * matches within one '
                        'part of a path (default: the top of the archive and the daemons\' '
                        'logs)')
    p.set_defaults(func=cmd_files)

    p = sub.add_parser('doubt', help='question the result for a job: an agent finds '
                                     'the cause, and fixes the records if it was wrong')
    p.add_argument('job', help='<run>/<job>, or a pulpito or qa-proxy URL of the job')
    p.add_argument('--says', help='the answer you expect: a problem id (its tracker '
                                  'number), or "new" for none')
    p.add_argument('--why', default='', help='why you doubt the result')
    p.add_argument('--by', default=getpass.getuser(),
                   help='who doubts it, a person or an agent (default: %(default)s)')
    p.add_argument('--analyst',
                   help='the agent that finds the cause; it reads the evidence files '
                        '(default: the config\'s analyst, or else the last of its judge\'s '
                        'agents)')
    p.add_argument('-i', '--interactive', action='store_true',
                   help='discuss each answer with the analyst before accepting it')
    p.add_argument('--dry-run', action='store_true',
                   help='write no records or labels')
    p.add_argument('--proposal',
                   help='a change kept for approval (qa_triage pending lists them): check it '
                        'again, and ask whether to keep it, instead of asking the analyst')
    p.add_argument('--results', help='the run\'s results.json (default: '
                                     'runs/<run>/results.json in the cache, if it exists)')
    p.add_argument('--corpus', action='append',
                   help='a glob of results.json files of earlier triage runs, whose jobs '
                        'a change must not claim unless they failed the same way; can be '
                        'given more than once (default: runs/*/results.json in the '
                        'cache)')
    p.add_argument('--out', help='evidence directory (default: runs/<run>/doubt-<job> in '
                                 'the cache)')
    p.set_defaults(func=cmd_doubt)

    p = sub.add_parser('settle', help='say, as a person, what a job is, as the records '
                                      'place it: no change may move it, and scores take it '
                                      'over the wiki\'s and the tracker\'s answers')
    p.add_argument('job', help='<run>/<job>, or a pulpito or qa-proxy URL of the job')
    p.add_argument('problem', help='the problem id (its tracker number), or "new" for none')
    p.add_argument('--why', required=True, help='why: what shows it')
    p.add_argument('--by', default=getpass.getuser(),
                   help='who settles it, a person (default: %(default)s)')
    p.add_argument('--unconfirmed', action='store_true',
                   help='it is settled for a person, such as by an assistant, and waits for '
                        'one to confirm it, by settling it again: qa_triage pending lists '
                        'such jobs, and score says what they change')
    p.add_argument('--dry-run', action='store_true', help='write no label')
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser('checkout', help='check out the source a job ran in a worktree '
                                        'of the ceph clone, and print where')
    p.add_argument('job', help='<run>/<job> or <run>, or a pulpito or qa-proxy URL')
    p.set_defaults(func=cmd_checkout)

    p = sub.add_parser('setup', help='a page to choose the settings and keys, kept in your '
                                     'config file; offered the first time qa_triage runs')
    p.add_argument('--auto', action='store_true',
                   help='set up what can be set up from what this machine has, with no page, '
                        'and save; it chooses no AI agent')
    p.set_defaults(func=None)

    p = sub.add_parser('answers', help='write the answers people gave for the failed jobs of '
                                       'the runs on the RADOS wiki of reviewed runs, for '
                                       'run --known')
    p.add_argument('--project', default='rados', help='the tracker project of the wiki')
    p.add_argument('--page', default='MAIN', help='its page (default: %(default)s)')
    p.add_argument('--tracker', action='store_true',
                   help='instead, the answers the tracker\'s issues give: each job one links '
                        'to, as an example of it, is placed in it')
    p.add_argument('--projects', default='rados,orchestrator,infrastructure',
                   help='with --tracker, the projects whose issues are read (default: '
                        '%(default)s)')
    p.add_argument('--since', default='2026-03-01',
                   help='with --tracker, read the issues updated since this date (default: '
                        '%(default)s)')
    p.add_argument('--out', help='where to write them (default: answers in the cache, or '
                                 'answers-tracker with --tracker)')
    p.set_defaults(func=cmd_answers)

    p = sub.add_parser('score', help='how often the tree and the records gave the answers '
                                     'people gave, for the runs triaged before, and where '
                                     'learning would gain most')
    p.add_argument('--answers', help='the answers, as qa_triage answers writes them (default: '
                                     'answers in the cache)')
    p.add_argument('--runs', help='the directory of the triaged runs (default: runs in the '
                                  'cache)')
    p.add_argument('--most', type=int, default=10, help='how many of each list to show')
    p.set_defaults(func=cmd_score)

    p = sub.add_parser('pending', help='the changes to the records and the tree that wait '
                                       'for your approval, and how to approve each')
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser('remember', help='write the library\'s memory: what people placed '
                                        'each way a job failed in, from the runs triaged '
                                        'with their answers, for triage to use with no '
                                        'judge')
    p.add_argument('--answers', help='the answers, as qa_triage answers writes them (default: '
                                     'answers in the cache)')
    p.add_argument('--runs', help='the directory of the triaged runs (default: runs in the '
                                  'cache)')
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser('teach', help='teach the analyst the mistakes of the runs triaged '
                                     'before, against the answers people gave, the '
                                     'commonest first: one lesson for each kind')
    p.add_argument('--answers', help='the answers, as qa_triage answers writes them (default: '
                                     'answers in the cache)')
    p.add_argument('--runs', help='the directory of the triaged runs (default: runs in the '
                                  'cache)')
    p.add_argument('--most', type=int, default=10, help='the most lessons to teach')
    p.add_argument('--source', default='the QA reviewers',
                   help='who gave the answers, for the settled jobs (default: %(default)s)')
    p.add_argument('--analyst', help='the agent that learns (default: the config\'s)')
    p.add_argument('--learn-budget', type=float,
                   help='the most one lesson may cost, in the analyst\'s unit')
    p.add_argument('--relearn', action='store_true',
                   help=f'teach again what was not learned before, with another of its jobs, '
                        f'up to {TRIES} times')
    p.add_argument('--shard', help='k/N: teach only the lessons whose place in the list is k '
                                   'modulo N, for N teachers at once')
    p.add_argument('--hints', help='a YAML file of {lesson: what the analyst should know}, '
                                   'such as doubts about how it was taught before; a lesson '
                                   'with one is taught again')
    p.set_defaults(func=cmd_teach)

    p = sub.add_parser('outcomes', help='recheck the records whose tracker issues people '
                                        'have written on since: comments, fixes, pull '
                                        'requests; with --learn, the analyst learns from them')
    p.add_argument('--learn', action='store_true',
                   help='ask the analyst to bring each record in line with what was written')
    p.add_argument('--all', action='store_true', help='every record, news or not')
    p.add_argument('--max', type=int, default=10, help='the most records to learn from')
    p.add_argument('--analyst', help='as for run')
    p.add_argument('--corpus', action='append', help='as for doubt')
    p.set_defaults(func=cmd_outcomes)

    p = sub.add_parser('share', help='propose what was learned, the records, nodes, settled '
                                     'jobs and scripts, to the shared library (the db setting)')
    p.add_argument('-m', '--message', help='what the change is (default: what changed)')
    p.add_argument('--yes', action='store_true', help='share without asking first')
    p.set_defaults(func=cmd_share)

    p = sub.add_parser('lint', help='check the library: the records, the nodes, the '
                                    'settled jobs and the memory')
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser('paths', help='walk each job of the library through the tree, and '
                                     'keep where it went, for lessons to check their changes '
                                     'without walking them all again')
    p.set_defaults(func=cmd_paths)

    p = sub.add_parser('stale', help='the records that may have had their day: their issue '
                                     'closed and no job placed lately, or never seen to work; '
                                     'for a person to retire or keep')
    p.add_argument('--days', type=int, default=90,
                   help='how lately a record must have placed a job, for its closed issue not '
                        'to make it stale (default: %(default)s)')
    p.add_argument('--corpus', action='append',
                   help='a glob of results.json files of runs triaged before (default: '
                        'runs/*/results.json in the cache)')
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser('config', help='show the settings, the config files they came '
                                      'from, and the judge and analyst they give')
    p.add_argument('--judge', help='as for run')
    p.add_argument('--analyst', help='as for run')
    p.set_defaults(func=cmd_config)

    p = sub.add_parser('mcp', help='serve the read-only evidence tools over MCP on stdin '
                                   'and stdout, for agents such as goose')
    p.add_argument('directory', help='the evidence directory')
    p.set_defaults(func=cmd_mcp)

    args = parser.parse_args(with_command(sys.argv[1:] if argv is None else argv,
                                          sub.choices, review_valued))
    if args.command == 'mcp':
        return cmd_mcp(args, None)
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    if args.command == 'setup' and args.auto:
        setup.automatically()
        return 0
    if args.command == 'setup' or (args.command in ('run', 'review') and terminal and
                                   not args.config and not config.files()):
        if not terminal:
            print('error: setup needs a terminal', file=sys.stderr)
            return 2
        if args.command != 'setup':
            print('qa_triage has no settings yet: the setup page helps you choose them '
                  '(qa_triage setup opens it again).', file=sys.stderr)
        saved = setup.run()
        if args.command == 'setup':
            print(f'Saved to {config.user_file()}.' if saved else 'Nothing saved.')
            return 0
    try:
        args.settings, args.config_files = config.load(args.config, args.set)
    except (ValueError, OSError, yaml.YAMLError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    for key in OPTIONS:
        if getattr(args, key, None) is None:
            setattr(args, key, args.settings[key])
    # With a key, the tracker is read as its owner: issues it may see that others
    # may not, and the wiki.
    try:
        use_tracker_key(tracker_key(args.settings.get('tracker_key')))
    except (OSError, ValueError) as e:
        print(f'Not using the tracker key: {e}', file=sys.stderr)
    # And GitHub as theirs, where gh is not signed in.
    try:
        use_github_key(github_token(args.settings.get('github_key')))
    except (OSError, ValueError) as e:
        print(f'Not using the GitHub token: {e}', file=sys.stderr)
    args.library = None
    if args.settings.get('db') and args.command not in ('config', 'setup'):
        # The shared library's parts, unless the settings name others.
        try:
            with slowly(f'Bringing the library {args.settings["db"]} up to date...'):
                library = args.library = share.update(args.settings['db'], args.cache,
                                                      say=err)
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            print(f'error: cannot take the library {args.settings["db"]}: {e}', file=sys.stderr)
            return 2
        for part in share.PARTS:
            if getattr(args, part) == config.DEFAULTS[part]:
                setattr(args, part, os.path.join(library, part))
    if args.command == 'config':
        return cmd_config(args, None)
    try:
        check_archive(args.archive)
        problems = db.load(args.problems)
        if os.path.exists(precision_file(args)):
            with open(precision_file(args)) as f:
                report.PRECISION.update(yaml.safe_load(f) or {})
        if args.command != 'lint':
            # lint loads each by itself, to say what is wrong with every one.
            learned.load(args.nodes)
            if '' not in NODES:
                raise ValueError(f'{args.nodes} has no root.py, the first node of the tree: '
                                 'the nodes setting must name the nodes/ of a library, '
                                 'which holds the whole tree')
        if args.command in ('run', 'review'):
            with slowly(f'Reading {args.run}, for its run...'):
                args.run = the_run(args.run, choose_run)
        if args.command in READS_ARCHIVE or (args.command == 'outcomes' and args.learn):
            why = unreachable(args)
            if why:
                raise ValueError(why)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    try:
        return args.func(args, problems)
    except ArchiveError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
