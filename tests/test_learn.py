import contextlib
import copy
import glob
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

from fakes import (REAL_RUN_TESTS, FakeFiles, FakeJudge, RUN, forget_learned, job, problems,
                   record)

from qa_triage import approve, db, learn, memory, report, share, tree
from qa_triage.agents import JudgeError, Stopped
from qa_triage.cli import judge_unplaced, retriage, search_originals
from qa_triage.graph import Triage
from qa_triage.learn import Doubt
from qa_triage.tree import learned

OP_REPLY = "api_io_pp: Perf counter 'objecter.op_reply' has incorrect value after operate()."
LEAK = '<kind>Leak_DefinitelyLost</kind> in ceph-mon'
ARCHIVES = {
    '1': {'teuthology.log': [OP_REPLY, LEAK]},           # the doubted job
    '2': {'teuthology.log': [OP_REPLY]},                 # an example of record 100
    '3': {'teuthology.log': ['FAILED test'],
          'remote/h1/log/ceph-osd.0.log': ['unstable write on shard, bouncing to primary']},
    '5': {'teuthology.log': ['an OSD died'],                # triaged before, not settled
          'remote/h1/log/ceph-osd.0.log': ['unstable write on shard, bouncing to primary']},
}
WATCHDOG = 'The WatchDog barked due to Daemon ceph.osd.4 has failed'
ISSUE = {'subject': 'a problem', 'status': {'name': 'New'},
         'project': {'id': 9, 'name': 'RADOS'}}
BACKPORT = dict(ISSUE, id=201, tracker={'name': 'Backport'},
                relations=[{'issue_id': 200, 'issue_to_id': 201, 'relation_type': 'copied_to'}])


def text(rec):
    return yaml.safe_dump(rec, sort_keys=False)


class FakeAnalyst:
    """Gives scripted answers, and records each prompt with the session it was
    asked in."""

    name = 'fake'
    cumulative_cost = True
    program = None

    def __init__(self, answers, resumes=True):
        self.answers = list(answers)
        self.resumes = resumes
        self.prompts = []
        self.tags = []

    def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
        self.prompts.append((prompt, session))
        self.tags.append(tag)
        if tag and os.path.isdir(workdir):
            # As an agent leaves its output.
            open(os.path.join(workdir, f'{tag}.out'), 'w').close()
        return dict(copy.deepcopy(self.answers.pop(0)), judge=self.name, session='s1',
                    cost=0.5)


def answer(problem, changes=(), quotes=(('teuthology.log', LEAK),), assessment='wrong',
           **extra):
    return dict({'assessment': assessment, 'problem': problem, 'summary': 'a summary',
                 'explanation': 'an explanation',
                 'evidence': [{'file': f, 'text': t} for f, t in quotes],
                 'changes': [{'id': c['id'], 'yaml': text(c)} for c in changes]}, **extra)


UNSTABLE = 'unstable write on shard, bouncing to primary'
# A node that explains the jobs whose OSD log has UNSTABLE, and one that claims
# every job that reaches it.
NODE = '''from qa_triage.tree.core import done, node, unknown

EXAMPLES = {'%s/6': 'workunit/unexplained/unstable-write'}


@node('workunit/unexplained', 'Why did the workunit fail?')
def workunit_unexplained(ctx):
    found = ctx.search('remote/*/log/ceph-osd.*.log', 'unstable write', 'unstable write')
    if found:
        return done('unstable-write', 'an OSD bounced an unstable write', found[0],
                    key='unstable write')
    return unknown('not one it knows')
''' % RUN
SLOPPY = NODE.replace("    if found:\n", "    if True:\n").replace('found[0],\n', '\n')

def describe_result(r):
    return r.job.id, r.verdict, r.problem.id if r.problem else None, r.path


