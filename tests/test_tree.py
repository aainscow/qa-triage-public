import json
import os
import tempfile
import unittest
from unittest import mock

import yaml

from fakes import FakeFiles, RUN, forget_learned, job, problems, record

from qa_triage import db, learn, report, tree
from qa_triage.graph import Triage, misses
from qa_triage.tree import core, learned
from qa_triage.tree.core import done, go, judge, node, unknown


def at(minute, text):
    return f'2026-10-12T17:{minute:02d}:00.000 {text}'


TASKS = [at(1, 'INFO:teuthology.run_tasks:Running task internal.base...'),
         at(2, 'INFO:teuthology.run_tasks:Running task install...'),
         at(3, 'INFO:teuthology.run_tasks:Running task ceph...'),
         at(4, 'INFO:teuthology.run_tasks:Running task rados...')]
OSD = 'INFO:tasks.ceph.osd.2.trial063.stderr:'
DUMP = ('Command failed on trial056 with status 124: \'sudo adjust-ulimits ceph-coverage '
        '/home/ubuntu/cephtest/archive/coverage timeout 120 ceph --cluster ceph osd dump '
        '--format=json\'')
CRASH = TASKS + [
    at(10, OSD + '/ceph/rpmbuild/BUILD/ceph-21.3.0/src/os/bluestore/BlueStore.cc: In function '
                 "'void BlueStore::_txc_add_transaction(TransContext*, ObjectStore::Transaction*)'"
                 ' thread 7f26 time 2026-10-12T17:10:00.000'),
    at(10, OSD + '/ceph/rpmbuild/BUILD/ceph-21.3.0/src/os/bluestore/BlueStore.cc: 16786: '
                 'ceph_abort_msg("unexpected error")'),
    at(10, OSD + ' 1: (ceph::__ceph_abort(char const*, int, char const*)+0xd3) [0x55]'),
    at(10, OSD + ' 2: (BlueStore::_txc_add_transaction(BlueStore::TransContext*, '
                 'ObjectStore::Transaction*)+0x1f1) [0x55]'),
    at(10, OSD + ' 3: (ECLegacy::ECBackendL::handle_sub_write(pg_shard_t, ECSubWrite&)+0xc0d)'
                 ' [0x55]'),
    at(10, OSD + '*** Caught signal (Aborted) **'),
    at(20, 'ERROR:teuthology.run_tasks:Manager failed: thrashosds'),
    'teuthology.exceptions.CommandFailedError: ' + DUMP]
OSD_LOG = ['... -1 bluestore(/var/lib/ceph/osd/ceph-2) _txc_add_transaction error (2) No such '
           'file or directory not handled on operation 15 (op 0, counting from 0)',
           '... 10 bluestore(/var/lib/ceph/osd/ceph-2) _txc_add_transaction op 15 got ENOENT on '
           '1#3:0d9e85f4:::obj:99#']
WORKUNIT = 'INFO:tasks.workunit.client.0.trial037.stderr:'


def explain(log, reason='', status='fail', description='rados/thrash/{x}', more=None):
    files = FakeFiles(dict({'teuthology.log': log} if log is not None else {}, **(more or {})))
    return tree.explain(job(reason=reason, status=status, description=description), files)


