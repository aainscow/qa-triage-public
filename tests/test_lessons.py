"""What the tool took from how others triage failures: held-out runs, labels that
wait for a person, how far a record's checks reach, what a passing job logs too,
what decided each job, edits apart from additions, who learned what, and a
memory that knows only the past."""

import os
import tempfile
import unittest

import yaml

from fakes import RUN, FakeFiles, job, problems, record
from test_learn import ARCHIVES, BASE, ISSUE, LEAK, OP_REPLY, FakeAnalyst, answer, text

from qa_triage import approve, db, learn, memory, pack, report, score
from qa_triage.graph import Triage
from qa_triage.learn import Doubt
from qa_triage.sources import Job

OLD = 'someone-2025-10-01_10:00:00-rados-wip-old-distro-default-trial'
RECENT = 'someone-2026-09-25_10:00:00-rados-wip-new-distro-default-trial'
NOW = 'someone-2026-10-01_10:00:00-rados-wip-now-distro-default-trial'


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='qa_triage-memory-')
        memory.write(self.dir, {OLD: {'sig': {'1': 4}}, RECENT: {'sig': {'2': 20}}})

    def test_older_answers_count_for_less(self):
        # 20 of 24 jobs agree, which is not enough; but the 4 are a year old.
        self.assertIsNone(memory.recall(memory.load(self.dir), 'sig'))
        self.assertEqual(memory.recall(memory.load(self.dir, before=NOW), 'sig'), ('2', 20))

    def test_a_run_knows_only_the_runs_before_it(self):
        self.assertEqual(memory.load(self.dir, before=RECENT).jobs, {'sig': {'1': 4}})
        self.assertEqual(memory.load(self.dir, before=OLD), {})
        self.assertEqual(memory.run_date(NOW).isoformat(), '2026-10-01')

    def test_a_memory_kept_another_way_is_left_out(self):
        path = os.path.join(self.dir, OLD.replace(':', '_') + '.yaml')
        with open(path) as f:
            data = yaml.safe_load(f)
        self.assertEqual(data['normalisation'], memory.NORMALISATION)
        with open(path, 'w') as f:
            yaml.safe_dump(dict(data, normalisation=memory.NORMALISATION + 1), f)
        found = memory.load(self.dir)
        self.assertEqual((found.stale, list(found)), ([OLD], ['sig']))

    def test_the_normalisation_is_as_it_was(self):
        # If this changes, raise memory.NORMALISATION: the memory is kept by it.
        entry = {'reason': 'Command failed on trial046 with status 1: 3.1fs0 at '
                           '2026-10-12T17:10:00.000 0x7f26 deadbeef1'}
        self.assertEqual(memory.signature_of(entry),
                         'Command failed on <host> with status #: <pg> at <time> <hex> <sha>')


class TestScore(unittest.TestCase):
    def test_suites_stages_overlaps_and_precision(self):
        results = [
            {'job': '1', 'problem': '100', 'sure': True, 'path': ['confirm']},
            {'job': '2', 'problem': '100', 'sure': True, 'path': ['confirm']},
            {'job': '3', 'problem': None, 'sure': False, 'verdict': 'ambiguous',
             'notes': ['matched 100, 200']},
            {'job': '4', 'problem': '300', 'sure': True, 'path': ['memory']}]
        triaged = [(RUN, {'1': '100', '2': '200', '3': '100', '4': '300'}, results)]
        found = score.score(triaged, [db.parse(record())])
        self.assertEqual(dict(found['stages']), {'records': 2, 'unsure': 1, 'memory': 1})
        self.assertEqual(dict(found['overlaps']), {'100, 200': 1})
        self.assertEqual(score.precision(found), {'100': {'placed': 2, 'right': 1},
                                                  '300': {'placed': 1, 'right': 1}})
        self.assertEqual(score.suite_of(RUN), 'rados')
        self.assertEqual(score.suite_of('a-2026-03-04_06:04:17-orch:cephadm:smb-wip-x'),
                         'orch')
        lines = score.lines(found)
        self.assertIn('Decided by the records 2, the memory 1, the tree 0, the judge 0; '
                      'unmatched 1. Without AI: 3 of 4 (75%).', lines)
        self.assertIn('Placed as people did, of the jobs each decided: the records 1 of 2 '
                      '(50%), the memory 1 of 1 (100%).', lines)
        self.assertIn('  1 jobs: 100, 200', lines)

    def test_held_out_runs(self):
        directory = tempfile.mkdtemp(prefix='qa_triage-holdout-')
        self.assertEqual(score.held_out(directory), {})
        with open(os.path.join(directory, 'index.yaml'), 'w') as f:
            yaml.safe_dump({RUN: 'a.yaml'}, f)
        with open(os.path.join(directory, 'a.yaml'), 'w') as f:
            yaml.safe_dump({7: 100}, f)
        self.assertEqual(score.held_out(directory), {RUN: {'7': '100'}})


