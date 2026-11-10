import datetime
import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from fakes import FakeJudge, RUN, job, problems, record

from qa_triage import db, report
from qa_triage import agents
from qa_triage.agents import (CascadeJudge, CommandJudge, JudgeError, Stopped,
                              extract_verdict, make_agent, make_judge)
from qa_triage.fingerprint import compact, fingerprint, template, to_regex
from qa_triage.fixcheck import fix_in_build, parse_version
from qa_triage.graph import Result
from qa_triage import cli, sources
from qa_triage.sources import (_ENTRY, find_qa_tracker, job_name, parse_run_page, pr_numbers,
                              run_name, size_bytes)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDatabase(unittest.TestCase):
    def test_shipped_records_are_valid(self):
        self.assertTrue(db.load(os.path.join(ROOT, 'library', 'problems')))

    def test_missing_confirm(self):
        r = record()
        del r['confirm']
        with self.assertRaisesRegex(ValueError, 'missing "confirm"'):
            db.parse(r)

    def test_a_check_of_the_date_a_line_was_logged(self):
        # Two problems are told apart by what happened, not by when.
        with self.assertRaisesRegex(ValueError, 'finds the time a line was logged'):
            db.parse(record(pattern=r'^2026-0\d-\d\dT'))
        for pattern in (r'\d{4}-\d\d-\d\dT.*FAILED', r'^\S+ INFO', r'osd\.\d+'):
            db.parse(record(pattern=pattern))

    def test_bad_regex(self):
        with self.assertRaisesRegex(ValueError, 'record'):
            db.parse(record(pattern='('))

    def test_duplicate_ids(self):
        with self.assertRaisesRegex(ValueError, 'duplicate problem ids'):
            problems(record('1'), dict(record('1'), tracker='x'))


class TestFingerprint(unittest.TestCase):
    def test_pgs(self):
        self.assertEqual(template('PG 3.bs0 marked LAGGY; acting [3,2147483647,2]'),
                         template('PG 2.0s0 marked LAGGY; acting [3,1,2]'))

    def test_hosts_and_numbers_masked(self):
        a = job(reason='Command failed on trial045 with status 2: osd pool create 64')
        b = job(reason='Command failed on smithi123 with status 2: osd pool create 128')
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_to_regex_matches_variants(self):
        regex = to_regex("Command failed on trial045 with status 2: 'k=4 m=2'")
        self.assertTrue(re.search(regex, "Command failed on trial155 with status 2: 'k=8 m=3'"))
        self.assertFalse(re.search(regex, "Command failed on trial155 with status 2: 'other'"))

    def test_compact(self):
        lines = [(1, '2026-10-13T02:00:00.000 a 10.0.0.1 failed'),
                 (2, '2026-10-13T02:00:01.000 a 10.0.0.2 failed'),
                 (3, '2026-10-13T02:00:02.000 b failed')]
        self.assertEqual(compact(lines), [
            '1: 2026-10-13T02:00:00.000 a 10.0.0.1 failed  (+1 similar)',
            '3: 2026-10-13T02:00:02.000 b failed'])

    def test_a_client_named_after_its_host(self):
        # A mgr's libcephfs session, named after the mgr: x, y or z.
        y = 'WRN mds evicting unresponsive client trial174:y (4828), after 300 seconds'
        z = 'WRN mds evicting unresponsive client trial150:z (4861), after 301 seconds'
        self.assertEqual(template(y), 'WRN mds evicting unresponsive client <host>:<name> (#), '
                                      'after # seconds')
        self.assertEqual(template(y), template(z))
        self.assertTrue(re.search(to_regex(y), z))
        # Not a host's other parts.
        self.assertEqual(template('trial153.stderr: ok; ubuntu@trial153:~$'),
                         '<host>.stderr: ok; ubuntu@<host>:~$')

    def test_uuid_masked_whole(self):
        self.assertEqual(template('id 3e40af3c-fdec-4412-ad6c-95e5ad2cf49a'), 'id <uuid>')


