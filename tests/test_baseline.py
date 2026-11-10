import datetime
import tempfile
import unittest

from qa_triage import baseline
from qa_triage.graph import Result
from qa_triage.sources import Job

RUN = 'skanta-2026-10-12_16:32:46-rados-wip-bharath7-testing-20261012.100201-main-distro-default-trial'
MAIN = 'ksirivad-2026-10-23_13:15:52-rados-main-distro-default-trial'
OLD = 'jenkins-build-2026-08-01_06:05:39-rados-main-distro-default-trial'
SMITHI = 'teuthology-2026-10-11_20:00:01-rados-main-distro-default-smithi'
HINFO = ('rados/singleton/{all/ec-inconsistent-hinfo mon_election/classic msgr-failures/many '
         'supported-random-distro$/{centos_9.stream}}')
LAGGY = '"2026-10-20T04:03:45.851439+0000 osd.3 (osd.3) 12 : cluster [WRN] PG 2.0s0 marked LAGGY"'


class TestBaseline(unittest.TestCase):
    def test_names(self):
        self.assertEqual(baseline.parts(RUN), (datetime.date(2026, 10, 12), 'rados',
                                               'wip-bharath7-testing-20261012.100201-main',
                                               'trial'))
        self.assertIsNone(baseline.parts('not-a-run'))
        for branch, base in (('wip-bharath7-testing-20261012.100201-main', 'main'),
                             ('wip-yuri12-testing-20261020.153859-squid', 'squid'),
                             ('wip-bharath5-testing-2026-06-27-0520', 'main'),
                             ('tentacle', 'tentacle')):
            self.assertEqual(baseline.base_of(branch), base)
        # The random facets differ between runs; the test is the same.
        self.assertEqual(baseline.test_of(HINFO), 'rados/singleton/ all/ec-inconsistent-hinfo')
        self.assertEqual(baseline.test_of(HINFO.replace('classic', 'connectivity')),
                         baseline.test_of(HINFO))
        self.assertEqual(baseline.test_of('rados/basic'), 'rados/basic')

    def test_the_base_branchs_failures(self):
        pages = []

        def list_runs(page):
            pages.append(page)
            return [MAIN, RUN, OLD, SMITHI]

        near = baseline.runs(RUN, list_runs)
        self.assertEqual(pages, ['https://pulpito.ceph.com/?branch=main&suite=rados'])
        self.assertEqual(near, [MAIN])
        listed = []

        def list_jobs(run):
            listed.append(run)
            return [Job(run, '1', 'fail', HINFO.replace('classic', 'connectivity'),
                        LAGGY.replace('osd.3', 'osd.5')),
                    Job(run, '2', 'pass', 'rados/basic', ''),
                    Job(run, '3', 'dead', 'rados/thrash/{workloads/radosbench}', 'timeout')]

        cache = tempfile.mkdtemp()
        for _ in range(2):
            failed = baseline.failures(near, list_jobs, cache)
        self.assertEqual(listed, [MAIN])
        self.assertEqual(sorted(failed), ['rados/singleton/ all/ec-inconsistent-hinfo',
                                          'rados/thrash/ workloads/radosbench'])
        r = Result(Job(RUN, '9', 'fail', HINFO, LAGGY))
        baseline.compare(r, failed, 'main')
        self.assertEqual(r.baseline, [(MAIN, '1', True)])
        self.assertIn(f'main failed this test the same way, so the PRs under test did not '
                      f'cause it: {MAIN}/1', r.notes)
        r = Result(Job(RUN, '8', 'fail', 'rados/thrash/{workloads/radosbench x/y}', 'crashed'))
        baseline.compare(r, failed, 'main')
        self.assertEqual(r.notes, [f'main failed this test too, another way: {MAIN}/3'])


if __name__ == '__main__':
    unittest.main()
