"""Watching several runs: which runs, and their failures together, the commonest first."""
import datetime
import unittest
from types import SimpleNamespace
from unittest import mock

from qa_triage import sources, watch
from qa_triage.db import Problem
from qa_triage.sources import Job

MAIN = 'jenkins-build-2026-11-03_06:05:10-rados-main-distro-default-trial'
OLD = 'jenkins-build-2026-10-20_06:05:39-rados-main-distro-default-trial'
BATCH = 'skanta-2026-11-04_12:54:04-rados-wip-bharath9-testing-20261104.043109-main-distro-' \
        'default-trial'
SQUID = 'yuriw-2026-11-04_16:44:33-rados-wip-yuri8-testing-20261102.202309-squid-distro-' \
        'default-trial'
FS = 'skanta-2026-11-04_13:00:00-fs-wip-bharath9-testing-20261104.043109-main-distro-' \
     'default-trial'


def problem(pid, kind='product'):
    return Problem(pid, f'https://tracker.ceph.com/issues/{pid}', f'problem {pid}', kind, {},
                   [])


def result(run, jid, verdict='duplicate', found=None, signature='', sure=True, path=('crash',)):
    return SimpleNamespace(job=Job(run, jid, 'fail', '', 'reason'), verdict=verdict,
                           problem=found, sure=sure, agent=None, signature=signature,
                           notes=[], finding=SimpleNamespace(path=list(path), steps=[],
                                                             where='/'.join(path)))


