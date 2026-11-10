import os
import subprocess
import tempfile
import unittest
from unittest import mock

from fakes import FakeFiles, FakeJudge, job, problems, record

from qa_triage import checkout, graph, pack, report
from qa_triage.graph import Result, Triage

LOG = ['2026-10-13T02:28:44.563 INFO:teuthology.packaging:ceph version 21.3.0-4162-gf64c8d74',
       "api_io_pp: Perf counter 'objecter.op_reply' has incorrect value after operate().",
       'api_io_pp: [  FAILED  ] LibRadosIoECPP.ZeroAndTruncateSameRequest/3']
CONFIG = ['branch: wip-test-main']


def files(log=LOG):
    return FakeFiles({'teuthology.log': log, 'orig.config.yaml': CONFIG})


class TestDeterministic(unittest.TestCase):
    def test_duplicate(self):
        r = Triage(problems(record())).run(job(), files())
        self.assertEqual(r.verdict, 'duplicate')
        self.assertEqual(r.problem.id, '100')
        self.assertEqual(r.path, ['explain', 'prefilter', 'confirm', 'fix-check'])
        self.assertTrue(r.sure)

    def test_an_agent_may_name_a_problem_by_its_tracker(self):
        r = Triage(problems(record())).run(job(), files())
        for named in ('100', 'https://tracker.ceph.com/issues/100', '#100', 100):
            self.assertTrue(graph.agrees(r, {'verdict': 'duplicate', 'problem': named}))
        self.assertFalse(graph.agrees(r, {'verdict': 'duplicate',
                                          'problem': 'https://tracker.ceph.com/issues/1000'}))

    def test_failed_check_goes_to_agent(self):
        r = Triage(problems(record(pattern='not in the log'))).run(job(), files())
        self.assertEqual(r.verdict, 'new')
        # The tree explained it, but a record nearly fitted: a judge decides.
        self.assertEqual(r.path, ['explain', 'prefilter', 'confirm', 'explained', 'agent'])
        self.assertEqual(r.checks[0][3], [])
        self.assertFalse(r.sure)

    def test_prefilter_miss(self):
        p = problems(record(reason='something else'))[0]
        r = Triage([p]).run(job(), files())
        # The tree explained it as a gtest failure, and no record fits: new, for sure.
        self.assertEqual(r.path, ['explain', 'prefilter', 'explained'])
        self.assertEqual((r.verdict, r.sure, r.finding.where), ('new', True, 'workunit/gtest'))
        self.assertFalse(r.checks)
        self.assertEqual(graph.misses(p, r), [
            'match reason /something else/ does not fit "Command failed (workunit test '
            'rados/test.sh) on trial1 with status 1"'])
        r = Triage([p]).run(job(reason=''), files())
        self.assertEqual(graph.misses(p, r), [
            'match reason /something else/: the job has no reason; its description is '
            '"rados/basic/{tasks/rados_api_tests}"'])

    def test_unexplained_goes_to_agent(self):
        r = Triage(problems(record(reason='something else'))).run(
            job(), files(['some line', 'another line']))
        self.assertEqual(r.path, ['explain', 'prefilter', 'explained', 'agent'])
        self.assertEqual((r.verdict, r.sure), ('new', False))
        self.assertFalse(r.finding.conclusive)

    def test_exclude(self):
        p = record(exclude=[{'file': 'teuthology.log', 'pattern': 'ZeroAndTruncate'}])
        r = Triage(problems(p)).run(job(), files())
        self.assertEqual(r.verdict, 'new')

    def test_ambiguous(self):
        r = Triage(problems(record('100'), record('200'))).run(job(), files())
        self.assertEqual(r.verdict, 'ambiguous')
        self.assertTrue(r.escalated)

    def test_deep_checks_only_with_deep(self):
        p = record(deep=[{'file': 'remote/*/log/ceph-osd.*.log.gz', 'pattern': 'unstable'}])
        self.assertEqual(Triage(problems(p)).run(job(), files()).verdict, 'duplicate')
        r = Triage(problems(p), deep=True).run(job(), files())
        self.assertEqual(r.verdict, 'new')

    def test_fix_not_in_build(self):
        p = record(fixes=[{'pr': 'https://github.com/ceph/ceph/pull/1',
                           'fixed_in': 'v21.3.0-4449-g05a7656731'}])
        r = Triage(problems(p)).run(job(), files())
        self.assertEqual(r.verdict, 'duplicate')
        self.assertIn('is not in this build', r.notes[0])

    def test_fix_in_build(self):
        p = record(fixes=[{'pr': 'https://github.com/ceph/ceph/pull/1',
                           'fixed_in': 'v21.3.0-4000-g05a7656731'}])
        with mock.patch.object(graph, 'fix_in_build', return_value=True):
            r = Triage(problems(p)).run(job(), files())
        self.assertEqual(r.verdict, 'seen-after-fix')

    def test_a_tests_fix_is_looked_for_in_its_suite(self):
        p = record(fixes=[{'pr': 'https://github.com/ceph/ceph/pull/1',
                           'fixed_in': 'v21.3.0-4000-g05a7656731'}])
        suite = FakeFiles({'teuthology.log': LOG,
                           'orig.config.yaml': CONFIG + ['suite_sha1: ' + 'c' * 40]})
        with mock.patch.object(graph, 'fix_in_build', return_value=False) as check:
            r = Triage(problems(p)).run(job(), suite)
        self.assertEqual(check.call_args[0][1], (None, None, 'c' * 40))
        self.assertIn('is not in the qa suite this job ran', r.notes[0])
        # A product's fix is in the build, whatever the suite.
        p = record(kind='product', fixes=p['fixes'])
        with mock.patch.object(graph, 'fix_in_build', return_value=True) as check:
            r = Triage(problems(p)).run(job(), suite)
        self.assertEqual(check.call_args[0][1], ('21.3.0', 4162, 'f64c8d74'))
        self.assertEqual(r.verdict, 'seen-after-fix')

    def test_fix_under_test(self):
        p = record(fixes=[{'pr': 'https://github.com/ceph/ceph/pull/7'}])
        r = Triage(problems(p), batch_prs={'7', '8'}).run(job(), files())
        self.assertEqual(r.verdict, 'seen-after-fix')
        self.assertIn('one of the PRs under test', r.notes[0])

    def test_fix_on_other_branch(self):
        p = record(fixes=[{'pr': 'https://github.com/ceph/ceph/pull/1', 'branch': 'squid',
                           'fixed_in': 'v19.2.0-10-g05a7656731'}])
        r = Triage(problems(p)).run(job(), files())
        self.assertEqual(r.verdict, 'duplicate')
        self.assertIn('not on wip-test-main', r.notes[0])


