import unittest

import yaml

from fakes import FakeFiles, job, problems, record

from qa_triage import hosts
from qa_triage.graph import Result


class Finding:
    def __init__(self, where):
        self.where = where

    def signature(self, job):
        return self.where


class TestHosts(unittest.TestCase):
    def test_machines(self):
        config = yaml.safe_dump({'targets': {'ubuntu@trial023.front.sepia.ceph.com': 'k',
                                             'trial152.front.sepia.ceph.com': 'k'}})
        self.assertEqual(hosts.machines(FakeFiles({'config.yaml': config.splitlines()})),
                         ['trial023', 'trial152'])
        self.assertEqual(hosts.machines(FakeFiles({})), [])

    def test_a_machine_that_failed_jobs_in_different_ways(self):
        [known] = problems(record())
        rs = []
        for n, (where, problem) in enumerate((('infra/a', None), ('infra/b', None),
                                              ('hung/c', None), ('infra/a', None),
                                              ('crash/d', known)), 1):
            r = Result(job(str(n)), verdict='duplicate' if problem else 'new',
                       problem=problem, finding=Finding(where))
            rs.append(r)
        on = {'1': ['trial1'], '2': ['trial1', 'trial2'], '3': ['trial1'], '4': ['trial2'],
              '5': ['trial2']}
        busy = hosts.crowded(rs, lambda r: on[r.job.id])
        # trial2's known problem does not count, and its two others are one way.
        self.assertEqual({m: [r.job.id for r in found] for m, found in busy.items()},
                         {'trial1': ['1', '2', '3']})
        lines = hosts.note(busy)
        self.assertEqual(lines[1], '  trial1 failed 3 jobs in 3 different ways, none a known '
                                   'problem of the test or the product: 1, 2, 3')
        self.assertIn('trial1 failed 3 jobs of this run in 3 different ways', rs[0].notes[0])


if __name__ == '__main__':
    unittest.main()