class TestCore(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(core.NODES, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_path_to_a_conclusion(self):
        node('', 'what?')(lambda ctx: go('a', 'it was a', key='key 12'))
        node('a', 'which a?')(lambda ctx: done('b', 'it was b', ctx.first('task'), fact=1))
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive, f.facts), ('a/b', True, {'fact': 1}))
        self.assertEqual(f.signature(job()), 'a/b | key #')
        self.assertEqual(f.lines()[:3], ['1. what? a: it was a', '2. which a? b: it was b',
                                         '   teuthology.log:1: ' + TASKS[0]])
        self.assertEqual(f.lines()[-2:], ['Conclusive: a/b.', 'Facts: fact=1'])

    def test_unsure_ends(self):
        node('', 'what?')(lambda ctx: go('a', 'it was a'))
        node('a', 'why?')(lambda ctx: judge('Why a?', 'the log does not say', branch='c'))
        f = explain(TASKS, reason='Command failed on trial1 with status 1')
        self.assertEqual((f.where, f.conclusive, f.question), ('a/c', False, 'Why a?'))
        # With no key, the failure reason groups it.
        self.assertEqual(f.signature(job(reason='status 1 on trial1')), 'a/c | status # on <host>')
        core.NODES['a'] = core.Node('a', 'why?', lambda ctx: unknown('no idea'))
        self.assertEqual(explain(TASKS).where, 'a')
        # A path that leads to no node, and a node that fails, end it unsure.
        core.NODES['a'] = core.Node('a', 'why?', lambda ctx: go('d', 'd'))
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive, f.steps[-1].says), ('a/d', False,
                                                                     'No node asks yet.'))
        core.NODES['a'] = core.Node('a', 'why?', lambda ctx: 1 / 0)
        f = explain(TASKS)
        self.assertIn('ZeroDivisionError', f.steps[-1].says)
        core.NODES['a'] = core.Node('a', 'why?', lambda ctx: go('a', 'again'))
        core.NODES['a/a'] = core.Node('a/a', 'why?', lambda ctx: go('a', 'again'))
        for depth in range(2, core.MAX_STEPS + 1):
            path = '/'.join(['a'] * depth)
            core.NODES[path] = core.Node(path, 'why?', lambda ctx: go('a', 'again'))
        self.assertFalse(explain(TASKS).conclusive)

    def test_the_tree_grows_where_it_is_unsure(self):
        node('', 'what?')(lambda ctx: judge('Why?', 'the log does not say', branch='c'))
        f = explain(TASKS)
        self.assertEqual((f.where, f.grows_at), ('c', 'c'))
        self.assertEqual(f.lines()[-1], 'No node at "c" answers it yet.')
        # A node added there answers it; one that cannot keeps the question.
        node('c', 'why c?')(lambda ctx: unknown('not one it knows'))
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive, f.question, f.grows_at), ('c', False, 'Why?', ''))
        core.NODES['c'] = core.Node('c', 'why c?', lambda ctx: done('d', 'it was d'))
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive, f.question), ('c/d', True, ''))

    def test_a_conclusive_answer_can_be_explained_further(self):
        node('', 'what?')(lambda ctx: done('symptom', 'the log has an error'))
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive, f.grows_at), ('symptom', True, 'symptom'))
        self.assertEqual(f.progress(), 'The tree explained it conclusively, answering 1 '
                         'question, at "symptom". A node at "symptom" would be asked to '
                         'explain more, such as the cause behind a symptom.')
        # A node there that does not know the job leaves the explanation as it was.
        node('symptom', 'why?')(lambda ctx: unknown('not one it knows'))
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive, len(f.steps)), ('symptom', True, 1))
        core.NODES['symptom'] = core.Node('symptom', 'why?',
                                          lambda ctx: done('cause', 'a race'))
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive, f.steps[-1].says), ('symptom/cause', True,
                                                                     'a race'))
        core.NODES['symptom'] = core.Node('symptom', 'why?', lambda ctx: 1 / 0)
        f = explain(TASKS)
        self.assertEqual((f.where, f.conclusive), ('symptom', True))
        self.assertIn('ZeroDivisionError', f.steps[-1].says)

    def test_progress_when_unsure(self):
        node('', 'what?')(lambda ctx: go('a', 'it was a'))
        node('a', 'why?')(lambda ctx: judge('Why a?', 'the log does not say', branch='c'))
        self.assertEqual(explain(TASKS).progress(), 'The tree answered 2 questions, to '
                         '"a/c", and stopped, inconclusive: it asks "Why a?". A node at "a/c" would '
                         'be asked next.')

    def test_the_reason_is_a_landmark(self):
        node('', 'where?')(lambda ctx: done('here', 'found', ctx.first('reason')))
        f = explain(CRASH, reason=DUMP)
        self.assertEqual(f.steps[0].evidence[0].line, len(CRASH))


