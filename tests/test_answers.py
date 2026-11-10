import os
import tempfile
import unittest

import yaml

from fakes import RUN, job

from qa_triage import answers

WIKI = """h1. MAIN

h3. https://tracker.ceph.com/issues/81368

Failures, unrelated:
# ['631086'] - https://tracker.ceph.com/issues/80986 - rados/test.sh: LibRadosIoECPP
# ['631092', '631262'] - https://tracker.ceph.com/issues/81857 - qa/mgr/dashboard: times out

h3. https://tracker.ceph.com/issues/81211

597567, 597303 - https://tracker.ceph.com/issues/81515 - see https://tracker.ceph.com/issues/1
1. https://tracker.ceph.com/issues/79831 - no jobs on this line
"""


class TestAnswers(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(answers.parse(WIKI), {
            '81368': {'631086': '80986', '631092': '81857', '631262': '81857'},
            '81211': {'597567': '81515', '597303': '81515'}})

    def test_gather_and_write(self):
        sections = answers.parse(WIKI)
        trackers = {'81368': {'custom_fields': [{'name': 'QA Runs', 'value': 'wip-a'}]},
                    '81211': {'custom_fields': [{'name': 'QA Runs', 'value': 'wip-b wip-c'}]}}
        runs = {'wip-a': ['run-a1', 'run-a2'], 'wip-b': ['run-b'], 'wip-c': ['run-c']}
        jobs = {'run-a1': ['631086'], 'run-a2': ['631092', '631262'], 'run-b': [],
                'run-c': ['597567']}
        said = []
        found = answers.gather(sections, trackers.__getitem__,
                               lambda url: runs[url.split('branch=')[1]],
                               lambda run: [job(j) for j in jobs[run]], said.append)
        self.assertEqual(found, {'run-a1': {'631086': '80986'},
                                 'run-a2': {'631092': '81857', '631262': '81857'},
                                 'run-c': {'597567': '81515'}})
        self.assertEqual(said[1], 'QA tracker 81211: 1 of 2 jobs found in the runs of wip-b, '
                                  'wip-c')
        directory = tempfile.mkdtemp(prefix='qa_triage-answers-')
        answers.write({RUN: {'631086': '80986'}}, directory)
        with open(os.path.join(directory, 'index.yaml')) as f:
            index = yaml.safe_load(f)
        with open(os.path.join(directory, index[RUN])) as f:
            self.assertEqual(yaml.safe_load(f), {631086: 80986})

    def test_the_jobs_an_issue_links_to(self):
        trial = 'skanta-2026-09-20_02:51:43-rados-wip-x-main-distro-default-trial'
        issue = {'id': 68586, 'updated_on': 'u1', 'description': (
                     f'Seen in /a/{trial}/570113 and\n'
                     'https://pulpito.ceph.com/teuthology-2026-01-01_00:00:00-rados-main-distro-'
                     'default-smithi/8000001/'),
                 'journals': [
                     {'notes': f'https://qa-proxy.ceph.com/teuthology/{trial.replace(":", "%3A")}'
                               '/570200/teuthology.log'},
                     {'notes': f'The rerun passed: /a/{trial}/570300'},
                     {'notes': None}]}
        self.assertEqual(answers.sightings(issue), {
            (trial, '570113'), (trial, '570200'),
            ('teuthology-2026-01-01_00:00:00-rados-main-distro-default-smithi', '8000001')})
        # Only the trial lab's runs are kept; a backport counts as its original;
        # a job two problems claim is left out; an issue is read once until it
        # changes.
        other = {'id': 99, 'updated_on': 'u2', 'description': f'/a/{trial}/570200',
                 'journals': []}
        backport = {'id': 80785, 'updated_on': 'u3', 'description': f'/a/{trial}/570400',
                    'journals': []}
        # A bot's issue about a machine lists the jobs that ran on it last, whatever
        # failed them: they are not answers, nor claims.
        machine = {'id': 78367, 'updated_on': 'u4', 'journals': [],
                   'subject': 'trial005: last 10 jobs all failed or died',
                   'description': f'/a/{trial}/570113\n/a/{trial}/570500'}
        issues = {'68586': issue, '99': other, '80785': backport, '78367': machine}
        read, said = [], []

        def tracker(number):
            read.append(number)
            return issues[number]

        cache = tempfile.mkdtemp()
        listed = [{'id': n, 'updated_on': i['updated_on']} for n, i in issues.items()]
        canonical = {'80785': '68586'}.get
        for _ in range(2):
            found = answers.harvest(listed, tracker, lambda n: canonical(n, n),
                                    say=said.append, cache=cache)
        self.assertEqual(found, {trial: {'570113': '68586', '570400': '68586'}})
        self.assertEqual(read, ['68586', '99', '80785', '78367'])
        self.assertIn(f'{trial}/570200: claimed by 68586, 99; left out', said)


if __name__ == '__main__':
    unittest.main()
