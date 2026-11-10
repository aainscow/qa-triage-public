"""The decision graph that triages one failed job.

    explain: the tree (tree/) explains the job, and says whether that is conclusive
        |
    prefilter --(no record fits the job)-----------> explained
        |
     confirm --(no record's checks all pass)-------> explained
        |------(several records' checks pass)------> agent, as ambiguous
        |
    fix-check --(the fix is in this build, or is a PR under test)--> seen-after-fix
        |
     duplicate

    explained --(the explanation is conclusive, and no record nearly fits)--> new
        |
      agent --(no judge)--> new, or ambiguous, unsure
        |
    verify-quotes --(the judge says new, or is unsure, or a quote is not in the
        |              evidence)--> new, unsure
        |
    fix-check --> probable-duplicate (or seen-after-fix)

A result is sure if the tree and the records decided it, or the judge placed it in
a record; an unsure one is what an analyst can look at, if asked. A judge's verdict is kept, by
the evidence it was given, and given again for the same evidence instead of asking
again. Escalated verdicts
(ambiguous, seen-after-fix, new) need a person; the others are duplicates of a
known problem.
"""

import copy
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field

import yaml

from . import baseline, memory, pack, tree
from .agents import JudgeError, Stopped, readable
from .checkout import (SHA1, CheckoutError, fetch, naming_each, touches, under_test,
                       under_test_text, versions)
from .db import is_pr, label, record_id
from .fingerprint import fingerprint
from .fixcheck import VERSION, applies, fix_in_build, parse_version, pr_number
from .sources import ArchiveError, Job
from .tree.common import short

VERDICTS = {
    'duplicate': 'a known problem: all of its record\'s checks matched',
    'probable-duplicate': 'a known problem according to the judge, whose quoted '
                          'evidence was verified',
    'seen-after-fix': 'a known problem whose fix is already in this build, or is '
                      'one of the PRs under test',
    'ambiguous': 'the checks of more than one record matched',
    'new': 'no known problem matched',
}
ESCALATED = ('seen-after-fix', 'ambiguous', 'new')
# How a result's notes give what an analyst found about its failure.
FOUND = 'The analyst found: '


@dataclass
class Result:
    job: Job
    verdict: str = ''
    problem: object = None
    path: list = field(default_factory=list)
    # (problem id, 'confirm' | 'deep' | 'exclude', Check, [Hit])
    checks: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    agent: dict = None
    workdir: str = ''
    # The job whose evidence the judge saw, for jobs that failed the same way.
    group: str = ''
    # What the tree found.
    finding: object = None
    # True if the tree and the records, or the judge, decided the verdict.
    sure: bool = False
    # For a verdict the tree and the records decided: the judge's answer when asked
    # to check it, with whether it agrees. The verdict stands.
    opinion: dict = None
    # What the analyst said of the failure, last: its answer, with "analyst" and "job",
    # whose archive its quotes are from. The report shows its account in place of the
    # judge's.
    analysis: dict = None
    # [(PR under test, a file it changes that the job ran)]: whatever the verdict,
    # the failure may be the PR's.
    suspects: list = field(default_factory=list)
    # [(PR under test, who found it caused the failure, what they said)]: an
    # analyst's answer, which passed the checks.
    blamed: list = field(default_factory=list)
    # [(run, job id, True if it failed the same way)] of the base branch's jobs
    # that failed this job's test.
    baseline: list = field(default_factory=list)
    # {file: the name the archive has it by now} of the files the tree and the checks
    # cite, where it is another, as the archive gzips older runs' logs.
    names: dict = field(default_factory=dict)

    @property
    def escalated(self):
        return self.verdict in ESCALATED

    @property
    def signature(self):
        return self.finding.signature(self.job) if self.finding else fingerprint(self.job)


