import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import yaml

from fakes import RUN, FakeFiles, FakeJudge, job
from test_learn import ARCHIVES, ISSUE, FakeAnalyst, answer, record, text

from qa_triage import approve, db, learn, report, setup, tui
from qa_triage.cli import judge_unplaced, quiet, retriage
from qa_triage.graph import Triage
from qa_triage.review import DIGEST, DISCUSS, LIVE, RESPONSE_TALK, REVISE, Review, State
from qa_triage.sources import SshJobFiles


class Talker(FakeAnalyst):
    """An analyst that can discuss: it says what it was told, and how often."""

    def chat(self, workdir, prompt, session=None, tag=None):
        self.prompts.append((prompt, session))
        return {'text': f'reply {len(self.prompts)}', 'session': 'c1', 'cost': 0.1}


class Program(Talker):
    """An analyst with a program of its own, where it writes the proposal it is
    given, if any."""

    program = 'its program'
    proposal = None

    def interactive(self, workdir, session):
        self.opened = (workdir, session)
        return ['cp', self.proposal, 'proposal.json'] if self.proposal else ['true']


class Reviewing(unittest.TestCase):
    """A review of a run of three jobs, with a judge and an analyst that talks."""

    def setUp(self):
        learn._FINDINGS.clear()
        root = self.root = tempfile.mkdtemp(prefix='qa_triage-review-')
        self.problems = os.path.join(root, 'problems')
        os.makedirs(self.problems)
        with open(os.path.join(self.problems, '100-op-reply.yaml'), 'w') as f:
            f.write(text(record(examples=[f'{RUN}/2'])))
        self.verdicts = os.path.join(root, 'verdicts')
        self.judge = FakeJudge({'verdict': 'new', 'summary': 'an unstable write', 'cost': 0.25},
                               'cheap')
        self.analyst = Talker([])
        self.review = self.start()

    extra = {}

    def start(self):
        """A review of the run, as it begins: anew, or again after quitting."""
        root = self.root
        files = {jid: FakeFiles(archive) for jid, archive in ARCHIVES.items() if jid != '5'}
        jobs = [job(jid) for jid in files]
        for jid, (reason, archive) in self.extra.items():
            files[jid] = FakeFiles(archive)
            jobs.append(job(jid, reason=reason))
        triage = Triage(db.load(self.problems), verdicts=self.verdicts,
                        workdir=os.path.join(root, 'judge'))
        results = [triage.run(j, files[j.id]) for j in jobs]
        judge_unplaced(triage, results, files, None)
        self.session = SimpleNamespace(run=RUN, jobs=jobs, out=os.path.join(root, 'out'),
                                       header=[], known={}, triage=triage, results=results,
                                       files=files)
        # Changes are kept once they pass the checks, edits too;
        # test_a_change_waits_for_approval asks first.
        args = SimpleNamespace(problems=self.problems, nodes=os.path.join(root, 'nodes'),
                               settings={'approve': 'always', 'approve_edits': 'always'},
                               cache=os.path.join(root, 'cache'))

        def learning():
            return dict(files_for=lambda j: files[j.id], jobs_for=lambda run: {j.id: j for j in jobs},
                        problems_dir=self.problems, labels_dir=os.path.join(root, 'labels'),
                        workdir=os.path.join(root, 'out', 'learn'), tracker=lambda n: ISSUE,
                        search=None, corpus=[], limit=5, source=None, nodes_dir=None,
                        history={}, relearn=False, known={}, budget=None, earlier=[])

        self.out = io.StringIO()
        review = Review(self.session, self.judge, self.analyst, learning,
                        lambda rs: retriage(triage, rs, files, None, quiet),
                        {'cheap': 'coins', 'fake': 'coins'}, args, stdout=self.out)
        # The tracker is not asked: its issues are open, as far as a test knows.
        review.read_issue = lambda number: {}
        return review

    def run_command(self, line):
        self.out.seek(0)
        self.out.truncate()
        stop = self.review.onecmd(line)
        return self.out.getvalue(), stop