class TestNodes(unittest.TestCase):
    def test_a_generic_abort_reads_the_daemons_log(self):
        f = explain(CRASH, reason=DUMP,
                    more={'remote/trial063/log/ceph-osd.2.log.gz': OSD_LOG})
        self.assertEqual(f.where, 'crash/abort/bluestore-error/error-2')
        self.assertTrue(f.conclusive)
        self.assertEqual((f.facts['daemon'], f.facts['op'], f.facts['error']),
                         ('osd.2', 'OP_SETATTRS', '2'))
        self.assertIn('ECLegacy::ECBackendL::handle_sub_write', f.facts['caller'])
        # The path keeps the error number; the signature masks it, as all numbers.
        self.assertIn('error # in OP_SETATTRS from ECLegacy', f.signature(job()))
        # Without the daemon's log, a judge must find the error.
        f = explain(CRASH, reason=DUMP)
        self.assertEqual((f.where, f.conclusive),
                         ('crash/abort/bluestore-error/unexplained', False))

    def test_a_test_that_crashes_daemons_on_purpose(self):
        log = TASKS + [
            at(5, 'INFO:tasks.ceph.osd.0.trial037.stderr:*** Caught signal (Aborted) **'),
            at(6, WORKUNIT + '++ ceph crash ls'), at(6, WORKUNIT + '++ wc -l'),
            at(6, WORKUNIT + "+ '[' 0 = 4 ']'"), at(6, WORKUNIT + '+ exit 1'),
            at(7, 'ERROR:teuthology.run_tasks:Saw exception from tasks.'),
            'teuthology.exceptions.CommandFailedError: Command failed (workunit test '
            'rados/test_crash.sh) on trial037 with status 1: \'cd -- /tmp\'']
        f = explain(log, reason='Command failed (workunit test rados/test_crash.sh) on trial037 '
                                'with status 1: \'cd -- /tmp\'',
                    description='rados/singleton/{all/test-crash}')
        self.assertEqual((f.where, f.facts['intended_crash']), ('workunit/set-e', 'osd.0'))
        self.assertIn('ceph crash ls; wc -l; [ 0 = 4 ]', f.keys[-1])

    def test_a_standalone_test(self):
        trace = 'INFO:tasks.workunit.client.0.trial042.stderr:/home/ubuntu/cephtest/' \
                'clone.client.0/qa/standalone/mon/misc.sh:'
        log = TASKS + [at(6, trace + '244: TEST_mon_features:  grep -q aes256k'),
                       at(6, trace + '245: TEST_mon_features:  return 1'),
                       at(6, trace + '31: run:  return 1'),
                       at(7, 'ERROR:teuthology.run_tasks:Saw exception from tasks.'),
                       'teuthology.exceptions.CommandFailedError: Command failed (workunit '
                       'test mon/misc.sh) on trial042 with status 1: \'cd\'']
        f = explain(log, reason='Command failed (workunit test mon/misc.sh) on trial042 with '
                                'status 1: \'cd\'')
        self.assertEqual((f.where, f.facts['test'], f.facts['check']),
                         ('workunit/standalone', 'TEST_mon_features', 'misc.sh:245'))
        self.assertIn('grep -q aes256k', f.steps[-1].evidence[0].text)

    def test_what_says_what_it_is(self):
        f = explain(TASKS, reason='Failed to fetch package version from https://shaman.ceph.com'
                                  '/api/search/?ref=wip-x')
        self.assertEqual((f.where, f.conclusive), ('infra/build', True))
        f = explain(TASKS, reason='"2026-10-20T21:07:17.747669+0000 mon.a (mon.0) 3815 : '
                                  'cluster [ERR] Health check failed: 1 filesystem is offline '
                                  '(MDS_ALL_DOWN)" in cluster log')
        self.assertEqual((f.where, f.facts['check'], f.keys), ('cluster-log/health',
                                                                'MDS_ALL_DOWN', ['ERR MDS_ALL_DOWN']))
        log = TASKS + [at(6, 'INFO:teuthology.orchestra.run.trial175.stderr:admin_socket: '
                             'exception: exception: timed out'),
                       'AssertionError: timeout expired in wait_for_all_osds_up']
        f = explain(log, reason='timeout expired in wait_for_all_osds_up')
        self.assertEqual((f.where, f.facts['wait']), ('timeout/admin-socket',
                                                      'wait_for_all_osds_up'))

    def test_a_command_and_what_it_said(self):
        command = 'sudo ceph-authtool /etc/ceph/ceph.keyring --import-keyring ' \
                  '/etc/ceph/ceph.client.0.keyring'
        reason = f"Command failed on trial193 with status 1: '{command}'"
        log = TASKS + [at(6, f'DEBUG:teuthology.orchestra.run.trial193:> {command}'),
                       at(6, 'INFO:teuthology.orchestra.run.trial193.stderr:error reading '
                             'file /etc/ceph/ceph.keyring: read error:(21) Is a directory'),
                       at(7, 'ERROR:teuthology.run_tasks:Saw exception from tasks.'),
                       f'teuthology.exceptions.CommandFailedError: {reason}']
        f = explain(log, reason=reason)
        self.assertEqual(f.where, 'command/error')
        self.assertEqual(f.facts['command'], 'ceph-authtool /etc/ceph/ceph.keyring '
                                             '--import-keyring /etc/ceph/ceph.client.0.keyring')
        self.assertTrue(f.facts['error'].startswith('error reading file'))
        # Nothing that looks like an error: a judge decides.
        f = explain(log[:-3] + log[-2:], reason=reason)
        self.assertEqual((f.where, f.conclusive), ('command/unexplained', False))
        self.assertEqual(f.grows_at, 'command/unexplained')

    def test_a_hang(self):
        run = 'DEBUG:teuthology.orchestra.run.trial069:> '
        log = TASKS + [at(5, 'INFO:teuthology.run_tasks:Running task cephadm.shell...'),
                       at(6, run + 'sudo /home/ubuntu/cephtest/cephadm shell -- bash -c \''),
                       at(6, run + 'set -ex'), at(6, run + 'HOST=trial069'),
                       at(6, run + 'ceph cephadm osd activate $HOST'),
                       at(6, run + 'while ! ceph orch ps | grep $OSD | grep running ; do '
                                   'sleep 5 ; done'),
                       at(6, run + "'"),
                       at(6, 'INFO:teuthology.orchestra.run.trial069.stderr:+ ceph orch ps'),
                       at(59, 'DEBUG:teuthology.exit:Got signal 15; running 1 handler...')]
        f = explain(log, reason='hit max job timeout', status='dead')
        self.assertEqual((f.where, f.conclusive), ('hung/command', False))
        self.assertEqual(f.facts['command'], 'ceph cephadm osd activate $HOST; while ! ceph '
                                             'orch ps | grep $OSD | grep running ; do sleep 5 ; '
                                             'done')
        self.assertEqual(f.question, 'Why did the cephadm.shell task hang?')

    def test_every_decision_cites_the_lines_it_was_decided_by(self):
        mds = ('2026-10-20T21:07:17.747669+0000 mon.a (mon.0) 3815 : cluster [ERR] Health '
               'check failed: 1 filesystem is offline (MDS_ALL_DOWN)')
        segv = TASKS + [at(10, OSD + '*** Caught signal (Segmentation fault) **'),
                        at(10, OSD + ' 1: (ceph::BackTrace::BackTrace(int)+0x10) [0x55]'),
                        at(10, OSD + ' 2: (PG::do_peering_event(PGPeeringEvent&)+0x2c) [0x55]'),
                        at(20, 'ERROR:teuthology.run_tasks:Manager failed: thrashosds')]
        asserted = TASKS + [at(10, OSD + '/ceph/src/osd/PG.cc: 123: FAILED ceph_assert(ok)'),
                            at(20, 'ERROR:teuthology.run_tasks:Manager failed: thrashosds')]
        for f, where in (
                (explain(TASKS + [at(30, 'INFO:teuthology.orchestra.run.trial117.stdout:' + mds),
                                  at(30, 'WARNING:tasks.cephadm:Found errors (ERR|WRN|SEC) in '
                                         'cluster log')],
                         reason=f'"{mds}" in cluster log'), 'cluster-log/health'),
                (explain(CRASH, reason=DUMP), 'crash/abort/bluestore-error/unexplained'),
                (explain(segv, reason='x'), 'crash/signal/backtrace'),
                (explain(asserted, reason='x'), 'crash/assert/specific'),
                (explain(TASKS, reason='', status='dead'), 'hung/no-command')):
            self.assertEqual(f.where, where)
            self.assertEqual(f.uncited(), [], where)
        # The cluster log's line, which the reason quotes, is what it cites.
        f = explain(TASKS + [at(30, 'INFO:teuthology.orchestra.run.trial117.stdout:' + mds)],
                    reason=f'"{mds}" in cluster log')
        self.assertEqual([h.line for s in f.steps for h in s.evidence], [5, 5])
        # Found where the cluster log was checked, after the last task began, it does
        # not make the failure that task's, such as an install's.
        f = explain(TASKS[:2] + [at(30, 'INFO:teuthology.orchestra.run.trial117.stdout:' + mds)],
                    reason=f'"{mds}" in cluster log')
        self.assertEqual((f.where, f.steps[0].evidence[0].line), ('cluster-log/health', 3))
        # The log keeps the cluster log's runs of spaces, which the reason has as one.
        f = explain(TASKS + [at(30, 'INFO:teuthology.orchestra.run.trial117.stdout:' +
                                mds.replace('[ERR] ', '[ERR]     '))],
                    reason=f'"{mds}" in cluster log')
        self.assertEqual(f.steps[0].evidence[0].line, 5)
        # The ceph task's check does not log it: then it is the cluster log's line.
        f = explain(TASKS, reason=f'"{mds}" in cluster log',
                    more={'remote/h1/log/ceph.log.gz': ['an earlier line', mds]})
        self.assertEqual([(h.file, h.line) for s in f.steps for h in s.evidence],
                         [('remote/h1/log/ceph.log.gz', 2)] * 2)
        # A dead job's last line is where it stopped.
        self.assertEqual(explain(TASKS, status='dead').steps[0].evidence[0].line, 4)

    def test_no_log(self):
        f = explain(None, reason='x')
        self.assertEqual((f.where, f.conclusive, f.steps[0].says),
                         ('', False, 'the job has no teuthology.log'))