class Triage:
    def __init__(self, problems, judge=None, deep=False, git_dir=None, workdir='.',
                 batch_prs=(), verdicts=None, search=None):
        self.problems = problems
        # search(words) gives the tracker issues that have all the words, as
        # [(number, title)], for the judge to consider beside the records; with
        # what each search found, and the PRs under test of each commit.
        self.search, self.searched, self.branches = search, {}, {}
        # {(commit under test, a test's name): the files it changes that have the name}
        self.named = {}
        # Whether each change under test changes the functions a job failed in, by
        # (commit, path, names).
        self.touched = {}
        # pr_of(fork URL, branch) gives the number of the PR whose head it is.
        self.pr_of = None
        # True to check what the tree and the records decided without telling the
        # judge what that was; and to note the PRs under test that change a file each
        # job ran.
        self.blind = self.blame = False
        # The base branch, and the failures of its runs by test (baseline.failures),
        # to compare each job with; None for none.
        self.base, self.base_failures = None, {}
        # What people placed the jobs that failed each way in: memory.load's.
        self.memory = {}
        # The jobs of the run that passed, and files_for(job), their files: the judge's
        # evidence leaves out the lines one of them logs too. And which, by job.
        self.passing, self.files_for, self.passed = [], None, {}
        # The judge's answers from before, by job id, to take instead of asking it.
        self.answers = {}
        # Where the judges' verdicts are kept, by the evidence; None to keep none.
        self.verdicts = verdicts
        # The numbers of the PRs the run tests, from its QA tracker.
        self.batch_prs = set(batch_prs)
        self.judge = judge
        self.deep = deep
        # The most lines of the logs a result shows for a line the judge quotes, of
        # those that have it. A record's checks keep the lines they always found, so
        # that the archive's answers kept from before, which are all a pruned archive
        # has left, still answer them.
        self.cited_lines = pack.MOST_SHOWN
        self.git_dir = git_dir
        self.workdir = workdir
        # The tree's findings, by job: they do not change when the records do.
        self.findings = {}
        # [(judge, cost)] for every time a judge was asked.
        self.spent = []
        # True to take only the verdicts kept from before, and ask no judge; and
        # True to ask the judge even if it judged the same evidence before.
        self.kept_only = self.fresh = False
        # Held while a commit's PRs under test are found, which fetches it: once for
        # all the jobs triaged at once.
        self._finding_branch = threading.Lock()

    def for_another_job(self):
        """A copy, to triage another job at the same time: it keeps its own job's
        files, and shares the records, the settings and what was found."""
        return copy.copy(self)

    def judging(self, judge, fresh=False):
        """A copy that asks judge, to judge one group while others are judged at the
        same time: with its own job's files, and its own list of what it spent."""
        triage = self.for_another_job()
        triage.judge, triage.fresh, triage.spent = judge, fresh, []
        return triage

    def run(self, job, files):
        r = self.resume(Result(job), files, 'explain')
        if self.blame:
            self.suspect(r)
        if self.base:
            baseline.compare(r, self.base_failures, self.base)
        if self.memory and not r.problem:
            self.recall(r)
        r.names = archived_names(r, files)
        return r

    def recall(self, r):
        """Give r, which no record places, what people placed the jobs that failed
        the same way in, if the memory agrees on it, with no judge: in its record, or
        in the tracker issue itself while no record names it, for a lesson to make one."""
        got = memory.recall(self.memory, r.signature, bool(r.finding and r.finding.keys))
        if not got:
            return
        issue, jobs = got
        # Its own record, or else the one that lists it under also, as the same problem
        # (a job placed there is right for it too), for a lesson to mend that record's
        # checks rather than make another; the memory keeps duplicates and backports as
        # their originals already.
        record = next((p for p in self.problems if p.id == issue), None) or \
            next((p for p in self.problems if issue in p.also), None)
        if r.path and r.path[-1] == 'agent':
            r.path.pop()
        r.path.append('memory')
        r.problem = record or memory.unrecorded(issue, getattr(self.memory, 'titles', {})
                                                .get(issue))
        r.verdict, r.sure = 'probable-duplicate', True
        r.notes.append(f'people placed {jobs} jobs that failed this way in {issue}' +
                       ('' if record and record.id == issue else
                        f', which record {record.id} lists under also' if record else
                        ', which has no record yet') +
                       ': the memory places it there, with no judge')
        if record:
            self.resume(r, self.files, 'fix-check')

    def resume(self, r, files, node):
        self.files = files
        while node:
            r.path.append(node)
            node = getattr(self, node.replace('-', '_'))(r)
        return r

    def reference(self, r):
        """(job, its files) of the job of r's run that passed most like r's, whose
        teuthology.log the archive has, to compare r's with; None if there is none."""
        if r.job.path not in self.passed:
            self.passed[r.job.path] = None
            same = [j for j in self.passing if j.run == r.job.run]
            same = pack.kept(same, self.files_for) if self.files_for else []
            for job in pack.alike(r.job, same)[:pack.PASSING_TRIED]:
                try:
                    files = self.files_for(job)
                    if files.glob('teuthology.log'):
                        self.passed[r.job.path] = (job, files)
                        break
                except ArchiveError:
                    continue
        return self.passed[r.job.path]

    def judge_group(self, group, files):
        """Judge the first of several unplaced jobs with the same failure
        reason, and give the others the same verdict."""
        first = group[0]
        if first.path[-1] == 'agent':
            first.path.pop()
        self.resume(first, files, 'agent')
        for r in group:
            r.group = first.job.id
        for r in group[1:]:
            r.verdict, r.problem, r.agent = first.verdict, first.problem, first.agent
            r.workdir, r.sure = first.workdir, first.sure
            r.path.append(f'same-as-{first.job.id}')
            r.notes.append(f'failed the same way as {first.job.id}, which was judged')

    def check_group(self, group, files):
        """Ask the judge to check the verdict that the tree and the records gave the
        first of several jobs that failed the same way, and to explain the failure:
        its answer is each job's opinion. The verdicts stand."""
        first = group[0]
        if not self.judge:
            return
        self.files = files
        first.workdir = os.path.join(self.workdir, first.job.id)
        # Blind, the judge decides the job as if nothing had: it is not told what
        # the tree and the records said, so that it cannot simply agree.
        opinion = self._cached(first) or self._ask_judge(
            first, pack.build(first.workdir, files, first, self.problems,
                              task=pack.BLIND if self.blind else pack.checking(first),
                              extra=self.evidence(first), blind=self.blind,
                              passing=self.reference(first)))
        if not opinion:
            return
        quotes = pack.quotes(opinion)
        opinion['quotes_found'] = bool(quotes) and all(pack.quote_found(first.workdir, q)
                                                       for q in quotes)
        opinion['agrees'] = agrees(first, opinion)
        opinion['blind'] = self.blind
        for r in group:
            r.opinion, r.workdir = opinion, first.workdir
            if opinion['agrees'] is False:
                r.notes.append(f'the judge ({readable(opinion.get("judge"))}) disagrees: it says '
                               f'{opinion.get("verdict")} {opinion.get("problem") or ""}' +
                               (f', caused by {opinion["pr"]} under test'
                                if opinion.get('pr') else '') +
                               f': {opinion.get("summary", "")}')

    def evidence(self, r):
        """{file name: text} of what the judge gets about r's job besides the logs
        and the records: the PRs under test, and the tracker issues a search for its
        tests finds."""
        extra = {'under-test.txt': self._under_test(r), 'trackers.txt': self._trackers(r)}
        return {name: text for name, text in extra.items() if text}

    def _under_test(self, r):
        """The PRs under test merged into the branch r's job ran, the files each
        changes, and the diffs of those its description, failure reason or the
        tree's explanation mention; '' for none."""
        changes, said = self._branch(), said_of(r)
        if not changes:
            return ''
        return under_test_text(self.git_dir, changes,
                               shown=lambda path: pack.mentioned(path, said),
                               most_files=pack.MOST_FILES)

    def _branch(self):
        """The PRs under test merged into the branch the current job ran, as
        under_test gives them: of its suite's commit, or else its build's; [] for
        none, or with no ceph clone to find them in."""
        if not self.git_dir:
            return []
        found = versions(self.files)
        for sha1, url in (found[name] for name in ('suite', 'source') if name in found):
            with self._finding_branch:
                if sha1 not in self.branches:
                    try:
                        fetch(self.git_dir, sha1, url, lambda line: None)
                        self.branches[sha1] = [(self._pr(what), *rest) for what, *rest in
                                               under_test(self.git_dir, sha1)]
                    except CheckoutError:
                        self.branches[sha1] = []
            if self.branches[sha1]:
                return self.branches[sha1]
        return []

    def _pr(self, what):
        """what, as under_test names it, with the number of the PR a branch merged
        with git pull is the head of, if pr_of finds one."""
        branch = re.match(r'branch (\S+) of (\S+)$', what)
        number = branch and self.pr_of and self.pr_of(branch.group(2), branch.group(1))
        return f'PR #{number}' if number else what

    def suspect(self, r):
        """Note on r the PRs under test that change a file its job ran, as its
        description, failure reason or the tree's explanation names it: whatever
        else it looks like, the failure may be theirs."""
        said, names = said_of(r), [n for n in pack.search_words(r.job) if '.' not in n]
        crashed = crashed_in(r)
        found, unlikely = [], []
        for what, commit, parent, files in self._branch():
            for path in files:
                if not pack.mentioned(path, said):
                    continue
                # A crash in a function the change leaves as it was, in a file it
                # changes elsewhere, is not much reason to think it the change's.
                if crashed and path.endswith(SOURCES) and \
                        not self._touches(parent, commit, path, crashed):
                    unlikely.append((what, path))
                else:
                    found.append((what, path))
            # A file the change gives the failing test in, by the test's name.
            if names and self.git_dir:
                found += [(what, path) for path in self._naming(commit, files, names)]
        r.suspects = list(dict.fromkeys(found))
        for what, path in r.suspects:
            r.notes.append(f'{what}, under test, changes {path}, which this job ran: the '
                           'failure may be its doing')
        for what, path in dict.fromkeys(unlikely):
            if (what, path) not in r.suspects:
                r.notes.append(f'{what}, under test, changes {path}, but not '
                               f'{", ".join(crashed)}, which the job failed in: unlikely to '
                               'be its doing')

    def _touches(self, parent, commit, path, names):
        key = (commit, path, tuple(names))
        if key not in self.touched:
            try:
                self.touched[key] = touches(self.git_dir, parent, commit, path, names)
            except CheckoutError:
                self.touched[key] = True
        return self.touched[key]

    def _naming(self, commit, files, names):
        """The files, of those a change under test changes, that have any of names at
        its commit: each name is looked for once in each change, for all the jobs."""
        wanted = [n for n in names if (commit, n) not in self.named]
        if wanted:
            found = naming_each(self.git_dir, commit, files, wanted)
            for name in wanted:
                self.named[(commit, name)] = found.get(name, set())
        return sorted(set().union(*(self.named[(commit, n)] for n in names)))

    def _trackers(self, r):
        """The tracker issues that a search for r's job's tests finds, those that
        have a record marked; '' for none."""
        if not self.search:
            return ''
        found = {}
        for words in pack.search_words(r.job):
            if words not in self.searched:
                try:
                    self.searched[words] = self.search(words)
                except (OSError, ValueError, KeyError):
                    self.searched[words] = []
            for number, title in self.searched[words]:
                found.setdefault(number, title)
        records = {p.id for p in self.problems}
        return ''.join(f'{title}' + (' (has a record)' if number in records else '') + '\n'
                       for number, title in list(found.items())[:pack.TRACKERS])

    def explain(self, r):
        if r.job.path not in self.findings:
            self.findings[r.job.path] = tree.explain(r.job, self.files)
        r.finding = self.findings[r.job.path]
        return 'prefilter'

    def prefilter(self, r):
        r.candidates = [p for p in self.problems if p.prefilter(r.job, r.finding)]
        return 'confirm' if r.candidates else 'explained'

    def confirm(self, r):
        confirmed = [p for p in r.candidates if self._checks_pass(r, p)]
        if len(confirmed) > 1:
            r.verdict = 'ambiguous'
            r.notes.append('matched ' + ', '.join(p.id for p in confirmed))
            return 'agent'
        if confirmed:
            r.problem = confirmed[0]
            r.verdict = 'duplicate'
            r.sure = True
            return 'fix-check'
        return 'explained'

    def explained(self, r):
        """No record placed the job: it is new for sure if the tree explained it
        conclusively, and no record nearly fitted it."""
        if r.finding and r.finding.conclusive and not r.candidates:
            r.verdict = 'new'
            r.sure = True
            return None
        return 'agent'

    def _checks_pass(self, r, p):
        checks = [('confirm', c) for c in p.confirm]
        if self.deep:
            checks += [('deep', c) for c in p.deep]
        for kind, check in checks:
            hits = self.files.search(check.file, check.pattern)
            r.checks.append((p.id, kind, check, hits))
            gone = getattr(self.files, 'last_gone', None)
            if gone and not hits:
                r.notes.append(f'the archive no longer has {len(gone)} of the files record '
                               f'{label(p.id)}\'s check reads ({check.file}): it has removed '
                               'them since, so the check could not look')
            if not hits:
                return False
        for check in p.exclude:
            hits = self.files.search(check.file, check.pattern)
            r.checks.append((p.id, 'exclude', check, hits))
            if hits:
                return False
        return True

    def fix_check(self, r):
        fixes = r.problem.fixes
        for fix in fixes:
            if pr_number(fix.pr) in self.batch_prs:
                r.verdict = 'seen-after-fix'
                r.notes.append(f'the fix {fix.pr} is one of the PRs under test')
                return None
        if not any(f.fixed_in for f in fixes):
            return None
        config = yaml.safe_load(self.files.text('orig.config.yaml')) or {}
        branch = config.get('branch', '')
        found = self.files.search('teuthology.log', VERSION, max_hits=1)
        build, what = parse_version(found[0].text) if found else None, 'this build'
        suite = str(config.get('suite_sha1') or '')
        if r.problem.kind == 'test' and SHA1.fullmatch(suite) and \
                not (build and suite.startswith(build[2])):
            # A test's fix is in qa/, which the job ran from its suite's commit, not
            # from the build's.
            build, what = (None, None, suite), 'the qa suite this job ran'
            if self.git_dir:
                try:
                    fetch(self.git_dir, suite, str(config.get('suite_repo') or ''),
                          say=lambda line: None)
                except CheckoutError:
                    pass
        for fix in fixes:
            if not fix.fixed_in:
                continue
            if not applies(fix, branch):
                r.notes.append(f'{fix.pr} is on {fix.branch}, not on {branch}')
                continue
            inside = fix_in_build(fix, build, self.git_dir)
            if inside:
                r.verdict = 'seen-after-fix'
                r.notes.append(f'the fix {fix.pr} ({fix.fixed_in}) is in {what}')
                return None
            r.notes.append(f'the fix {fix.pr} is not in {what}' if inside is False else
                           f'could not tell whether the fix {fix.pr} is in {what}')
        return None

    def agent(self, r):
        if not self.judge:
            r.verdict = r.verdict or 'new'
            return None
        if not self.files.glob('teuthology.log'):
            # The judge has only the failure reason to go on: it is nearly always unsure.
            r.verdict = r.verdict or 'new'
            r.notes.append('the archive has no teuthology.log of this job, so no judge was '
                           'asked')
            return None
        r.workdir = os.path.join(self.workdir, r.job.id)
        extra = self.evidence(r)
        r.agent = self._cached(r) or self._ask_judge(
            r, pack.build(r.workdir, self.files, r, self.problems, extra=extra,
                          passing=self.reference(r)))
        needs = r.agent and r.agent.get('verdict') == 'unsure' and r.agent.get('needs')
        if needs and not r.agent.get('kept'):
            # A judge that could not decide says what would decide it: it is given that,
            # once, from the archive, if the archive has it.
            try:
                text = pack.needed(self.files, needs)
            except ArchiveError:
                text = ''
            if text:
                r.notes.append('the judge could not decide, and asked for '
                               f'{short("; ".join(map(str, needs)), 200)}: it was given '
                               'them from the archive, and asked again')
                first = r.agent
                r.agent = self._ask_judge(r, pack.build(
                    r.workdir, self.files, r, self.problems,
                    extra=dict(extra, **{'needed.txt': text}),
                    passing=self.reference(r))) or first
                if r.agent is not first:
                    r.agent['cost'] = (r.agent.get('cost') or 0) + (first.get('cost') or 0)
        if not r.agent:
            r.verdict = r.verdict or 'new'
            return None
        return 'verify-quotes'

    def _cached(self, r):
        """The judge's answer about r's job from before, read from the cache of them
        (answers), with its evidence still where it was; None if there is none."""
        answer = self.answers.get(r.job.id)
        if not answer or not os.path.isdir(r.workdir):
            return None
        r.notes.append(f'{readable(answer.get("judge"))}\'s verdict from before, read from the '
                       'cache')
        return pack.locate(r.workdir, dict(answer, kept=True), self.files, self.cited_lines)

    def _ask_judge(self, r, prompts):
        """The judge's answer about r's job from prompts: the one it gave the same
        evidence before, unless fresh; None, with a note why, if there is none."""
        # The inline prompt has all the evidence, the list of records among it.
        kept = self.verdicts and os.path.join(self.verdicts, hashlib.sha256(
            f'{self.judge.name}\0{prompts["inline"]}'.encode()).hexdigest() + '.json')
        if kept and os.path.exists(kept) and not self.fresh:
            with open(kept) as f:
                answer = dict(json.load(f), kept=True)
            r.notes.append(f'{readable(answer.get("judge"))} judged the same evidence before: its '
                           'verdict, at no cost')
            return pack.locate(r.workdir, answer, self.files, self.cited_lines)
        if self.kept_only:
            r.notes.append('the judge\'s verdict was on other evidence: judge it again to '
                           'have one')
            return None
        try:
            answer = self.judge(r.workdir, prompts)
        except Stopped:
            raise
        except JudgeError as e:
            self.spent.append((self.judge.name, e.cost or 0))
            r.notes.append(str(e))
            return None
        for name, cost in answer.get('costs') or [(answer.get('judge'), answer.get('cost'))]:
            self.spent.append((name, cost or 0))
        pack.locate(r.workdir, answer, self.files, self.cited_lines)
        if kept:
            os.makedirs(self.verdicts, exist_ok=True)
            with open(kept, 'w') as f:
                json.dump(answer, f)
        return answer

    def verify_quotes(self, r):
        v = r.agent
        r.verdict = r.verdict or 'new'
        if v.get('verdict') != 'duplicate':
            if v.get('verdict') == 'unsure' and v.get('needs'):
                r.notes.append(f'the judge ({readable(v.get("judge"))}) could not decide: '
                               'it would need ' + short('; '.join(map(str, v['needs'])), 300))
            # A judge that finds no problem it knows is not sure the job is new: it has
            # the records and a tracker search, not every issue people place jobs in.
            if v.get('verdict') == 'new':
                r.notes.append(f'the judge ({readable(v.get("judge"))}) knows no problem it is, '
                               'which is not to say it is new: look on the tracker')
            return None
        problem = next((p for p in self.problems if p.id == named(v.get('problem'))), None)
        if not problem:
            number = named(v.get('problem'))
            r.notes.append(f'the judge says it is {label(number)}, which has no record '
                           'yet: a lesson can make one' if number.isdigit() or is_pr(number) else
                           f'the judge named an unknown problem {v.get("problem")}')
            return None
        quotes = pack.quotes(v)
        missing = [q for q in quotes if not pack.quote_found(r.workdir, q)]
        if not quotes or missing:
            r.notes.append(f'the judge said {problem.id}, but {len(missing)} of '
                           f'{len(quotes)} quotes were not found in the evidence')
            return None
        # A line a job that passed logs too shows nothing of what failed.
        passing = self.reference(r)
        try:
            common = pack.passed_too(passing, [str(q.get('text', '')) for q in quotes])
        except ArchiveError:
            common = set()
        if len(common) == len(quotes):
            r.notes.append(f'the judge said {problem.id}, but {passing[0].path}, which passed, '
                           'logs every line it quotes too: they show nothing of the failure')
            return None
        r.problem = problem
        r.verdict = 'probable-duplicate'
        r.sure = True
        return 'fix-check'