class TestReview(Reviewing):
    def test_lessons_at_once(self):
        # learn all, with lessons_at_once: each lesson asks the analyst before either
        # has an answer, and the jobs are triaged again once, after them all.
        import threading
        self.run_command('judge all')
        both = threading.Barrier(2, timeout=10)

        class Together(Talker):
            def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
                both.wait()
                return super().ask(workdir, prompt, kind, session, tag)

        self.review.analyst = Together([answer(None, assessment='unsure') for _ in range(2)])
        self.review.args.lessons_at_once = 'all'
        again, retriaged = self.review.again, []
        self.review.again = lambda: retriaged.append(True) or again()
        said, _ = self.run_command('learn all')
        self.assertIn('2 lessons, 2 at once; each line of one begins with its job', said)
        self.assertIn('1: 1 and 1 like it: asking fake', said)
        self.assertIn('\n3: asking fake', said)
        # Each line of what a lesson says begins with its job.
        self.assertIn('3: Checks:\n3: FAIL  no answer: it cannot tell', said)
        self.assertEqual(retriaged, [True])
        self.assertEqual(self.review.to_learn(), [])

    def test_looking_is_free(self):
        said, _ = self.run_command('summary')
        self.assertIn('  [1]    TR 100  1, 2  problem 100', said)
        self.assertIn('  [2] new, unmatched: 3', said)
        self.assertIn('Cost: nothing; no judge or analyst was asked.', said)
        said, _ = self.run_command('show 2')
        self.assertIn('1. What went wrong first? workunit', said)
        self.assertIn('  record TR 100: confirm /objecter.op_reply/ not found', said)
        said, _ = self.run_command('grep 3 bouncing remote/*/log/*')
        self.assertIn('remote/h1/log/ceph-osd.0.log:1: unstable write', said)
        said, _ = self.run_command('files 3 remote/*/log/*')
        self.assertIn('remote/h1/log/ceph-osd.0.log', said)
        argv = self.review.opening('3 remote/h1/log/ceph-osd.0.log:1')
        self.assertEqual(argv[-2:], ['+1', os.path.join(self.review.args.cache, 'opened', RUN,
                                                        '3', 'remote/h1/log/ceph-osd.0.log')])
        # By default, where its result starts, if it cites a line.
        with self.assertRaisesRegex(ValueError, 'give the file and line'):
            self.review.opening('1')
        with mock.patch('qa_triage.report.start', lambda r: ' (teuthology.log:1)'):
            self.assertEqual(self.review.opening('1')[-2], '+1')
        self.assertIn('problem 100', self.run_command('comment')[0])
        self.assertEqual(self.review.prompt, f'{RUN.split("-distro-")[0][-40:]} [no AI yet]> ')

    def test_mistakes(self):
        self.assertIn('no group 9, and no job 9', self.run_command('show 9')[0])
        self.assertIn('No command "frobnicate"', self.run_command('frobnicate')[0])
        self.assertIn('give a regex', self.run_command('grep 1')[0])
        self.assertIn('is not in the archive of job 3',
                      self.run_command('open 3 remote/h9/log/ceph-osd.0.log:1')[0])

    def test_the_judge_costs(self):
        said, _ = self.run_command('judge 2')
        self.assertIn('3: new, unmatched (cost 0.25)', said)
        self.assertIn('an unstable write', said)
        # What it failed on, then why, come first in show, and so in the full screen's
        # lower pane; then where the logs are, and what was said and found.
        said, _ = self.run_command('show 2')
        self.assertTrue(said.startswith('New issue: the judge said: an unstable write (new, '
                                        'unmatched)\n\nWhy, as the judge (cheap) tells it:\n'
                                        'an unstable write\n\n' + '─' * 40), said)
        self.assertIn('Jobs: 3\nDescription: rados/basic/', said)
        self.assertRegex(said, r'From the judge \(cheap\), [-\d :]+, for 0.25:\n\nan unstable '
                               r'write\n\n3: new, unmatched \(cost 0.25\)\nThe judge \(cheap\) said '
                               r'new : an unstable write')
        self.assertIn('[0.25 coins]', self.review.prompt)
        # Asking again shows what it said, for nothing, unless asked again on purpose.
        said, _ = self.run_command('judge 2')
        self.assertIn('Asked before', said)
        self.assertIn('The judge (cheap) said new : an unstable write', said)
        self.assertEqual(len(self.judge.calls), 1)
        self.run_command('judge 2 again')
        self.assertEqual(len(self.judge.calls), 2)

    def test_judges_at_once(self):
        # judge all, with judges_at_once: each group asks the judge before either has
        # an answer; each line of one begins with its job, and every cost is kept.
        import threading
        both = threading.Barrier(2, timeout=10)
        judge, seen = self.judge, []

        class Together(FakeJudge):
            def __call__(self, workdir, prompts):
                both.wait()
                seen.append(dict(review.at_work))
                return judge(workdir, prompts)

        review = self.review
        review.judge = Together({}, 'cheap')
        review.args.judges_at_once = 4
        said, _ = self.run_command('judge all')
        self.assertIn('2 groups, 2 at once; each line of one begins with its job', said)
        self.assertIn('Group 1 of 2, job ', said)
        self.assertIn('Group 2 of 2, job ', said)
        self.assertIn('1: 1,2: duplicate 100, as the tree and the records decided; the judge '
                      'disagrees: it says new (cost 0.25)', said)
        self.assertIn('3: 3: new, unmatched (cost 0.25)', said)
        # While they worked, both groups were at work; then neither is.
        self.assertEqual([sorted(s) for s in seen], [['1', '3'], ['1', '3']])
        self.assertIsNone(review.at_work)
        self.assertEqual(review.unjudged(), [])
        self.assertEqual(sum(c for _, c in self.session.triage.spent), 0.5)
        self.assertEqual(len(review.state.spent), 2)

    def test_the_ai_is_shared_by_turns(self):
        # One turn of the judge, as watch's runs share them: the groups are judged one
        # at a time, though judges_at_once is more, and the one waiting is said to.
        import threading
        review, judge, at_once, seen = self.review, self.judge, [], []
        lock = threading.Lock()

        class Counting(FakeJudge):
            def __call__(self, workdir, prompts):
                with lock:
                    at_once.append(1)
                    seen.append((len(at_once), set(review.waiting_for_ai)))
                try:
                    import time
                    time.sleep(0.2)
                    return judge(workdir, prompts)
                finally:
                    with lock:
                        at_once.pop()

        review.judge = Counting({}, 'cheap')
        review.args.judges_at_once = 4
        review.turns = {'judge': threading.BoundedSemaphore(1)}
        self.run_command('judge all')
        self.assertEqual([n for n, _ in seen], [1, 1])
        # While the first was judged, the other waited.
        self.assertEqual(len(seen[0][1]), 1)
        self.assertEqual(seen[1][1], set())
        self.assertEqual(review.waiting_for_ai, set())
        self.assertEqual(review.unjudged(), [])

    def test_the_prs_under_test(self):
        triage = self.session.triage
        self.assertIn('No PRs under test', self.run_command('prs')[0])
        self.session.header = ['QA tracker https://tracker.ceph.com/issues/555, PRs under '
                               'test: #100, #200']
        triage.batch_prs = {'200', '100'}
        triage.branches = {'abc': [('PR #200', 'c', 'p', ['src/osd/a.cc', 'b.cc']),
                                   ('PR #300', 'c', 'p', ['qa/x.sh'])]}
        self.review.results[0].suspects = [('PR #300', 'qa/x.sh')]
        self.assertEqual(self.review.qa_tracker(), '555')
        self.assertEqual(list(self.review.under_test()), ['100', '200', '300'])
        titles = {'100': ('osd: a fix', 'open'), '300': ('qa: a test', 'merged')}
        with mock.patch('qa_triage.review.pr_title', titles.get):
            said, _ = self.run_command('prs')
        self.assertIn('3 PRs under test, of QA tracker https://tracker.ceph.com/issues/555:',
                      said)
        self.assertIn('#100  osd: a fix (open)\n    its QA tracker lists it: '
                      'https://github.com/ceph/ceph/pull/100', said)
        self.assertIn('#200  (GitHub did not say its title)\n    its QA tracker lists it; '
                      'merged into its branch, changing 2 files', said)
        self.assertIn('#300  qa: a test (merged)\n    merged into its branch, changing 1 '
                      'file: https://github.com/ceph/ceph/pull/300\n    the jobs of group 1 '
                      'ran a file it changes', said)

    def test_judging_all_the_groups_left(self):
        # Those the tree and the records decided too: the judge checks them.
        self.assertEqual(len(self.review.unjudged()), 2)
        self.judge.verdict = dict(self.judge.verdict, explanation='The OSD bounced a write.')
        said, _ = self.run_command('judge all')
        # What it explains comes first.
        self.assertIn('Group 1 of 2, job 1:\nThe OSD bounced a write.\n\n1,2: duplicate 100, '
                      'as the tree and the records decided; the judge disagrees: it says new '
                      '(cost 0.25)', said)
        self.assertIn('Group 2 of 2, job 3:\nThe OSD bounced a write.\n\n3: new, unmatched (cost '
                      '0.25)', said)
        # A decision the judge disagrees with stands, and is for the analyst.
        self.assertEqual([r.verdict for r in self.review.results[:2]], ['duplicate'] * 2)
        self.assertIs(self.review.results[0].opinion['agrees'], False)
        self.assertIn('The analyst could learn from 2 groups, so that the tree and the '
                      'records decide jobs like them by themselves next time', said)
        self.assertIn('  [1] 1: The tool decided this job by itself: duplicate 100', said)
        self.assertIn('Its judge (cheap), asked to check that, disagrees', said)
        self.assertIn('  [2] 3: No record placed this job, and the judge (cheap) said new',
                      said)
        self.assertEqual(self.review.unjudged(), [])
        self.assertIn('Every group has been judged.', self.run_command('judge all')[0])
        self.assertEqual(len(self.judge.calls), 2)
        # The analyst learns from each, the disagreement first; then from none.
        self.analyst.answers = [answer(None, assessment='unsure') for _ in range(2)]
        said, _ = self.run_command('learn all')
        self.assertIn('Group 1 of 2, job 1:', said)
        self.assertIn('Group 2 of 2, job 3:', said)
        self.assertIn('Its judge (cheap), asked to check', self.analyst.prompts[0][0])
        self.assertEqual(self.review.to_learn(), [])
        self.assertIn('Nothing to learn from', self.run_command('learn all')[0])
        # Starting again: the judge's answers, from those kept by their evidence.
        self.review = self.start()
        self.assertIs(self.review.results[0].opinion['agrees'], False)
        self.assertEqual(len(self.judge.calls), 2)

    def test_the_analyst_learns(self):
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        self.analyst.answers = [answer('200', [new], quotes=(('teuthology.log', 'FAILED test'),))]
        # Not judged yet, and not new for sure: there is nothing to learn from it.
        said, _ = self.run_command('learn 2')
        self.assertIn('Nothing to learn from it', said)
        self.run_command('judge 2')
        said, _ = self.run_command('learn 2')
        self.assertIn('3: learned 200', said)
        # The analyst's answer, its change and the checks, not a pointer to them.
        said, _ = self.run_command('show 2')
        self.assertRegex(said, r'From a lesson \(fake\), [-\d :]+, for 0.50:')
        self.assertIn('fake: the result is wrong. a summary', said)
        self.assertIn('+++ b/problems/200-problem-200.yaml', said)
        self.assertIn('Checks:', said)
        said, _ = self.run_command('summary')
        self.assertIn('  [2]    TR 200  3  problem 200', said)
        self.assertIn('Cost: cheap 0.25 coins (1 judge); fake 0.50 coins (1 lesson). In all: '
                      '0.75 coins; in this review: 0.75 coins.', said)
        # The lesson is not paid for again.
        said, _ = self.run_command('learn 2')
        self.assertIn('Asked before', said)
        self.assertEqual(len(self.analyst.prompts), 1)

    def test_a_lesson_not_begun_or_stopped_is_asked_again(self):
        self.run_command('judge 2')
        # An analyst that gives nothing at all, as one out of credit does: nothing kept.
        self.analyst.ask = lambda *args, **kwargs: (_ for _ in ()).throw(
            learn.JudgeError('fake gave no answer (exit 1): out of credit'))
        said, _ = self.run_command('learn 2')
        self.assertIn(learn.NOT_BEGUN + 'fake gave no answer', said)
        self.assertIsNone(self.review.state.latest(self.review.results[1].job.id, 'lesson'))
        # Stopped by a person, from the screen: nothing kept either.
        self.analyst.ask = lambda *args, **kwargs: (_ for _ in ()).throw(
            learn.Stopped('fake: stopped'))
        said, _ = self.run_command('learn 2')
        self.assertIn('Stopped: nothing it was doing is kept', said)
        self.assertIsNone(self.review.state.latest(self.review.results[1].job.id, 'lesson'))
        self.assertNotIn('Asked before', self.run_command('learn 2')[0])

    def test_starting_again_keeps_what_was_paid_for(self):
        self.run_command('judge 2')
        self.run_command('discuss 2 why did it fail?')
        self.review = self.start()
        said, _ = self.run_command('show 2')
        self.assertIn('The discussion with fake:', said)
        self.assertIn('you:\n  why did it fail?\n\nfake:\n  reply 1', said)
        self.assertIn('From the judge (cheap)', said)
        # Its verdict is back, from the one kept, without asking the judge, and
        # without gathering its evidence again.
        self.assertEqual((self.session.results[2].verdict, self.session.results[2].agent['verdict']),
                         ('new', 'new'))
        self.assertEqual(len(self.judge.calls), 1)
        self.assertIn("cheap's verdict from before, read from the cache",
                      self.session.results[2].notes)
        self.assertIn('Cost: cheap 0.25 coins (1 judge); fake 0.10 coins (1 discussion). In '
                      'all: 0.35 coins.', self.run_command('cost')[0])

    def test_retriage_says_what_changed(self):
        said, _ = self.run_command('retriage')
        self.assertIn('Triaged the 3 jobs again with the records and the tree as they are '
                      'now: no result changed.', said)
        # A record edited by hand no longer places job 1.
        with open(os.path.join(self.problems, '100-op-reply.yaml'), 'w') as f:
            f.write(text(record(examples=[f'{RUN}/2'], exclude=[
                {'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}])))
        said, _ = self.run_command('retriage')
        self.assertIn('1 changed.', said)
        self.assertRegex(said, r'  1: duplicate 100 \(.*\) -> new')

    def test_a_discussion(self):
        said, _ = self.run_command('discuss 2 is it a race?')
        self.assertIn('fake:\n  reply 1', said)
        prompt, session = self.analyst.prompts[0]
        # A first message gives it the evidence, what the tree found, and the question.
        self.assertIsNone(session)
        self.assertIn('A person is reviewing this failed job', prompt)
        self.assertIn('What the tool did with this job before you:', prompt)
        self.assertTrue(prompt.endswith('is it a race?'))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'out', 'learn', 'discuss-3',
                                                    'explanation.txt')))
        # Then it continues the session.
        self.run_command('discuss 2 and then?')
        self.assertEqual(self.analyst.prompts[1], ('and then?', 'c1'))
        self.assertIn('give a group', self.run_command('discuss')[0])

    def test_a_doubt(self):
        self.analyst.answers = [answer(None, [record(examples=[f'{RUN}/2'], exclude=[
            {'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}])])]
        said, _ = self.run_command('doubt 1 --says new it leaked')
        self.assertIn('1: doubted, settled, wrote problems/100-op-reply.yaml', said)
        self.assertIn('Why you are asked: it leaked', self.analyst.prompts[0][0])

    def hold_a_doubt(self):
        """A doubt whose change waits for approval, as on the full screen."""
        self.review.args.settings = {'approve': 'ask'}
        self.analyst.answers = [answer(None, [record(examples=[f'{RUN}/2'], exclude=[
            {'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}])],
            summary='The monitor leaked a buffer at exit.')]
        said, _ = self.run_command('doubt 1 --says new it leaked')
        self.assertIn('the change waits for approval', said)

    def record_text(self):
        with open(os.path.join(self.problems, '100-op-reply.yaml')) as f:
            return f.read()

    def number(self, job_id):
        """The number of the group job_id is in now."""
        return next(n for n, (_, rs) in enumerate(report.groups(self.review.results), 1)
                    if any(r.job.id == job_id for r in rs))

    def test_a_doubts_response_agrees_and_is_accepted_or_dismissed(self):
        self.hold_a_doubt()
        said, _ = self.run_command('response 1')
        self.assertIn('This is my response, as fake gives it:\n\nThe monitor leaked a buffer '
                      'at exit.', said)
        self.assertIn('It says the job is new, a problem no record has.\nYou said new, a '
                      'problem no record has: it agrees with you.', said)
        self.assertIn('Its change waits for your approval:', said)
        self.assertIn('It changes problems/100-op-reply.yaml.', said)
        self.assertIn('accept 1 dismiss 1; talk 1 response discusses it', said)
        self.assertEqual(self.review.response_lines('1')[1], ['accept', 'dismiss'])
        said, _ = self.run_command('accept 1')
        self.assertIn('Kept the response to the doubt about 1: wrote '
                      'problems/100-op-reply.yaml, labels/', said)
        self.assertIn('Leak_DefinitelyLost', self.record_text())
        self.assertEqual(self.review.waiting(), [])
        lines, can = self.review.response_lines('1')
        self.assertIn('You accepted it.', lines)
        self.assertEqual(can, [])
        self.assertIn('no response waits to be accepted',
                      self.run_command(f'accept {self.number("1")}')[0])
        # Dismissed, nothing it made is kept.
        self.setUp()
        self.hold_a_doubt()
        said, _ = self.run_command('dismiss 1')
        self.assertIn('1: its response is dismissed; nothing it made is kept', said)
        self.assertNotIn('Leak_DefinitelyLost', self.record_text())
        self.assertEqual(self.review.waiting(), [])
        self.assertIn('You dismissed it.', self.review.response_lines('1')[0])

    def test_a_doubts_response_that_kept_nothing_says_what_failed(self):
        # It agrees with the person, but the records still place the job: nothing is
        # kept, and the response says which check stopped it, for the person to discuss.
        self.analyst.answers = [answer(None, summary='It is new.')] * 10
        self.run_command('doubt 1 --says new it is not 100')
        lines, can = self.review.response_lines('1')
        text = '\n'.join(lines)
        self.assertIn('You said new, a problem no record has: it agrees with you.', text)
        self.assertIn('Nothing is kept: its answer failed these checks:', text)
        self.assertIn('would be placed in 100, not new.', text)
        self.assertEqual(can, ['dismiss'])

    def test_a_doubts_response_that_disagrees_is_accepted(self):
        # The person says job 1 is 100, as the tool does; the analyst says it is new,
        # with an exclude: it disagrees, and its change is kept aside, for the person.
        self.analyst.answers = [answer(None, [record(examples=[f'{RUN}/2'], exclude=[
            {'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}])],
            summary='The monitor leaked a buffer at exit.')]
        self.run_command('doubt 1 --says 100 it is 100')
        self.assertNotIn('Leak_DefinitelyLost', self.record_text())
        lines, can = self.review.response_lines('1')
        self.assertIn('You said TR 100, problem 100: it disagrees with you.', lines)
        self.assertIn('It answers otherwise than you, and its answer passed the checks: accept '
                      'it if it convinced you, and the job is settled as it says, with its '
                      'change, which is kept aside until then.', lines)
        self.assertEqual(can, ['accept', 'dismiss'])
        said, _ = self.run_command('accept 1')
        self.assertIn('Kept the response to the doubt about 1: wrote', said)
        self.assertIn('Leak_DefinitelyLost', self.record_text())
        # The job is the person's now, as the analyst said.
        [(j, problem, who)] = learn.load_labels(os.path.join(self.root, 'labels'))
        self.assertEqual((j.id, problem), ('1', None))
        self.assertNotIn('qa_triage', who)

    def test_a_doubts_response_is_discussed_in_the_analysts_program(self):
        self.analyst = Program([])
        self.review = self.start()
        self.review.args.settings = {'approve': 'ask'}
        change = [record(examples=[f'{RUN}/2'], exclude=[
            {'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}])]
        self.analyst.answers = [answer(None, change, summary='It leaked.'),
                                answer(None, change, summary='As we said, it leaked.')]
        self.run_command('doubt 1 --says new it leaked')
        [(before, _)] = self.review.waiting()
        said, _ = self.run_command('talk 1 response')
        # It goes on from the doubt's own session, told to discuss, not to look further;
        # then, once the person leaves its program, it gives its final answer there.
        self.assertEqual(self.analyst.prompts[1], (RESPONSE_TALK, 's1'))
        self.assertEqual(self.analyst.opened[1], 'c1')
        prompt, session = self.analyst.prompts[2]
        self.assertEqual(session, 'c1')
        self.assertIn(DIGEST, prompt)
        self.assertIn('1: its final answer, its change waits for your approval', said)
        lines, can = self.review.response_lines('1')
        self.assertIn('This is my response, after our discussion, as fake gives it:', lines)
        self.assertIn('As we said, it leaked.', lines)
        self.assertEqual(can, ['accept', 'dismiss'])
        # What it answered before gave way to its final answer.
        [(after, _)] = self.review.waiting()
        self.assertEqual(after, before)
        self.assertIn('discuss its response first',
                      self.run_command('digest 2')[0])

    def test_a_change_that_waits_is_approved(self):
        self.hold_a_doubt()
        said, _ = self.run_command('pending')
        self.assertIn(f'[1] {RUN}/1', said)
        self.assertIn('    The monitor leaked a buffer at exit.', said)
        self.assertIn('It decides this job, and those that fail the same way, as new: no '
                      'record has its problem yet. It changes problems/100-op-reply.yaml.', said)
        self.assertIn('Leak_DefinitelyLost', self.run_command('change 1')[0])
        self.assertNotIn('Leak_DefinitelyLost', self.record_text())
        self.assertIn('give the numbers of changes that wait, from 1 to 1',
                      self.run_command('approve 2')[0])
        said, _ = self.run_command('approve 1')
        self.assertIn(f'{RUN}/1: kept, wrote problems/100-op-reply.yaml', said)
        self.assertIn('Kept 1 of the 1 change.', said)
        self.assertIn('Leak_DefinitelyLost', self.record_text())
        self.assertEqual(self.review.waiting(), [])
        self.assertIn('No change waits for approval.', self.run_command('pending')[0])

    def test_a_change_that_waits_says_why_it_is_not_kept(self):
        self.hold_a_doubt()
        # Meanwhile, the record gained an example: the change, made again with it,
        # does not place it.
        self.change_record(examples=[f'{RUN}/2', f'{RUN}/3'])
        said, _ = self.run_command('approve 1')
        failing = (f'    With this change, {RUN}/3, an example of record 100, would be new, '
                   'placed in no record, not 100.')
        line = (f'{RUN}/1: not kept, so it still waits: reject drops it. Checked again, '
                'against the library as it is now, it fails these checks, which keep a change '
                'from undoing what the library gets right:')
        self.assertIn(f'{line}\n{failing}\nrevise 1 asks the analyst', said)
        self.assertIn('Kept 0 of the 1 change.', said)
        self.assertEqual(len(self.review.waiting()), 1)
        # The screen shows that, not only how many were kept.
        self.assertEqual(tui.not_kept(said.split('\n')), [line, failing])

    def git_library(self):
        """The library in a git repository, as the shared one is: a change that waits
        is a commit on it, made on the library as it was."""
        patcher = mock.patch.dict(os.environ, {
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_NOSYSTEM': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)
        for args in (['init', '-q', '-b', 'main'], ['add', 'problems'],
                     ['commit', '-q', '-m', 'library']):
            subprocess.run(['git', '-C', self.root, *args], check=True, capture_output=True)

    def change_record(self, **changed):
        """Change the record as another's lesson would, meanwhile."""
        data = dict(yaml.safe_load(self.record_text()), **changed)
        with open(os.path.join(self.problems, '100-op-reply.yaml'), 'w') as f:
            yaml.safe_dump(data, f, sort_keys=False)

    def test_a_change_that_waits_is_made_again_onto_the_library_as_it_is(self):
        self.hold_a_doubt()
        self.change_record(also=['200'])
        said, _ = self.run_command('approve 1')
        self.assertIn(f'{RUN}/1: kept (made again onto what others changed meanwhile: '
                      'record 100 (also)), wrote problems/100-op-reply.yaml', said)
        data = yaml.safe_load(self.record_text())
        self.assertEqual(data['also'], ['200'])
        self.assertEqual(data['exclude'], [{'file': 'teuthology.log',
                                            'pattern': 'Leak_DefinitelyLost'}])
        self.assertEqual(self.review.waiting(), [])

    def test_a_change_kept_before_what_it_was_made_on_was_is_made_again_too(self):
        # From its commit, on the library as it was.
        self.git_library()
        self.hold_a_doubt()
        [(path, held)] = self.review.waiting()
        self.assertTrue(held['commit'])
        with open(path, 'w') as f:
            json.dump({k: v for k, v in held.items() if k not in ('before', 'writes')}, f)
        self.change_record(also=['200'])
        said, _ = self.run_command('approve 1')
        self.assertIn('Kept 1 of the 1 change.', said)
        data = yaml.safe_load(self.record_text())
        self.assertEqual((data['also'], len(data['exclude'])), (['200'], 1))

    def forget_what_it_was_made_on(self):
        """The change that waits, as one kept before changes kept the files they were
        made on, and the analyst that made them."""
        [(path, held)] = self.review.waiting()
        with open(path, 'w') as f:
            json.dump({k: v for k, v in held.items() if k not in ('before', 'analyst')}, f)
        return path

    def test_a_change_that_does_not_know_what_it_was_made_on(self):
        # As a change kept before others wrote the same record differently: the two
        # clash, and it no longer passes as it was.
        self.hold_a_doubt()
        self.forget_what_it_was_made_on()
        self.change_record(title='another title', examples=[f'{RUN}/2', f'{RUN}/3'])
        said, _ = self.run_command('approve 1')
        self.assertIn('    record 100: another lesson may have changed it meanwhile, with a '
                      'different title\nrevise 1 asks the analyst', said)
        # Where they clash but it passes as it was, it is kept as it was.
        self.change_record(examples=[f'{RUN}/2'])
        said, _ = self.run_command('approve 1')
        self.assertIn('Kept 1 of the 1 change.', said)
        self.assertEqual(yaml.safe_load(self.record_text())['title'], 'problem 100')

    def test_a_revision_in_a_new_session_keeps_the_old_evidence(self):
        self.hold_a_doubt()
        path = self.forget_what_it_was_made_on()
        with open(path) as f:
            held = json.load(f)
        held['analyst'] = 'another'
        with open(path, 'w') as f:
            json.dump(held, f)
        self.change_record(exclude=[{'file': 'teuthology.log', 'pattern': 'never seen'}],
                           examples=[f'{RUN}/2', f'{RUN}/3'])
        quoted = os.path.join(held['workdir'], 'requested', '1-1.txt')
        os.makedirs(os.path.dirname(quoted), exist_ok=True)
        with open(quoted, 'w') as f:
            f.write('what the lesson requested\n')
        self.analyst.answers = [answer(None, assessment='unsure', summary='Not sure.')]
        self.run_command('revise 1')
        prompt, session = self.analyst.prompts[-1]
        self.assertIsNone(session)
        self.assertIn(REVISE.split('{why}')[0], prompt)
        self.assertTrue(os.path.exists(quoted))
        self.assertTrue(os.path.exists(held['workdir'].rstrip(os.sep) + '-revised'))

    def test_the_screen_says_what_a_revision_came_to(self):
        screen = self.wrapping_screen(lambda title, options: None)
        offered = []
        screen.offer_changes = lambda: offered.append(True)
        for said, shown in ((f'{RUN}/1: revised, but its revision does not pass the checks '
                             'either, so the old change still waits: reject drops it (cost '
                             '1.00); see x/analysis.md', True),
                            (f'{RUN}/1: revised, its new change waits for your approval in '
                             'place of the old: A asks about it (cost 1.00); see x', False)):
            self.shown.clear()
            screen.finished({'kind': 'analyst', 'line': f'revise {RUN}/1', 'job': '1',
                             'title': 'Revising the change', 'out': io.StringIO(said + '\n')})
            self.assertEqual(screen.flash, said)
            self.assertEqual(bool(self.shown), shown)
        self.assertEqual(offered, [True])

    def test_a_change_that_clashes_is_made_again_by_the_analyst(self):
        self.hold_a_doubt()
        theirs = [{'file': 'teuthology.log', 'pattern': 'never seen'}]
        self.change_record(exclude=theirs)
        said, _ = self.run_command('approve 1')
        self.assertIn(f'{RUN}/1: not kept, so it still waits: reject drops it. Others changed '
                      'what it changes meanwhile, in ways that clash with it:\n'
                      '    record 100: another lesson changed it meanwhile too, with a '
                      'different exclude\n'
                      'revise 1 asks the analyst to make it again for the library as it is '
                      'now (costs AI).', said)
        both = theirs + [{'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}]
        self.analyst.answers = [answer(None, [record(examples=[f'{RUN}/2'], exclude=both)],
                                       summary='The monitor leaked; both excluded.')]
        said, _ = self.run_command(f'revise {RUN}/1')
        # In the session it made the change in, told why it cannot be kept now.
        self.assertEqual(self.analyst.prompts[-1], (
            REVISE.format(why='record 100: another lesson changed it meanwhile too, with a '
                              'different exclude'), 's1'))
        self.assertIn(f'{RUN}/1: revised, its new change waits for your approval in place of '
                      'the old: A asks about it', said)
        [(_, held)] = self.review.waiting()
        self.assertEqual(held['summary'], 'The monitor leaked; both excluded.')
        said, _ = self.run_command('approve 1')
        self.assertIn('Kept 1 of the 1 change.', said)
        self.assertEqual(yaml.safe_load(self.record_text())['exclude'], both)

    def test_a_change_the_library_has_made_already(self):
        # Its job is placed as it would place it: it may not be needed any more.
        r = next(r for r in self.review.results if r.job.id == '2')
        self.assertIn('The library places the job in 100 already',
                      self.review._needless(SimpleNamespace(result=r), {'problem': '100'}))
        self.assertEqual(self.review._needless(SimpleNamespace(result=r), {'problem': None}),
                         '')

    def test_the_screen_offers_to_revise_a_change_not_kept(self):
        self.hold_a_doubt()
        [(path, _)] = self.review.waiting()
        screen = tui.Screen.__new__(tui.Screen)
        screen.model, screen.bg, screen.offered, screen.flash = tui.Model(self.review), None, \
            {path}, None
        asked, started = [], []
        screen.draw = lambda: None
        screen.choose = lambda title, options, chosen=0, body=(): asked.append(
            (title, options, body)) or 0
        screen.background = lambda title, line, kind, job=None: started.append(
            (title, line, kind, job))
        line = (f'{RUN}/1: not kept, so it still waits: reject drops it. Checked again, '
                'against the library as it is now, it fails these checks:')
        screen.not_kept_box([line, '    record 100 loses examples: x', 'Kept 0 of the 1 change.'])
        [(title, options, body)] = asked
        self.assertEqual(title, f'Not kept: the change for {RUN}/1')
        self.assertEqual(options, [tui.REVISE, tui.SHOW_CHECKS, 'Reject it', tui.LATER])
        self.assertEqual(body, [line, '    record 100 loses examples: x'])
        self.assertEqual(started, [(f'Revising the change for {RUN}/1', f'revise {RUN}/1',
                                    'analyst', '1')])
        # Its revision is offered once it waits.
        self.assertNotIn(path, screen.offered)

    def test_a_change_that_waits_is_rejected(self):
        self.hold_a_doubt()
        said, _ = self.run_command('reject all')
        self.assertIn(f'{RUN}/1: rejected; it is set aside in ', said)
        self.assertEqual(self.review.waiting(), [])
        self.assertNotIn('Leak_DefinitelyLost', self.record_text())
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'cache', 'pending',
                                                     'rejected'))), 1)

    def test_the_analysts_account_of_a_change_that_waits(self):
        # Its answer passed the checks: its account is the group's, saying that its
        # change waits; once kept, it is the group's as another's is; and the review
        # keeps it, for when it starts again.
        self.hold_a_doubt()
        job_id = report.groups(self.review.results)[0][1][0].job.id

        def shown():
            number = next(n for n, (_, rs) in enumerate(report.groups(self.review.results), 1)
                          if any(r.job.id == job_id for r in rs))
            return self.run_command(f'show {number}')[0]

        self.assertIn('Why, as the analyst (fake) tells it:\nIts change to the records or the '
                      'tree waits for your approval: A asks.\nan explanation\n', shown())
        self.run_command('approve 1')
        self.assertIn('Why, as the analyst (fake) tells it:\nan explanation\n', shown())
        self.review = self.start()
        self.assertIn('Why, as the analyst (fake) tells it:\nan explanation\n', shown())
        r = next(r for r in self.review.results if r.job.id == job_id)
        self.assertIn('The analyst found: The monitor leaked a buffer at exit.', r.notes)
        # And the PR under test it found caused it, once its change is kept.
        r.analysis = None
        self.review.state.answers[job_id][-1]['account']['pr'] = 'PR #300'
        self.review.restore_accounts()
        self.assertEqual(r.blamed, [('PR #300', 'the analyst (fake)',
                                     'The monitor leaked a buffer at exit.')])

    def test_an_account_kept_before_accounts_were(self):
        # A lesson's directory has what the analyst printed: its last answer is its
        # account, if it passed the checks or its change waited for approval.
        workdir = os.path.join(self.root, 'learn', '7')
        os.makedirs(workdir)
        for n, said in ((1, {'assessment': 'unsure', 'summary': 'first', 'explanation': 'x'}),
                        (2, answer('100', summary='at last'))):
            with open(os.path.join(workdir, f'doubt-{n}.out'), 'w') as f:
                f.write(f'Thinking.\n{json.dumps(said)}\n')
        with open(os.path.join(workdir, 'lesson.json'), 'w') as f:
            json.dump({'outcome': 'unsettled'}, f)
        latest = {'agent': 'cmd:cat', 'workdir': workdir, 'lines': [
            f'7: {RUN}/7: the change waits for approval: qa_triage doubt {RUN}/7 --proposal '
            '/cache/pending/7.json; to inspect it, git show abc']}
        account = Review.recovered(latest)
        self.assertEqual((account['summary'], account['explanation'], account['problem']),
                         ('at last', 'an explanation', '100'))
        self.assertEqual((account['analyst'], account['job'], account['held']),
                         ('cmd:cat', '7', '/cache/pending/7.json'))
        self.assertEqual(self.review.standing(account), None)
        # One whose answer did not pass the checks has none.
        self.assertIsNone(Review.recovered(dict(latest, lines=['7: not settled'])))
        with open(os.path.join(workdir, 'lesson.json'), 'w') as f:
            json.dump({'outcome': 'learned'}, f)
        self.assertNotIn('held', Review.recovered(dict(latest, lines=['7: learned 100'])))

    def test_updating_the_records_outside(self):
        # A known problem: a comment on its tracker issue, that it was seen again.
        groups = report.groups(self.review.results)
        known = next(rs for _, rs in groups if rs[0].problem)
        new = next(rs for _, rs in groups if not rs[0].problem)
        # Only its jobs, as /a/ paths, as people mostly write it, first; or with what was
        # found.
        ways = self.review.ways(known)
        self.assertEqual([(w['way'], w['number'], w['recommended']) for w in ways][:2],
                         [('jobs', known[0].problem.id, True),
                          ('tracker', known[0].problem.id, False)])
        _, text = self.review.draft(known, 'jobs', known[0].problem.id)
        self.assertEqual(text, '\n'.join(f'/a/{RUN}/{r.job.id}' for r in known))
        _, text = self.review.draft(known, 'tracker', known[0].problem.id)
        self.assertTrue(text.startswith(f'Seen again in {len(known)} job'))
        # A new failure: a new issue; once an analyst found a PR under test caused it,
        # a comment on that PR, for its developer.
        self.assertEqual([(w['way'], w['recommended']) for w in self.review.ways(new)][0],
                         ('issue', True))
        new[0].analysis = {'analyst': 'fake', 'job': new[0].job.id, 'pr': 'PR #300',
                           'summary': 'it drops messages', 'explanation': 'fake broke it.',
                           'walkthrough': [{'title': 'The failure', 'do': 'Open the log.',
                                            'finds': 'It times out.', 'evidence': [
                                                {'file': 'teuthology.log', 'text': 'timed out',
                                                 'at': 'teuthology.log:12'}]}]}
        ways = self.review.ways(new)
        self.assertEqual([(w['way'], w['number'], w['recommended']) for w in ways][:2],
                         [('pr', '300', True), ('issue', None, False)])
        _, text = self.review.draft(new, 'pr', '300')
        self.assertIn('the failure looks to be caused by it', text)
        self.assertIn('**Why:**\n\nthe analysis broke it.', text)
        self.assertIn('1. **The failure.** Open the log. It times out.\n   ```\n'
                      '   teuthology.log:12: timed out\n   ```', text)
        self.assertNotIn('fake', text)
        # Posted, it is kept, and not recommended again.
        posted = []
        self.review.comment_pr = lambda number, body: posted.append((number, body)) or \
            'https://github.com/ceph/ceph/pull/300#issuecomment-1'
        said, _ = self.run_command(f'post {new[0].job.id} pr')
        self.assertIn('Commented on PR #300: https://github.com/ceph/ceph/pull/300#issuecomment-1',
                      said)
        self.assertEqual(posted, [('300', text)])
        ways = self.review.ways(new)
        self.assertTrue(ways[0]['posted'])
        self.assertFalse(any(w['recommended'] for w in ways if w['way'] == 'pr'))
        said, _ = self.run_command(f'update {new[0].job.id}')
        self.assertIn('  pr 300: Comment on PR #300, which the analyst found caused it', said)
        # A tracker issue needs its key.
        self.review.tracker_key = lambda: None
        said, _ = self.run_command(f'post {known[0].job.id} tracker')
        self.assertIn('error: no API key for the tracker', said)
        noted = []
        self.review.tracker_key = lambda: 'key'
        self.review.comment_issue = lambda key, number, notes: noted.append(number) or \
            f'https://tracker.ceph.com/issues/{number}'
        said, _ = self.run_command(f'post {known[0].job.id} jobs')
        self.assertEqual(noted, [known[0].problem.id])
        self.assertIn(f'Commented on tracker issue {known[0].problem.id}', said)
        # Either is the comment on the issue: neither is recommended again, nor is the
        # issue in the run's comments still to post.
        self.assertTrue(all(w['posted'] for w in self.review.ways(known)
                            if w['way'] in ('jobs', 'tracker')))
        self.assertTrue(all(f[4] for f in self.review.sightings()))

    def test_an_issue_resolved_after_the_run_began_is_not_news(self):
        known = next(rs for _, rs in report.groups(self.review.results) if rs[0].problem)
        issue = known[0].problem.id
        asked = []

        def read_issue(number):
            asked.append(number)
            return {'status': {'name': 'Resolved', 'is_closed': True},
                    'closed_on': '2099-01-01T00:00:00Z'}
        self.review.read_issue = read_issue
        self.assertEqual(self.review.resolved_after_run(issue), '2099-01-01')
        self.assertEqual(self.review.sightings(news=True), [])
        self.assertEqual(len(self.review.sightings()), 1)
        said, _ = self.run_command('sightings')
        self.assertIn('left out, resolved on 2099-01-01, after the run began', said)
        # Asked once a review.
        self.review.sightings(news=True)
        self.assertEqual(asked, [issue])
        # Resolved before the run, it was seen again after: news.
        self.review._resolved = {}
        self.review.read_issue = lambda number: {'status': {'is_closed': True},
                                                 'closed_on': '2001-01-01T00:00:00Z'}
        self.assertEqual(len(self.review.sightings(news=True)), 1)
        # Open, or not to be read: news.
        self.review._resolved = {}
        self.review.read_issue = lambda number: {'status': {'is_closed': False},
                                                 'closed_on': None}
        self.assertEqual(len(self.review.sightings(news=True)), 1)

    def test_every_tracker_issue_the_run_saw_again(self):
        known = next(rs for _, rs in report.groups(self.review.results) if rs[0].problem)
        [(issue, numbers, groups, text, posted)] = self.review.sightings()
        self.assertEqual((issue, groups, posted), (known[0].problem.id, [known], None))
        self.assertEqual(text, '\n'.join(f'/a/{RUN}/{r.job.id}' for r in known))
        self.review.tracker_key = lambda: 'key'
        noted = []
        self.review.comment_issue = lambda key, number, notes: noted.append(
            (number, notes)) or f'https://tracker.ceph.com/issues/{number}'
        said, _ = self.run_command('sightings post')
        self.assertEqual(noted, [(issue, text)])
        self.assertIn('Posted 1 of the 1 comments for the tracker issues the run saw again.',
                      said)
        # Not again; and kept as the group's update, as i posts it.
        said, _ = self.run_command('sightings post')
        self.assertEqual(len(noted), 1)
        self.assertTrue(self.review.sightings()[0][4])
        self.assertTrue(next(w for w in self.review.ways(known)
                             if w['way'] == 'tracker')['posted'])
        # A failure a PR under test caused is the PR's to hear of.
        known[0].blamed.append(('PR #300', 'the analyst (fake)', 'it broke it'))
        self.assertEqual(self.review.sightings(), [])

    def test_approving_the_prs_under_test(self):
        self.session.header = ['QA tracker https://tracker.ceph.com/issues/82144, PRs under '
                               'test: #300, #301, #302']
        triage = self.session.triage
        triage.batch_prs = {'300', '301', '302'}
        triage.branches = {'b': [('PR #300', 'c1', 'p1', ['src/a.cc']),
                                 ('PR #301', 'c2', 'p2', ['src/b.cc'])]}
        new = next(rs for _, rs in report.groups(self.review.results) if not rs[0].problem)
        new[0].blamed.append(('PR #301', 'the analyst (fake)', 'it drops messages'))
        text, rows = self.review.approval()
        self.assertEqual(text, 'Rados approved: https://tracker.ceph.com/projects/rados/wiki/'
                               'MAIN#httpstrackercephcomissues82144')
        self.assertEqual([(n, ok) for n, _, ok in rows],
                         [('300', True), ('301', False), ('302', False)])
        self.assertIn('not merged into the build the run tested', rows[2][1])
        self.assertIn('an analysis found it caused group', rows[1][1])
        posted = []
        self.review.comment_pr = lambda number, body: posted.append((number, body)) or \
            f'https://github.com/ceph/ceph/pull/{number}#issuecomment-1'
        said, _ = self.run_command('approvals post')
        self.assertEqual(posted, [('300', text)])
        self.assertIn('Posted the approval on 1 of the 1 PRs to approve.', said)
        said, _ = self.run_command('approvals post')
        self.assertEqual(len(posted), 1)
        self.assertIn('#300  approve: ', self.run_command('approvals')[0])
        self.assertIn('(posted ', self.run_command('approvals')[0])
        # With no QA tracker, there is no wiki section to link to.
        self.session.header = []
        self.assertIn('error: no QA tracker is known', self.run_command('approvals')[0])

    def run_with_a_qa_tracker(self):
        self.session.header = ['QA tracker https://tracker.ceph.com/issues/82144, PRs under '
                               'test: #300']
        self.session.triage.batch_prs = {'300'}
        self.session.triage.branches = {'b': [('PR #300', 'c1', 'p1', ['src/a.cc'])]}

    def test_the_run_is_approved_on_its_qa_tracker(self):
        self.run_with_a_qa_tracker()
        self.review.read_issue = lambda number: {'author': {'id': 10897, 'name': 'A Builder'}}
        said, _ = self.run_command('qa')
        self.assertIn('    the comment: Rados approved: https://tracker.ceph.com/projects/rados/'
                      'wiki/MAIN#httpstrackercephcomissues82144', said)
        self.assertIn('    assigned to: A Builder', said)
        changed = []
        self.review.tracker_key = lambda: 'key'
        self.review.update_issue = lambda key, number, **change: changed.append(
            (number, change)) or f'https://tracker.ceph.com/issues/{number}'
        said, _ = self.run_command('qa approve')
        self.assertEqual(changed, [('82144', {
            'notes': 'Rados approved: https://tracker.ceph.com/projects/rados/wiki/MAIN'
                     '#httpstrackercephcomissues82144', 'status_id': 25,
            'assigned_to_id': 10897})])
        self.assertIn('Approved the run on its QA tracker: https://tracker.ceph.com/issues/82144',
                      said)
        self.assertIn('qa', self.review.posted_for_run())

    def wrapping_screen(self, answer):
        """The screen, with what the person answers each box scripted by answer(title,
        options), and what it starts, shows and copies kept."""
        screen = tui.Screen.__new__(tui.Screen)
        screen.model, screen.bg, screen.offered, screen.flash = tui.Model(self.review), None, \
            set(), None
        screen.queued = []
        self.started, self.shown, self.asked = [], [], []
        screen.draw = lambda *a, **k: None
        screen.choose = lambda title, options, chosen=0, body=(), **k: self.asked.append(
            title) or answer(title, options)
        screen.background = lambda title, line, kind='judge', **k: self.started.append(
            (line, kind))
        screen.pager = lambda title, lines: self.shown.append((title, list(lines)))
        screen.ask = lambda *a, **k: ''
        return screen

    def test_done_with_the_run_with_the_keys(self):
        self.run_with_a_qa_tracker()
        self.review.tracker_key = lambda: 'key'
        self.review.read_issue = lambda number: {'author': {'id': 10897, 'name': 'A Builder'}}

        def answer(title, options):
            if title.startswith('Done with the run'):
                return 0
            if title.startswith(('Comment on the', 'Post the approval')):
                return options.index('Not now')
            return None
        screen = self.wrapping_screen(answer)
        copied = []
        with mock.patch.object(setup, '_gh_user', return_value='someone'), \
                mock.patch.object(tui, 'copy', lambda text: copied.append(text) or 'pbcopy'):
            screen.wrap_up()
        # The wiki is not edited: its section is copied, to paste in.
        self.assertEqual(self.started, [('qa approve', 'post')])
        self.assertTrue(copied[0].startswith('h3. https://tracker.ceph.com/issues/82144'))
        title, said = self.shown[-1]
        self.assertEqual(title, 'Done with the run: what was done')
        self.assertIn("The wiki: the run's section copied, to paste in.", said)
        self.assertIn('The QA tracker: approved, and assigned to A Builder.', said)

    def test_done_with_the_run_by_hand(self):
        self.run_with_a_qa_tracker()
        self.review.tracker_key = lambda: None
        self.review.read_issue = lambda number: {'author': {'id': 10897, 'name': 'A Builder'}}

        def answer(title, options):
            if title.startswith('Done with the run'):
                return 0
            if title.startswith('Posting to tracker.ceph.com'):
                return next(i for i, o in enumerate(options) if o.startswith('Do these'))
            if title.startswith('Posting to GitHub'):
                return next(i for i, o in enumerate(options) if o.startswith('Skip'))
            return None
        screen = self.wrapping_screen(answer)
        copied, opened = [], []
        with mock.patch.object(setup, '_gh_user', return_value=None), \
                mock.patch.dict(os.environ), \
                mock.patch.object(tui.shutil, 'which', return_value=None), \
                mock.patch.object(tui, 'copy', lambda text: copied.append(text) or 'pbcopy'), \
                mock.patch.object(setup, 'open_page', lambda url: opened.append(url) or True):
            os.environ.pop('GITHUB_TOKEN', None)
            screen.wrap_up()
        self.assertEqual(self.started, [])
        # Asked once for the tracker's key, though several steps post there.
        self.assertEqual(sum(t.startswith('Posting to tracker') for t in self.asked), 1)
        self.assertNotIn('https://tracker.ceph.com/projects/rados/wiki/MAIN/edit', opened)
        self.assertIn('https://tracker.ceph.com/issues/82144', opened)
        self.assertTrue(any(text.startswith('h3. https://tracker.ceph.com/issues/82144')
                            for text in copied))
        self.assertIn('Rados approved: https://tracker.ceph.com/projects/rados/wiki/MAIN'
                      '#httpstrackercephcomissues82144', copied)
        said = self.shown[-1][1]
        self.assertIn("The wiki: the run's section copied, to paste in.", said)
        self.assertIn('The PRs: left; P approves them.', said)

    def test_done_with_the_run_skipping_the_tracker(self):
        # Skipped once, the steps that post to the tracker ask nothing more.
        self.run_with_a_qa_tracker()
        self.review.tracker_key = lambda: None

        def answer(title, options):
            if title.startswith('Done with the run'):
                return 0
            if title.startswith('Posting to'):
                return len(options) - 1
            return None
        screen = self.wrapping_screen(answer)
        with mock.patch.object(setup, '_gh_user', return_value=None), \
                mock.patch.dict(os.environ), mock.patch.object(tui.shutil, 'which',
                                                               return_value=None):
            os.environ.pop('GITHUB_TOKEN', None)
            screen.wrap_up()
        self.assertEqual(sum(t.startswith('Posting to tracker') for t in self.asked), 1)
        after = self.asked[next(i for i, t in enumerate(self.asked)
                                if t.startswith('Posting to tracker')):]
        # Only the wiki's step, which needs no key: its section is copied.
        self.assertEqual([t.split(': ', 1)[1] for t in after
                          if t.startswith('Done with the run')],
                         ["the run's section of the RADOS wiki, page MAIN, to paste in"])
        said = self.shown[-1][1]
        self.assertIn('The known issues seen again: left; I comments on them.', said)
        self.assertIn('The QA tracker: left; qa approve, at the prompt, approves it.', said)

    def test_the_report_is_written_as_each_change_is_done(self):
        written = os.path.join(self.root, 'out', 'results.json')
        self.assertFalse(os.path.exists(written))
        self.run_command('show 1')
        self.assertFalse(os.path.exists(written))
        self.run_command('judge 1')
        self.assertTrue(os.path.exists(written))

    def test_a_draft_edited(self):
        self.assertEqual(tui.parse_draft('Subject: a new one\n\nthe text\n', True),
                         ('a new one', 'the text'))
        self.assertEqual(tui.parse_draft('\nthe text\n\n', False), (None, 'the text'))

    def test_the_screen_asks_about_a_change_that_waits_once(self):
        self.hold_a_doubt()
        screen = tui.Screen.__new__(tui.Screen)
        screen.model, screen.bg, screen.offered, screen.flash = tui.Model(self.review), None, \
            set(), None
        asked, started = [], []
        screen.draw = lambda: None
        screen.choose = lambda title, options, chosen=0, body=(): asked.append((title, body)) or 0
        screen.background = lambda title, line, kind: started.append((title, line, kind))
        self.assertEqual(screen.waiting_count(), 1)
        self.assertTrue(screen.offer_changes())
        [(title, body)] = asked
        self.assertEqual(title, 'A change to the tree waits for your approval')
        self.assertIn('The monitor leaked a buffer at exit.', body)
        self.assertEqual(started, [('Keeping 1 change', 'approve 1', 'approve')])
        self.assertFalse(screen.offer_changes())

    def test_the_screen_shows_a_doubts_response_and_discusses_it(self):
        self.analyst = Program([])
        self.review = self.start()
        self.review.args.settings = {'approve': 'ask'}
        change = [record(examples=[f'{RUN}/2'], exclude=[
            {'file': 'teuthology.log', 'pattern': 'Leak_DefinitelyLost'}])]
        self.analyst.answers = [answer(None, change, summary='It leaked.'),
                                answer(None, change, summary='As we said, it leaked.')]
        self.run_command('doubt 1 --says new it leaked')
        screen = tui.Screen.__new__(tui.Screen)
        screen.model, screen.bg, screen.offered, screen.flash, screen.queued = \
            tui.Model(self.review), None, set(), None, []
        asked, shown, handed, started = [], [], [], []
        picks = iter([2, 1, 0])
        screen.draw = lambda: None
        screen.choose = lambda title, options, body=(): \
            asked.append((title, options, body)) or next(picks)
        screen.pager = lambda title, lines: shown.append((title, lines))
        screen.wait = lambda title, line, render, hint, stops=True: screen.model.command(line)
        screen.hand_over = lambda argv, workdir, program, said: handed.append(said) or 1.0
        screen.background = lambda title, line=None, kind='judge', load=None, job=None: \
            started.append((title, line, kind, job))
        # Once the doubt is done, its response, whatever it says, in a box.
        screen.finished({'kind': 'analyst', 'line': 'doubt 1 --says new it leaked', 'job': '1',
                         'title': 'The analyst on a doubt about group 1', 'out': io.StringIO()})
        title, options, body = asked[0]
        self.assertEqual(title, "fake's response to your doubt about group 1")
        self.assertEqual(body[0], 'This is my response, as fake gives it:')
        self.assertEqual(options, [
            'Accept it: keep its answer, and any change it made, checked again and pushed to '
            'the shared library', 'Discuss it with fake in its program: when you leave, it '
            'gives its final answer', 'Show all it found, and its change',
            'Dismiss it: keep nothing it made', 'Leave it for now: d on the group shows it again'])
        # Shown whole, then discussed in its program, going on from the doubt's session;
        # then its final answer is asked for, in the background.
        self.assertEqual(shown[0][0], "fake's response, whole")
        self.assertIn('What it found:', shown[0][1])
        self.assertEqual(self.analyst.prompts[1], (RESPONSE_TALK, 's1'))
        self.assertIn('It goes on from its response, with all the evidence it had. Ask it what '
                      'you like, and tell', handed[0])
        self.assertEqual(started, [("fake's final answer on group 1", 'digest 1', 'analyst', '1')])
        # Once it gives that, the box shows it, to accept.
        self.run_command('digest 1')
        screen.finished({'kind': 'analyst', 'line': 'digest 1', 'job': '1',
                         'title': "fake's final answer on group 1", 'out': io.StringIO()})
        self.assertEqual(asked[-1][2][0], 'This is my response, after our discussion, as fake '
                                          'gives it:')
        self.assertEqual(started[-1], ("Accepting fake's response on group 1", 'accept 1',
                                       'approve', '1'))
        # Its change was asked about there, not again as a lesson's.
        self.assertFalse(screen.offer_changes())

    def test_the_screen_asks_as_it_opens_about_the_changes_that_wait(self):
        self.hold_a_doubt()
        screen = tui.Screen.__new__(tui.Screen)
        screen.model, screen.bg, screen.offered, screen.flash = tui.Model(self.review), None, \
            set(), None
        asked, shown = [], []
        screen.draw = lambda: None
        screen.confirm = lambda question: asked.append(question) or True
        screen.approvals = lambda paths: shown.append(paths)
        screen.offer_waiting()
        self.assertEqual(asked, ['1 change to the tree, which the analyst made before, waits '
                                 'for your approval. Look at it now?'])
        self.assertEqual(len(shown[0]), 1)
        screen.offer_waiting()
        self.assertEqual(len(asked), 1)

    def test_background_work_waits_for_what_is_there(self):
        import threading
        import time
        screen = tui.Screen.__new__(tui.Screen)
        screen.model, screen.offered, screen.queued, screen.flash = tui.Model(self.review), \
            set(), [], None
        screen.chosen = lambda: None
        busy = threading.Event()
        screen.bg = {'title': 'The judge on 2 groups', 'thread': mock.Mock(is_alive=busy.is_set),
                     'out': io.StringIO(), 'kind': 'judge', 'line': 'judge all', 'done': 0}
        busy.set()
        screen.background('Keeping 1 change', 'summary', kind='approve')
        self.assertEqual(screen.queued, [{'title': 'Keeping 1 change', 'line': 'summary',
                                          'kind': 'approve', 'load': None, 'job': None}])
        self.assertEqual(screen.flash, 'Keeping 1 change: it waits (…) for the judge on 2 '
                                       'groups, and starts once that is done.')
        # A lesson asked for meanwhile waits too, and its group says so.
        groups = screen.model.groups()
        first, second = groups[0][1][0].job.id, groups[1][1][0].job.id
        screen.background('The analyst on group 2', 'learn 2', kind='analyst')
        self.assertEqual(screen.queued[1]['job'], second)
        self.assertEqual(screen.queued_groups(), {2})
        self.assertEqual(screen.waits(2), '… The analyst on group 2 waits for the judge on 2 '
                                          'groups, and starts once that is done; x stops both.')
        self.assertIsNone(screen.waits(1))
        # Stopped, what waits for it and asks an agent is not begun; a change kept is.
        self.assertEqual(screen.unqueued(), ' What waits for it is not begun either: the '
                                            'analyst on group 2.')
        screen.unqueue()
        self.assertEqual([t['title'] for t in screen.queued], ['Keeping 1 change'])
        # Begun, it is for its group under its number then.
        begun = []
        with mock.patch.object(screen, 'background',
                               lambda *a, **k: begun.append((a, k))):
            screen.number_of = {second: 1, first: 2}.get
            screen.begin({'title': 'The analyst on group 2', 'line': 'learn 2 again',
                          'kind': 'analyst', 'load': None, 'job': second})
            del screen.number_of
        self.assertEqual(begun, [(('The analyst on group 1', 'learn 1 again', 'analyst', None),
                                  {'job': second})])
        # Once that is done, it starts.
        busy.clear()
        screen.suggest_learning = lambda: None
        screen.watch()
        self.assertEqual((screen.queued, screen.bg['title']), ([], 'Keeping 1 change'))
        screen.bg['thread'].join(10)
        self.assertIn('[2]', screen.bg['out'].getvalue())

    def test_a_proposal_from_a_discussion(self):
        self.assertIn('discuss it with fake first', self.run_command('propose 2')[0])
        self.run_command('discuss 2 the OSD log says bouncing')
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        self.analyst.answers = [answer('200', [new], quotes=(('teuthology.log', 'FAILED test'),))]
        said, _ = self.run_command('propose 2 use the OSD log')
        self.assertIn('3: proposed, kept, wrote problems/200-problem-200.yaml', said)
        # In the discussion's session, told what to give.
        prompt, session = self.analyst.prompts[-1]
        self.assertEqual(session, 'c1')
        self.assertTrue(prompt.startswith('The person asks for your proposal.'))
        self.assertTrue(prompt.endswith('The person adds: use the OSD log'))
        said, _ = self.run_command('summary')
        self.assertIn('  [2]    TR 200  3  problem 200', said)
        said, _ = self.run_command('show 2')
        self.assertIn('you:\n  /propose use the OSD log', said)

    def test_a_proposal_that_changes_nothing(self):
        self.run_command('discuss 2 is the judge right?')
        workdir = self.review.state.discussions['3'][-1]['workdir']
        with open(os.path.join(workdir, 'doubt.yaml')) as f:
            began = f.read()
        self.analyst.answers = [answer(None, assessment='right',
                                       quotes=(('teuthology.log', 'FAILED test'),))]
        said, _ = self.run_command('propose 2')
        self.assertIn('3: proposed, no change to the records or the tree', said)
        # The analyst's answer is not a reviewer's: no label says so.
        self.assertFalse(os.path.exists(os.path.join(self.root, 'labels')))
        self.analyst.answers = [answer(None, assessment='right',
                                       quotes=(('teuthology.log', 'FAILED test'),))]
        self.run_command('propose 2 again')
        # Each turn's output is kept, and the doubt the session began with.
        self.assertEqual(self.analyst.tags, ['doubt-1', 'doubt-2'])
        with open(os.path.join(workdir, 'doubt.yaml')) as f:
            self.assertEqual(f.read(), began)
        said, _ = self.run_command('show 2')
        # Twice as it said it, and first as why: it passed the checks.
        self.assertEqual(said.count('an explanation'), 3)
        self.assertIn('Why, as the analyst (fake) tells it:\nan explanation', said)

    def test_a_copied_review_leaves_the_original_alone(self):
        self.run_command('discuss 2 is the judge right?')
        out = os.path.join(self.root, 'out')
        with open(os.path.join(out, 'review.json')) as f:
            kept = json.load(f)
        self.assertEqual(kept['discussions']['3'][0]['workdir'], 'learn/discuss-3')
        self.assertEqual(State(os.path.join(out, 'review.json')).discussions['3'][0]['workdir'],
                         os.path.join(out, 'learn', 'discuss-3'))
        copied = os.path.join(self.root, 'copy')
        shutil.copytree(out, copied)
        self.assertEqual(State(os.path.join(copied, 'review.json')).discussions['3'][0]['workdir'],
                         os.path.join(copied, 'learn', 'discuss-3'))
        # One kept by an older review, as a whole path, is the original's; so is
        # the one an answer that only points at its analysis names.
        lesson = os.path.join(out, 'learn', '3')
        kept['discussions']['3'][0]['workdir'] = os.path.join(out, 'learn', 'discuss-3')
        kept['answers']['3'] = [{'kind': 'lesson', 'agent': 'fake', 'cost': 0, 'steps': [],
                                 'lines': [f'3: learned (cost 1.00); see {lesson}/analysis.md'],
                                 'session': 's1', 'workdir': lesson, 'when': ''}]
        with open(os.path.join(copied, 'review.json'), 'w') as f:
            json.dump(kept, f)
        state = State(os.path.join(copied, 'review.json'))
        self.assertIsNone(state.discussions['3'][0]['workdir'])
        self.assertIsNone(state.latest('3', 'lesson')['workdir'])
        with open(os.path.join(out, 'review.json'), 'w') as f:
            json.dump(kept, f)
        self.assertEqual(State(os.path.join(out, 'review.json')).latest('3', 'lesson')['workdir'],
                         lesson)

    def test_choosing_the_analyst(self):
        # Each kind of agent installed here, with its own default model.
        with mock.patch('qa_triage.review.installed',
                        return_value=[('claude', 'Claude Code'), ('goose', 'goose')]):
            said, _ = self.run_command('analyst')
            self.assertEqual(self.review.described()[1],
                             ('claude', 'claude+files', 'Claude Code'))
        self.assertIn('The analyst is fake.', said)
        # Each with where a person talks with it.
        self.assertIn('  fake    talk: here, with discuss\n'
                      '  claude  talk: Claude Code\n'
                      '  goose   talk: here, with discuss', said)
        # Or any agent, with its model.
        said, _ = self.run_command('analyst claude:opus')
        # By its proper name, and not "+files": an analyst always reads them.
        self.assertIn('The analyst is now Claude (opus).', said)
        self.assertEqual(self.review.units['claude:opus+files'], 'dollars')

    def test_talking_in_the_analysts_program(self):
        self.assertIn('fake has no program of its own to talk in: discuss talks with it here',
                      self.run_command('talk 2')[0])
        self.analyst = Program([])
        self.review = self.start()
        said, _ = self.run_command('ready 2')
        # Its first turn: the evidence and the instructions, and what comes next.
        [(prompt, session)] = self.analyst.prompts
        self.assertIsNone(session)
        self.assertTrue(prompt.startswith(DISCUSS))
        self.assertIn('A record is YAML', prompt)
        self.assertTrue(prompt.endswith(LIVE))
        workdir = os.path.join(self.root, 'out', 'learn', 'discuss-3')
        self.assertIn(f'Ready: true  (in {workdir})', said)
        self.assertIn('reply 1', said)
        # Ready once: going back asks nothing first.
        said, _ = self.run_command('talk 2')
        self.assertEqual(len(self.analyst.prompts), 1)
        self.assertEqual(self.analyst.opened, (workdir, 'c1'))
        self.assertIn('3: talked in its program; it wrote no proposal', said)
        # What it proposes there is checked, asking it nothing, and kept.
        new = record(id='200', pattern='bouncing to primary', examples=[f'{RUN}/3'])
        new['confirm'][0]['file'] = 'remote/*/log/ceph-osd.*.log'
        self.analyst.proposal = os.path.join(self.root, 'proposal')
        with open(self.analyst.proposal, 'w') as f:
            json.dump(answer('200', [new], quotes=(('teuthology.log', 'FAILED test'),)), f)
        said, _ = self.run_command('talk 2')
        self.assertIn('3: proposed in its program, kept, wrote problems/200-problem-200.yaml',
                      said)
        self.assertEqual(len(self.analyst.prompts), 1)
        self.assertTrue(os.path.exists(os.path.join(workdir, 'proposal-1.json')))
        self.assertFalse(os.path.exists(os.path.join(workdir, 'proposal.json')))
        said, _ = self.run_command('summary')
        self.assertIn('  [2]    TR 200  3  problem 200', said)
        self.assertIn('Cost: fake 0.10 coins (1 discussion)', said)
        # One that fails the checks is not kept, and says where they are.
        with open(self.analyst.proposal, 'w') as f:
            json.dump(answer('300', quotes=(('teuthology.log', 'not in the log'),)), f)
        said, _ = self.run_command('talk 2')
        self.assertIn(f'3: proposed in its program, not kept; the checks are at the end of '
                      f'{workdir}/analysis.md', said)
        self.assertIn('FAIL  0 of 1 quoted lines found in the evidence', said)
        with open(self.analyst.proposal, 'w') as f:
            f.write('{"assessment": ')
        self.assertIn('not kept: its proposal.json does not load',
                      self.run_command('talk 2')[0])
        # The discussion says what happened, for the screen and a later review.
        said, _ = self.run_command('show 2')
        self.assertIn('qa_triage:\n  (Asked it to get ready to talk in its program', said)
        self.assertIn('you:\n  (Talked with it in its program, 1 min.)', said)

    def test_leaving(self):
        said, stop = self.run_command('quit')
        self.assertTrue(stop)
        self.assertIn('Cost: nothing', said)
        # With the report written, as it is now.
        self.assertIn(f'Report: {self.root}/out/report.md', said)
        self.assertTrue(os.path.exists(os.path.join(self.root, 'out', 'results.json')))


class TestTracking(Reviewing):
    """A new problem the tree explains: a draft issue, then a record once filed."""

    extra = {'7': ('valgrind error: Leak_DefinitelyLost malloc',
                   {'teuthology.log': ['valgrind found leaks']})}

    def test_issue_then_track(self):
        # On the screen, the heading says they are new for sure.
        self.assertIn((2, '  2  1 job    valgrind/Leak_DefinitelyLost: valgrind reported '
                          'Leak_DefinitelyLost in malloc'), tui.Model(self.review).rows())
        said, _ = self.run_command('summary')
        self.assertIn('New problems the tree explains: each needs a tracker filed (1 job):\n'
                      '  [2] new, matched: 7', said)
        self.assertIn('For a person to look into (1 job):\n  [3] new, unmatched: 3', said)
        said, _ = self.run_command('issue 2')
        self.assertIn('Subject: valgrind reported Leak_DefinitelyLost in malloc', said)
        self.assertIn(f'* https://pulpito.ceph.com/{RUN}/7/', said)
        self.assertIn("What qa_triage's checks found:\n\n<pre>\n1. What went wrong first?", said)
        self.assertIn('give the number', self.run_command('track 2')[0])
        self.assertIn('the kind is', self.run_command('track 2 300 broken')[0])
        said, _ = self.run_command('track 2 300 test')
        self.assertIn('tree: valgrind/Leak_DefinitelyLost', said)
        self.assertIn('ok    tracker 300: a problem (New)', said)
        said, _ = self.run_command('summary')
        self.assertIn('  [2]    TR 300  7  a problem', said)
        self.assertIn('Cost: nothing', said)
        self.assertTrue(os.path.exists(os.path.join(self.problems, '300-a-problem.yaml')))


    def test_filing_and_the_wiki(self):
        filed = []

        def file_issue(key, project, subject, description):
            filed.append((key, project, subject, description))
            return '300'

        self.review.file_issue = file_issue
        # A problem seen in another run before.
        with open(os.path.join(self.problems, '100-op-reply.yaml'), 'w') as f:
            f.write(text(record(examples=[f'{RUN}/2', 'another-run/1'])))
        self.review.again()
        # Its key: from the environment, a file, or none.
        with mock.patch.dict(os.environ, {'REDMINE_API_KEY': ''}), \
                mock.patch('qa_triage.sources.os.path.expanduser',
                           return_value=os.path.join(self.root, 'no-key')):
            self.assertIn('no API key for the tracker', self.run_command('file 2')[0])
        keys = os.path.join(self.root, 'api_keys.json')
        with open(keys, 'w') as f:
            json.dump({'tracker.ceph.com_token': 'k1', 'other': 'x'}, f)
        self.review.args.settings = {'tracker_key': keys, 'tracker_project': 'rados',
                                     'approve': 'always', 'approve_edits': 'always'}
        # The judge's explanation, for the issue; but not its name.
        self.judge.verdict = dict(self.judge.verdict, explanation='A write bounced on cheap.')
        self.run_command('judge 3')
        with mock.patch.dict(os.environ, {'REDMINE_API_KEY': ''}):
            self.assertIn('it is placed in 100 already', self.run_command('file 1')[0])
            said, _ = self.run_command('file 2 test')
        [(key, project, subject, description)] = filed
        self.assertEqual((key, project, subject),
                         ('k1', 'rados', 'valgrind reported Leak_DefinitelyLost in malloc'))
        self.assertIn('Filed https://tracker.ceph.com/issues/300: valgrind reported', said)
        self.assertIn('ok    tracker 300: a problem (New)', said)
        said, _ = self.run_command('summary')
        self.assertIn('  [2]    TR 300  7  a problem', said)
        _, description = self.review.issue_text(self.review.group('3')[0])
        self.assertIn('A write bounced on the analysis.', description)
        self.assertNotIn('cheap', description)
        # The wiki's section: the problem filed is new, the others known, and the
        # job nobody has placed is for the reviewer.
        said, _ = self.run_command('wiki')
        self.assertEqual(said, f'h3. https://pulpito.ceph.com/{RUN}/\n\n'
                               'New failures, unrelated:\n'
                               "# ['7'] - https://tracker.ceph.com/issues/300 - a problem\n\n"
                               'Failures, unrelated:\n'
                               "# ['1', '2'] - https://tracker.ceph.com/issues/100 - problem 100"
                               '\n\nStill to look into:\n'
                               "# ['3'] - new, unmatched - the judge said: an unstable write\n\n")
        self.session.header = ['QA tracker https://tracker.ceph.com/issues/81636, PRs under '
                               'test: #1', '']
        self.assertTrue(self.run_command('wiki')[0].startswith(
            'h3. https://tracker.ceph.com/issues/81636\n'))
        self.review.write()
        with open(os.path.join(self.root, 'out', 'drafts', 'wiki.textile')) as f:
            self.assertTrue(f.read().startswith('h3. https://tracker.ceph.com/issues/81636'))


class TestScreen(Reviewing):
    """What the full screen shows, without curses."""

    def test_rows_details_and_commands(self):
        model = tui.Model(self.review)
        self.assertEqual(model.rows(), [
            (None, 'Known problems, placed by their records'),
            (1, '  1     TR 100  2 jobs   problem 100'),
            (None, 'For a person to look into'),
            (2, '  2  new       unmatched 1 job    workunit/unexplained: the log shows no TEST_ '
                'function, gtest or traced command that failed; the last lines the workunit '
                'wrote are shown')])
        self.assertIn('1. What went wrong first? workunit: the workunit rados/test.sh failed',
                      model.detail(2))
        self.assertIs(model.detail(2), model.detail(2))
        self.assertEqual(model.command('judge 2')[:3], ['an unstable write', '',
                                                         '3: new, unmatched (cost 0.25)'])
        self.assertEqual(model.details, {})
        self.assertIn('In all: 0.25 coins', model.status())

    def test_the_mouse(self):
        # The terminal's reports, in SGR's form: a press, a drag, letting go, the wheel.
        self.assertEqual(tui.sgr('[<0;5;3M'), tui.Pointer(0, 2, 4, True))
        self.assertEqual(tui.sgr('[<32;9;4M'), tui.Pointer(0, 3, 8, True, motion=True))
        self.assertEqual(tui.sgr('[<0;9;4m'), tui.Pointer(0, 3, 8, False))
        self.assertEqual(tui.sgr('[<65;1;1M').wheel, 1)
        self.assertIsNone(tui.sgr('[A'))
        screen = tui.Screen.__new__(tui.Screen)
        screen.event = screen.anchor = screen.extent = screen.clicked = screen.flash = None
        screen.shadow = {2: ' first line of it', 3: ' second line', 4: ' third'}

        def report(*pointer):
            screen.event = tui.Pointer(*pointer)
            return screen.mouse()

        # A drag selects what the screen shows, copied as it is let go.
        copied = []
        with mock.patch.object(tui, 'copy', lambda text: copied.append(text) or 'pbcopy'):
            self.assertEqual(report(0, 2, 7, True), (None, 2))
            self.assertEqual(report(0, 3, 4, True, True), ('drag', 3))
            self.assertEqual(report(0, 3, 4, False), ('copied', 3))
        # To the column the drag ends on, as a terminal selects.
        self.assertEqual(copied, ['line of it\n seco'])
        self.assertEqual(screen.flash, 'Copied 16 characters, with pbcopy.')
        # A click is pressing and letting go in one place; two at once, a double click.
        report(0, 4, 1, True)
        self.assertEqual(report(0, 4, 1, False), ('click', 4))
        report(0, 4, 1, True)
        self.assertEqual(report(0, 4, 1, False), ('double', 4))
        self.assertEqual(report(0, 4, 1, True, False, -1), ('up', 4))

    def test_what_an_analyst_may_cost(self):
        screen = tui.Screen.__new__(tui.Screen)
        screen.model = tui.Model(self.review)
        self.review.units['fake'] = 'coins'
        self.assertEqual(screen.most(), 'There is no limit on what a lesson costs, in coins: '
                                        'the learn_budget setting sets one.')
        self.review.args.learn_budget = 12
        self.assertEqual(screen.most(), 'Each lesson costs up to 12 coins.')

    def test_what_the_agent_at_work_is_doing_is_shown_with_its_group(self):
        screen = tui.Screen.__new__(tui.Screen)
        screen.model, screen.bg, screen.queued = tui.Model(self.review), None, []
        groups = screen.model.groups()
        first, second = groups[0][1][0].job.id, groups[1][1][0].job.id
        self.assertIsNone(screen.at_work(1))
        working = mock.Mock(is_alive=lambda: True)
        screen.bg = {'out': io.StringIO('Looking at the evidence\n'), 'job': first,
                     'thread': working, 'kind': 'analyst'}
        self.assertEqual(screen.at_work(1), ['Looking at the evidence'])
        self.assertIsNone(screen.at_work(2))
        # Of several groups, the one it has begun last.
        screen.bg['out'] = io.StringIO(f'Group 1 of 2, job {first}:\nan answer\n'
                                       f'Group 2 of 2, job {second}:\n  read_file: x\n')
        self.assertIsNone(screen.at_work(1))
        self.assertEqual(screen.at_work(2), [f'Group 2 of 2, job {second}:', '  read_file: x'])
        # Of groups judged at once, each it works on, with what it said of it.
        self.review.at_work = {first: ['reading'], second: ['thinking']}
        self.assertEqual(screen.busy(), {1, 2})
        self.assertEqual((screen.at_work(1), screen.at_work(2)), (['reading'], ['thinking']))
        self.assertEqual(screen.progress(), (0, 2))
        self.review.at_work = {second: ['thinking']}
        self.assertEqual((screen.busy(), screen.progress()), ({2}, (1, 2)))
        self.review.at_work = None
        # A group that waits for a turn of the AI.
        self.review.waiting_for_ai = {second}
        self.assertEqual(screen.waiting_for_ai(), {2})
        self.assertEqual(screen.waits(2), '… This group waits for a turn of the analyst, '
                                          'which the groups before it have now.')
        self.review.waiting_for_ai = set()
        working.is_alive = lambda: False
        self.assertIsNone(screen.at_work(2))

    def test_what_the_judge_writes_is_shown_as_it_writes_it(self):
        screen = tui.Screen.__new__(tui.Screen)
        screen.model = tui.Model(self.review)
        review, judge, seen = self.review, self.judge, []

        class Writing(FakeJudge):
            streams = True

            def __call__(self, workdir, prompts):
                self.written += ['{\n  "verdict": ', '"new",\n  "explanation": "the OSD']
                job_id = next(iter(review.writing))
                screen.bg = {'kind': 'judge', 'job': job_id, 'out': io.StringIO(),
                             'thread': mock.Mock(is_alive=lambda: True)}
                seen.append((screen.written(1), screen.working_on(1, True)))
                return judge(workdir, prompts)

        review.judge = Writing({}, 'cheap')
        self.run_command('judge 1')
        job_id = screen.model.groups()[0][1][0].job.id
        self.assertEqual(seen, [(['{', '  "verdict": "new",', '  "explanation": "the OSD'],
                                 f' The judge (cheap) on group 1, job {job_id}: what it '
                                 'writes, as it writes it ')])
        # Once it is done, its answer is with its group's.
        self.assertEqual(review.writing, {})
        self.assertIsNone(screen.written(1))
        # One that writes nothing until it is done says so.
        review.judge = FakeJudge({}, 'quiet')
        self.assertIn('it gives its answer all at once', screen.working_on(1))

    def test_the_text_selected_is_kept_and_extended(self):
        screen = tui.Screen.__new__(tui.Screen)
        screen.stdscr = mock.Mock(getmaxyx=lambda: (10, 20))
        screen.shadow = {2: 'the first line', 3: 'and the second'}
        screen.kept, screen.flash = ((2, 4), (2, 8)), None
        with mock.patch.object(tui, 'copy', return_value='pbcopy') as copied:
            # Shift and an arrow move its far end, and copy it again.
            self.assertTrue(screen.selecting(tui.curses.KEY_SRIGHT))
            self.assertEqual(screen.kept, ((2, 4), (2, 9)))
            copied.assert_called_with('first')
            self.assertTrue(screen.selecting(tui.curses.KEY_SF))
            self.assertEqual(screen.kept, ((2, 4), (3, 9)))
            copied.assert_called_with('first line\nand the se')
            self.assertEqual(screen.flash, 'Copied 21 characters, with pbcopy.')
        # The mouse's reports and none at all leave it; another key does not.
        for key in (-1, tui.curses.KEY_MOUSE):
            self.assertFalse(screen.selecting(key))
            self.assertIsNotNone(screen.kept)
        self.assertFalse(screen.selecting(ord('j')))
        self.assertIsNone(screen.kept)
        # With nothing selected, shift and up or down scroll the pane as before.
        self.assertFalse(screen.selecting(tui.curses.KEY_SR))

    def test_settings_saved_while_it_is_open(self):
        review = self.review
        other = FakeJudge({'verdict': 'new', 'summary': 'x', 'cost': 0.1}, 'other')
        review.agents = lambda: (other, self.analyst)
        said = review.take({'judge': 'other', 'editor': 'less -N +{line} {file}',
                            'cited_lines': 3, 'learn_budget': 12, 'archive': 'https',
                            'mouse': False})
        self.assertIs(review.judge, other)
        self.assertEqual((review.args.judge, review.args.learn_budget), ('other', 12))
        self.assertEqual(review.args.settings['editor'], 'less -N +{line} {file}')
        self.assertEqual(review.session.triage.cited_lines, 3)
        self.assertIs(review.args.settings['mouse'], False)
        # The run was triaged with the job files it had.
        self.assertNotIn('archive', review.args.settings)
        self.assertEqual(said, 'Taken up now: Judge, Editor, Lines shown, Lesson budget, '
                               'Mouse. From the next review: Job files. The judge is other, '
                               'the analyst fake.')
        # A setting back to its default.
        review.take({'editor': None})
        self.assertIsNone(review.args.settings['editor'])
        self.assertEqual(review.take({}), 'No settings were changed.')

    def test_wrap(self):
        self.assertEqual(tui.wrap(['  a b c d'], 5), ['  a b', '    c', '    d'])
        self.assertEqual(tui.wrap([''], 5), [''])

    def test_what_is_typed_in_a_box_and_yes_or_no(self):
        import curses
        screen, pending = tui.Screen.__new__(tui.Screen), []

        def get_wch():
            if not pending:
                raise curses.error('no input')
            return pending.pop(0)

        window, came = mock.Mock(get_wch=get_wch), []
        self.assertEqual(screen.typed('a', '', came, window), ('a', False))
        # The lines of a paste are one answer.
        pending.append('b')
        self.assertEqual(screen.typed('\n', 'a', came, window), ('a ', False))
        self.assertEqual(screen.typed(came.pop(), 'a ', came, window), ('a b', False))
        self.assertEqual(screen.typed('\x7f', 'a b', came, window), ('a ', False))
        self.assertEqual(screen.typed('\r', 'a b', came, window), ('a b', True))
        # Escape alone cancels; the escape sequence that begins a paste does not.
        self.assertEqual(screen.typed('\x1b', 'a', came, window), ('a', None))
        pending.extend('[200~')
        self.assertEqual(screen.typed('\x1b', 'a', came, window), ('a', False))
        self.assertEqual(pending, [])
        # Yes or no: no unless yes is chosen; y and n choose them, for enter.
        asked = []
        screen.choose = lambda question, options, chosen, **kw: asked.append(
            (question, options, chosen, kw['keys'])) or answers.pop(0)
        answers = [0, 1, None]
        with mock.patch.object(tui.curses, 'flushinp'):
            self.assertEqual([screen.confirm('Stop it?') for _ in range(3)], [True, False, False])
        self.assertEqual(asked[0][:3], ('Stop it?', ['Yes', 'No'], 1))
        self.assertEqual((asked[0][3][ord('y')], asked[0][3][ord('n')]), (0, 1))



class TestOpen(unittest.TestCase):
    """qa_triage open: a file of a job's archive at a line, in the person's editor."""

    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache)
        self.files = FakeFiles({'teuthology.log': [f'line {n}' for n in range(1, 101)],
                                'remote/h1/log/ceph-osd.0.log': ['an osd line']})

    def open(self, where='teuthology.log', files=None, environ=None, **settings):
        from qa_triage import cli
        ran, err = [], io.StringIO()
        with mock.patch.object(cli, 'files_of', lambda args, job: files or self.files), \
                mock.patch.object(cli.subprocess, 'run',
                                  lambda argv: ran.append(argv) or SimpleNamespace(returncode=0)), \
                mock.patch.dict(os.environ, dict({'VISUAL': '', 'EDITOR': ''},
                                                 **(environ or {}))), \
                mock.patch('sys.stderr', err):
            code = cli.cmd_open(SimpleNamespace(job=f'{RUN}/1', where=where, cache=self.cache,
                                                settings=settings), [])
        return code, ran, err.getvalue()

    def test_here_in_vim_by_default_on_a_copy(self):
        code, ran, _ = self.open('teuthology.log:50')
        copy = os.path.join(self.cache, 'opened', RUN, '1', 'teuthology.log')
        self.assertEqual((code, ran), (0, [['vim', '+50', copy]]))
        with open(copy) as f:
            self.assertEqual(f.read().splitlines()[49], 'line 50')

    def test_the_editor_the_settings_or_the_environment_say(self):
        copy = os.path.join(self.cache, 'opened', RUN, '1', 'teuthology.log')
        self.assertEqual(self.open('teuthology.log:7', environ={'EDITOR': 'emacs -nw'})[1],
                         [['emacs', '-nw', '+7', copy]])
        self.assertEqual(self.open('teuthology.log:7', editor="lnav -c ':goto {line}' {file}")[1],
                         [['lnav', '-c', ':goto 7', copy]])
        self.assertEqual(self.open()[1], [['vim', '+1', copy]])

    def test_on_the_archive_host_where_the_file_is(self):
        files = SshJobFiles(job(), 'trial200', '/a', ssh=['ssh', '-o', 'BatchMode=yes'])
        # The archive has gzipped the log since.
        files._files = {'teuthology.log.gz': 1000}
        code, ran, _ = self.open('teuthology.log:50', files=files)
        self.assertEqual(ran, [['ssh', '-o', 'BatchMode=yes', '-t', 'trial200',
                                f'${{VISUAL:-${{EDITOR:-vim}}}} +50 /a/{RUN}/1/teuthology.log.gz']])
        self.assertEqual(self.open('teuthology.log:50', files=files,
                                   editor='lnav {file}')[1][0][-1],
                         f'lnav /a/{RUN}/1/teuthology.log.gz')

    def test_a_file_not_in_the_archive(self):
        code, ran, err = self.open('remote/h2/log/ceph-osd.0.log:3')
        self.assertEqual((code, ran), (1, []))
        self.assertIn('not in the archive', err)


if __name__ == '__main__':
    unittest.main()


class TestPlain(unittest.TestCase):
    def test_failed_checks_in_plain_words(self):
        self.assertEqual(approve.plain('record 7 loses examples: r/1'), (
            "It drops r/1 from record 7's examples, the jobs the record must place; another "
            'lesson may have added them since this change was made.'))
        self.assertEqual(approve.plain(
            'r/2 (settled by qa_triage (bob+files)): new (explain -> prefilter -> explained); '
            'should be 7, or a record that lists it under also'),
            'With this change, r/2, which qa_triage (bob+files) settled as 7, would be new, '
            'placed in no record, not 7.')
        self.assertEqual(approve.plain(
            'r/1 (doubted): duplicate 100 (explain -> confirm), matched; should be no record'),
            'With this change, r/1, the job this change is for, would be placed in 100, not '
            'new.')
        self.assertEqual(approve.plain('0 of 3 quoted lines found'), '0 of 3 quoted lines found')


class TestMergeRecord(unittest.TestCase):
    BASE = 'id: "1"\ntitle: t\nexamples:\n  - a/1\nnotes: |\n  one\n  two\n'

    def test_each_sides_jobs_are_kept(self):
        now = self.BASE.replace('  - a/1\n', '  - a/1\n  - a/2\n')
        theirs = self.BASE.replace('  - a/1\n', '  - a/3\n') + 'confirm:\n- {file: x, pattern: y}\n'
        text, took = approve.merge_record(self.BASE, now, theirs)
        data = yaml.safe_load(text)
        # a/1, which the change dropped, stays dropped; a/2, which others added, is added.
        self.assertEqual(data['examples'], ['a/3', 'a/2'])
        self.assertEqual(data['confirm'], [{'file': 'x', 'pattern': 'y'}])
        self.assertEqual(data['notes'], 'one\ntwo\n')
        self.assertIn('notes: |', text)
        self.assertEqual(took, ['examples'])

    def test_what_only_others_changed_is_theirs(self):
        now = self.BASE.replace('title: t', 'title: better')
        theirs = self.BASE + 'also: ["2"]\n'
        data = yaml.safe_load(approve.merge_record(self.BASE, now, theirs)[0])
        self.assertEqual((data['title'], data['also']), ('better', ['2']))

    def test_unchanged_is_as_the_change_wrote_it(self):
        theirs = self.BASE + 'also: ["2"]\n'
        self.assertEqual(approve.merge_record(self.BASE, self.BASE, theirs), (theirs, []))

    def test_both_changed_clash(self):
        with self.assertRaises(approve.Clash) as e:
            approve.merge_record(self.BASE, self.BASE.replace('title: t', 'title: a'),
                                 self.BASE.replace('title: t', 'title: b'))
        self.assertEqual(e.exception.clashes,
                         ['another lesson changed it meanwhile too, with a different title'])
        with self.assertRaises(approve.Clash) as e:
            approve.merge_record(None, self.BASE, self.BASE.replace('title: t', 'title: b'))
        self.assertEqual(e.exception.clashes,
                         ['another lesson wrote it meanwhile too, with a different title'])
        with self.assertRaises(approve.Clash):
            approve.merge_record(self.BASE, None, self.BASE)