class TestJudge(unittest.TestCase):
    def triage(self, verdict):
        judge = FakeJudge(verdict)
        t = Triage(problems(record(pattern='not in the log')), judge=judge,
                   workdir=tempfile.mkdtemp())
        return t, judge

    def test_evidence_is_never_another_jobs(self):
        # A workdir used before for another job, as a lesson's is when it is taught
        # again with another of its jobs, keeps nothing of that job's.
        workdir = tempfile.mkdtemp()
        pack.gather(workdir, FakeFiles({'teuthology.log': ['the first job failed']}),
                    Result(job('1')), [])
        with open(os.path.join(workdir, 'daemon-log.txt'), 'w') as f:
            f.write('the first job\'s osd\n')
        pack.gather(workdir, FakeFiles({'teuthology.log': ['the second job failed']}),
                    Result(job('2')), [])
        with open(os.path.join(workdir, 'teuthology.log')) as f:
            self.assertEqual(f.read(), 'the second job failed\n')
        self.assertFalse(os.path.exists(os.path.join(workdir, 'daemon-log.txt')))
        # The same job's, gathered again, is kept.
        pack.gather(workdir, FakeFiles({'teuthology.log': ['not read again']}),
                    Result(job('2')), [])
        with open(os.path.join(workdir, 'teuthology.log')) as f:
            self.assertEqual(f.read(), 'the second job failed\n')

    def test_no_judge_is_asked_of_a_job_with_no_logs(self):
        t, judge = self.triage({'verdict': 'unsure', 'problem': None, 'summary': '',
                                'evidence': []})
        r = t.run(job(), FakeFiles({'orig.config.yaml': CONFIG}))
        self.assertEqual((r.verdict, r.sure, judge.calls), ('new', False, []))
        self.assertIn('no teuthology.log of this job, so no judge was asked', r.notes[-1])

    def test_probable_duplicate_with_verified_quote(self):
        t, judge = self.triage({'verdict': 'duplicate', 'problem': '100', 'summary': '',
                                'evidence': [{'file': 'excerpts.txt',
                                              'text': 'objecter.op_reply'}]})
        r = t.run(job(), files())
        self.assertEqual(r.verdict, 'probable-duplicate')
        self.assertEqual(r.path[-3:], ['agent', 'verify-quotes', 'fix-check'])
        workdir, prompts = judge.calls[0]
        self.assertIn('=== excerpts.txt ===', prompts['inline'])
        self.assertNotIn('=== excerpts.txt ===', prompts['files'])

    def test_the_judge_says_where_each_line_it_quotes_is_and_what_it_shows(self):
        t, _ = self.triage({'verdict': 'duplicate', 'problem': 'TR 100', 'summary': 's',
                            'explanation': 'The gtest counted one reply too many.',
                            'evidence': [{'file': 'excerpts.txt',
                                          'text': 'objecter.op_reply',
                                          'shows': 'the counter was wrong'},
                                         {'file': 'teuthology.log', 'text': '[  FAILED  ]',
                                          'shows': 'the test failed'},
                                         {'file': 'job.yaml', 'text': 'rados_api_tests',
                                          'shows': 'the test'}]})
        r = t.run(job(), files())
        self.assertEqual((r.verdict, r.problem.id), ('probable-duplicate', '100'))
        self.assertEqual([q.get('at') for q in r.agent['evidence']],
                         ['teuthology.log:2', 'teuthology.log:3', None])
        text = report._job_section('run', r)
        self.assertIn('### The judge: duplicate TR 100 (fake, cost None)\n\n'
                      'The gtest counted one reply too many.\n\nThe lines it quotes, as the '
                      'logs have them, the quote in bold:', text)
        # The whole line, with the quote in bold.
        self.assertIn('   - [`teuthology.log:2`](https://qa-proxy.ceph.com/teuthology/run/1/'
                      'teuthology.log): the counter was wrong\n'
                      "     ` api_io_pp: Perf counter ' `**` objecter.op_reply `**` ' has "
                      'incorrect value after operate(). `', text)
        # A line from a file the tool wrote, not the archive, is not linked there.
        self.assertIn('   - `job.yaml:4`: the test\n'
                      '     ` description: rados/basic/{tasks/ `**` rados_api_tests `**` } `', text)

    def test_every_line_that_has_a_quote_up_to_the_most_shown(self):
        log = LOG + [f'api_io_pp: retry {n}: objecter.op_reply again' for n in range(1, 15)]
        t, _ = self.triage({'verdict': 'duplicate', 'problem': '100', 'summary': 's',
                            'evidence': [{'file': 'teuthology.log', 'text': 'objecter.op_reply',
                                          'shows': 'replies'},
                                         {'file': 'excerpts.txt', 'text': '[  FAILED  ]'}]})
        t.cited_lines = 4
        r = t.run(job(), files(log))
        many, one = r.agent['evidence']
        self.assertEqual((many['count'], [row[1] for row in many['lines']]), (15, [2, 4, 5, 6]))
        self.assertEqual(many['lines'][0][3], [25, 42])
        self.assertEqual((one['count'], one['at'], one['archived']), (1, 'teuthology.log:3', True))
        text = report._job_section('run', r)
        self.assertIn('   - replies; 15 lines have it, the first 4 here:\n'
                      '     - [`teuthology.log:2`](https://qa-proxy.ceph.com/teuthology/run/1/'
                      "teuthology.log)\n       ` api_io_pp: Perf counter ' `**` objecter.op_reply "
                      "`**` ' has incorrect value after operate(). `", text)

    def test_a_walkthrough_takes_the_reviewer_through_the_logs(self):
        t, _ = self.triage({
            'verdict': 'duplicate', 'problem': '100', 'summary': 'the counter',
            'explanation': 'The gtest counted one reply too many.',
            'walkthrough': [
                {'title': 'Where the test failed', 'do': 'Search teuthology.log for FAILED.',
                 'finds': 'The gtest failed.',
                 'evidence': [{'file': 'excerpts.txt', 'text': '[  FAILED  ]',
                               'shows': 'the test failed'}]},
                {'title': 'What to do', 'do': 'Nothing.', 'finds': 'It is TR 100 again.',
                 'evidence': []}]})
        r = t.run(job(), files())
        # The steps' quotes are checked, and found where the logs have them.
        self.assertEqual((r.verdict, r.problem.id), ('probable-duplicate', '100'))
        quote = r.agent['walkthrough'][0]['evidence'][0]
        self.assertEqual((quote['at'], [row[1] for row in quote['lines']]),
                         ('teuthology.log:3', [3]))
        text = report._job_section('run', r)
        self.assertIn('The gtest counted one reply too many.\n\nHow to check it, step by step',
                      text)
        self.assertIn('1. **Where the test failed.** Search teuthology.log for FAILED. The gtest '
                      'failed.\n   Open it: `qa_triage open run/1 teuthology.log:3`\n'
                      '   - [`teuthology.log:3`](https://qa-proxy.ceph.com/teuthology/run/1/'
                      'teuthology.log): the test failed\n', text)
        self.assertIn('2. **What to do.** Nothing. It is TR 100 again.', text)
        self.assertEqual(report.told_text(r.agent)[2:4], [
            'How to check it:', '1. Where the test failed. Search teuthology.log for FAILED. '
                                'The gtest failed.'])
        # An analyst's account, once it passed the checks, takes the judge's place.
        r.analysis = {'analyst': 'bob+files', 'job': '1', 'problem': '100',
                      'assessment': 'right', 'explanation': 'It is the counter, as the judge '
                      'said.', 'walkthrough': []}
        text = report._job_section('run', r)
        self.assertIn('### The analyst (Bob): TR 100, right result\n\nIt is the counter, as the '
                      'judge said.', text)
        self.assertIn('the counter Its account is replaced by the analyst\'s, above.', text)
        self.assertNotIn('How to check it', text)

    def test_a_quote_as_the_log_has_it(self):
        # Without the line number and the count of lines like it that excerpts.txt adds.
        self.assertEqual(pack._as_logged('3153550: 2026 WARNING: retried  (+1 similar)'),
                         '2026 WARNING: retried')
        self.assertEqual(pack._as_logged('teuthology.log:12: a line'), 'a line')

    def test_agents_by_their_proper_names(self):
        from qa_triage.agents import readable
        self.assertEqual([readable(n) for n in ('bob+files', 'claude:opus', 'gemma,claude:haiku',
                                                'bob-analyst+files', 'ollama:gemma4:26b')],
                         ['Bob', 'Claude (opus)', 'gemma, Claude (haiku)', 'bob-analyst',
                          'Ollama (gemma4:26b)'])

    def test_the_line_the_judge_read_is_shown_if_it_is_beyond_the_first(self):
        workdir = tempfile.mkdtemp()
        log = [f'{n} same' for n in range(1, 30)]
        answer = {'evidence': [{'file': 'teuthology.log', 'text': 'same',
                                'at': 'teuthology.log:20'}]}
        pack.shown(workdir, answer, FakeFiles({'teuthology.log': log}), most=3)
        self.assertEqual([row[1] for row in answer['evidence'][0]['lines']], [1, 2, 20])

    def test_quote_not_found(self):
        t, _ = self.triage({'verdict': 'duplicate', 'problem': '100', 'summary': '',
                            'evidence': [{'file': 'excerpts.txt', 'text': 'invented'}]})
        r = t.run(job(), files())
        self.assertEqual(r.verdict, 'new')
        self.assertIn('1 of 1 quotes were not found', r.notes[0])

    def test_failed_line_is_an_excerpt(self):
        t, _ = self.triage({'verdict': 'duplicate', 'problem': '100', 'summary': '',
                            'evidence': [{'file': 'excerpts.txt',
                                          'text': 'Unit sshd.service not found'}]})
        log = LOG + ['Failed to restart sshd.service: Unit sshd.service not found.']
        self.assertEqual(t.run(job(), files(log)).verdict, 'probable-duplicate')

    def test_quote_with_the_line_number_read_file_gives(self):
        t, _ = self.triage({'verdict': 'duplicate', 'problem': '100', 'summary': '',
                            'evidence': [{'file': 'teuthology.log',
                                          'text': '3: api_io_pp: [  FAILED  ]'}]})
        self.assertEqual(t.run(job(), files()).verdict, 'probable-duplicate')

    def test_quote_of_several_lines(self):
        for text, verdict in (("'objecter.op_reply' has incorrect value\n"
                               "    api_io_pp: [  FAILED  ] LibRadosIoECPP", 'probable-duplicate'),
                              ('ceph version 21.3.0\n[  FAILED  ]', 'new')):
            t, _ = self.triage({'verdict': 'duplicate', 'problem': '100', 'summary': '',
                                'evidence': [{'file': 'teuthology.log', 'text': text}]})
            self.assertEqual(t.run(job(), files()).verdict, verdict)

    def test_quote_outside_evidence(self):
        t, _ = self.triage({'verdict': 'duplicate', 'problem': '100', 'summary': '',
                            'evidence': [{'file': '../../etc/passwd', 'text': 'root'}]})
        self.assertEqual(t.run(job(), files()).verdict, 'new')

    def test_unknown_problem(self):
        t, _ = self.triage({'verdict': 'duplicate', 'problem': '999', 'summary': '',
                            'evidence': [{'file': 'excerpts.txt', 'text': 'op_reply'}]})
        r = t.run(job(), files())
        self.assertEqual(r.verdict, 'new')
        self.assertIn('TR 999, which has no record yet', r.notes[0])
        t, _ = self.triage({'verdict': 'duplicate', 'problem': 'x', 'summary': '',
                            'evidence': [{'file': 'excerpts.txt', 'text': 'op_reply'}]})
        self.assertIn('unknown problem x', t.run(job(), files()).notes[0])

    def test_the_judge_sees_the_trackers_a_search_for_its_tests_finds(self):
        searches = []

        def search(words):
            searches.append(words)
            return [('100', 'Bug #100 (New): test.sh fails'),
                    ('200', 'Bug #200 (New): test.sh times out')]

        t, judge = self.triage({'verdict': 'new', 'problem': None, 'summary': '',
                                'evidence': []})
        t.search = search
        t.run(job(), files())
        t.run(job('2'), files())
        # One search for the test the reason names, whichever jobs ran it.
        self.assertEqual(searches, ['test.sh'])
        inline = judge.calls[0][1]['inline']
        self.assertIn('=== trackers.txt ===\nBug #100 (New): test.sh fails (has a record)\n'
                      'Bug #200 (New): test.sh times out\n', inline)
        self.assertNotIn('=== under-test.txt ===', inline)

    def test_a_blind_check(self):
        t = Triage(problems(record()), workdir=tempfile.mkdtemp())
        r = t.run(job(), files())
        self.assertEqual(r.verdict, 'duplicate')
        for blind in (False, True):
            t.judge = FakeJudge({'verdict': 'duplicate', 'problem': '100', 'summary': '',
                                 'evidence': [{'file': 'excerpts.txt', 'text': 'op_reply'}]})
            t.blind = blind
            t.check_group([r], files())
            inline = t.judge.calls[0][1]['inline']
            # Blind, the judge is not told what the tree and the records decided.
            self.assertEqual('The tool decided this job by itself' in inline, not blind)
            self.assertEqual('Not given: decide from the logs' in inline, blind)
            self.assertEqual((r.opinion['agrees'], r.opinion['blind']), (True, blind))

    def test_what_a_job_names(self):
        self.assertEqual(pack.search_words(job(description='rados/cephadm/workunits/{task/'
                                                           'test_iscsi_container}',
                                               reason='hit max job timeout')),
                         ['test_iscsi_container'])
        self.assertEqual(pack.search_words(job(description='rados/singleton/{all/ec-'
                                                           'inconsistent-hinfo msgr/async}',
                                               reason='"PG 2.0s0 marked LAGGY" in cluster log')),
                         ['ec-inconsistent-hinfo'])
        self.assertTrue(pack.mentioned('qa/standalone/mon/misc.sh', 'workunit test mon/misc.sh'))
        self.assertTrue(pack.mentioned('src/osd/ECBackend.cc',
                                       '/ceph/src/osd/ECBackend.cc: 1250: FAILED ceph_assert'))
        self.assertFalse(pack.mentioned('doc/dev/release-checklists.rst', 'workunit test'))
        self.assertFalse(pack.mentioned('qa/tasks/ceph.py', 'rados/{tasks/ceph_test_rados}'))

    def test_a_pr_under_test_that_changes_what_the_job_ran(self):
        t = Triage(problems(record()))
        changes = [('PR #2', 'c2', 'p2', ['doc/x.rst', 'qa/workunits/rados/test.sh']),
                   ('PR #1', 'c1', 'p1', ['src/osd/OSD.cc'])]
        t._branch = lambda: changes
        # Off unless asked for, as for the corpus and the doubts' checks.
        self.assertEqual(t.run(job(), files()).suspects, [])
        t.blame = True
        r = t.run(job(), files())
        self.assertEqual((r.verdict, r.suspects),
                         ('duplicate', [('PR #2', 'qa/workunits/rados/test.sh')]))
        self.assertIn('PR #2, under test, changes qa/workunits/rados/test.sh', r.notes[-1])
        # A branch merged with git pull is named by its PR, if GitHub has one.
        t.pr_of = lambda url, branch: '69289' if branch == 'fix' else None
        self.assertEqual([t._pr('branch fix of https://github.com/a/ceph'),
                          t._pr('branch other of https://github.com/a/ceph'), t._pr('PR #1')],
                         ['PR #69289', 'branch other of https://github.com/a/ceph', 'PR #1'])

    def test_a_change_to_another_function_of_the_file_a_crash_is_in(self):
        # PR #3 changes the file the crash is in, but not the function: not suspected,
        # and said to be unlikely.
        t = Triage(problems(record()))
        t.blame, t.git_dir = True, 'a clone'
        t._branch = lambda: [('PR #3', 'c3', 'p3', ['src/osd/OSDMap.cc'])]
        t._naming = lambda commit, files, names: []
        r = t.run(job(reason='/ceph/src/osd/OSDMap.cc: 6550: FAILED ceph_assert(x)'), files())
        r.finding.facts['function'] = 'float OSDMap::calc_deviations(const std::map<int, float>&)'
        key = ('c3', 'src/osd/OSDMap.cc', ('calc_deviations',))
        t.touched[key], r.notes = False, []
        t.suspect(r)
        self.assertEqual(r.suspects, [])
        self.assertEqual(r.notes, ['PR #3, under test, changes src/osd/OSDMap.cc, but not '
                                   'calc_deviations, which the job failed in: unlikely to be '
                                   'its doing'])
        # One that changes the function is suspected.
        t.touched[key], r.notes = True, []
        t.suspect(r)
        self.assertEqual(r.suspects, [('PR #3', 'src/osd/OSDMap.cc')])

    def test_whether_a_change_touches_a_function(self):
        repo = tempfile.mkdtemp()

        def git(*args):
            return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                                  text=True).stdout.strip()
        git('init', '-q')
        path = os.path.join(repo, 'a.cc')
        body = ['int one(int x)', '{', '  return x;', '}', '', 'int two(int x)', '{',
                '  return x + 1;', '}']
        with open(path, 'w') as f:
            f.write('\n'.join(body) + '\n')
        git('add', '.')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'one')
        parent = git('rev-parse', 'HEAD')
        body[7] = '  return x + 2;'
        with open(path, 'w') as f:
            f.write('\n'.join(body) + '\n')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'two')
        commit = git('rev-parse', 'HEAD')
        self.assertTrue(checkout.touches(repo, parent, commit, 'a.cc', ['two']))
        self.assertFalse(checkout.touches(repo, parent, commit, 'a.cc', ['one']))

    def test_a_verdict_is_paid_for_once(self):
        verdicts = tempfile.mkdtemp()
        verdict = {'verdict': 'duplicate', 'problem': '100', 'summary': '', 'cost': 0.25,
                   'evidence': [{'file': 'excerpts.txt', 'text': 'objecter.op_reply'}]}
        rs = []
        for log in (LOG, LOG, LOG + ['something else failed']):
            t, judge = self.triage(verdict)
            t.verdicts = verdicts
            rs.append(t.run(job(), files(log)))
            # Asked only when the evidence differs from what it judged before.
            self.assertEqual(len(judge.calls), 0 if len(rs) == 2 else 1)
            self.assertEqual(t.spent, [] if len(rs) == 2 else [('fake', 0.25)])
        self.assertEqual([r.verdict for r in rs], ['probable-duplicate'] * 3)
        self.assertTrue(rs[1].agent['kept'])
        self.assertIn('judged the same evidence before', rs[1].notes[0])
        self.assertEqual(report.judge_costs(rs[:2]), {'fake': (0.25, 1)})

    def test_group_shares_one_judgement(self):
        t = Triage(problems(record(pattern='not in the log')), workdir=tempfile.mkdtemp())
        group = [t.run(job(id), files()) for id in ('1', '2', '3')]
        t.judge = FakeJudge({'verdict': 'new', 'problem': None, 'evidence': [],
                             'summary': 'something broke'})
        t.judge_group(group, files())
        self.assertEqual(len(t.judge.calls), 1)
        self.assertEqual([r.group for r in group], ['1', '1', '1'])
        self.assertEqual(group[2].agent['summary'], 'something broke')
        self.assertEqual(group[0].path, ['explain', 'prefilter', 'confirm', 'explained',
                                         'agent', 'verify-quotes'])
        self.assertEqual(group[1].path[-1], 'same-as-1')


if __name__ == '__main__':
    unittest.main()