def archived_names(r, files):
    """{file: the name the archive has it by now} of the files r's tree and checks
    cite, where it is another."""
    cited = {h.file for step in (r.finding.steps if r.finding else []) for h in step.evidence}
    cited |= {h.file for *_, hits in r.checks for h in hits}
    try:
        return {rel: real for rel in sorted(cited) if (real := files.real(rel)) != rel}
    except ArchiveError:
        return {}


def said_of(r):
    """What r's job says of the files it ran: its description, failure reason, and
    the tree's explanation with the log lines it read."""
    return ' '.join([r.job.description, r.job.reason] +
                    (r.finding.lines(pack.LINE_LIMIT) if r.finding else []))


def agrees(r, opinion):
    """True if the judge's opinion bears r's verdict out, False if not, None if it is
    unsure."""
    verdict = opinion.get('verdict')
    if verdict == 'new':
        return r.verdict == 'new'
    if verdict == 'duplicate':
        return bool(r.problem) and named(opinion.get('problem')) == r.problem.id
    return None


def named(problem):
    """The problem id an agent named, which it may give as its tracker issue's URL,
    as #<number> or TR <number>, or as a pull request's URL or PR <number>."""
    return record_id(problem)


# The files a crash's function is in, for a change to them to be weighed by it.
SOURCES = ('.cc', '.cpp', '.cxx', '.c', '.h', '.hpp')