BASE = record(examples=[f'{RUN}/2'])
EXCLUDING = record(examples=[f'{RUN}/2'],
                   exclude=[{'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}])


class TestDoubt(unittest.TestCase):
    def setUp(self):
        # The tests give the same job ids different archives.
        learn._FINDINGS.clear()
        root = tempfile.mkdtemp(prefix='qa_triage-learn-')
        self.problems = os.path.join(root, 'problems')
        self.labels = os.path.join(root, 'labels')
        self.workdir = os.path.join(root, 'work')
        os.makedirs(self.problems)
        with open(os.path.join(self.problems, '100-op-reply.yaml'), 'w') as f:
            f.write(text(BASE))
        self.files = {jid: FakeFiles(files) for jid, files in ARCHIVES.items()}
        self.jobs = {jid: job(jid) for jid in ARCHIVES}
        self.jobs['5'] = job('5', reason=WATCHDOG)
        self.said = []

    def doubt(self, analyst, jid='1', replies=(), **kw):
        replies = iter(replies)
        return Doubt(self.jobs[jid], self.files[jid], self.problems, self.labels, analyst,
                     self.workdir, files_for=lambda j: self.files[j.id],
                     jobs_for=lambda run: self.jobs, tracker=lambda n: ISSUE,
                     by='tester', why='it leaked', ask=lambda prompt: next(replies),
                     say=self.said.append, **kw)

    def record_text(self):
        with open(os.path.join(self.problems, '100-op-reply.yaml')) as f:
            return f.read()

    def test_jobs_triaged_at_once_keep_their_own_files(self):
        import threading
        import time
        from qa_triage.cli import triage_all
        triage = Triage(db.load(self.problems))
        ids = sorted(self.jobs)
        alone = [describe_result(Triage(db.load(self.problems)).run(self.jobs[j], self.files[j]))
                 for j in ids]
        running, most, lock = set(), [0], threading.Lock()

        def files_for(job):
            with lock:
                running.add(job.id)
                most[0] = max(most[0], len(running))
            time.sleep(0.05)
            with lock:
                running.discard(job.id)
            return self.files[job.id]
        found = list(triage_all(triage, [self.jobs[j] for j in ids], files_for, at_once=4))
        self.assertEqual([job.id for job, _, _ in found], ids)
        self.assertEqual([describe_result(r) for _, _, r in found], alone)
        self.assertGreater(most[0], 1)

    def test_a_stopped_triage_leaves_a_job_that_is_stuck(self):
        """Stopped, the triage gives up a job whose files take long to read, and those
        not begun: the jobs triaged by then stay, and the stuck one holds nothing up."""
        import threading
        from types import SimpleNamespace
        from qa_triage import cli
        ids = sorted(self.jobs)
        stuck = threading.Event()
        self.addCleanup(stuck.set)

        def files_of(args, job):
            if job.id == ids[0]:
                stuck.wait()
            return self.files[job.id]
        session = SimpleNamespace(triage=Triage(db.load(self.problems)), files={}, results=[],
                                  jobs=[self.jobs[j] for j in ids], header=[],
                                  stop=threading.Event(), skipped=[])
        said = []

        def say(line):
            said.append(line)
            if len(session.files) == len(ids) - 1:
                session.stop.set()
        with mock.patch.object(cli, 'files_of', files_of):
            cli.triage_jobs(SimpleNamespace(settings={'at_once': 2}, cache=self.workdir),
                            session, say)
        self.assertEqual([r.job.id for r in session.results], ids[1:])
        self.assertEqual(session.skipped, ids[:1])
        self.assertTrue(session.triaged)
        self.assertIn(f'Triage stopped: 1 jobs not triaged: {ids[0]}. Starting the review '
                      'again triages them, and the rest from what the cache kept of them.', said)

    def test_wrong_duplicate_gets_an_exclude(self):
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        self.assertTrue(self.doubt(analyst).run())
        self.assertIn('Leak_DefinitelyLost', self.record_text())
        [(j, problem, who)] = learn.load_labels(self.labels)
        # The tester gave no answer: the label is the analyst's, not theirs.
        self.assertEqual((j.path, problem, who), (f'{RUN}/1', None, 'settled by qa_triage (fake)'))
        with open(os.path.join(self.labels, os.listdir(self.labels)[0])) as f:
            self.assertEqual(yaml.safe_load(f)['jobs']['1']['why'],
                             "tester doubted it: it leaked; this is the analyst's answer, "
                             'not theirs')
        with open(os.path.join(self.workdir, 'doubt.yaml')) as f:
            doubt = yaml.safe_load(f)
        self.assertEqual(doubt['result']['problem'], '100')
        self.assertTrue(os.path.exists(os.path.join(self.workdir, 'records',
                                                    '100-op-reply.yaml')))

    def test_a_person_approves_a_change_first(self):
        said, prompts = [], []
        for replies, kept in ((['n'], False), (['f', 'y'], True)):
            self.setUp()
            analyst = FakeAnalyst([answer(None, [EXCLUDING])])
            answers = iter(replies)
            approval = approve.terminal(ask=lambda prompt: prompts.append(prompt) or
                                        next(answers), say=said.append)
            self.assertEqual(bool(self.doubt(analyst, approve=approval).run()), kept)
            self.assertEqual('Leak_DefinitelyLost' in self.record_text(), kept)
        shown = '\n'.join(said)
        self.assertIn('What was wrong: the tool said 100; the analyst says no known problem',
                      shown)
        self.assertIn('+exclude:', shown)
        # An analyst with no program of its own to talk in offers no discussion.
        self.assertEqual(prompts[0], '[y]es, keep it and push it, [n]o, [f]ull diff: ')

    def test_a_person_talks_it_over_with_the_analyst_first(self):
        class Talks(FakeAnalyst):
            program = 'its own program'

            def chat(self, workdir, prompt, session=None, tag=None):
                self.told = prompt
                return {'text': 'it was a leak', 'session': 's2'}

            def interactive(self, workdir, session):
                return ['its-program', '--resume', session]

            def env(self):
                return None

        # In its program, the person asks for another change, which it writes.
        revised = answer(None, [record(examples=[f'{RUN}/2'], exclude=[
            {'file': 'teuthology.log', 'pattern': 'in ceph-mon'}])])
        ran = []

        def run(argv, cwd, env):
            ran.append(argv)
            with open(os.path.join(cwd, approve.PROPOSAL), 'w') as f:
                json.dump(revised, f)

        analyst = Talks([answer(None, [EXCLUDING])])
        answers = iter(['d', 'y'])
        approval = approve.terminal(ask=lambda prompt: next(answers), say=self.said.append,
                                    run=run)
        self.assertTrue(self.doubt(analyst, approve=approval).run())
        self.assertEqual(ran, [['its-program', '--resume', 's2']])
        # The person is told how to have it change the tree, and so is it.
        self.assertIn(f'How to have {analyst.program} change how qa_triage decides',
                      '\n'.join(self.said))
        self.assertIn('A person must approve your change', analyst.told)
        self.assertIn('how they can have you change it', analyst.told)
        self.assertIn('in ceph-mon', self.record_text())
        self.assertNotIn('Leak_DefinitelyLost', self.record_text())

    def test_a_change_waits_for_approval(self):
        pending = os.path.join(self.workdir, 'pending')
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        doubt = self.doubt(analyst, approve=approve.hold(pending))
        self.assertFalse(doubt.run())
        self.assertNotIn('Leak_DefinitelyLost', self.record_text())
        [name] = os.listdir(pending)
        self.assertEqual(doubt.held, os.path.join(pending, name))
        with open(os.path.join(pending, name)) as f:
            held = json.load(f)
        self.assertEqual(held['job'], f'{RUN}/1')
        # Later, the person approves it: checked again, and kept.
        doubt = self.doubt(FakeAnalyst([]),
                           approve=approve.terminal(ask=lambda prompt: 'y', say=self.said.append))
        settled, _ = doubt.settle(held)
        self.assertTrue(settled)
        self.assertIn('Leak_DefinitelyLost', self.record_text())

    def test_a_change_held_is_a_commit_to_inspect(self):
        # The library is a git repository: the change that waits is a commit of it
        # too, for the person to inspect, and its checkout is left as it was.
        root = os.path.dirname(self.problems)
        git = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
               'GIT_COMMITTER_EMAIL': 't@t'}

        def run(*argv):
            return subprocess.run(['git', '-C', root, *argv], capture_output=True, text=True)

        with mock.patch.dict(os.environ, git):
            for argv in (('init', '-q'), ('add', 'problems'), ('commit', '-qm', 'the library')):
                self.assertEqual(run(*argv).returncode, 0)
            pending = os.path.join(self.workdir, 'pending')
            doubt = self.doubt(FakeAnalyst([answer(None, [EXCLUDING])]),
                               approve=approve.hold(pending))
            self.assertFalse(doubt.run())
            with open(doubt.held) as f:
                held = json.load(f)
            self.assertEqual(list(held['writes']), ['problems/100-op-reply.yaml'])
            self.assertEqual(os.path.realpath(held['repository']), os.path.realpath(root))
            shown = run('show', held['commit']).stdout
            self.assertIn('Proposed from', shown)
            self.assertIn('+  pattern: Leak_DefinitelyLost', shown)
            self.assertIn(f'git -C {held["repository"]} show {held["commit"]}',
                          '\n'.join(self.said))
            self.assertNotIn('Leak_DefinitelyLost', self.record_text())
            self.assertEqual((run('diff', '--quiet').returncode,
                              run('diff', '--cached', '--quiet').returncode), (0, 0))
            # One held before its files and commit were kept has them made again.
            for key in ('writes', 'repository', 'commit'):
                del held[key]
            self.assertEqual(list(learn.held_writes(held, self.problems)),
                             ['problems/100-op-reply.yaml'])
            repository, commit = approve.commit_of(doubt.held, held, self.problems)
            ref = share.PENDING_REFS + approve.ref_name(doubt.held)
            self.assertEqual(run('rev-parse', ref).stdout.strip(), commit)
            with open(doubt.held) as f:
                self.assertEqual(json.load(f)['commit'], commit)
            # Once kept, neither is.
            approve.approved(doubt.held, held)
            self.assertFalse(os.path.exists(doubt.held))
            self.assertNotEqual(run('rev-parse', '--verify', '-q', ref).returncode, 0)

    def test_people_placed_it_in_an_issue_that_is_the_same_problem(self):
        # People say job 2 is 200; the analyst finds 200 is 100's problem too, and
        # says so in 100's also: that agrees with them.
        same = record(examples=[f'{RUN}/2'], also=['200'])
        analyst = FakeAnalyst([answer('100', [same], quotes=(('teuthology.log', OP_REPLY),),
                                      assessment='right')])
        self.assertTrue(self.doubt(analyst, jid='2', expected='200').run())
        self.assertEqual([p.also for p in db.load(self.problems)], [['200']])
        [(j, problem, _)] = learn.load_labels(self.labels)
        self.assertEqual((j.id, problem), ('2', '100'))

    def test_a_settled_job_is_right_in_a_record_that_lists_its_issue_under_also(self):
        # People settled job 2 in 200, which record 100 says is the same problem: a
        # change that leaves job 2 in 100 keeps it right, as the score counts it.
        with open(os.path.join(self.problems, '100-op-reply.yaml'), 'w') as f:
            f.write(text(record(examples=[f'{RUN}/2'], also=['200'])))
        learn.save_label(self.labels, self.jobs['2'], '200', 'a person', 'it is 200', '')
        excluding = dict(EXCLUDING, also=['200'])
        analyst = FakeAnalyst([answer(None, [excluding])])
        self.assertTrue(self.doubt(analyst).run())
        self.assertIn(f'ok    {RUN}/2 (settled by a person): duplicate 100',
                      '\n'.join(self.said))

    def test_a_person_who_says_what_it_is_is_not_met_by_also(self):
        # People's earlier answers are met by a record that lists theirs under also,
        # and the label keeps what they said; a person who doubts the result now and
        # says what the job is is not: the analyst must place it in their issue.
        same = record(examples=[f'{RUN}/2'], also=['200'])
        for firm in (False, True):
            self.setUp()
            analyst = FakeAnalyst([answer('100', [same], quotes=(('teuthology.log', OP_REPLY),),
                                          assessment='right')] * 4)
            doubt = self.doubt(analyst, jid='2', expected='200', said_by_people=True,
                               firm=firm)
            self.assertEqual(doubt.run(), not firm)
            prompt = analyst.prompts[0][0]
            self.assertEqual(learn.PERSON_SAYS in prompt, firm)
            self.assertEqual(learn.ALSO_AGREES in prompt, not firm)
            labels = [(j.id, problem, who) for j, problem, who in learn.load_labels(self.labels)]
            self.assertEqual(labels, [] if firm else [('2', '200', 'settled by tester')])
        self.assertIn('tester expected 200, the analyst says 100: a person needs to decide',
                      '\n'.join(self.said))

    def test_people_overrule_what_only_an_agent_settled(self):
        # Job 2, an example of 100 and settled as it, failed the way job 7 did.
        # People say job 7 is 200: a change that moves both there is accepted if
        # only an agent settled job 2, and not if a person did.
        moved = record(examples=[f'{RUN}/2'],
                       exclude=[{'file': 'teuthology.log', 'pattern': 'op_reply'}])
        new = record(id='200', examples=[f'{RUN}/7'])
        for by, settles in (('qa_triage (fake)', True), ('a person', False)):
            self.setUp()
            self.files['7'], self.jobs['7'] = FakeFiles({'teuthology.log': [OP_REPLY]}), job('7')
            learn.save_label(self.labels, self.jobs['2'], '100', by, 'it was', 'op_reply')
            analyst = FakeAnalyst([answer('200', [moved, new],
                                          quotes=(('teuthology.log', OP_REPLY),))] * 4)
            settled = self.doubt(analyst, jid='7', expected='200', said_by_people=True).run()
            self.assertEqual(bool(settled), settles, by)
            labels = {j.id: problem for j, problem, _ in learn.load_labels(self.labels)}
            self.assertEqual(labels, {'2': '200', '7': '200'} if settles else {'2': '100'})
            self.assertEqual(f'{RUN}/2' in self.record_text(), not settles)
            # Its last example gone, record 100 has none, and still loads.
            self.assertEqual([p.examples for p in db.load(self.problems) if p.id == '100'],
                             [[] if settles else [f'{RUN}/2']])
            self.assertEqual('examples:' in self.record_text(), not settles)

    def test_the_source_is_linked(self):
        source = os.path.join(tempfile.mkdtemp(prefix='qa_triage-source-'), 'a' * 40)
        os.makedirs(source)
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        self.assertTrue(self.doubt(analyst, source=lambda files, say: {'source': source}).run())
        self.assertEqual(os.readlink(os.path.join(self.workdir, 'source')), source)
        self.assertIn(f'- source/: the ceph source the job ran, at {"a" * 40}',
                      analyst.prompts[0][0])
        # Without a checkout there is no link, and the prompt does not mention one.
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        self.doubt(analyst).run()
        self.assertFalse(os.path.lexists(os.path.join(self.workdir, 'source')))
        self.assertNotIn('source/', analyst.prompts[0][0])

    def test_a_pr_under_test_caused_it(self):
        from test_archive import commit, git
        repo = tempfile.mkdtemp(prefix='qa_triage-under-test-')
        git(repo, 'init', '-q', '-b', 'main')
        base = commit(repo, 'base')
        git(repo, 'checkout', '-q', '-b', 'fix', base)
        commit(repo, 'a fix')
        git(repo, 'checkout', '-q', '-b', 'testing', base)
        git(repo, 'merge', '-q', '--no-ff', 'fix', '-m', 'Merge pull request #1 from a/fix')
        git(repo, 'checkout', '-q', '-b', 'leaky', 'fix')
        commit(repo, 'Leak_DefinitelyLost, by a PR')
        git(repo, 'checkout', '-q', 'testing')
        git(repo, 'merge', '-q', '--no-ff', 'leaky', '-m',
            'Merge PR #72810 into wip-x-testing-20261019.181708-main')
        blames = (('teuthology.log', LEAK), ('under-test.txt', 'PR #72810'))
        mine = record(examples=[f'{RUN}/2', f'{RUN}/1'])
        analyst = FakeAnalyst([
            # Named as no PR under test; with the job still in a record; with a record
            # that claims it; and then as it should be.
            answer(None, [EXCLUDING], quotes=blames, pr='PR #99999'),
            answer('100', quotes=blames, pr='PR #72810'),
            answer(None, [mine], quotes=blames, pr='PR #72810'),
            answer(None, [EXCLUDING], quotes=blames, pr='PR #72810', summary='PR #72810 leaks')])
        doubt = self.doubt(analyst, source=lambda files, say: {'source': repo})
        self.assertTrue(doubt.run())
        self.assertIn('The PRs under test. A run tests a batch of PRs', analyst.prompts[0][0])
        self.assertIn('FAIL  PR #99999 caused it: but under-test.txt names no such PR',
                      analyst.prompts[1][0])
        self.assertIn('FAIL  PR #72810 caused it: then the job is new, the PR\'s',
                      analyst.prompts[2][0])
        self.assertIn('FAIL  PR #72810 caused it: then it needs no record', analyst.prompts[3][0])
        self.assertIn(f'{RUN}/1: {analyst.name} found that PR #72810, under test, caused it',
                      '\n'.join(self.said))
        [(j, problem, who)] = learn.load_labels(self.labels)
        self.assertEqual((j.id, problem), ('1', None))
        with open(learn._label_file(self.labels, RUN)) as f:
            self.assertEqual(yaml.safe_load(f)['jobs']['1']['pr'], 'PR #72810')
        # Without PRs under test, the prompt says nothing of them.
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        self.doubt(analyst).run()
        self.assertNotIn('The PRs under test.', analyst.prompts[0][0])

    def test_a_change_that_breaks_an_example_is_sent_back(self):
        strict = record(pattern='Leak_DefinitelyLost', examples=[f'{RUN}/2'])
        analyst = FakeAnalyst([answer('100', [strict], assessment='right'),
                               answer(None, [EXCLUDING])])
        doubt = self.doubt(analyst)
        self.assertTrue(doubt.run())
        prompt, session = analyst.prompts[1]
        self.assertIn('did not pass', prompt)
        self.assertIn(f'FAIL  {RUN}/2 (example of 100): new', prompt)
        self.assertEqual(session, 's1')
        self.assertIn('exclude', self.record_text())
        self.assertEqual(doubt.cost(), 0.5)

    def test_more_of_the_archive_and_a_new_record(self):
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        analyst = FakeAnalyst([
            answer(None, quotes=(), assessment='need-more',
                   requests=[{'file': 'remote/*/log/ceph-osd.*.log', 'pattern': 'bouncing'}]),
            answer('200', [new], quotes=(('requested/1-1.txt', 'bouncing to primary'),))])
        self.assertTrue(self.doubt(analyst, jid='3').run())
        self.assertIn('requested/1-1.txt', analyst.prompts[1][0])
        self.assertTrue(os.path.exists(os.path.join(self.problems,
                                                    '200-problem-200.yaml')))
        self.assertIn('ok    tracker 200: a problem (New)', '\n'.join(self.said))

    def test_a_record_of_a_pull_request(self):
        import urllib.error

        def of_pr(number, url='https://github.com/ceph/ceph/pull/'):
            pr = record(id=f'PR{number}', pattern='bouncing to primary', examples=[f'{RUN}/3'],
                        title='an OSD bounces writes', pr=f'{url}{number}')
            del pr['tracker']
            pr['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
            return pr

        def pulls(number):
            if number != '68305':
                raise urllib.error.HTTPError('', 404, 'Not Found', {}, None)
            return {'number': number, 'title': 'osd: stop bouncing writes', 'state': 'closed',
                    'merged': True, 'merge_commit': 'abc', 'body': 'Fixes the bounce.',
                    'files': [('src/osd/ECBackend.cc', ['handle_sub_write'])]}
        quotes = (('requested/1-1.txt', 'bouncing to primary'),)
        analyst = FakeAnalyst([
            answer(None, quotes=(), assessment='need-more',
                   requests=[{'file': 'remote/*/log/ceph-osd.*.log', 'pattern': 'bouncing'},
                             {'search_prs': 'bouncing writes'}, {'pr': '#68305'}]),
            answer('PR99999', [of_pr('99999')], quotes=quotes),
            answer('PR68305', [of_pr('68305')], quotes=quotes)])
        doubt = self.doubt(analyst, jid='3')
        doubt.pulls = pulls
        doubt.search_pulls = lambda words: [('68305', 'osd: stop bouncing writes', 'merged')]
        self.assertTrue(doubt.run())
        with open(os.path.join(self.workdir, 'requested', '1-2.txt')) as f:
            self.assertEqual(f.read(), 'PR 68305 (merged): osd: stop bouncing writes\n')
        with open(os.path.join(self.workdir, 'requested', '1-3.txt')) as f:
            self.assertIn('Pull request #68305: osd: stop bouncing writes', f.read())
        self.assertIn('FAIL  PR 99999 does not exist', analyst.prompts[2][0])
        self.assertIn('ok    PR 68305: osd: stop bouncing writes (merged)', '\n'.join(self.said))
        [p] = [p for p in db.load(self.problems) if p.id == 'PR68305']
        self.assertEqual((p.label, p.tracker, os.path.basename(p.source)),
                         ('PR 68305', 'https://github.com/ceph/ceph/pull/68305',
                          'PR68305-an-osd-bounces-writes.yaml'))
        [(j, problem, _)] = learn.load_labels(self.labels)
        self.assertEqual((j.path, problem), (f'{RUN}/3', 'PR68305'))
        # A record of a pull request names it, as one of an issue names the issue.
        for wrong, says in ((dict(of_pr('1'), pr=None), 'gives "pr"'),
                            (dict(of_pr('1'), id='PR2'), 'gives "pr"'),
                            (dict(of_pr('1'), id='x'), 'or PR and the number')):
            with self.assertRaisesRegex(ValueError, says):
                db.parse(wrong)
        self.assertEqual([learn.problem_id(n) for n in ('PR 68305', 'https://github.com/ceph/'
                                                        'ceph/pull/68305', 'TR 100', 'new')],
                         ['PR68305', 'PR68305', '100', None])

    def test_a_change_must_not_claim_jobs_that_failed_differently(self):
        loose = record(id='200', reason='.', pattern='bouncing to primary',
                       examples=[f'{RUN}/3'])
        loose['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        narrow = dict(loose, match={'reason': 'rados/test\\.sh'})
        quotes = (('teuthology.log', 'FAILED test'),)
        analyst = FakeAnalyst([answer('200', [loose], quotes=quotes),
                               answer('200', [narrow], quotes=quotes)])
        self.assertTrue(self.doubt(analyst, jid='3', corpus=[self.jobs['5']]).run())
        self.assertIn(f'FAIL  {RUN}/5 (not settled): no record -> 200, but it failed '
                      f'differently: "{WATCHDOG}"', analyst.prompts[1][0])
        self.assertIn('0 of the 1 jobs triaged before change result', '\n'.join(self.said))

    def test_a_record_in_the_tree_must_not_take_away_certainty(self):
        # 7 is explained conclusively, and no record fits it: new, for sure.
        self.jobs['7'] = job('7', reason='valgrind error: Leak_DefinitelyLost malloc')
        self.files['7'] = FakeFiles({'teuthology.log': ['valgrind found leaks']})
        broad = record(id='200', reason='.', pattern='bouncing to primary', tree='valgrind')
        broad.pop('match')
        analyst = FakeAnalyst([answer(None, [EXCLUDING, broad]), answer(None, [EXCLUDING])])
        self.assertTrue(self.doubt(analyst, corpus=[self.jobs['7']]).run())
        self.assertIn(f'FAIL  {RUN}/7 (not settled): matched -> unmatched new (valgrind/'
                      'Leak_DefinitelyLost): it is no longer matched, because 200 nearly fit it',
                      analyst.prompts[1][0])

    def test_a_change_may_give_back_certainty_a_record_took(self):
        self.jobs['7'] = job('7', reason='valgrind error: Leak_DefinitelyLost malloc')
        self.files['7'] = FakeFiles({'teuthology.log': ['valgrind found leaks']})
        broad = record(id='200', pattern='bouncing to primary', tree='valgrind')
        broad.pop('match')
        with open(os.path.join(self.problems, '200-broad.yaml'), 'w') as f:
            f.write(text(broad))
        narrow = dict(broad, facts={'kind': 'Leak_StillReachable'})
        analyst = FakeAnalyst([answer(None, [EXCLUDING, narrow])])
        self.assertTrue(self.doubt(analyst, corpus=[self.jobs['7']]).run())
        self.assertIn(f'ok    {RUN}/7 (not settled): unmatched -> matched new (valgrind/'
                      'Leak_DefinitelyLost), since a changed record no longer nearly fits it',
                      '\n'.join(self.said))

    def test_a_change_must_not_give_a_known_answer_another_wrong_one(self):
        loose = record(id='200', reason='.', pattern='bouncing to primary',
                       examples=[f'{RUN}/3'])
        loose['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        narrow = dict(loose, confirm=loose['confirm'] + [{'file': 'teuthology.log',
                                                          'pattern': 'FAILED test'}])
        quotes = (('teuthology.log', 'FAILED test'),)
        analyst = FakeAnalyst([answer('200', [loose], quotes=quotes),
                               answer('200', [narrow], quotes=quotes)])
        # 5 fails the way 3 does, so no other check stops the loose record; but its
        # answer is known.
        self.jobs['5'] = job('5')
        doubt = self.doubt(analyst, jid='3', known=[(self.jobs['5'], '300')])
        self.assertTrue(doubt.run())
        self.assertIn(f'FAIL  {RUN}/5 (known: 300): no record -> 200', analyst.prompts[1][0])

    def test_a_new_record_needs_its_trackers_fix(self):
        issue = dict(ISSUE, custom_fields=[{'name': 'Fixed In', 'value': 'v21.3.0-1-gabcdef1'},
                                           {'name': 'Pull request ID', 'value': '7'}])
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        fixed = dict(new, fixes=[{'pr': 'https://github.com/ceph/ceph/pull/7',
                                  'fixed_in': 'v21.3.0-1-gabcdef1'}])
        quotes = (('teuthology.log', 'FAILED test'),)
        analyst = FakeAnalyst([answer('200', [new], quotes=quotes),
                               answer('200', [fixed], quotes=quotes)])
        doubt = self.doubt(analyst, jid='3')
        doubt.tracker = lambda n: issue
        self.assertTrue(doubt.run())
        self.assertIn('it was fixed in v21.3.0-1-gabcdef1 (PR 7)', analyst.prompts[1][0])

    def test_interactive(self):
        analyst = FakeAnalyst([answer(None, [EXCLUDING], quotes=(('teuthology.log', 'nope'),)),
                               answer(None, [EXCLUDING])])
        doubt = self.doubt(analyst, replies=['a', 'that quote is not in the log', 'a'],
                           interactive=True)
        self.assertTrue(doubt.run())
        self.assertIn('cannot be accepted', '\n'.join(self.said))
        self.assertIn('tester replies', analyst.prompts[1][0])
        self.assertIn('that quote is not in the log', analyst.prompts[1][0])

    def test_disagreeing_with_the_doubter_needs_a_person(self):
        from qa_triage import approve
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        kept = os.path.join(self.workdir, 'disputed')
        doubt = self.doubt(analyst, expected='100', dispute=approve.keep_disputed(kept))
        self.assertFalse(doubt.run())
        self.assertNotIn('exclude', self.record_text())
        self.assertEqual(learn.load_labels(self.labels), [])
        # What the analyst says instead, for a person to weigh against the people's.
        self.assertEqual(doubt.disputed, 'no record')
        # Its change is kept for a person who agrees, answering as the analyst did.
        self.assertEqual(doubt.kept, approve.kept(kept, doubt.job))
        with open(doubt.kept) as f:
            held = json.load(f)
        self.assertEqual((held['expected'], held['disputes'], held['job']),
                         ('new', {'expected': '100', 'by': 'tester'}, doubt.job.path))
        self.assertNotIn('by', held)
        self.assertIn('Leak_DefinitelyLost', json.dumps(held['changes']))

    def test_gives_up_after_the_revisions(self):
        bad = answer(None, [EXCLUDING], quotes=(('teuthology.log', 'not there'),))
        analyst = FakeAnalyst([bad] * (learn.ROUNDS + 1))
        self.assertFalse(self.doubt(analyst).run())
        self.assertEqual(len(analyst.prompts), learn.ROUNDS + 1)
        self.assertNotIn('exclude', self.record_text())

    def test_an_analyst_that_stops_without_answering_is_asked_once_more(self):
        class Stops(FakeAnalyst):
            def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
                if not self.prompts:
                    self.prompts.append((prompt, session))
                    raise JudgeError('fake gave no answer', session='s1', cost=2.0)
                return super().ask(workdir, prompt, kind, session, tag)

        analyst = Stops([answer(None, [EXCLUDING])])
        self.assertTrue(self.doubt(analyst).run())
        self.assertEqual(analyst.prompts[1], (learn.ANSWER_NOW, 's1'))
        # Once only.
        analyst = Stops([])
        analyst.ask = lambda *a, **kw: (_ for _ in ()).throw(
            JudgeError('fake gave no answer', session='s1'))
        with self.assertRaisesRegex(JudgeError, 'no answer'):
            self.doubt(analyst).run()

    def test_a_lesson_has_a_budget(self):
        strict = record(pattern='Leak_DefinitelyLost', examples=[f'{RUN}/2'])
        analyst = FakeAnalyst([answer('100', [strict], assessment='right'),
                               answer(None, [EXCLUDING])])
        with self.assertRaisesRegex(JudgeError, 'cost 0.50, which is over the budget of 0.4'):
            self.doubt(analyst, budget=0.4).run()
        self.assertEqual(len(analyst.prompts), 1)

    def test_conversation_repeated_without_sessions(self):
        analyst = FakeAnalyst([answer('100', assessment='right', quotes=(('x', 'y'),)),
                               answer(None, [EXCLUDING])], resumes=False)
        self.assertTrue(self.doubt(analyst).run())
        self.assertIn('You are reviewing', analyst.prompts[1][0])

    def test_searching_the_tracker(self):
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        quotes = (('teuthology.log', 'FAILED test'),)
        analyst = FakeAnalyst([
            answer(None, quotes=(), assessment='need-more',
                   requests=[{'search': 'bouncing primary'}, {'tracker': '201'}]),
            answer('201', [dict(new, id='201')], quotes=quotes),
            answer('200', [new], quotes=quotes)])
        doubt = self.doubt(analyst, jid='3')
        doubt.search = lambda words: [('201', 'Backport #201 (New): squid: a problem')]
        doubt.tracker = lambda n: BACKPORT if n == '201' else ISSUE
        self.assertTrue(doubt.run())
        with open(os.path.join(self.workdir, 'requested', '1-1.txt')) as f:
            self.assertEqual(f.read(), '#201 Backport #201 (New): squid: a problem\n')
        with open(os.path.join(self.workdir, 'requested', '1-2.txt')) as f:
            self.assertIn('copied from #200', f.read())
        self.assertIn('it is a backport; give the record the number of the issue it names '
                      '(#200)', analyst.prompts[2][0])

    def test_a_run_learns(self):
        triage = Triage(db.load(self.problems), workdir=os.path.join(self.workdir, 'judge'))
        files = {'3': self.files['3']}
        results = [triage.run(self.jobs['3'], files['3'])]
        # The analyst looks only at what the judge was unsure of.
        sure = FakeJudge({'verdict': 'new', 'summary': 'an unstable write'}, 'cheap')
        judge_unplaced(triage, results, files, sure)
        # The judge was sure, but the tree needed it: a lesson for the tree.
        [(_, _, why, _)] = learn.lessons(results)
        self.assertIn('add a node at "workunit/unexplained"', why)
        results = [triage.run(self.jobs['3'], files['3'])]
        judge = FakeJudge({'verdict': 'unsure', 'summary': 'an unstable write'}, 'cheap')
        judge_unplaced(triage, results, files, judge)
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        analyst = FakeAnalyst([answer('200', [new], quotes=(('teuthology.log', 'FAILED test'),))])
        [line] = learn.learn_from_run(results, lambda j: self.files[j.id],
                                      lambda run: self.jobs, self.problems, self.labels,
                                      analyst, self.workdir, lambda n: ISSUE, None, [], 5,
                                      say=self.said.append)
        self.assertIn('3: learned 200, wrote problems/200-problem-200.yaml', line)
        with open(os.path.join(self.workdir, '3', 'doubt.yaml')) as f:
            self.assertIn('the judge (cheap) said unsure', f.read())
        triage.problems = db.load(self.problems)
        [r] = retriage(triage, results, files, judge)
        self.assertEqual((r.verdict, r.problem.id), ('duplicate', '200'))
        self.assertEqual(len(judge.calls), 1)

    def test_judges_at_once(self):
        # Two groups the tree could not decide: at once, each asks the judge before
        # either has an answer, and what both cost is kept.
        import threading
        self.files['8'] = FakeFiles({'teuthology.log': ['FAILED other']})
        self.jobs['8'] = job('8', reason='Command failed (workunit test rados/other.sh) on '
                                         'trial1 with status 1')
        triage = Triage(db.load(self.problems), workdir=os.path.join(self.workdir, 'judge'))
        results = [triage.run(self.jobs[j], self.files[j]) for j in ('3', '8')]
        both = threading.Barrier(2, timeout=10)

        class Together(FakeJudge):
            def __call__(self, workdir, prompts):
                both.wait()
                return super().__call__(workdir, prompts)

        judge = Together({'verdict': 'new', 'summary': 'a failure', 'cost': 0.5}, 'cheap')
        judge_unplaced(triage, results, {j: self.files[j] for j in ('3', '8')}, judge,
                       self.said.append, at_once=4)
        self.assertEqual([r.verdict for r in results], ['new', 'new'])
        self.assertEqual(sorted(self.said), ['3 judged new ', '8 judged new '])
        self.assertEqual(triage.spent, [('cheap', 0.5), ('cheap', 0.5)])
        self.assertIsNone(triage.judge)

    def test_lessons_at_once(self):
        # Two groups, each a lesson; at once, each asks the analyst before either has
        # an answer, and their lines come back in order.
        import threading
        self.files['8'] = FakeFiles({'teuthology.log': ['FAILED other']})
        self.jobs['8'] = job('8', reason='Command failed (workunit test rados/other.sh) on '
                                         'trial1 with status 1')
        triage = Triage(db.load(self.problems), workdir=os.path.join(self.workdir, 'judge'))
        results = [triage.run(self.jobs[j], self.files[j]) for j in ('3', '8')]
        judge_unplaced(triage, results, {j: self.files[j] for j in ('3', '8')},
                       FakeJudge({'verdict': 'unsure', 'summary': 'a failure'}, 'cheap'))
        both = threading.Barrier(2, timeout=10)

        class Together(FakeAnalyst):
            def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
                both.wait()
                line = 'FAILED test' if os.path.basename(workdir) == '3' else 'FAILED other'
                self.progress(f'  reads {os.path.basename(workdir)}')
                return dict(answer(None, quotes=(('teuthology.log', line),),
                                   assessment='right'), judge=self.name, session='s1', cost=0.5)

        from qa_triage.agents import Stoppable

        class Analyst(Together, Stoppable):
            pass

        lines = learn.learn_from_run(results, lambda j: self.files[j.id], lambda run: self.jobs,
                                     self.problems, self.labels, Analyst([]), self.workdir,
                                     lambda n: ISSUE, None, [], 5, say=self.said.append,
                                     at_once='all')
        self.assertEqual([line.split(':')[0] for line in lines], ['3', '8'])
        self.assertTrue(all('no record should place it' in line for line in lines), lines)
        self.assertIn('2 lessons, 2 at once; each line of one begins with its job', self.said)
        # Each step says whose lesson it is.
        self.assertIn('3:   reads 3', self.said)
        self.assertIn('8:   reads 8', self.said)

    def test_a_record_another_lesson_changed_meanwhile_is_changed_again(self):
        # Two lessons at once both change record 100. The first keeps its change; the
        # second, which was given record 100 before that, is told to make its change
        # again on it as it is now, and is given it so.
        first = self.doubt(FakeAnalyst([]))
        first.setup()
        given, self.workdir = self.workdir, self.workdir + '-2'
        second = self.doubt(FakeAnalyst([]))
        self.workdir = given
        second.setup()
        ok, report, writes = first.check(answer(None, [EXCLUDING]))
        self.assertTrue(ok, report)
        self.assertTrue(first._accept(answer(None, [EXCLUDING]), writes, report))
        other = record(examples=[f'{RUN}/2'],
                       exclude=[{'file': 'teuthology.log', 'pattern': 'in ceph-mon'}])
        ok, report, _ = second.check(answer(None, [other]))
        self.assertFalse(ok)
        self.assertIn('FAIL  problems/100-op-reply.yaml changed in the library while you '
                      'worked', '\n'.join(report))
        with open(os.path.join(second.workdir, 'records', '100-op-reply.yaml')) as f:
            self.assertIn('Leak_DefinitelyLost', f.read())
        # Made again on it, it is checked as any change is.
        ok, report, _ = second.check(answer(None, [EXCLUDING]))
        self.assertNotIn('changed in the library', '\n'.join(report))

    def test_a_job_settled_while_the_analyst_worked_is_not_written_over(self):
        doubt = self.doubt(FakeAnalyst([]))
        doubt.setup()
        learn.save_label(self.labels, self.jobs['1'], '100', 'another reviewer', 'they looked',
                         'a summary')
        ok, report, _ = doubt.check(answer(None, [EXCLUDING]))
        self.assertFalse(ok)
        self.assertIn(f'FAIL  {RUN}/1 was settled by another reviewer, as 100, while you '
                      'worked', '\n'.join(report))
        # An answer that agrees with them is checked as any is.
        _, report, _ = doubt.check(answer('100'))
        self.assertNotIn('while you worked', '\n'.join(report))

    def test_qa_triage_processes_on_one_machine_take_turns_at_a_library(self):
        import fcntl
        import hashlib
        import threading
        name = hashlib.sha1(os.path.realpath(self.problems).encode()).hexdigest()[:12]
        other = open(os.path.join(tempfile.gettempdir(), f'qa_triage-{name}.lock'), 'a')
        self.addCleanup(other.close)
        fcntl.flock(other, fcntl.LOCK_EX)
        threading.Timer(0.3, lambda: fcntl.flock(other, fcntl.LOCK_UN)).start()
        said = []
        with learn.holding(self.problems, said.append):
            self.assertEqual(said, ['  waiting for another qa_triage on this machine, which is '
                                    'changing the library'])
            # Held, the other waits.
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_change_others_pushed_over_is_checked_again_with_theirs(self):
        from test_share import ENV, git
        patched = mock.patch.dict(os.environ, {k: v for k, v in ENV.items()
                                               if k.startswith('GIT_')})
        patched.start()
        self.addCleanup(patched.stop)
        root = os.path.dirname(self.problems)
        seed, remote = os.path.join(root, 'seed'), os.path.join(root, 'library.git')
        shutil.copytree(self.problems, os.path.join(seed, 'problems'))
        git('init', '-q', '-b', 'main', seed)
        git('-C', seed, 'add', '-A')
        git('-C', seed, 'commit', '-q', '-m', 'seed')
        git('clone', '-q', '--bare', seed, remote)
        mine, theirs = os.path.join(root, 'mine'), os.path.join(root, 'theirs')
        for clone in (mine, theirs):
            git('clone', '-q', remote, clone)

        def push_theirs(name, rec):
            with open(os.path.join(theirs, 'problems', name), 'w') as f:
                f.write(text(rec))
            share.publish(theirs, f'Learned {rec["id"]}')

        def accept(theirs_change):
            # They push just after this lesson brought the library up to date, and
            # before it pushes.
            self.labels = os.path.join(mine, 'labels')
            self.problems = os.path.join(mine, 'problems')
            doubt = self.doubt(FakeAnalyst([]), library=mine, publish=True)
            doubt.setup()
            given = answer(None, [EXCLUDING])
            ok, report, writes = doubt.check(given)
            self.assertTrue(ok, report)
            sync, raced = share.sync, []

            def racing(directory, say=print):
                moved = sync(directory, say)
                if not raced:
                    raced.append(True)
                    push_theirs(*theirs_change)
                return moved
            with mock.patch.object(share, 'sync', racing):
                return doubt._accept(given, writes, report)

        # A change of theirs to another record: this one is checked again with it, and
        # pushed after it.
        other = record(id='200', pattern='nothing like this')
        self.assertTrue(accept(('200-other.yaml', other)))
        self.assertIn(f'{RUN}/1: the library changed while this change was checked',
                      '\n'.join(self.said))
        self.assertEqual(git('-C', remote, 'log', '--format=%s', 'main').splitlines(),
                         [f'Learned from {RUN}/1: a summary', 'Learned 200', 'seed'])
        # A change of theirs to the same record: this one would undo it, and is for the
        # analyst to make again on theirs.
        git('-C', theirs, 'pull', '-q')
        theirs_100 = record(examples=[f'{RUN}/2'],
                            exclude=[{'file': 'teuthology.log', 'pattern': 'in ceph-mon'}])
        with open(os.path.join(mine, 'problems', '100-op-reply.yaml'), 'w') as f:
            f.write(text(BASE))
        git('-C', mine, 'commit', '-q', '-am', 'back to the base')
        git('-C', mine, 'push', '-q')
        git('-C', theirs, 'pull', '-q')
        with self.assertRaises(learn.Collided) as caught:
            accept(('100-op-reply.yaml', theirs_100))
        self.assertIn('problems/100-op-reply.yaml changed in the library while you worked',
                      str(caught.exception))
        self.assertEqual(git('-C', remote, 'log', '--format=%s', '-1', 'main'), 'Learned 100')

    def test_a_run_teaches_the_tree(self):
        self.addCleanup(forget_learned)
        nodes = os.path.join(os.path.dirname(self.problems), 'nodes')
        self.files['6'] = FakeFiles({'teuthology.log': ['FAILED test'],
                                     'remote/h1/log/ceph-osd.0.log': [UNSTABLE]})
        self.files['7'] = FakeFiles({'teuthology.log': ['FAILED test']})
        for jid, script in (('6', 'other'), ('7', 'third')):
            self.jobs[jid] = job(jid, reason=f'Command failed (workunit test rados/{script}.sh) '
                                             'on trial1 with status 1')
        triage = Triage(db.load(self.problems), workdir=os.path.join(self.workdir, 'judge'))
        files = {'6': self.files['6']}
        results = [triage.run(self.jobs['6'], files['6'])]
        judge = FakeJudge({'verdict': 'new', 'summary': 'an unstable write'}, 'cheap')
        judge_unplaced(triage, results, files, judge)
        # A judge's "new" is not sure: it knows the records, not every issue.
        self.assertFalse(results[0].sure)
        quotes = (('teuthology.log', 'FAILED test'),)
        analyst = FakeAnalyst([
            answer(None, quotes=quotes, assessment='right',
                   nodes=[{'file': 'workunit-unexplained.py', 'code': SLOPPY}]),
            answer(None, quotes=quotes, assessment='right',
                   nodes=[{'file': 'nodes/workunit-unexplained.py', 'code': NODE}])])
        [line] = learn.learn_from_run(results, lambda j: self.files[j.id],
                                      lambda run: self.jobs, self.problems, self.labels,
                                      analyst, self.workdir, lambda n: ISSUE, None,
                                      [self.jobs['6'], self.jobs['7']], 5,
                                      say=self.said.append, nodes_dir=nodes,
                                      history=learn.history_of(results))
        self.assertIn('6: learned a new problem, wrote nodes/workunit-unexplained.py', line)
        self.assertTrue(os.path.exists(os.path.join(self.workdir, '6', 'tree', 'core.py')))
        # The node that claimed a job that failed differently was sent back.
        self.assertIn(f'FAIL  {RUN}/7 (not settled): unmatched -> matched new', analyst.prompts[1][0])
        self.assertIn('but it failed differently', analyst.prompts[1][0])
        # And it decided without citing the lines it decided by.
        self.assertIn('answered unstable-write and cited no line', analyst.prompts[1][0])
        self.assertTrue(os.path.exists(os.path.join(nodes, 'workunit-unexplained.py')))
        # From now on, the tree decides jobs like it without the judge.
        [r] = retriage(triage, results, files, judge)
        self.assertEqual((r.verdict, r.sure, r.finding.where),
                         ('new', True, 'workunit/unexplained/unstable-write'))
        self.assertEqual(len(judge.calls), 1)

    def unstable(self):
        """Job 6, which NODE explains, and an answer that teaches NODE from it."""
        self.addCleanup(forget_learned)
        self.files['6'] = FakeFiles({'teuthology.log': ['FAILED test'],
                                     'remote/h1/log/ceph-osd.0.log': [UNSTABLE]})
        self.jobs['6'] = job('6', reason='Command failed (workunit test rados/other.sh) '
                                         'on trial1 with status 1')
        return answer(None, quotes=(('teuthology.log', 'FAILED test'),), assessment='right',
                      nodes=[{'file': 'nodes/workunit-unexplained.py', 'code': NODE}])

    def test_the_checks_say_how_far_they_have_got_and_stop_with_the_analyst(self):
        # The checks read the logs of each job they walk, on the archive's host or
        # fetched from qa-proxy, which can take a long while after the analyst is
        # done: the person sees them, and can stop them.
        nodes = os.path.join(os.path.dirname(self.problems), 'nodes')
        given = self.unstable()
        analyst, steps = FakeAnalyst([]), []

        def step(line):
            steps.append(line)
            if stop and line.startswith('  checks: 1 of 4 jobs walked'):
                analyst.stopped = True

        doubt = self.doubt(analyst, jid='6', nodes_dir=nodes, progress=step,
                           corpus=[self.jobs[j] for j in ('1', '2', '3', '5')])
        doubt.setup()
        stop = False
        with mock.patch.object(learn, 'TICK', 0):
            doubt.check(given)
        self.assertEqual(steps[0], '  checks: its answer, against the jobs it could change')
        for n in range(1, 5):
            self.assertIn(f'  checks: {n} of 4 jobs walked through the tree, to see if the '
                          'change could reach them', steps)
        self.assertIn('  checks: 1 of 1 jobs settled before triaged with the change', steps)
        # Stopped half way: it checks no more. And its logs downloaded over https, it
        # says that is slow.
        steps.clear()
        learn._FINDINGS.clear()
        stop = True
        doubt.files = mock.Mock(spec=learn.JobFiles)
        with mock.patch.object(learn, 'TICK', 0), self.assertRaises(Stopped):
            doubt.check(given)
        self.assertFalse([line for line in steps if 'with the change' in line])
        self.assertIn('downloaded from qa-proxy over https, which is slow', steps[1])
        # Nor does it begin, once stopped.
        steps.clear()
        with self.assertRaises(Stopped):
            doubt.check(given)
        self.assertEqual(steps, [])

    def test_a_change_to_the_nodes_must_pass_the_tools_tests(self):
        # Every node is the analyst's to change, the root's too: the tool's tests are
        # run on the tree as the change leaves it, and a test it fails sends it back.
        nodes = os.path.join(os.path.dirname(self.problems), 'nodes')
        given = self.unstable()
        doubt = self.doubt(FakeAnalyst([]), jid='6', nodes_dir=nodes)
        doubt.setup()
        ok, report, _ = doubt.check(given)
        self.assertTrue(ok, report)
        self.assertIn("ok    the tool's tests pass with the change", report)
        tested = []

        def run_tests(directory):
            with open(os.path.join(directory, 'workunit-unexplained.py')) as f:
                tested.append(f.read())
            return ['FAIL: test_x (test_tree.TestTree.test_x)', "  AssertionError: 'a' != 'b'"]

        with mock.patch.object(learn, 'run_tests', run_tests):
            ok, report, _ = doubt.check(given)
        self.assertEqual(tested, [NODE])
        self.assertFalse(ok)
        self.assertIn("FAIL  the tool's tests fail with the change: make the nodes pass "
                      'them, or, if a test expects what is wrong, say which in '
                      '"tool_suggestions"', report)
        self.assertIn('      FAIL: test_x (test_tree.TestTree.test_x)', report)

    def test_a_changed_node_keeps_its_examples(self):
        # A node file may not drop an example, nor move one: only the doubted job's
        # path may change, since people said what it is.
        nodes = os.path.join(os.path.dirname(self.problems), 'nodes')
        given = self.unstable()
        os.makedirs(nodes, exist_ok=True)
        before = "EXAMPLES = {'%s/6': 'workunit/unexplained/unstable-write'}" % RUN
        with open(os.path.join(nodes, 'workunit-unexplained.py'), 'w') as f:
            f.write(NODE.replace(before, "EXAMPLES = {'%s/6': 'workunit/unexplained/was', "
                                         "'%s/5': 'workunit/unexplained/other'}" % (RUN, RUN)))
        learned.load(nodes)
        doubt = self.doubt(FakeAnalyst([]), jid='6', nodes_dir=nodes)
        doubt.setup()
        ok, report, _ = doubt.check(given)
        self.assertFalse(ok)
        self.assertIn(f'FAIL  nodes/workunit-unexplained.py loses examples: {RUN}/5: keep '
                      'every example, with its path', report)
        self.assertFalse([line for line in report if f'{RUN}/6 from' in line])
        moved = copy.deepcopy(given)
        moved['nodes'][0]['code'] = NODE.replace(
            before, "EXAMPLES = {'%s/6': 'workunit/unexplained/unstable-write', "
                    "'%s/5': 'workunit/unexplained/else'}" % (RUN, RUN))
        ok, report, _ = doubt.check(moved)
        self.assertIn(f'FAIL  nodes/workunit-unexplained.py moves its example {RUN}/5 from '
                      'workunit/unexplained/other to workunit/unexplained/else: only this '
                      "job's path may change; doubt that job to move it", report)

    def test_the_library_keeps_where_the_tree_takes_its_jobs(self):
        # Where the tree took a job of the library, kept, says the change cannot reach
        # it: its logs are not read. The jobs the change was tried on keep where it
        # takes them, for the next change's checks.
        nodes = os.path.join(os.path.dirname(self.problems), 'nodes')
        given = self.unstable()
        paths = learn.paths_dir(self.labels)
        learn.save_paths(paths, {f'{RUN}/2': tree.Finding(path=['cluster-log', 'message'],
                                                          facts={'daemon': 'osd'})})
        self.assertEqual(learn.load_paths(paths)[f'{RUN}/2'].facts, {'daemon': 'osd'})
        read = []
        doubt = self.doubt(FakeAnalyst([]), jid='6', nodes_dir=nodes,
                           corpus=[self.jobs['1']])
        doubt.files_for = lambda j: read.append(j.id) or self.files[j.id]
        doubt.setup()
        ok, report, writes = doubt.check(given)
        self.assertTrue(ok, report)
        self.assertNotIn('2', read)
        self.assertTrue(doubt._accept(given, writes, report))
        kept = learn.load_paths(paths)
        self.assertEqual(kept[f'{RUN}/6'].where, 'workunit/unexplained/unstable-write')
        self.assertEqual(kept[f'{RUN}/2'].where, 'cluster-log/message')
        # Kept by other tree code, it is walked again, and the checks say why; what
        # they walk is kept with this code, and the rest of the run's file is not.
        [name] = os.listdir(paths)
        with open(os.path.join(paths, name)) as f:
            data = yaml.safe_load(f)
        with open(os.path.join(paths, name), 'w') as f:
            yaml.safe_dump(dict(data, code='other'), f, sort_keys=False)
        stale = []
        self.assertEqual(learn.load_paths(paths, stale), {})
        self.assertEqual(stale, [name])
        learn._FINDINGS.clear()
        read.clear()
        steps = []
        doubt = self.doubt(FakeAnalyst([]), jid='6', nodes_dir=nodes, progress=steps.append,
                           corpus=[self.jobs['1']])
        doubt.files_for = lambda j: read.append(j.id) or self.files[j.id]
        doubt.setup()
        ok, report, writes = doubt.check(given)
        self.assertIn('2', read)
        self.assertIn('  checks: the paths kept for 1 runs of the library were found by other '
                      'tree code, so their jobs are walked again (qa_triage paths walks them all)',
                      steps)
        learn.save_paths(paths, {f'{RUN}/1': tree.Finding(path=['crash'])})
        self.assertEqual(sorted(learn.load_paths(paths)), [f'{RUN}/1'])
        # Without it, the job is walked, as before.
        learn._FINDINGS.clear()
        shutil.rmtree(paths)
        read.clear()
        doubt.check(given)
        self.assertIn('2', read)

    def test_only_the_examples_a_change_can_reach_are_walked(self):
        files = {'a.py': mock.Mock(examples={'r/1': 'workunit/unexplained/x'}),
                 'b.py': mock.Mock(examples={'r/2': 'crash/signal'}),
                 'c.py': mock.Mock(examples={'r/3': 'workunit'})}
        with mock.patch.dict(learned.FILES, files, clear=True):
            self.assertEqual(learn.reachable_examples({'d.py': ''}, {'workunit/unexplained'}),
                             [('a.py', 'r/1', 'workunit/unexplained/x'),
                              ('c.py', 'r/3', 'workunit')])
            # A changed file's own, wherever they are.
            self.assertEqual(learn.reachable_examples({'b.py': ''}, {'crash/signal/x'}),
                             [('b.py', 'r/2', 'crash/signal')])

    def test_the_analyst_tells_it_better_and_words_the_tree(self):
        from qa_triage.tree import core
        self.addCleanup(core.WORDING.clear)
        nodes = os.path.join(os.path.dirname(self.problems), 'nodes')
        os.makedirs(nodes)
        judged = {'judge': 'cheap', 'verdict': 'new', 'summary': 'a test failed',
                  'explanation': 'The judge\'s account.',
                  'walkthrough': [{'title': 'The test', 'do': 'Look.', 'finds': 'It failed.',
                                   'evidence': []}]}
        better = [{'title': 'Where it failed', 'do': 'Open teuthology.log at line 1.',
                   'finds': 'The test failed.',
                   'evidence': [{'file': 'teuthology.log', 'text': 'FAILED test',
                                 'shows': 'the failure'}]}]
        analyst = FakeAnalyst([answer(None, quotes=(('teuthology.log', 'FAILED test'),),
                                      assessment='right', walkthrough=better, wording=[
                                          {'node': '', 'answer': 'workunit',
                                           'says': 'The {workunit} workunit failed.'}])])
        doubt = self.doubt(analyst, jid='3', nodes_dir=nodes, judged=judged)
        self.assertTrue(doubt.run())
        # It was given the judge's account, to improve on.
        with open(os.path.join(self.workdir, 'judge.txt')) as f:
            given = f.read()
        self.assertIn('The judge (cheap) said new: a test failed', given)
        self.assertIn('1. The test. Look. It failed.', given)
        self.assertIn('nodes/wording.yaml', doubt.written)
        # Its account is the job's, and the jobs' that failed the same way, with the
        # line it quotes found in the archive.
        r = Triage(db.load(self.problems)).run(self.jobs['3'], self.files['3'])
        learn.keep_account([r], r, doubt.answer, 'fake', doubt.files, doubt.workdir)
        self.assertEqual((r.analysis['analyst'], r.analysis['walkthrough'][0]['title']),
                         ('fake', 'Where it failed'))
        self.assertEqual(r.analysis['walkthrough'][0]['evidence'][0]['at'], 'teuthology.log:1')
        # The tree's answer reads as the analyst worded it, from now on.
        with open(os.path.join(nodes, 'wording.yaml')) as f:
            self.assertEqual(learned.wording(f.read()),
                             {'': {'workunit': 'The {workunit} workunit failed.'}})
        finding = tree.explain(self.jobs['3'], self.files['3'])
        self.assertEqual(finding.steps[0].says, 'The rados/test.sh workunit failed.')

    def test_wording_of_a_node_there_is_not(self):
        nodes = os.path.join(os.path.dirname(self.problems), 'nodes')
        os.makedirs(nodes)
        quotes = (('teuthology.log', 'FAILED test'),)
        analyst = FakeAnalyst([
            answer(None, quotes=quotes, assessment='right',
                   wording=[{'node': 'no/such', 'answer': 'x', 'says': 'y'}]),
            answer(None, quotes=quotes, assessment='right')])
        self.assertTrue(self.doubt(analyst, jid='3', nodes_dir=nodes).run())
        self.assertIn('FAIL  wording: there is no node at "no/such"', analyst.prompts[1][0])

    def test_a_lesson_is_paid_for_once(self):
        results = [Triage(db.load(self.problems)).run(self.jobs['3'], self.files['3'])]
        results[0].agent, results[0].group = {'judge': 'cheap', 'verdict': 'new'}, '3'

        class Stops(FakeAnalyst):
            """Stops without answering when it has no answers left. Its costs are
            the session's so far, as some agents report them."""

            def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
                if not self.answers:
                    self.prompts.append((prompt, session))
                    raise JudgeError('fake gave no answer', session='s1', cost=3.0)
                return dict(super().ask(workdir, prompt, kind, session, tag), cost=3.5)

        def learn_(analyst, out):
            return learn.learn_from_run(
                results, lambda j: self.files[j.id], lambda run: self.jobs, self.problems,
                self.labels, analyst, os.path.join(self.workdir, out, 'learn'),
                lambda n: ISSUE, None, [], 5, say=self.said.append,
                earlier=glob.glob(os.path.join(self.workdir, '*', 'learn', '*', '')))

        # The analyst stops without answering, even when asked for its answer.
        analyst = Stops([])
        [line] = learn_(analyst, 'run1')
        self.assertIn('not settled', line)
        self.assertEqual([p for p, _ in analyst.prompts][1:], [learn.ANSWER_NOW])
        with open(os.path.join(self.workdir, 'run1', 'learn', '3', 'lesson.json')) as f:
            lesson = json.load(f)
        self.assertEqual((lesson['outcome'], lesson['session'], lesson['cost']),
                         ('no-answer', 's1', 3.0))
        # A later run does not start it again: it resumes the session, once.
        analyst = Stops([answer(None, quotes=(('teuthology.log', 'FAILED test'),),
                                assessment='right')])
        [line] = learn_(analyst, 'run2')
        self.assertEqual(analyst.prompts, [(learn.ANSWER_NOW, 's1')])
        self.assertIn('no record should place it', line)
        self.assertIn('cost 0.50', line)
        # And then leaves it to a person.
        analyst = Stops([])
        [line] = learn_(analyst, 'run3')
        self.assertEqual(analyst.prompts, [])
        self.assertIn('3: taught before, for 0.50: there was nothing to learn: a summary', line)
        self.assertIn('Left for a person', line)
        # A lesson that expects another answer, such as a reviewer's, is another lesson.
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        analyst = Stops([answer('200', [new], quotes=(('teuthology.log', 'FAILED test'),))])
        [line] = learn.learn_from_run(
            results, lambda j: self.files[j.id], lambda run: self.jobs, self.problems,
            self.labels, analyst, os.path.join(self.workdir, 'run4', 'learn'),
            lambda n: ISSUE, None, [], 5, say=self.said.append, known={'3': '200'},
            earlier=glob.glob(os.path.join(self.workdir, '*', 'learn', '*', '')))
        self.assertIn('3: learned 200', line)
        # Settled as the reviewers said, so no agent's lesson may move it later.
        self.assertIn((f'{RUN}/3', '200', 'settled by the QA reviewers, via qa_triage (fake)'),
                      [(j.path, p, by) for j, p, by in learn.load_labels(self.labels)])

    def test_an_analyst_that_does_not_answer_or_is_stopped(self):
        results = [Triage(db.load(self.problems)).run(self.jobs['3'], self.files['3'])]
        results[0].agent, results[0].group = {'judge': 'cheap', 'verdict': 'new'}, '3'

        class Broken(FakeAnalyst):
            def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
                self.prompts.append((prompt, session))
                raise JudgeError('fake gave no answer (exit 1): out of credit')

        class Stopping(FakeAnalyst):
            def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
                self.prompts.append((prompt, session))
                raise Stopped('fake: stopped')

        def learn_(analyst, out):
            return learn.learn_from_run(
                results, lambda j: self.files[j.id], lambda run: self.jobs, self.problems,
                self.labels, analyst, os.path.join(self.workdir, out, 'learn'),
                lambda n: ISSUE, None, [], 5, say=self.said.append,
                earlier=glob.glob(os.path.join(self.workdir, '*', 'learn', '*', '')))

        # It gave nothing at all: no more lessons are asked, and it is not taught.
        analyst = Broken([])
        [line] = learn_(analyst, 'run1')
        self.assertEqual(line, '3: ' + learn.NOT_BEGUN + 'fake gave no answer (exit 1): out of '
                               'credit')
        self.assertEqual(len(analyst.prompts), 1)
        with open(os.path.join(self.workdir, 'run1', 'learn', '3', 'lesson.json')) as f:
            self.assertEqual(json.load(f)['outcome'], 'not-begun')
        # A person stops it: so it is not taught, and is asked again later.
        analyst = Stopping([])
        with self.assertRaises(Stopped):
            learn_(analyst, 'run2')
        with open(os.path.join(self.workdir, 'run2', 'learn', '3', 'lesson.json')) as f:
            self.assertEqual(json.load(f)['outcome'], 'stopped')
        analyst = FakeAnalyst([answer(None, quotes=(('teuthology.log', 'FAILED test'),),
                                      assessment='right')])
        [line] = learn_(analyst, 'run3')
        self.assertEqual(len(analyst.prompts), 1)
        self.assertIn('no record should place it', line)

    def test_a_lesson_stopped_before_it_was_written(self):
        class Reports(FakeAnalyst):
            def session(self, output):
                return output['stats']['task_id']

            def cost(self, output):
                return output['stats']['session_costs']

        directory = os.path.join(self.workdir, 'learn', '3')
        os.makedirs(directory)
        for name, text in (('doubt.yaml', f'job: {RUN}/3\n'),
                           ('analysis.md', f'# Doubt about {RUN}/3\n\n'),
                           ('doubt-1.out', '{"type": "error"}\n{"type": "result", "stats": '
                                           '{"task_id": "t1", "session_costs": 7.5}}\n')):
            with open(os.path.join(directory, name), 'w') as f:
                f.write(text)
        lesson = learn.lesson_of(directory, Reports([]))
        self.assertEqual((lesson['job'], lesson['outcome'], lesson['session'], lesson['cost']),
                         (f'{RUN}/3', 'no-answer', 't1', 7.5))
        self.assertIsNone(learn.lesson_of(self.workdir, Reports([])))

    def test_the_analyst_gets_the_log_of_the_daemon_that_logged_the_error(self):
        error = 'Error -2 reading object 33:40961bb0:ns::myobject:head'
        self.jobs['8'] = job('8', reason=f'"2026-10-12T20:56:49.022882+0000 osd.4 (osd.4) 3 : '
                                         f'cluster [ERR] {error}" in cluster log')
        osd = [f'line {n}' for n in range(1, 1001)]
        osd[499] = 'do_read_op: will send ECSubRead for 33:40961bb0:ns::myobject:head'
        osd[599] = f'log_channel(cluster) log [ERR] : {error}'
        self.files['8'] = FakeFiles({'teuthology.log': ['the job failed'],
                                     'remote/h1/log/ceph-osd.4.log': osd})
        analyst = FakeAnalyst([answer(None, quotes=(('daemon-log.txt', 'will send ECSubRead'),),
                                      assessment='right')])
        self.assertTrue(self.doubt(analyst, jid='8').run())
        with open(os.path.join(self.workdir, 'daemon-log.txt')) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], '# remote/h1/log/ceph-osd.4.log, lines 200 to 650: around '
                                   'where osd.4 logged the error')
        self.assertEqual((lines[1], lines[-6]), ('200: line 200', '650: line 650'))
        self.assertEqual(lines[-3:], ['# remote/h1/log/ceph-osd.4.log: the lines that name '
                                      '33:40961bb0:ns::myobject:head (at most 200)',
                                      '500: do_read_op: will send ECSubRead for '
                                      '33:40961bb0:ns::myobject:head',
                                      f'600: log_channel(cluster) log [ERR] : {error}'])

    def test_the_analyst_is_told_how_far_the_tree_got(self):
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        self.assertTrue(self.doubt(analyst).run())
        prompt = analyst.prompts[0][0]
        self.assertIn('What the tool did with this job before you:', prompt)
        self.assertIn('The tree answered 2 questions, to "workunit/unexplained", and stopped, '
                      'inconclusive', prompt)
        self.assertIn('1. What went wrong first? workunit:', prompt)
        self.assertIn('Its result: duplicate 100', prompt)
        self.assertIn('Why you are asked: it leaked', prompt)

    def test_a_new_failure_is_debugged(self):
        triage = Triage(db.load(self.problems))
        self.jobs['5'] = job('5', reason='"2026-10-12T20:56:49.022882+0000 osd.4 (osd.4) 3 : '
                                         'cluster [ERR] Error -2 reading object x" in cluster log')
        self.jobs['6'] = job('6', reason='Failed to fetch package version from https://shaman')
        self.files['6'] = FakeFiles({'teuthology.log': ['INFO:teuthology.run_tasks:Running '
                                                        'task internal.check_packages...']})
        results = [triage.run(self.jobs[jid], self.files[jid]) for jid in ('5', '6')]
        self.assertEqual([(r.verdict, r.sure) for r in results], [('new', True)] * 2)
        # The infrastructure's failure is left out.
        [(r, expected, why, _)] = learn.lessons(results)
        self.assertEqual((r.job.id, expected), ('5', None))
        self.assertIn('Nobody has looked for its cause', why)
        self.assertIn('a node at "cluster-log/message"', why)
        analyst = FakeAnalyst([answer(None, quotes=(('teuthology.log', 'an OSD died'),),
                                      assessment='right', summary='a harmless read race')])
        learn.learn_from_run(results, lambda j: self.files[j.id], lambda run: self.jobs,
                             self.problems, self.labels, analyst, self.workdir,
                             lambda n: ISSUE, None, [], 5, say=self.said.append)
        self.assertIn('The analyst found: a harmless read race', results[0].notes)
        self.assertIn("['5'] - a harmless read race", report.qa_comment(RUN, results))

    def test_what_the_memory_placed_is_for_the_records_to_place(self):
        # The records' checks missed the job, and what people answered for the same
        # failure placed it: there is something to learn, that the checks place it.
        triage = Triage(db.load(self.problems))
        r = triage.run(self.jobs['3'], self.files['3'])
        record = next(p for p in triage.problems if p.id == '100')
        r.path.append('memory')
        r.problem, r.verdict, r.sure = record, 'probable-duplicate', True
        [(got, expected, why, size)] = learn.lessons([r])
        self.assertEqual((got.job.id, expected, size), ('3', '100', 1))
        self.assertIn("the tool's memory of their answers placed it there", why)
        self.assertIn('change record 100: where one of its checks finds no line', why)
        # An issue that has no record yet is given one.
        r.problem = memory.unrecorded('300')
        [(_, expected, why, _)] = learn.lessons([r])
        self.assertEqual(expected, '300')
        self.assertIn('add a record for 300, which has none yet', why)

    def test_the_decisions_for_the_console(self):
        triage = Triage(db.load(self.problems))
        placed, nearly = (triage.run(self.jobs[jid], self.files[jid]) for jid in ('1', '3'))
        lines = report.decisions(placed)
        self.assertEqual(lines[0], '  What went wrong first? workunit: the workunit '
                                   'rados/test.sh failed')
        self.assertIn('  inconclusive at workunit/unexplained', lines)
        self.assertEqual(lines[-3:], ['  record TR 100: its checks found the lines they look for',
                                      '      teuthology.log:1: api_io_pp: Perf counter '
                                      "'objecter.op_reply' has incorrect value after operate().",
                                      "  => duplicate TR 100, matched: record TR 100's checks all "
                                      'passed'])
        self.assertEqual(report.decisions(nearly)[-2:], [
            '  record TR 100: confirm /objecter.op_reply/ not found',
            '  => new, unmatched: records nearly fit it, so a judge must decide'])

    def test_what_people_wrote(self):
        issue = dict(ISSUE, id=300, custom_fields=[{'name': 'Pull request ID', 'value': '72389'}],
                     journals=[{'user': {'name': 'A Person'}, 'created_on': '2026-10-20T10:00Z',
                                'notes': 'The cause is a race in WriteOp::_finish; see '
                                         'https://github.com/ceph/ceph/pull/72400'},
                               {'user': {'name': 'B'}, 'notes': ''}])
        self.assertEqual(learn.pull_numbers(issue), ['72389', '72400'])
        asked = []

        def pulls(number):
            asked.append(number)
            if number == '72400':
                raise OSError('no network')
            return {'number': number, 'title': 'osd: fix the race', 'state': 'closed',
                    'merged': True, 'merge_commit': 'abc', 'body': 'Fixes the race.',
                    'files': [('src/osd/PrimaryLogPG.cc', ['void PrimaryLogPG::finish()'])]}

        text = learn.tracker_text(issue, pulls)
        self.assertIn('Comments (1):\n\n--- A Person, 2026-10-20:\nThe cause is a race', text)
        self.assertIn('Pull request #72389: osd: fix the race\nstate: merged, merge commit abc',
                      text)
        self.assertIn('  src/osd/PrimaryLogPG.cc: void PrimaryLogPG::finish()', text)
        self.assertIn('Pull request #72400: could not read it (no network)', text)
        self.assertEqual(asked, ['72389', '72400'])

    def test_a_refusal_is_not_asked_again(self):
        class Refusing(FakeAnalyst):
            def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
                self.prompts.append((prompt, session))
                raise JudgeError('fake refused to answer', session='s1', cost=0.1,
                                 refused=True)

        analyst = Refusing([])
        results = [Triage(db.load(self.problems)).run(self.jobs['3'], self.files['3'])]
        # The judge was needed for it: something to learn from.
        results[0].agent, results[0].group = {'judge': 'cheap', 'verdict': 'new'}, '3'
        [line] = learn.learn_from_run(results, lambda j: self.files[j.id], lambda run: self.jobs,
                                      self.problems, self.labels, analyst, self.workdir,
                                      lambda n: ISSUE, None, [], 5, say=lambda *a: None)
        # Asked once: not again for its answer, which the same session would refuse.
        self.assertEqual(len(analyst.prompts), 1)
        self.assertIn('not settled: fake refused to answer', line)
        with open(os.path.join(self.workdir, '3', 'lesson.json')) as f:
            self.assertEqual(json.load(f)['outcome'], 'refused')

    def test_the_summary_for_the_console(self):
        triage = Triage(db.load(self.problems))
        results = [triage.run(self.jobs[jid], self.files[jid]) for jid in ('1', '2', '3')]
        results[2].notes.append('The analyst found: an unstable write')
        lines = report.summary(RUN, results, ['QA tracker x, PRs under test: #1', ''],
                               [f'3: learned a new problem, wrote nodes/x.py (cost 1.50); '
                                f'see /a/analysis.md'], [('agent', 0.25), ('agent', 0.5)],
                               {'agent': 'coins'}, 'agent')
        self.assertEqual(lines[:5], [f'Triage of {RUN}', 'QA tracker x, PRs under test: #1', '',
                                     'Known problems, placed by their records (2 jobs):',
                                     '     TR 100  1, 2  problem 100'])
        self.assertIn('  new, unmatched: 3', lines)
        self.assertIn('          the analyst found: an unstable write', lines)
        self.assertIn('  3: learned a new problem, wrote nodes/x.py (cost 1.50)', lines)
        self.assertEqual(lines[-2:], ['3 failed or dead jobs: 2 duplicate, 1 new. Decided by '
                                      'the records 2, the memory 0, the tree 0, the judge 0; '
                                      'unmatched 1. Without AI: 2 of 3 (66%).',
                                      'Cost: judge agent 0.75 coins in 2 calls; analyst agent '
                                      '1.50 coins in 1 lesson. In all: 2.25 coins.'])
        self.assertEqual(report.summary(RUN, results[:1])[-1],
                         'Cost: nothing; no judge or analyst was asked.')

    def test_known_answers_come_first(self):
        triage = Triage(db.load(self.problems))
        results = [triage.run(self.jobs[jid], self.files[jid]) for jid in ('1', '2', '3')]
        results[2].agent, results[2].group = {'judge': 'cheap', 'verdict': 'new'}, '3'
        # 1 and 2 are placed in 100; the reviewers agree about 2, and say 1 and 3
        # are 200, which has no record yet.
        known = {'1': '200', '2': '100', '3': '200'}
        found = learn.lessons(results, known)
        self.assertEqual([(r.job.id, expected) for r, expected, _, _ in found],
                         [('1', '200'), ('3', '200')])
        self.assertIn('The QA reviewers placed this job in 200, but the tool said duplicate '
                      '100', found[0][2])
        lines = report.compared(results, known, 'As triaged')
        self.assertEqual(lines[0], 'As triaged, 1 of the 3 jobs with a known answer got it.')
        self.assertIn('- 3: known 200; the tool said new', lines[5])
        # Record 100 says people place its failures in 200 too: job 1 is right, and
        # there is nothing to teach about it.
        results[0].problem.also = ['200']
        self.assertEqual([r.job.id for r, _, _, _ in learn.lessons(results, known)], ['3'])
        self.assertEqual(report.compared(results, known, 'As triaged')[0],
                         'As triaged, 2 of the 3 jobs with a known answer got it.')

    def test_the_judge_checks_against_known_answers(self):
        triage = Triage(db.load(self.problems))
        results = [triage.run(self.jobs[jid], self.files[jid]) for jid in ('1', '2', '3')]
        results[2].agent, results[2].group = {'judge': 'cheap', 'verdict': 'new'}, '3'
        # The judge catches 1, wrongly placed in 100, and agrees about 2; 3 was
        # the judge's own verdict, not one to check.
        results[0].opinion = {'judge': 'cheap', 'verdict': 'duplicate', 'problem': '200',
                              'agrees': False}
        results[1].opinion = {'judge': 'cheap', 'verdict': 'duplicate', 'problem': '100',
                              'agrees': True}
        lines = report.checked(results, {'1': '200', '2': '100', '3': '200'})
        self.assertEqual(lines[0], 'The judge checked what the tree and the records decided. '
                                   'Of the 1 jobs they placed on a record the known answer '
                                   'does not name, it disagreed with 1 (1 naming the known '
                                   'answer), agreed with 0, was not sure of 0 and gave no '
                                   'answer for 0. Of the 1 they got right, it disagreed with 0.')
        self.assertTrue(lines[4].startswith('- 1: known 200; the tool said duplicate 100'))
        self.assertTrue(lines[4].endswith('; the judge (cheap) disagreed: it said duplicate 200'))
        self.assertEqual(len(lines), 6)

    def test_a_known_answer_that_is_a_duplicate(self):
        duplicate = dict(ISSUE, id=202, status={'name': 'Duplicate'},
                         relations=[{'issue_id': 202, 'issue_to_id': 200,
                                     'relation_type': 'duplicates'}])
        issues = {'201': BACKPORT, '202': duplicate, '200': dict(ISSUE, id=200)}
        self.assertEqual([learn.canonical(n, issues.get) for n in ('200', '201', '202', 'x')],
                         ['200', '200', '200', 'x'])
        # A search for the judge gives the issue a backport is of, once.
        original = dict(ISSUE, id=200, tracker={'name': 'Bug'})
        issues['200'] = original
        found = search_originals('words', search=lambda words: [
            ('201', 'Backport #201 (New): squid: a problem'), ('200', 'Bug #200 (New): a problem'),
            ('203', 'Bug #203 (New): another')], tracker=lambda n: issues[n])
        self.assertEqual(found, [('200', 'Bug #200 (New): a problem'),
                                 ('203', 'Bug #203 (New): another')])

    def test_a_run_settles_no_job_without_a_record(self):
        results = [Triage(db.load(self.problems)).run(self.jobs['3'], self.files['3'])]
        results[0].agent, results[0].group = {'judge': 'cheap', 'verdict': 'new'}, '3'
        analyst = FakeAnalyst([answer(None, quotes=(('teuthology.log', 'FAILED test'),),
                                      assessment='right')])
        [line] = learn.learn_from_run(results, lambda j: self.files[j.id],
                                      lambda run: self.jobs, self.problems, self.labels,
                                      analyst, self.workdir, lambda n: ISSUE, None, [], 5,
                                      say=self.said.append)
        self.assertIn('3: no record should place it: a summary (cost 0.50)', line)
        self.assertEqual(learn.load_labels(self.labels), [])

    def test_a_run_searches_the_tracker_first(self):
        results = [Triage(db.load(self.problems)).run(self.jobs['3'], self.files['3'])]
        results[0].agent = {'judge': 'cheap', 'verdict': 'new', 'searches': ['bouncing primary']}
        results[0].group = '3'
        analyst = FakeAnalyst([answer(None, quotes=(('teuthology.log', 'FAILED test'),),
                                      assessment='right')])
        learn_ = lambda search: learn.learn_from_run(
            results, lambda j: self.files[j.id], lambda run: self.jobs, self.problems,
            self.labels, analyst, self.workdir, lambda n: ISSUE, search, [], 5,
            say=self.said.append)
        self.assertEqual(learn_(lambda words: []), [
            '3: no tracker issue found by searching for "bouncing primary"; a person should '
            'file one'])
        self.assertEqual(analyst.prompts, [])
        learn_(lambda words: [('200', 'Bug #200 (New): bouncing to primary')])
        with open(os.path.join(self.workdir, '3', 'doubt.yaml')) as f:
            self.assertIn('found: #200 Bug #200 (New): bouncing to primary. It is in '
                          'tracker-<number>.txt.', f.read())
        # Given with the evidence, not a turn later.
        with open(os.path.join(self.workdir, '3', 'tracker-200.txt')) as f:
            self.assertIn(ISSUE['subject'], f.read())

    def test_dry_run(self):
        analyst = FakeAnalyst([answer(None, [EXCLUDING])])
        self.assertTrue(self.doubt(analyst, write=False).run())
        self.assertNotIn('exclude', self.record_text())