class TestReport(unittest.TestCase):
    def tearDown(self):
        report.PRECISION.clear()

    def test_what_decided_it_and_its_track_record(self):
        p = db.parse(record())
        triage = Triage([p])
        r = triage.run(job('2'), FakeFiles({'teuthology.log': [OP_REPLY]}))
        self.assertEqual((report.stage(r), report.certainty(r)), ('records', 'matched by its record'))
        self.assertEqual(report.headline(r), 'TR 100: problem 100 (duplicate, matched by its record)')
        report.PRECISION['100'] = {'placed': 38, 'right': 24}
        self.assertEqual(report.headline(r), 'TR 100: problem 100 (duplicate, matched by its record; '
                                             'right 24 of the 38 times people answered)')
        self.assertIn('placed by its record, right 24 of the 38 times people answered',
                      report.wiki(RUN, [r]))
        self.assertEqual(report.stage_of({'sure': True, 'judge': {}, 'path': ['memory'],
                                          'problem': '1'}), 'memory')


class TestLabels(unittest.TestCase):
    def test_a_job_settled_for_a_person_waits_for_one(self):
        directory = tempfile.mkdtemp(prefix='qa_triage-labels-')
        learn.save_label(directory, job('1'), '100', 'an assistant, for alex', 'why', 's',
                         confirmed=False)
        learn.save_label(directory, job('2'), '200', 'alex', 'why', 's')
        self.assertEqual(learn.settled_by_people(directory),
                         {f'{RUN}/1': '100', f'{RUN}/2': '200'})
        self.assertEqual(learn.settled_by_people(directory, unconfirmed=False),
                         {f'{RUN}/2': '200'})
        [(found, problem, where)] = learn.unconfirmed_labels(directory)
        self.assertEqual((found.id, problem), ('1', '100'))
        self.assertIn('not yet confirmed by a person', where)
        # Settled again, by a person, it is confirmed.
        learn.save_label(directory, job('1'), '100', 'alex', 'why', 's')
        self.assertEqual(learn.unconfirmed_labels(directory), [])


class Lessons(unittest.TestCase):
    """A doubt of job 1, which record 100 places, as test_learn's."""

    def setUp(self):
        learn._FINDINGS.clear()
        root = self.root = tempfile.mkdtemp(prefix='qa_triage-lessons-')
        self.problems, self.labels = os.path.join(root, 'problems'), os.path.join(root, 'labels')
        os.makedirs(self.problems)
        with open(os.path.join(self.problems, '100-op-reply.yaml'), 'w') as f:
            f.write(text(BASE))
        self.files = {jid: FakeFiles(files) for jid, files in ARCHIVES.items()}
        self.jobs = {jid: job(jid) for jid in ARCHIVES}
        self.said = []

    def doubt(self, analyst, **kw):
        return Doubt(self.jobs['1'], self.files['1'], self.problems, self.labels, analyst,
                     os.path.join(self.root, 'work'), files_for=lambda j: self.files[j.id],
                     jobs_for=lambda run: self.jobs, tracker=lambda n: ISSUE, by='tester',
                     why='it leaked', say=self.said.append, **kw)

    def record_text(self):
        with open(os.path.join(self.problems, '100-op-reply.yaml')) as f:
            return f.read()