class TestWatch(unittest.TestCase):
    def test_the_runs_of_the_last_days(self):
        # The suite's runs of main, and of the QA batches for main whose tracker was
        # updated, in the last days: not those of a batch for another base, of another
        # suite, or from before.
        pages = {'main': [MAIN, OLD], 'wip-bharath9-testing-20261104.043109-main': [BATCH, FS],
                 'wip-yuri8-testing-20261102.202309-squid': [SQUID]}
        asked = []

        def list_runs(url):
            asked.append(url)
            branch = url.split('branch=')[1].split('&')[0]
            return pages.get(branch, []) if 'page=' not in url else []

        issues = [{'custom_fields': [{'name': 'QA Runs', 'value': branch}]}
                  for branch in ('wip-bharath9-testing-20261104.043109-main',
                                 'wip-yuri8-testing-20261102.202309-squid')]
        queries = []

        def jobs_of(run, n=300):
            return [Job(run, str(i), 'pass', '', '') for i in range(n)]

        week = watch.window(now=datetime.datetime(2026, 11, 5, 12))
        found, left = watch.recent(list_runs, lambda q: queries.append(q) or issues, jobs_of,
                                   *week)
        self.assertEqual((found, left), ([BATCH, MAIN], []))
        self.assertIn('updated_on=%3E%3D2026-10-29', queries[0])
        self.assertTrue(all('suite=rados&date=2026-10-29&to_date=2026-11-05' in url
                            for url in asked))
        self.assertEqual(len(asked), 2)
        found, _ = watch.recent(list_runs, lambda q: issues, jobs_of, *week, most=1)
        self.assertEqual(found, [BATCH])
        # Between two dates: the batch's runs, of 11-04, are after them.
        found, _ = watch.recent(list_runs, lambda q: issues, jobs_of,
                                *watch.window(datetime.date(2026, 11, 1),
                                              datetime.date(2026, 11, 3)))
        self.assertEqual(found, [MAIN])
        # Not a developer's few jobs, nor a run still waiting for machines.
        sizes = {BATCH: 20, MAIN: 300}
        waiting = {MAIN: 'queued'}
        found, left = watch.recent(
            list_runs, lambda q: issues, lambda run: [
                Job(run, str(i), waiting.get(run, 'pass') if i == 0 else 'pass', '', '')
                for i in range(sizes[run])], *week)
        self.assertEqual(found, [])
        self.assertEqual(left, [(BATCH, 'it has 20 jobs, a part of the suite'),
                                (MAIN, '1 of its 300 jobs has not begun')])

    def test_the_runs_of_prs(self):
        # The runs of a QA batch whose tracker lists a PR, of a build of a PR's head,
        # and of the person's own builds, or a branch named as a PR's, that have its
        # commits; not someone else's, a run from before, or one not begun.
        def row(run, sha1, user=None, queued=0):
            return {'run': run, 'user': user or run.split('-')[0], 'branch': watch.branch_of(run),
                    'suite': watch.suite_of(run), 'sha1': sha1, 'queued': queued, 'waiting': 0,
                    'running': 0}

        mine = 'aainscow-2026-11-06_19:44:56-rados-wip-aainscow-stretch-qa-261106f-distro-' \
               'default-smithi'
        older = 'aainscow-2026-11-05_10:00:00-rados:standalone-wip-aainscow-stretch-qa-' \
                '261105-distro-default-smithi'
        theirs = 'bill-2026-11-05_09:00:00-rados-wip-bill-ec-stretch-code-x-distro-default-' \
                 'trial'
        head = 'jamie-2026-11-04_08:00:00-rados-wip-whatever-distro-default-trial'
        other = 'yuvalif-2026-11-04_11:47:55-rgw-wip-background-process1-distro-default-trial'
        before = 'aainscow-2026-10-01_10:00:00-rados-wip-aainscow-stretch-qa-261001-distro-' \
                 'default-smithi'
        queued = 'aainscow-2026-11-06_20:00:00-rados-wip-aainscow-stretch-qa-261106g-distro-' \
                 'default-smithi'
        builds = [row(queued, 'q' * 40, queued=3), row(mine, 'a' * 40), row(older, 'b' * 40),
                  row(BATCH, 'c' * 40), row(theirs, 'd' * 40), row(head, 'e' * 40),
                  row(other, 'f' * 40), row(before, 'a' * 40)]
        prs = {'72643': {'head': 'e' * 40, 'ref': 'wip-ec-stretch-code',
                         'commits': [('1' * 40, 'a@x', 'osd: stretch'),
                                     ('2' * 40, 'a@x', 'mon: stretch')]},
               '73254': {'head': '9' * 40, 'ref': 'fix',
                         'commits': [('3' * 40, 'a@x', 'osd: fix')]}}
        issues = [{'id': 82000,
                   'custom_fields': [{'name': 'QA Runs', 'value': watch.branch_of(BATCH)}],
                   'description': '* https://github.com/ceph/ceph/pull/73254'}]
        read = {'a' * 40: ({'72643': ['a commit that changes its files names it'],
                            '69112': ['merged into its branch']}, []),
                'b' * 40: ({}, [('a@x', 'osd: stretch')]), 'd' * 40: ({}, []), 'q' * 40: ({}, [])}
        asked = []

        def reader(sha1s):
            asked.append(sha1s)
            return {sha1: read.get(sha1) for sha1 in sha1s}

        start, end = datetime.datetime(2026, 10, 30), datetime.datetime(2026, 11, 6, 23)
        taken, left, tests = watch.pr_runs(prs, builds, lambda q: issues, reader, start, end,
                                           users=['aainscow'])
        self.assertEqual(taken, [mine, older, theirs, BATCH, head])
        self.assertEqual(tests[mine], {'72643': ['a commit that changes its files names it']})
        self.assertEqual(tests[older], {'72643': ['1 of its 2 commits are in the build, by '
                                                  'their titles']})
        self.assertEqual(tests[theirs], {'72643': ['its branch is named as the PR\'s is']})
        self.assertEqual(tests[BATCH], {'73254': ['its QA tracker, #82000, lists it']})
        self.assertEqual(tests[head], {'72643': ['it is a build of the PR\'s head']})
        self.assertEqual(left, [])
        # Only the person's builds, and the named branch's, are read, the newest first.
        self.assertEqual(asked, [['q' * 40, 'a' * 40, 'b' * 40, 'd' * 40]])
        # A run not begun waits; one past the most is left out, and said to be.
        read['q' * 40] = ({'72643': ['merged into its branch']}, [])
        taken, left, _ = watch.pr_runs(prs, builds, lambda q: issues, reader, start, end,
                                       users=['aainscow'], most=2)
        self.assertEqual(taken, [mine, older])
        self.assertEqual(left[0], (queued, '3 of its jobs have not begun'))
        self.assertEqual(left[1:], [(run, 'only the newest 2 runs are taken')
                                    for run in (theirs, BATCH, head)])
        lines = watch.pr_lines(tests, {'72643': 'EC stretch'})
        self.assertEqual(lines[0], '#72643 EC stretch: 4 runs')
        self.assertIn('a commit that changes its files names it', lines[1])
        self.assertEqual(watch.said({'prs': 'mine', 'days': 7}), 'your PRs, the 7 days to now')
        self.assertEqual(watch.following({'prs': ['73070']}), 'PR #73070')

    def test_prs_are_followed_for_one_watch(self):
        from qa_triage.cli import follow_prs
        state = watch.kept(self.root if hasattr(self, 'root') else '/nonexistent')
        state.update(found=['a run'], found_at='then')
        follow_prs('mine', state)
        self.assertEqual((state['prs'], state['found']), ('mine', []))
        state.update(found=['a pr run'])
        follow_prs('#73070, 73100', state)
        self.assertEqual(state['prs'], ['73070', '73100'])
        # The next watch without --prs takes the suite's runs, not the PRs' again.
        state.update(found=['a pr run'])
        follow_prs(None, state)
        self.assertEqual((state['prs'], state['found']), (None, []))
        state.update(found=['a suite run'])
        follow_prs(None, state)
        self.assertEqual(state['found'], ['a suite run'])
        with self.assertRaises(ValueError):
            follow_prs('mine,73070', state)

    def test_the_builds_pulpito_lists(self):
        page = ('<table><tr><th>User</th></tr><tr class="danger"> <td data-title="User"> '
                f'<a href="/{MAIN}"> jenkins </a> </td> <td data-title="Suite"> <a href="/x"> '
                'rados </a> </td> <td data-title="Branch"> <a href="/x"> main </a> </td> '
                '<td data-title="Revision"><a href="/?sha1=' + 'a' * 40 + '" target="_blank">'
                'aaaaaaa</a></td> <td data-title="Queued">2</td> <td data-title="Waiting">0</td>'
                ' <td data-title="Running">5</td> </tr></table>')
        with mock.patch.object(sources, '_get_text', return_value=page):
            self.assertEqual(sources.list_builds('https://pulpito.ceph.com/?date=x'),
                             [{'run': MAIN, 'user': 'jenkins', 'suite': 'rados', 'branch': 'main',
                               'sha1': 'a' * 40, 'queued': 2, 'waiting': 0, 'running': 5}])

    def test_the_days_watched(self):
        today = datetime.date(2026, 11, 5)
        self.assertEqual(watch.day('10-29', today), datetime.date(2026, 10, 29))
        self.assertEqual(watch.day('2026-09-01'), datetime.date(2026, 9, 1))
        self.assertIsNone(watch.day(' '))
        with self.assertRaisesRegex(ValueError, 'not a date'):
            watch.day('last week')
        start, end = watch.window(datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))
        self.assertEqual((start, end), (datetime.datetime(2026, 9, 1),
                                        datetime.datetime(2026, 9, 30, 23, 59, 59, 999999)))
        with self.assertRaisesRegex(ValueError, 'is after'):
            watch.window(datetime.date(2026, 9, 30), datetime.date(2026, 9, 1))
        self.assertEqual(watch.said({'since': '2026-09-01', 'until': None}), '09-01 to now')
        self.assertEqual(watch.said({'days': 14}), 'the 14 days to now')

    def test_failures_together_the_commonest_first(self):
        # A known problem in two runs comes before a new failure with more jobs in one;
        # the infrastructure's are apart.
        known, infra = problem('100'), problem('200', kind='infra')
        results = [result(MAIN, '1', found=known), result(BATCH, '2', found=known),
                   result(BATCH, '3', 'new', signature='crash/abort | x'),
                   result(BATCH, '4', 'new', signature='crash/abort | x'),
                   result(BATCH, '5', 'new', signature='crash/abort | x'),
                   result(BATCH, '6', 'seen-after-fix', found=problem('300')),
                   result(MAIN, '7', 'new', signature='hung | y', sure=False),
                   result(MAIN, '8', found=infra), result(BATCH, '9', found=infra)]
        failures = watch.combine(results)
        self.assertEqual([(f.label, len(f.runs), len(f.results)) for f in failures],
                         [('TR 100', 2, 2), ('TR 200', 2, 2), ('new', 1, 3),
                          ('TR 300', 1, 1), ('unexplained', 1, 1)])
        self.assertEqual([f.kind for f in failures],
                         ['known', 'known', 'new', 'seen-after-fix', 'unexplained'])
        self.assertTrue(failures[1].infra)
        lines = watch.summary(failures, [BATCH, MAIN], 'main')
        self.assertEqual(lines[0], 'The commonest failures of 2 runs, of 9 jobs that failed or '
                                   'died:')
        self.assertIn('  1. TR 100          2 runs,   2 jobs  problem 100', lines)
        self.assertIn('                   a known problem; on main too (1 of its runs)', lines)
        self.assertTrue(any(line.startswith('  2. new') for line in lines))
        self.assertIn('                   a known problem seen again after its fix; only in '
                      'QA batches', lines)
        self.assertEqual(lines[-1], 'The test infrastructure failed 2 jobs in 2 runs, not '
                                    'listed: problem 200 (2)')
        self.assertEqual(len(watch.summary(failures, [BATCH, MAIN], 'main', top=1)),
                         2 + 2 + 1 + 2)
        text = watch.markdown(failures, [BATCH, MAIN], 'main', {BATCH: 6, MAIN: 3})
        self.assertIn(f'| [{BATCH}](https://pulpito.ceph.com/{BATCH}/) | 6 |', text)
        self.assertIn('| 1 | TR 100 | 2 | 2 | problem 100 |', text)
        self.assertIn('| 5 | TR 200 (infra) | 2 | 2 | problem 200 |', text)
        self.assertIn(f'- {BATCH}: [3](https://pulpito.ceph.com/{BATCH}/3/), '
                      f'[4](https://pulpito.ceph.com/{BATCH}/4/)', text)


if __name__ == '__main__':
    unittest.main()
