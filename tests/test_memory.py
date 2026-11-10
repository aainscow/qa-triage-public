import os
import tempfile
import unittest
from collections import Counter
from unittest import mock

import yaml

from fakes import FakeFiles, job, problems, record

from qa_triage import graph, memory, report
from qa_triage.graph import Triage

REASON = 'Command failed (workunit test post-file.sh) on trial1 with status 1'


def entry(jid, reason=REASON, tree=None, status='fail'):
    return {'job': jid, 'reason': reason, 'status': status, 'tree': tree}


class TestMemory(unittest.TestCase):
    def test_signatures(self):
        self.assertEqual(memory.signature_of(entry('1')), memory.signature_of(
            entry('2', reason=REASON.replace('trial1', 'trial9'))))
        tree = {'path': 'crash/assert', 'keys': ['osd/ECBackend.cc:1250 at 0x7f00ab']}
        self.assertEqual(memory.signature_of(entry('1', tree=tree)),
                         'crash/assert | osd/ECBackend.cc:# at <hex>')
        self.assertEqual(memory.signature_of(entry('1', reason='', status='dead')),
                         '<dead with no reason>')

    def test_kept_by_an_older_masking(self):
        # Before clients named after their host were masked: one memory now.
        directory = tempfile.mkdtemp()
        for run, name in (('run1', 'x'), ('run2', 'y')):
            with open(os.path.join(directory, f'{run}.yaml'), 'w') as f:
                yaml.safe_dump({'run': run, 'normalisation': memory.NORMALISATION,
                                'signatures': {f'cluster-log/message | client <host>:{name} '
                                               '(#)': {'73278': 1}}}, f)
        tree = {'path': 'cluster-log/message', 'keys': ['client trial174:z (4828)']}
        signature = memory.signature_of(entry('1', tree=tree))
        self.assertEqual(signature, 'cluster-log/message | client <host>:<name> (#)')
        self.assertEqual(memory.recall(memory.load(directory), signature), ('73278', 2))

    def test_build_load_and_recall(self):
        triaged = [('run1', {'1': '74517', '2': '201'}, [entry('1'), entry('2'), entry('3')]),
                   ('run2', {'4': '74517'}, [entry('4')]),
                   ('run3', {'5': '74517'}, [entry('5')])]
        found = memory.build(triaged, canonical=lambda n: {'201': '200'}.get(n, n))
        signature = memory.signature_of(entry('1'))
        self.assertEqual(found['run1'], {signature: {'74517': 1, '200': 1}})
        directory = tempfile.mkdtemp()
        memory.write(directory, found)
        mem = memory.load(directory)
        self.assertEqual(mem[signature], Counter({'74517': 3, '200': 1}))
        # 3 of 4 is too few to agree; without run1's other answer, 2 agree, but a
        # signature of the failure reason alone needs 3.
        self.assertIsNone(memory.recall(mem, signature, specific=False))
        self.assertIsNone(memory.recall(memory.load(directory, leave_out=('run1',)),
                                        signature, specific=False))
        self.assertEqual(memory.recall(memory.load(directory, leave_out=('run1',)),
                                       signature), ('74517', 2))

    def test_triage_takes_the_memorys_answer(self):
        # Record 100 does not match this job's log, but people placed jobs that
        # failed the same way in 100: no judge is needed.
        t = Triage(problems(record(pattern='not in the log')))
        r = t.run(job(), FakeFiles({'teuthology.log': ['something else']}))
        self.assertEqual((r.verdict, r.path[-1]), ('new', 'agent'))
        t.memory = {r.signature: Counter({'100': 3})}
        r = t.run(job(), FakeFiles({'teuthology.log': ['something else']}))
        self.assertEqual((r.verdict, r.problem.id, r.sure, r.path[-2:]),
                         ('probable-duplicate', '100', True, ['memory', 'fix-check']))
        self.assertIn('people placed 3 jobs that failed this way in 100', r.notes[-1])
        self.assertIn('the memory places it there', report.decisions(r)[-1])
        # An issue no record names yet is still the answer, with its title.
        t.memory = memory.Memory({r.signature: Counter({'999': 3})})
        t.memory.titles['999'] = 'rados/test.sh fails'
        r = t.run(job(), FakeFiles({'teuthology.log': ['something else']}))
        self.assertEqual((r.verdict, r.problem.id, r.problem.title, r.sure, r.path[-1]),
                         ('probable-duplicate', '999', 'rados/test.sh fails', True, 'memory'))
        self.assertEqual(r.problem.tracker, 'https://tracker.ceph.com/issues/999')
        self.assertIn('999, which has no record yet', r.notes[-1])
        self.assertIn(f'**probable-duplicate**, {report.REMEMBERED}: '
                      '[TR 999](https://tracker.ceph.com/issues/999) (rados/test.sh fails)',
                      report._job_section('run', r))
        self.assertEqual(report.certainty(r), 'matched by the memory')

    def test_the_memorys_issue_by_the_record_that_names_it_in_also(self):
        # People placed jobs in 999, which record 100 lists under also, as the same
        # problem: 100 is its record, for a lesson to mend, not one to make for 999.
        t = Triage(problems(record(pattern='not in the log', also=['999'])))
        files = FakeFiles({'teuthology.log': ['something else']})
        t.memory = memory.Memory({t.run(job(), files).signature: Counter({'999': 3})})
        r = t.run(job(), files)
        self.assertEqual((r.verdict, r.problem.id), ('probable-duplicate', '100'))
        self.assertTrue(any('in 999, which record 100 lists under also' in n for n in r.notes))

    def test_the_memorys_record_is_checked_for_its_fix(self):
        p = record(pattern='not in the log', fixes=[
            {'pr': 'https://github.com/ceph/ceph/pull/1', 'fixed_in': 'v21.3.0-4000-g05a7656731'}])
        t = Triage(problems(p))
        files = FakeFiles({'teuthology.log': ['something else'],
                           'orig.config.yaml': ['branch: wip-test-main']})
        t.memory = {t.run(job(), files).signature: Counter({'100': 3})}
        with mock.patch.object(graph, 'fix_in_build', return_value=True):
            r = t.run(job(), files)
        self.assertEqual((r.verdict, r.problem.id), ('seen-after-fix', '100'))

    def test_titles(self):
        triaged = [('run1', {'1': '74517', '2': '74518'}, [entry('1'), entry('2', tree={
            'path': 'crash/assert', 'keys': ['x']})])]
        directory = tempfile.mkdtemp()
        memory.write(directory, memory.build(triaged), {'74517': 'test fails', '9': 'other'})
        with open(os.path.join(directory, 'run1.yaml')) as f:
            self.assertEqual(yaml.safe_load(f)['titles'], {'74517': 'test fails'})
        self.assertEqual(memory.load(directory).titles, {'74517': 'test fails'})
        # A run whose answers are gone has no memory.
        memory.write(directory, memory.build([('run1', {}, [entry('1')])]))
        self.assertEqual(os.listdir(directory), [])


if __name__ == '__main__':
    unittest.main()