class TestEdits(Lessons):
    def test_additions_and_edits(self):
        d = self.doubt(FakeAnalyst([]))
        grown = text(dict(BASE, examples=BASE['examples'] + [f'{RUN}/1'], also=['99']))
        changed = text(dict(BASE, exclude=[{'file': 'teuthology.log', 'pattern': 'x'}]))
        self.assertEqual(d.edited({'problems/100-op-reply.yaml': grown,
                                   'problems/200-new.yaml': 'id: 200\n'}), [])
        self.assertEqual(d.edited({'problems/100-op-reply.yaml': changed}),
                         ['problems/100-op-reply.yaml'])

    def test_who_learned_it(self):
        once = learn.stamped('problems/1.yaml', 'id: 1\nlearned:\n- "before"\n', 'now')
        self.assertEqual(yaml.safe_load(once)['learned'], ['before', 'now'])
        twice = learn.stamped('problems/1.yaml', once, 'later')
        self.assertEqual(yaml.safe_load(twice), {'id': 1, 'learned': ['before', 'now', 'later']})
        self.assertTrue(learn.stamped('nodes/x.py', 'X = 1\n', 'now').endswith(
            '\n# learned: now\n'))
        # What the file said before stays, if the analyst left it out.
        self.assertEqual(yaml.safe_load(learn.stamped('problems/1.yaml', 'id: 1\n', 'later',
                                                      before=once))['learned'],
                         ['before', 'now', 'later'])
        self.assertEqual(learn.stamped('nodes/x.py', 'X = 2\n', 'later',
                                       before='X = 1\n# learned: now\n'),
                         'X = 2\n# learned: now\n# learned: later\n')

    def test_an_edit_waits_where_additions_do_not(self):
        excluding = dict(BASE, exclude=[{'file': 'teuthology.log',
                                         'pattern': 'Leak_DefinitelyLost'}])
        cache = os.path.join(self.root, 'cache')
        d = self.doubt(FakeAnalyst([answer(None, [excluding])]), label_none=False,
                       **approve.doubting({'approve': 'always'}, cache, interactive=False))
        self.assertFalse(d.run())
        self.assertTrue(d.held.startswith(approve.edits(cache)))
        self.assertNotIn('exclude', self.record_text())
        # Unless edits are kept too.
        d = self.doubt(FakeAnalyst([answer(None, [excluding])]), label_none=False,
                       **approve.doubting({'approve': 'always', 'approve_edits': 'always'},
                                          cache, interactive=False))
        self.assertTrue(d.run())
        self.assertIn('exclude', self.record_text())
        self.assertIn('learned:\n- "', self.record_text())


class TestHoldout(Lessons):
    def test_the_held_out_jobs_are_counted_not_shown(self):
        self.files['7'] = FakeFiles({'teuthology.log': [OP_REPLY, 'held out']})
        self.jobs['7'] = job('7')
        excluding = dict(BASE, exclude=[{'file': 'teuthology.log', 'pattern': 'held out'}])
        d = self.doubt(FakeAnalyst([]), expected='100', holdout=[(self.jobs['7'], '100')])
        d.setup()
        ok, report_, _ = d.check(answer('100', [excluding], quotes=(('teuthology.log',
                                                                     OP_REPLY),)))
        self.assertFalse(ok)
        self.assertIn('FAIL  the held-out jobs it could change, which you are not shown: '
                      '1 of 1 placed rightly before, 0 after', report_)
        self.assertFalse([line for line in report_ if f'{RUN}/7' in line])


class TestBreadth(Lessons):
    def test_checks_that_jobs_that_passed_match_too(self):
        for n in range(20, 26):
            jid = str(n)
            self.jobs[jid] = job(jid, status='pass', reason='')
            self.files[jid] = FakeFiles({'teuthology.log': ['a line every job logs' if n < 23
                                                            else 'all well']})
        d = self.doubt(FakeAnalyst([]))
        found = []

        def result(good, line):
            found.append(('ok    ' if good else 'FAIL  ') + line)

        broad = db.parse(record(id='300', pattern='every job logs', examples=[f'{RUN}/1']))
        d._breadth(broad, None, result, found)
        self.assertEqual(found[0], 'FAIL  record 300: its confirm checks all found their '
                                   'lines in 3 of 6 jobs of its runs that passed: they find '
                                   'what jobs that did not fail log too, not the failure; '
                                   'check for the line that shows the cause, such as the '
                                   'first error')
        found.clear()
        d._breadth(db.parse(record(id='300', pattern='Leak_Definitely')), None, result, found)
        self.assertEqual(found, ['ok    record 300: its confirm checks all found their lines '
                                 'in 0 of 6 jobs of its runs that passed'])
        # A record whose checks are as they were is not tried again.
        found.clear()
        d._breadth(broad, broad, result, found)
        self.assertEqual(found, [])