def crashed_in(r):
    """The short names of the functions r's job failed in, as the tree found them,
    such as calc_deviations for float OSDMap::calc_deviations(...); [] if none."""
    function = str(((r.finding.facts if r.finding else None) or {}).get('function') or '')
    return list(dict.fromkeys(re.findall(r'([A-Za-z_]\w*)\s*\(', function)[:1]))


def placed(r):
    """The id of the problem a result names: None if none, 'ambiguous' if several."""
    if r.verdict == 'ambiguous':
        return 'ambiguous'
    return r.problem.id if r.problem else None


def fits(r, answer):
    """True if r gives the answer people gave: the problem, or one its record says
    people also place the same failures in."""
    return placed(r) == answer or bool(r.problem and answer in r.problem.also)


def describe(r):
    return f'{r.verdict}{" " + r.problem.id if r.problem else ""} ({" -> ".join(r.path)})'


def misses(p, r):
    """Why record p did not place r.job: where the tree put the job, if not under
    the record; its facts and match keys that do not fit the job; and its checks
    that found a line they should not have, or none."""
    lines = []
    if p.trees:
        where = r.finding.where if r.finding else ''
        if not p.hangs(where):
            tops = ' or '.join(f'"{top}"' for top in p.trees)
            lines.append(f'tree: the job\'s path is "{where}", not under {tops}')
        for name, regex in p.facts.items():
            value = str((r.finding.facts if r.finding else {}).get(name, ''))
            if not regex.search(value):
                lines.append(f'fact {name} /{regex.pattern}/ does not fit "{value[:200]}"')
    for name, regex in p.match.items():
        value = getattr(r.job, name)
        if not regex.search(value):
            lines.append(f'match {name} /{regex.pattern}/ does not fit "{value[:200]}"'
                         if value else
                         f'match {name} /{regex.pattern}/: the job has no {name}; its '
                         f'description is "{r.job.description[:200]}"')
    for pid, kind, check, hits in r.checks:
        if pid == p.id and bool(hits) == (kind == 'exclude'):
            lines.append(f'{kind} {check}: {"found" if hits else "not found"}')
    return lines