class TestSettle(unittest.TestCase):
    def test_a_person_settles_what_the_records_place(self):
        from types import SimpleNamespace
        from qa_triage import cli
        root = tempfile.mkdtemp(prefix='qa_triage-settle-')
        labels = os.path.join(root, 'labels')
        args = SimpleNamespace(job=f'{RUN}/1', problem='100', why='its checks found it',
                               by='a reviewer', dry_run=False, labels=labels)
        files = FakeFiles({'teuthology.log': ['objecter.op_reply']})

        def settle(problem):
            args.problem = problem
            with mock.patch.object(cli, 'list_jobs', return_value=[job('1')]), \
                    mock.patch.object(cli, 'files_of', return_value=files), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.cmd_settle(args, problems(record()))
            return code, out.getvalue() + err.getvalue()
        # Not where the records place it: that is for a doubt.
        code, said = settle('200')
        self.assertEqual(code, 1)
        self.assertIn('the records place', said)
        self.assertEqual(learn.settled_by_people(labels), {})
        self.assertEqual(settle('100')[0], 0)
        # An agent's settled jobs are not a person's.
        learn.save_label(labels, job('2'), '100', 'the tracker, via qa_triage (bob)', '', '')
        self.assertEqual(learn.settled_by_people(labels), {f'{RUN}/1': '100'})

    def test_a_person_who_agrees_with_the_analyst_keeps_its_change(self):
        from types import SimpleNamespace
        from qa_triage import approve, cli
        root = tempfile.mkdtemp(prefix='qa_triage-settle-')
        labels, problems_dir = os.path.join(root, 'labels'), os.path.join(root, 'problems')
        os.makedirs(problems_dir)
        args = SimpleNamespace(job=f'{RUN}/1', problem='200', why='its checks found it',
                               by='a reviewer', dry_run=False, labels=labels,
                               problems=problems_dir, nodes=None, cache=root)
        files = FakeFiles({'teuthology.log': ['objecter.op_reply']})
        kept = approve.kept(approve.disputed(root), job('1'))
        os.makedirs(os.path.dirname(kept))
        with open(kept, 'w') as f:
            json.dump({'problem': '200', 'expected': '200', 'job': f'{RUN}/1',
                       'disputes': {'expected': '100', 'by': 'the wiki'}}, f)
        asked = []

        def doubt(given, _):
            # The analyst's change, checked again and written: record 200.
            asked.append((given.proposal, given.says, given.by))
            with open(os.path.join(problems_dir, '200.yaml'), 'w') as f:
                yaml.safe_dump(record(id='200'), f)
            os.remove(given.proposal)
            return 0

        def settle(**more):
            vars(args).update(more)
            with mock.patch.object(cli, 'list_jobs', return_value=[job('1')]), \
                    mock.patch.object(cli, 'files_of', return_value=files), \
                    mock.patch.object(cli, 'cmd_doubt', side_effect=doubt), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.cmd_settle(args, [])
            return code, out.getvalue() + err.getvalue()
        code, said = settle(dry_run=True)
        self.assertEqual((code, asked), (0, []))
        self.assertIn('would keep the analyst\'s change', said)
        code, said = settle(dry_run=False)
        self.assertEqual(code, 0, said)
        self.assertEqual(asked, [(kept, '200', 'a reviewer')])
        self.assertEqual(learn.settled_by_people(labels), {f'{RUN}/1': '200'})
        self.assertFalse(os.path.exists(kept))