class TestWording(unittest.TestCase):
    """How the tree's answers read: the library's wording, in place of the nodes' own."""

    def tearDown(self):
        core.WORDING.clear()

    def test_wording(self):
        files = FakeFiles({'teuthology.log': ['FAILED test']})
        before = core.explain(job(), files).steps[0].says
        core.WORDING.update(learned.wording(
            "'': {workunit: 'The {workunit} workunit failed, so its own log says why ({says}).'}\n"
            "workunit: {unexplained: 'Lost {nothing}'}\n"))
        steps = core.explain(job(), files).steps
        self.assertEqual(steps[0].says, f'The rados/test.sh workunit failed, so its own log says '
                                        f'why ({before}).')
        # A sentence that names a fact this job lacks is not used.
        self.assertNotIn('Lost', steps[1].says)
        for wrong in ('a: b', "x: {y: 'reads {self.__class__}'}", "x: {y: 'a {'}"):
            with self.assertRaises(learned.LearnedError):
                learned.wording(wrong)


class TestRecords(unittest.TestCase):
    def files(self):
        return FakeFiles({'teuthology.log': CRASH, 'orig.config.yaml': ['branch: x'],
                          'remote/trial063/log/ceph-osd.2.log.gz': OSD_LOG})

    def test_a_record_in_the_tree(self):
        r = record('300', tree='crash/abort/bluestore-error',
                   facts={'op': 'OP_SETATTRS', 'caller': 'handle_sub_write'})
        del r['match'], r['confirm']
        result = Triage(problems(r)).run(job(reason=DUMP), self.files())
        self.assertEqual((result.verdict, result.problem.id, result.sure),
                         ('duplicate', '300', True))
        r['facts']['op'] = 'OP_WRITE'
        [p] = problems(r)
        result = Triage([p]).run(job(reason=DUMP), self.files())
        self.assertEqual((result.verdict, result.sure), ('new', True))
        self.assertEqual(misses(p, result), ['fact op /OP_WRITE/ does not fit "OP_SETATTRS"'])
        with self.assertRaisesRegex(ValueError, 'missing "match"'):
            db.parse(dict(r, tree=''))
        with self.assertRaisesRegex(ValueError, '"facts" needs "tree"'):
            db.parse(dict(record('1'), facts={'op': 'x'}))

    def test_a_record_in_two_places(self):
        r = record('300', tree=['timeout/unexplained', 'crash/abort/bluestore-error/'],
                   facts={'op': 'OP_SETATTRS'})
        del r['match'], r['confirm']
        [p] = problems(r)
        self.assertEqual(p.trees, ('timeout/unexplained', 'crash/abort/bluestore-error'))
        result = Triage([p]).run(job(reason=DUMP), self.files())
        self.assertEqual((result.verdict, result.problem.id, result.sure),
                         ('duplicate', '300', True))
        [p] = problems(dict(r, tree=['timeout/unexplained', 'crash/segv']))
        result = Triage([p]).run(job(reason=DUMP), self.files())
        self.assertEqual(result.verdict, 'new')
        self.assertEqual(misses(p, result)[0], 'tree: the job\'s path is '
                         '"crash/abort/bluestore-error/error-2", not under '
                         '"timeout/unexplained" or "crash/segv"')
        with self.assertRaisesRegex(ValueError, '"tree" must be a path, or a list'):
            db.parse(dict(r, tree=['crash/abort', '']))

    def test_the_report_explains(self):
        out = tempfile.mkdtemp(prefix='qa_triage-report-')
        result = Triage([]).run(job(reason=DUMP), self.files())
        report.write(out, RUN, [result])
        with open(os.path.join(out, 'report.md')) as f:
            text = f.read()
        self.assertIn('## 1: new, matched by the tree', text)
        self.assertIn('Conclusive at `crash/abort/bluestore-error/error-2`.', text)
        # Each answer's lines, each linked to its file in the archive.
        self.assertIn(f'   - [`remote/trial063/log/ceph-osd.2.log.gz:2`](https://qa-proxy.ceph.com/'
                      f'teuthology/{RUN}/1/remote/trial063/log/ceph-osd.2.log.gz)\n'
                      '     ` ... 10 bluestore(/var/lib/ceph/osd/ceph-2)  `**` '
                      '_txc_add_transaction op 15 got ENOENT on 1#3:0d9e85f4:::obj:99# `**',
                      text)
        self.assertIn('So: **new**, matched: the tree is conclusive, and no record nearly fits.', text)
        self.assertIn('Decided by the records 0, the memory 0, the tree 1, the judge 0; unmatched 0. '
                      'Without AI: 1 of 1 (100%).', text)
        with open(os.path.join(out, 'drafts', 'records', '1.yaml')) as f:
            draft = yaml.safe_load(f)
        self.assertEqual(draft['tree'], 'crash/abort/bluestore-error/error-2')
        self.assertEqual(draft['facts']['op'], 'OP_SETATTRS')
        self.assertNotIn('confirm', draft)
        with open(os.path.join(out, 'results.json')) as f:
            [saved] = json.load(f)
        self.assertEqual((saved['sure'], saved['tree']['path']),
                         (True, 'crash/abort/bluestore-error/error-2'))
        # It says its run, whatever its directory is called.
        path = os.path.join(out, 'results.json')
        self.assertEqual([j.path for j in learn.load_corpus([path])], [f'{RUN}/1'])
        self.assertEqual(list(learn.load_history([path])), [f'{RUN}/1'])