class TestFixCheck(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(parse_version('ceph-21.3.0-4162.gf64c8d74.el10'),
                         ('21.3.0', 4162, 'f64c8d74'))
        self.assertIsNone(parse_version('nothing here'))

    def test_older_build_cannot_have_fix(self):
        fix = db.Fix(pr='x', fixed_in='v21.3.0-4449-g05a7656731')
        self.assertFalse(fix_in_build(fix, ('21.3.0', 4162, 'f64c8d74')))
        self.assertIsNone(fix_in_build(fix, ('21.3.0', 5000, 'f64c8d74')))
        self.assertIsNone(fix_in_build(fix, ('22.0.0', 10, 'f64c8d74')))


class TestAgents(unittest.TestCase):
    VERDICT = {'verdict': 'new', 'problem': None, 'evidence': [], 'summary': 's'}

    def test_verdict_in_a_message(self):
        out = json.dumps({'type': 'result', 'stats': {'cost': 0.02},
                          'message': 'Here:\n```json\n' + json.dumps(self.VERDICT) +
                                          '\n```'})
        self.assertEqual(extract_verdict(out), self.VERDICT)


    def test_bob_output(self):
        out = json.dumps({'type': 'result', 'stats': {'session_costs': 0.02},
                          'last_message': 'Here:\n```json\n' + json.dumps(self.VERDICT) +
                                          '\n```'})
        self.assertEqual(extract_verdict(out), self.VERDICT)

    def test_claude_output(self):
        out = json.dumps({'type': 'result', 'result': 'done',
                          'structured_output': self.VERDICT})
        self.assertEqual(extract_verdict(out), self.VERDICT)

    def test_no_verdict(self):
        self.assertIsNone(extract_verdict('{"a": 1} and {not json'))

    def test_cascade_stops_when_sure(self):
        unsure = FakeJudge(dict(self.VERDICT, verdict='unsure'), 'cheap')
        sure = FakeJudge(self.VERDICT, 'strong')
        never = FakeJudge(self.VERDICT, 'never')
        verdict = CascadeJudge([unsure, sure, never])('/nonexistent', {})
        self.assertEqual(verdict['judge'], 'strong')
        self.assertEqual(never.calls, [])

    def test_make_judge(self):
        self.assertIsNone(make_judge('none'))
        self.assertEqual(make_judge('claude:haiku,goose+files').name, 'claude:haiku,goose+files')
        self.assertTrue(make_judge('goose+files').files)
        self.assertEqual(make_judge('cmd:my-agent --x a,b').argv, ['my-agent', '--x', 'a,b'])
        with self.assertRaises(ValueError):
            make_judge('nobody')

    def test_agent_error_is_shown(self):
        judge = make_judge("cmd:sh -c 'echo started; echo API key is required >&2; exit 1'")
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaisesRegex(JudgeError, r'\(exit 1\): API key is required;'):
                judge.ask(workdir, 'a prompt')

    def test_an_agent_that_ran_out_of_turns(self):
        class Agent(CommandJudge):
            resumes = True

            def session(self, output):
                return output['stats']['task_id']

            def cost(self, output):
                return output['stats']['cost']

        # It reports the error, then its session and cost, each on a line.
        agent = Agent(['sh', '-c', 'echo \'{"type": "error"}\'; '
                                   'echo \'{"stats": {"task_id": "t1", "cost": 2.5}}\''])
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaises(JudgeError) as caught:
                agent.ask(workdir, 'a prompt')
        self.assertEqual((caught.exception.session, caught.exception.cost), ('t1', 2.5))


    def test_a_resumed_bob_session(self):
        # Bob prints the turns before this one again first, stamped with the time
        # it prints them; this turn's prompt was asked in one of them, too.
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')
        lines = [{'type': 'message', 'role': 'user', 'content': 'the lesson'},
                 {'type': 'message', 'role': 'assistant', 'content': 'an old thought\n'},
                 {'type': 'tool_use', 'tool_name': 'grep', 'parameters': {'pattern': 'x'}},
                 {'type': 'message', 'role': 'user', 'content': 'your proposal'},
                 {'type': 'message', 'role': 'assistant', 'content': '{"old": 1}'},
                 {'type': 'message', 'role': 'user', 'content': 'your proposal'},
                 {'type': 'message', 'role': 'assistant', 'content': 'a new thought\n'},
                 {'type': 'tool_use', 'tool_name': 'read_file', 'parameters': {'path': 'y'}},
                 {'type': 'message', 'role': 'assistant', 'content': '{"new": 1}'},
                 {'type': 'result', 'stats': {'tool_calls': 2, 'session_costs': 3.5}}]
        bob, steps, written = make_judge('bob+files'), [], []
        with tempfile.TemporaryDirectory() as workdir:
            with open(os.path.join(workdir, 'out.json'), 'w') as f:
                f.writelines(json.dumps(dict(line, timestamp=now[:-3] + 'Z')) + '\n'
                             for line in lines)
            bob.command(workdir, 'chat', 'task-1')
            bob.progress, bob.written = steps.append, written
            out, _, _ = bob._run(['sh', '-c', 'cat > /dev/null; cat out.json'],
                                 'your proposal', workdir)
        self.assertNotIn('  thinks: an old thought', steps)
        self.assertNotIn('  grep: pattern=x', steps)
        self.assertEqual(steps[-3:], ['  thinks: a new thought', '  read_file: path=y',
                                      '  done: the session has made 2 tool calls and cost 3.50 Bob coins'])
        self.assertEqual(bob.answer_text(out), 'a new thought\n\n{"new": 1}')
        # What it wrote, as it is, of this turn alone: its answer too.
        self.assertEqual(''.join(written), 'a new thought\n\n  read_file: path=y\n{"new": 1}')
        # Each thread follows its own call of it.
        seen = []
        thread = threading.Thread(target=lambda: seen.append((bob._since, bob._resumed,
                                                              bob.written)))
        thread.start()
        thread.join()
        self.assertEqual(seen, [('', False, None)])
        # A session begun anew has nothing before it.
        bob.command(workdir, 'chat')
        self.assertEqual(bob.answer_text(out).count('thought'), 2)


    def test_bob_asks_again_when_its_filter_blocks(self):
        bob = make_judge('bob+files')
        with tempfile.TemporaryDirectory() as workdir:
            bob.command(workdir, 'chat')
            # Blocked the first time only.
            script = ('cat > /dev/null; if [ -e tried ]; then echo \'{"type": "message", '
                      '"role": "assistant", "content": "hello"}\'; else touch tried; '
                      'echo "Error: Request Blocked. This request was blocked by the content '
                      'filter." >&2; exit 1; fi')
            out, err, status = bob._run(['sh', '-c', script], 'a prompt', workdir)
        self.assertEqual((bob.answer_text(out), err, status), ('hello', '', 0))

    def test_a_bob_that_cannot_run(self):
        bob = make_judge('bob')
        with mock.patch.dict(os.environ, {'BOB_API_KEY': 'k'}), \
                mock.patch.object(agents.shutil, 'which', return_value='/bin/bob'), \
                mock.patch.dict(agents._versions, {'bob': (2, 0, 5)}):
            self.assertEqual(bob.missing(), 'bob: this Bob Shell is version 2.0.5, and '
                                            'qa_triage needs 2.0.6 or later: update it the way '
                                            'it was installed')
            agents._versions['bob'] = (2, 1)
            self.assertIsNone(bob.missing())
        for said, version in (('2.0.6\ncommit: 88346061a\n', (2, 0, 6)), ('1.0.12\n', (1, 0, 12)),
                              ('', None)):
            with mock.patch.dict(agents._versions, clear=True), \
                    mock.patch.object(agents.subprocess, 'run',
                                      return_value=mock.Mock(stdout=said)):
                self.assertEqual(agents.bob_version(), version)
        # What an older one, or one with a key it does not know, says, explained.
        self.assertIn('older than version 2, and has no bob run',
                      bob.trouble('Unknown arguments: format, workspace, trust, disable-mcp'))
        self.assertIn('which it says as well when it does not know the API key',
                      bob.trouble('Error: A license agreement is required. Please accept'))
        self.assertIsNone(bob.trouble('something else'))

    def test_a_person_stops_an_agent(self):
        with tempfile.TemporaryDirectory() as workdir:
            for streams in (False, True):
                agent = make_agent('cmd:sleep 30')
                agent.streams = streams
                threading.Timer(0.3, agent.stop).start()
                started = time.time()
                with self.assertRaises(Stopped):
                    agent.ask(workdir, 'a prompt')
                self.assertLess(time.time() - started, 10)
                # Nor is it asked again, until it may go.
                with self.assertRaises(Stopped):
                    agent.chat(workdir, 'a prompt')
                agent.go()
                agent.argv = ['echo', 'hello']
                self.assertEqual(agent.chat(workdir, 'a prompt')['text'], 'hello')
            # A cascade stops as a whole: the judge after it is not asked instead.
            cascade = CascadeJudge([make_agent('cmd:sleep 30'), make_agent('cmd:cat')])
            threading.Timer(0.3, cascade.stop).start()
            with self.assertRaises(Stopped):
                cascade(workdir, {'inline': 'a prompt', 'files': 'a prompt'})
            # And says it is stopped, for what it was asked for to stop with it.
            self.assertTrue(cascade.stopped)
            cascade.go()
            self.assertFalse(cascade.stopped)

    def test_lessons_at_once_each_hear_their_own_agent(self):
        # One agent, asked by lessons at once, says each one's steps to its own.
        agent = make_agent('cmd:cat')
        heard = {}

        def lesson(name):
            agent.progress = heard.setdefault(name, []).append
            time.sleep(0.05)
            agent.progress(f'{name} reads a log')

        threads = [threading.Thread(target=lesson, args=(name,)) for name in ('a', 'b')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(heard, {'a': ['a reads a log'], 'b': ['b reads a log']})
        self.assertIsNone(agent.progress)
        # And a person who stops it stops every lesson's.
        with tempfile.TemporaryDirectory() as workdir:
            agent = make_agent('cmd:sleep 30')
            stopped = []

            def ask():
                try:
                    agent.ask(workdir, 'a prompt')
                except Stopped:
                    stopped.append(True)

            threads = [threading.Thread(target=ask) for _ in range(2)]
            for t in threads:
                t.start()
            time.sleep(0.3)
            agent.stop()
            for t in threads:
                t.join(10)
            self.assertEqual(stopped, [True, True])

    def test_claude_keeps_its_sessions_inside_claude_code(self):
        # Run in a Claude Code session, its children keep no transcript unless told to:
        # then neither a later turn nor the person's talk could go on from one.
        from qa_triage.agents import ClaudeJudge
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_CHILD_SESSION': '1'}):
            env = ClaudeJudge('sonnet', files=True).env()
        self.assertEqual(env['CLAUDE_CODE_FORCE_SESSION_PERSISTENCE'], '1')
        self.assertEqual(env['CLAUDE_CODE_CHILD_SESSION'], '1')

    def test_claudes_cost_is_the_sessions(self):
        # Claude Code's total_cost_usd is what the whole session has cost so far, so a
        # lesson that resumes its session costs what its last turn says, not their sum.
        from qa_triage.agents import ClaudeJudge
        from qa_triage.learn import Doubt
        doubt = Doubt.__new__(Doubt)
        doubt.analyst, doubt.resume, doubt.resumed_cost = ClaudeJudge(files=True), None, 0
        doubt.costs = [('s1', 0.13), ('s1', 0.36), ('s1', 0.46)]
        self.assertAlmostEqual(doubt.cost(), 0.46)

    def test_a_refusal(self):
        from qa_triage.agents import ClaudeJudge
        said = json.dumps({'type': 'result', 'stop_reason': 'refusal', 'is_error': True,
                           'result': 'API Error: its safeguards flagged this message\nmore',
                           'session_id': 's1', 'total_cost_usd': 0.2})

        with tempfile.TemporaryDirectory() as workdir:
            with open(os.path.join(workdir, 'said.json'), 'w') as f:
                f.write(said + '\n')

            class Refusing(ClaudeJudge):
                def command(self, workdir, kind='verdict', session=None):
                    return ['cat', os.path.join(workdir, 'said.json')]

            for call in (lambda a: a.ask(workdir, 'a prompt', 'doubt'),
                         lambda a: a.chat(workdir, 'a prompt')):
                with self.assertRaises(JudgeError) as caught:
                    call(Refusing(files=True))
                e = caught.exception
                self.assertTrue(e.refused)
                self.assertEqual((e.session, e.cost), ('s1', 0.2))
                self.assertIn('refused to answer: API Error: its safeguards flagged this '
                              'message. Safety filters flag safe requests now and then', str(e))

    def test_the_agents_installed(self):
        # Each kind installed, with each model it offers; the others not.
        with mock.patch('qa_triage.agents.shutil.which',
                        side_effect=lambda name: '/bin/claude' if name == 'claude' else None):
            from qa_triage.agents import installed
            self.assertEqual(installed(), [('claude', 'Claude Code'),
                                           ('claude:haiku', 'Claude Code with haiku'),
                                           ('claude:opus', 'Claude Code with opus'),
                                           ('claude:sonnet', 'Claude Code with sonnet')])

    def test_doubt_sessions(self):
        resumed = make_judge('claude+files').command('/w', 'doubt', 'session-1')
        self.assertEqual(resumed[-2:], ['--resume', 'session-1'])
        bob = make_judge('bob+files').command('/w', 'doubt', 'task-1')
        self.assertEqual(bob[-2:], ['--resume', 'task-1'])
        self.assertEqual(bob[bob.index('--max-turns') + 1], '50')
        claude = make_judge('claude+files').command('/w', 'doubt')
        self.assertIn('"assessment"', claude[claude.index('--json-schema') + 1])
        self.assertNotIn('--resume', claude)
        # A person can go on with the session in Claude Code itself; not with a command.
        self.assertEqual(make_judge('claude:opus+files').interactive('/w', 'session-1'),
                         ['claude', '--resume', 'session-1', '--model', 'opus'])
        bob = make_judge('bob+files').interactive('/w', 't1')
        self.assertEqual((bob[:4], bob[-2:]), (['bob', 'chat', '--workspace', '/w'],
                                               ['--resume', 't1']))
        self.assertIn('--disable-mcp', bob)
        self.assertIsNone(make_judge('cmd:cat').interactive('/w', 'session-1'))
        self.assertEqual(extract_verdict('x {"assessment": "right"} y', 'assessment'),
                         {'assessment': 'right'})
        # An agent that prints its prompt prints the answer's schema too: not an
        # answer, which has a word for its key.
        schema_then_answer = (json.dumps({'properties': {'verdict': {'enum': ['new']}}}) + ' ' +
                              json.dumps({'verdict': 'new'}))
        self.assertEqual(extract_verdict(schema_then_answer), {'verdict': 'new'})
        self.assertIsNone(extract_verdict(json.dumps({'verdict': {'enum': ['new']}})))
        # And what it gives as evidence is quotes, or is left out.
        from qa_triage import pack
        self.assertEqual(pack.quotes({'evidence': {'type': 'array'}}), [])
        self.assertEqual(pack.quotes({'evidence': ['x', {'file': 'a', 'text': 'b'}]}),
                         [{'file': 'a', 'text': 'b'}])
        # Agents write regexes with escapes that JSON does not allow.
        self.assertEqual(extract_verdict(r'{"verdict": "new", "p": "a\'b \\S \S \d"}'),
                         {'verdict': 'new', 'p': r"a\'b \S \S \d"})


class TestSources(unittest.TestCase):
    def test_run_name(self):
        for arg in (RUN, f'https://pulpito.ceph.com/{RUN}/',
                    f'https://qa-proxy.ceph.com/teuthology/{RUN}/631086/teuthology.log',
                    f'/a/{RUN}/631086'):
            self.assertEqual(run_name(arg), RUN)
        with self.assertRaisesRegex(ValueError, 'names no run'):
            run_name('https://pulpito.ceph.com/?branch=wip-x')

    def test_filing_an_issue(self):
        import http.server
        import threading
        got = []

        class Tracker(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                got.append((self.path, self.headers['X-Redmine-API-Key'], json.loads(body)))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"issue": {"id": 82001}}')

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Tracker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            number = sources.file_issue('k1', 'rados', 'a subject', 'h1. text',
                                        url=f'http://127.0.0.1:{server.server_port}')
        finally:
            server.shutdown()
        self.assertEqual(number, '82001')
        self.assertEqual(got, [('/issues.json', 'k1', {'issue': {
            'project_id': 'rados', 'tracker_id': 1, 'subject': 'a subject',
            'description': 'h1. text'}})])
        # The key: the environment's, or a file's, plain or JSON.
        with tempfile.TemporaryDirectory() as d:
            plain, keys = os.path.join(d, 'key'), os.path.join(d, 'api_keys.json')
            with open(plain, 'w') as f:
                f.write('k2\n')
            with open(keys, 'w') as f:
                json.dump({'github_token': 'g', 'tracker.ceph.com_token': 'k3'}, f)
            self.assertEqual(sources.tracker_key(plain, {}), 'k2')
            self.assertEqual(sources.tracker_key(keys, {}), 'k3')
            self.assertEqual(sources.tracker_key(keys, {'REDMINE_API_KEY': 'k4'}), 'k4')
            self.assertIsNone(sources.tracker_key(os.path.join(d, 'none'), {}))

    def test_the_tracker_is_read_with_the_key_and_nothing_else_is(self):
        sent = {}

        def urlopen(req, timeout):
            sent[req.full_url] = req.get_header('X-redmine-api-key')
            return mock.MagicMock()

        with mock.patch('urllib.request.urlopen', urlopen):
            for key in (None, 'k1'):
                sources.use_tracker_key(key)
                sources._open(f'{sources.TRACKER}/issues/1.json?{key}')
                sources._open(f'{sources.PULPITO}/?{key}')
        sources.use_tracker_key(None)
        self.assertEqual(sent, {f'{sources.TRACKER}/issues/1.json?None': None,
                                f'{sources.PULPITO}/?None': None,
                                f'{sources.TRACKER}/issues/1.json?k1': 'k1',
                                f'{sources.PULPITO}/?k1': None})

    def test_the_runs_of_a_branch(self):
        branch = 'https://pulpito.ceph.com/?branch=wip-x'
        other = RUN.replace('23:32:34', '20:00:00')
        page = (f'<a href="/">pulpito</a><a href="/nodes/">nodes</a><tr class="danger">'
                f'<td><a href="/{RUN}">someone</a></td><td><a href="/{RUN}">x</a></td></tr>')
        with mock.patch.object(sources, '_get_text', return_value=page) as get:
            self.assertEqual(cli.the_run(branch), RUN)
            get.assert_called_once_with(branch)
            # A run's page with a query is the run's.
            self.assertEqual(cli.the_run(f'https://pulpito.ceph.com/{RUN}/?status=fail'), RUN)
        page += f'<tr><td><a href="/{other}/">them</a></td></tr>'
        with mock.patch.object(sources, '_get_text', return_value=page):
            self.assertEqual(cli.the_run(branch, lambda runs: runs[1]), other)
            with self.assertRaisesRegex(ValueError, f'lists 2 runs; give one of them:\n  {RUN}'):
                cli.the_run(branch, lambda runs: None)
        with mock.patch.object(sources, '_get_text', return_value='nothing'):
            with self.assertRaisesRegex(ValueError, 'lists no runs'):
                cli.the_run(branch)
        # A branch by its name alone: pulpito's page of it; a run's name is the run's.
        said = []
        with mock.patch.object(sources, '_get_text', return_value=page) as get:
            self.assertEqual(cli.the_run('wip-x', lambda runs: runs[0], said.append), RUN)
            get.assert_called_once_with(branch)
            self.assertEqual(cli.the_run(RUN), RUN)
            self.assertEqual(get.call_count, 1)
            with self.assertRaisesRegex(ValueError, 'branch wip-x has 2 runs; give one'):
                cli.the_run('wip-x')
        with mock.patch.object(sources, '_get_text', return_value=page.split('<tr><td>')[0]):
            self.assertEqual(cli.the_run('wip-x', say=said.append), RUN)
            self.assertEqual(said, [f'wip-x: {RUN}'])
        with mock.patch.object(sources, '_get_text', return_value='nothing'):
            with self.assertRaisesRegex(ValueError, 'wip-y is not a run\'s name, and pulpito '
                                                    'has no runs of a branch of that name'):
                cli.the_run('wip-y')

    def test_the_run_of_a_tracker_issue(self):
        rerun = RUN.replace('wip-test', 'wip-test2')
        batch = {'subject': 'wip-test', 'custom_fields': [{'name': 'QA Runs', 'value': 'wip-new'}],
                 'description': 'https://pulpito.ceph.com/?branch=wip-old',
                 'journals': [{'notes': '**New Branch:** https://pulpito.ceph.com/?branch=wip-new'
                               ' and https://pulpito.ceph.com/?branch=wip-old'}]}
        bug = {'subject': 'a bug', 'description': f'https://pulpito.ceph.com/{RUN}/631086/',
               'journals': [{'notes': 'Seen again: https://qa-proxy.ceph.com/teuthology/'
                                      f'{rerun.replace(":", "%3A")}/7/teuthology.log'}]}
        self.assertEqual(sources.issue_runs(batch), (['wip-new', 'wip-old'], []))
        self.assertEqual(sources.issue_runs(bug), ([], [rerun, RUN]))
        self.assertIsNone(sources.issue_number(RUN))
        url = 'https://tracker.ceph.com/issues/81636'
        pages = {f'{sources.PULPITO}/?branch=wip-old': f'<a href="/{RUN}/">x</a>',
                 f'{sources.PULPITO}/?branch=wip-new': ''}

        def get(issue):
            return lambda u: (json.dumps({'issue': issue}) if '/issues/81636.json' in u
                              else pages[u])
        said = []
        with mock.patch.object(sources, '_get_text', get(batch)):
            # The branch of the batch has no runs yet: the one before it does.
            self.assertEqual(cli.the_run(url, say=said.append), RUN)
            self.assertIn('wip-old, as wip-new has no runs yet', said[-1])
            pages[f'{sources.PULPITO}/?branch=wip-new'] = f'<a href="/{rerun}/">x</a>'
            self.assertEqual(cli.the_run(url + '#note-3', say=said.append), rerun)
            self.assertIn('of its branch wip-new', said[-1])
        with mock.patch.object(sources, '_get_text', get(bug)):
            self.assertEqual(cli.the_run(url, lambda runs: runs[1], said.append), RUN)
            with self.assertRaisesRegex(ValueError, 'points at 2 runs, of the runs it links'):
                cli.the_run(url, say=said.append)
        with mock.patch.object(sources, '_get_text', get({'subject': 'nothing'})):
            with self.assertRaisesRegex(ValueError, 'points at no run'):
                cli.the_run(url, say=said.append)

    def test_job_name(self):
        for arg in (f'{RUN}/631086', f'https://pulpito.ceph.com/{RUN}/631086/',
                    f'https://qa-proxy.ceph.com/teuthology/{RUN}/631086/teuthology.log'):
            self.assertEqual(job_name(arg), (RUN, '631086'))
        with self.assertRaises(ValueError):
            job_name(RUN)

    def test_parse_run_page(self):
        page = ('<tr class="job job_fail" data-toggle="collapse" data-target="#631086">'
                '<td data-title="Description">rados/basic/{a &amp; b}</td></tr>'
                '<tr class="tablesorter-childRow x"><td><p class="code-text">Command '
                'failed\n  with status 1</p></td></tr>')
        [j] = parse_run_page(RUN, page)
        self.assertEqual((j.id, j.status, j.description, j.reason),
                         ('631086', 'fail', 'rados/basic/{a & b}',
                          'Command failed with status 1'))

    def test_find_qa_tracker(self):
        branch = 'wip-x-testing-20261012.132505-main'
        issues = {
            branch: [],
            'wip-x-testing': [
                {'id': 1, 'custom_fields': [{'name': 'QA Runs',
                                             'value': 'wip-x-testing-20261002.1-main'}]},
                {'id': 2, 'custom_fields': [{'name': 'QA Runs', 'value': branch}],
                 'description': '"PR #71826":https://github.com/ceph/ceph/pull/71826 and '
                                'https://github.com/ceph/ceph/pull/72236'}]}
        with mock.patch.object(sources, '_issues',
                               lambda q: issues[q.rpartition('~')[2]]):
            issue = find_qa_tracker(branch)
        self.assertEqual(issue['id'], 2)
        self.assertEqual(pr_numbers(issue), {'71826', '72236'})

    def test_the_latest_list_of_prs(self):
        def link(*numbers):
            return ' '.join(f'https://github.com/ceph/ceph/pull/{n}' for n in numbers)
        # The latest list in a comment, the first in the description.
        issue = {'created_on': '2026-10-01', 'description': link(1, 2, 3, 4), 'journals': [
            {'created_on': '2026-10-02', 'notes': link(1, 2, 5, 6)},
            # A comment that names a PR in passing is no list.
            {'created_on': '2026-10-03', 'notes': f'{link(7)} broke it'}]}
        self.assertEqual(pr_numbers(issue), {'1', '2', '5', '6'})
        # The latest in the description, edited after the comment with the list before.
        issue['journals'].append({'created_on': '2026-10-04', 'notes': '',
                                  'details': [{'property': 'attr', 'name': 'description'}]})
        self.assertEqual(pr_numbers(issue), {'1', '2', '3', '4'})

    def test_listing(self):
        page = ('<pre><a href="../">../</a>\r\n'
                '<a href="trial004/">trial004/</a>     13-Oct-2026 03:37      -\r\n'
                '<a href="x.log.gz">x.log.gz</a>     13-Oct-2026 03:37   836088\r\n')
        self.assertEqual(_ENTRY.findall(page), [('trial004/', '-'), ('x.log.gz', '836088')])
        self.assertEqual(size_bytes('1.5M'), 3 << 19)


class TestReport(unittest.TestCase):
    def test_qa_comment(self):
        [p] = problems(record())
        results = [Result(job('1'), verdict='duplicate', problem=p),
                   Result(job('2'), verdict='duplicate', problem=p),
                   Result(job('3', reason='x' * 300), verdict='new', group='3'),
                   Result(job('4', reason='x' * 300), verdict='new', group='3')]
        text = report.qa_comment(RUN, results)
        self.assertIn("1. ['1', '2'] - https://tracker.ceph.com/issues/100 - problem 100", text)
        self.assertIn("1. ['3', '4'] - " + 'x' * 200 + '...', text)
        # A job that a PR under test may have failed is listed as that, and is not
        # called unrelated, whatever record it fits.
        results[1].suspects = [('PR #72810', 'qa/standalone/mon/misc.sh')]
        text = report.qa_comment(RUN, results)
        self.assertIn(report.SUSPECTED + "\n1. ['2'] - PR #72810 changes qa/standalone/mon/"
                      'misc.sh, which they ran; otherwise duplicate TR 100', text)
        self.assertIn("1. ['1'] - https://tracker.ceph.com/issues/100", text)
        wiki = report.wiki(RUN, results)
        self.assertIn("Possibly caused by PRs under test:\n# ['2'] - PR #72810 - changes "
                      'qa/standalone/mon/misc.sh, which they ran - [duplicate TR 100]', wiki)
        self.assertNotIn("['1', '2']", wiki)
        # An analyst that found a PR under test caused the jobs says so, first.
        results[2].blamed = results[3].blamed = [('PR #72555', 'the analyst (bob)',
                                                  'its log acknowledgements leak')]
        text = report.qa_comment(RUN, results)
        self.assertIn(report.SUSPECTED + "\n1. ['3', '4'] - PR #72555: the analyst (bob) found "
                      'that it caused them: its log acknowledgements leak\n2. [\'2\']', text)
        self.assertNotIn('x' * 200, text)
        self.assertIn("# ['3', '4'] - PR #72555 - the analyst (bob) found that it caused them: "
                      'its log acknowledgements leak', report.wiki(RUN, results))
        # The other issues people place the problem in are named, for the reviewer to
        # choose among: they may be duplicates.
        p.also = ['90', 'PR123']
        also = (' (also tracked as https://tracker.ceph.com/issues/90, '
                'https://github.com/ceph/ceph/pull/123: possibly the same problem)')
        self.assertIn("1. ['1'] - https://tracker.ceph.com/issues/100 - problem 100" + also,
                      report.qa_comment(RUN, results))
        self.assertIn("# ['1'] - https://tracker.ceph.com/issues/100 - problem 100" + also,
                      report.wiki(RUN, results))

    def test_draft_record(self):
        r = Result(job('3', reason='Command failed on trial1 with status 2'), verdict='new',
                   agent={'summary': 'it broke', 'suggested_checks': [
                       {'file': 'teuthology.log', 'pattern': 'broke'}]})
        text = report.draft_record(RUN, [r])
        self.assertIn('Command failed on \\S+ with status \\d+', text)
        self.assertIn(f'{RUN}/3', text)


if __name__ == '__main__':
    unittest.main()