class TestRunTests(unittest.TestCase):
    def test_a_change_to_the_root_reaches_every_job(self):
        self.assertTrue(learn.reaches('crash/assert/specific', ''))
        self.assertTrue(learn.reaches('crash/assert/specific', 'crash'))
        self.assertTrue(learn.reaches('crash', 'crash/assert'))
        self.assertFalse(learn.reaches('hung/command', 'crash'))

    def test_what_the_tools_tests_say_fails(self):
        stderr = ('..F.\n' + '=' * 70 + '\nFAIL: test_root (test_tree.TestTree.test_root)\n' +
                  '-' * 70 + '\nTraceback (most recent call last):\n  File "t.py", line 3, '
                  'in test_root\n    self.assertEqual(a, b)\nAssertionError: \'crash\' != '
                  '\'infra\'\n\n' + '-' * 70 + '\nRan 4 tests in 0.1s\n\nFAILED (failures=1)\n')
        failed = subprocess.CompletedProcess([], 1, '', stderr)
        with mock.patch.object(learn.subprocess, 'run', return_value=failed) as run:
            lines = REAL_RUN_TESTS('/some/nodes')
        self.assertEqual(lines[0], 'FAIL: test_root (test_tree.TestTree.test_root)')
        self.assertIn("  AssertionError: 'crash' != 'infra'", lines)
        self.assertFalse([line for line in lines if 'Ran 4 tests' in line])
        self.assertEqual(run.call_args.kwargs['env']['QA_TRIAGE_NODES'], '/some/nodes')
        passed = subprocess.CompletedProcess([], 0, '', 'OK')
        with mock.patch.object(learn.subprocess, 'run', return_value=passed):
            self.assertEqual(REAL_RUN_TESTS('/some/nodes'), [])
        with mock.patch.object(learn, 'TESTS', '/nowhere'):
            self.assertIn('are not in /nowhere', REAL_RUN_TESTS('/some/nodes')[0])