if __name__ == '__main__':
    unittest.main()


ODD = TASKS + [at(5, 'INFO:tasks.rados:an odd thing here')]
GOOD = """import re

from qa_triage.tree.core import done, node, unknown

EXAMPLES = {'run/1': 'other/odd'}


@node('other', 'What else went wrong?')
def other(ctx):
    found = ctx.search('teuthology.log', r'odd thing (\\w+)', 'odd thing')
    if found:
        return done('odd', 'an odd thing happened', found[0], key='odd',
                    what=re.search(r'odd thing (\\w+)', found[0].text).group(1))
    return unknown('not one it knows')
"""


class TestLearned(unittest.TestCase):
    def setUp(self):
        self.addCleanup(forget_learned)

    def test_what_a_learned_file_may_not_do(self):
        self.assertEqual(learned.check('good.py', GOOD), [])
        for code, says in (('import os', 'may not import os'),
                           ('from os import path', 'may not import from os'),
                           ('from .core import node', 'may not import from .core'),
                           ('from qa_triage.tree.core import NODES', 'may not import NODES'),
                           ('open("x")', 'may not use open'),
                           ('getattr(x, "y")', 'may not use getattr'),
                           ('x = ctx.files', 'the attribute files'),
                           ('x.__class__', 'the attribute __class__'),
                           ('"{0.x}".format(1)', 'the attribute format'),
                           ('ctx.job = 1', 'may not set attributes'),
                           ('def f():\n    global x', 'global')):
            self.assertIn(says, ' '.join(learned.check('bad.py', code)), code)
        self.assertIn('name a node file', learned.check('Bad Name.py', GOOD)[0])
        self.assertIn('bad.py:1:', learned.check('bad.py', 'def (')[0])

    def test_trying_a_learned_file(self):
        nodes = dict(core.NODES)
        f = explain(ODD, reason='something odd')
        self.assertEqual((f.where, f.grows_at), ('other', 'other'))
        with learned.trying({'other.py': GOOD}) as [loaded]:
            self.assertEqual((loaded.paths, loaded.examples), (['other'], {'run/1': 'other/odd'}))
            f = explain(ODD, reason='something odd')
            self.assertEqual((f.where, f.conclusive, f.facts['what']), ('other/odd', True, 'here'))
            # A failure it does not know stays unsure.
            self.assertEqual(explain(TASKS, reason='something odd').where, 'other')
        self.assertEqual(core.NODES, nodes)
        for code, says in ((GOOD.replace("@node('other'", "@node(''"), 'two nodes at ""'),
                           (GOOD.replace('EXAMPLES', 'SAMPLES'), 'give EXAMPLES'),
                           ('EXAMPLES = {"a": "b"}', 'registers no node'),
                           (GOOD.replace('EXAMPLES =', 're.compile("(")\nEXAMPLES ='),
                            'other.py: ')):
            with self.assertRaisesRegex(learned.LearnedError, says):
                with learned.trying({'other.py': code}):
                    pass
            self.assertEqual(core.NODES, nodes)

    def test_loading_the_learned_files(self):
        directory = tempfile.mkdtemp(prefix='qa_triage-nodes-')
        with open(os.path.join(directory, 'other.py'), 'w') as f:
            f.write(GOOD)
        [loaded] = learned.load(directory)
        self.assertEqual(learned.load(directory), [loaded])
        self.assertEqual(explain(ODD, reason='something odd').where, 'other/odd')
        with open(os.path.join(directory, 'other.py'), 'w') as f:
            f.write(GOOD.replace("done('odd'", "done('strange'"))
        learned.load(directory)
        self.assertEqual(explain(ODD, reason='something odd').where, 'other/strange')
        self.assertEqual(learned.load(os.path.join(directory, 'none')), [])