class TestPassing(unittest.TestCase):
    def test_what_a_job_that_passed_logs_too_is_left_out(self):
        self.assertEqual(pack.key('2026-10-12T17:10:00.000 INFO:teuthology.run:Running '
                                  'task ceph 1234'), 'INFO:teuthology.run:Running task ceph')
        self.assertEqual(pack.key('12345 short'), '')
        workdir = tempfile.mkdtemp(prefix='qa_triage-passing-')
        with open(os.path.join(workdir, 'excerpts.txt'), 'w') as f:
            f.write('# The last error-looking lines of teuthology.log (line: text)\n'
                    '10: 2026-10-12T17:10:00.000 INFO:tasks.ceph:Waiting for all PGs to be '
                    'active and clean, 1\n'
                    '20: 2026-10-12T17:11:00.000 ERROR:tasks.ceph:the OSD crashed for good\n')
        passing = (job('9', status='pass'), FakeFiles({'teuthology.log': [
            '2026-10-11T01:00:00.000 INFO:tasks.ceph:Waiting for all PGs to be active and '
            'clean, 7']}))
        pack.leave_out(workdir, passing)
        pack.leave_out(workdir, passing)
        with open(os.path.join(workdir, 'excerpts.txt')) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], f'# Left out: 1 lines that {RUN}/9, a job of the same run '
                                   'that passed, logs too: they are not what failed')
        self.assertEqual(lines[2:], ['20: 2026-10-12T17:11:00.000 ERROR:tasks.ceph:the OSD '
                                     'crashed for good'])
        self.assertEqual(pack.passed_too(passing, ['INFO:tasks.ceph:Waiting for all PGs to '
                                                   'be active and clean, 3', 'other']), {0})
        self.assertEqual(pack.alike(job('1', description='rados/a b'),
                                    [job('2', description='x'), job('3', description='b')]),
                         [job('3', description='b'), job('2', description='x')])

    def test_the_logs_a_judge_needs(self):
        files = FakeFiles({'teuthology.log': ['x'],
                           'remote/h1/log/ceph-osd.3.log': ['ok', 'an Error here', 'last']})
        text_ = pack.needed(files, ['ceph-osd.3.log, around the error'])
        self.assertEqual(text_.splitlines(), [
            '# remote/h1/log/ceph-osd.3.log: its last error-looking lines (line: text)',
            '2: an Error here',
            '# remote/h1/log/ceph-osd.3.log: its last lines (line: text)',
            '1: ok', '2: an Error here', '3: last'])
        self.assertEqual(pack.needed(files, ['nothing named']), '')


class Judge:
    name = 'judge'

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, workdir, prompts):
        self.calls.append(prompts)
        return dict(self.answers.pop(0), judge=self.name, cost=0.25)


class TestNeeds(unittest.TestCase):
    def test_a_judge_that_cannot_decide_is_given_what_it_needs_once(self):
        files = FakeFiles(ARCHIVES['3'])
        unsure = {'verdict': 'unsure', 'problem': None, 'explanation': 'e', 'walkthrough': [],
                  'summary': 's', 'needs': ['remote/*/log/ceph-osd.0.log']}
        judge = Judge([unsure, dict(unsure, needs=[])])
        triage = Triage(problems(BASE), judge=judge,
                        workdir=tempfile.mkdtemp(prefix='qa_triage-needs-'))
        r = triage.run(job('3', reason='something else'), files)
        self.assertEqual(len(judge.calls), 2)
        self.assertIn('needed.txt', judge.calls[1]['inline'])
        self.assertIn('unstable write on shard', judge.calls[1]['inline'])
        self.assertEqual(r.agent['cost'], 0.5)
        self.assertTrue(any('asked again' in note for note in r.notes))


if __name__ == '__main__':
    unittest.main()