class TestPaths(unittest.TestCase):
    def test_paths_walks_the_library_and_keeps_where_each_job_went(self):
        from types import SimpleNamespace
        from qa_triage import cli
        root = tempfile.mkdtemp(prefix='qa_triage-paths-')
        labels = os.path.join(root, 'labels')
        os.makedirs(labels)
        with open(os.path.join(labels, 'a.yaml'), 'w') as f:
            yaml.safe_dump({'run': RUN, 'jobs': {1: {'problem': '100'}}}, f)
        paths = learn.paths_dir(labels)
        learn.save_paths(paths, {'gone-run/9': tree.Finding(path=['crash'])})
        files = {jid: FakeFiles(f) for jid, f in ARCHIVES.items()}
        args = SimpleNamespace(labels=labels, cache=root, archive='trial200:/a')
        with mock.patch.object(cli, 'files_of', lambda args, j: files[j.id]), \
                mock.patch.object(cli, 'list_jobs', lambda run: [job('2')]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_paths(args, [db.parse(record(examples=[f'{RUN}/2']))])
        kept = learn.load_paths(paths)
        self.assertEqual(sorted(kept), [f'{RUN}/1', f'{RUN}/2'])
        self.assertEqual(kept[f'{RUN}/2'].where,
                         tree.explain(job('2'), files['2']).where)
        self.assertIn(f'2 paths in {paths}, 2 files changed', out.getvalue())
        [name] = os.listdir(paths)
        with open(os.path.join(paths, name)) as f:
            self.assertEqual(yaml.safe_load(f)['code'], tree.code())

    def test_a_triage_by_other_tree_code_does_not_say_where_the_tree_goes(self):
        root = tempfile.mkdtemp(prefix='qa_triage-history-')
        self.addCleanup(shutil.rmtree, root)
        path = os.path.join(root, 'results.json')
        found = {'path': 'crash/abort', 'facts': {'daemon': 'osd'}}
        with open(path, 'w') as f:
            json.dump([{'run': RUN, 'job': 1, 'verdict': 'known', 'tree': found},
                       {'run': RUN, 'job': 2, 'verdict': 'known',
                        'tree': dict(found, code=tree.code())},
                       {'run': RUN, 'job': 3, 'verdict': 'known',
                        'tree': dict(found, code='other')}], f)
        history = learn.load_history([path])
        self.assertEqual([history[f'{RUN}/{n}']['tree'] for n in (1, 2, 3)],
                         ['crash/abort', 'crash/abort', None])
        self.assertEqual(history[f'{RUN}/3']['facts'], {})


class TestLint(unittest.TestCase):
    def test_lint_says_what_is_wrong_with_each_part(self):
        from types import SimpleNamespace
        from qa_triage import cli
        self.addCleanup(forget_learned)
        root = tempfile.mkdtemp(prefix='qa_triage-lint-')
        parts = {part: os.path.join(root, part) for part in ('problems', 'nodes', 'labels',
                                                              'memory')}
        for path in parts.values():
            os.makedirs(path)
        with open(os.path.join(parts['problems'], '100.yaml'), 'w') as f:
            yaml.safe_dump(record(), f)
        with open(os.path.join(parts['nodes'], 'workunit-unexplained.py'), 'w') as f:
            f.write(NODE)
        with open(os.path.join(parts['labels'], 'a.yaml'), 'w') as f:
            yaml.safe_dump({'run': RUN, 'jobs': {1: {'problem': '100'}, 2: {'problem': None}}}, f)
        with open(os.path.join(parts['memory'], 'a.yaml'), 'w') as f:
            yaml.safe_dump({'run': RUN, 'signatures': {'a way': {'100': 2}}}, f)
        args = SimpleNamespace(problems=parts['problems'], nodes=parts['nodes'],
                               labels=parts['labels'])

        def lint():
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.cmd_lint(args, db.load(parts['problems']))
            return code, out.getvalue()
        self.assertEqual(lint(), (0, f'1 problem records in {parts["problems"]} are valid\n'
                                     '1 node files, 2 settled jobs and the memory of 1 runs: '
                                     'valid\n'))
        with open(os.path.join(parts['nodes'], 'bad.py'), 'w') as f:
            f.write('import os\n')
        with open(os.path.join(parts['labels'], 'b.yaml'), 'w') as f:
            yaml.safe_dump({'run': RUN, 'jobs': {3: {'problem': '999'}}}, f)
        with open(os.path.join(parts['memory'], 'b.yaml'), 'w') as f:
            f.write('run: [\n')
        code, out = lint()
        self.assertEqual(code, 1)
        self.assertIn('3 wrong:', out)
        self.assertIn('nodes/bad.py:1: it may not import os', out)
        self.assertIn('labels/b.yaml: job 3 is settled in 999, which no record is', out)
        self.assertIn('memory/b.yaml: ', out)
        # Paths kept by other tree code are not wrong, but their jobs are walked again.
        paths = learn.paths_dir(parts['labels'])
        os.makedirs(paths)
        with open(os.path.join(paths, 'a.yaml'), 'w') as f:
            yaml.safe_dump({'run': RUN, 'code': 'other', 'jobs': {1: {'tree': 'crash'}}}, f)
        self.assertIn('The tree paths of 1 runs were found by other tree code: qa_triage paths '
                      'walks their jobs again\n', lint()[1])


if __name__ == '__main__':
    unittest.main()
