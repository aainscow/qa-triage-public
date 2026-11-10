import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qa_triage.agents import JudgeError, make_agent
from qa_triage.chat import LAST_TURN, NO_ANSWER
from qa_triage.tools import Tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERDICT = {'verdict': 'new', 'problem': None, 'summary': 's', 'evidence': []}


class FakeServer:
    """An HTTP server that gives scripted replies, and keeps the requests."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                server.requests.append((self.path, dict(self.headers), body))
                data = json.dumps(server.replies.pop(0)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def openai_reply(content='', calls=(), usage=None):
    msg = {'role': 'assistant', 'content': content}
    if calls:
        msg['tool_calls'] = [{'id': f'c{i}', 'type': 'function',
                              'function': {'name': name, 'arguments': json.dumps(args)}}
                             for i, (name, args) in enumerate(calls)]
    return {'choices': [{'message': msg}], 'usage': usage or {}}


class TestChat(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='qa_triage-chat-')
        with open(os.path.join(self.workdir, 'teuthology.log'), 'w') as f:
            f.write('started\nFAILED: assert backfillfull < 0.9\nended\n')
        os.environ['QA_TRIAGE_TEST_KEY'] = 'k'

    def serve(self, replies):
        server = FakeServer(replies)
        self.addCleanup(server.close)
        return server

    def test_openai_with_tools(self):
        server = self.serve([
            openai_reply(calls=[('grep', {'regex': 'FAILED'})],
                         usage={'prompt_tokens': 1000, 'completion_tokens': 10}),
            openai_reply(json.dumps(VERDICT),
                         usage={'prompt_tokens': 2000, 'completion_tokens': 100})])
        agent = make_agent('a', {'a': {'provider': 'openai', 'model': 'm', 'files': True,
                                       'url': server.url + '/v1', 'price': {'input': 1, 'output': 10},
                                       'api_key_env': 'QA_TRIAGE_TEST_KEY'}})
        answer = agent.ask(self.workdir, 'judge this')
        self.assertEqual((answer['verdict'], answer['judge']), ('new', 'a'))
        self.assertAlmostEqual(answer['cost'], (3000 + 1100) / 1e6)
        path, headers, body = server.requests[1]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer k')
        self.assertEqual(body['messages'][-1], {
            'role': 'tool', 'tool_call_id': 'c0',
            'content': 'teuthology.log:2: FAILED: assert backfillfull < 0.9'})
        # A later turn continues the conversation.
        server.replies.append(openai_reply(json.dumps(VERDICT)))
        agent.ask(self.workdir, 'and now?', session=answer['session'])
        self.assertEqual(len(server.requests[2][2]['messages']), 5)

    def test_openai_inline_asks_for_the_schema(self):
        server = self.serve([openai_reply('Here: ' + json.dumps(VERDICT))])
        agent = make_agent(f'openai:m@{server.url}/v1')
        agent.api_key_env = ''
        self.assertEqual(agent.ask(self.workdir, 'judge this')['verdict'], 'new')
        body = server.requests[0][2]
        self.assertEqual(body['response_format']['json_schema']['name'], 'verdict')
        self.assertNotIn('tools', body)

    def test_tool_calls_are_limited(self):
        call = openai_reply(calls=[('read_file', {'path': 'teuthology.log'})])
        server = self.serve([call, call, openai_reply(json.dumps(VERDICT))])
        agent = make_agent('a', {'a': {'provider': 'openai', 'model': 'm', 'files': True,
                                       'url': server.url, 'max_turns': 2, 'api_key_env': ''}})
        agent.ask(self.workdir, 'judge this')
        last = server.requests[2][2]
        self.assertEqual(last['messages'][-1]['content'], LAST_TURN)
        self.assertNotIn('tools', last)

    def test_answer_given_as_a_tool_call(self):
        server = self.serve([openai_reply(calls=[('verdict', VERDICT)])])
        agent = make_agent('a', {'a': {'provider': 'openai', 'model': 'm', 'files': True,
                                       'url': server.url, 'api_key_env': ''}})
        self.assertEqual(agent.ask(self.workdir, 'judge this')['summary'], 's')
        self.assertEqual(len(server.requests), 1)

    def test_no_answer_after_tools_is_asked_for_again(self):
        server = self.serve([openai_reply(calls=[('grep', {'regex': 'FAILED'})]),
                             openai_reply('I looked at the log.'),
                             openai_reply(json.dumps(VERDICT))])
        agent = make_agent('a', {'a': {'provider': 'openai', 'model': 'm', 'files': True,
                                       'url': server.url, 'api_key_env': ''}})
        self.assertEqual(agent.ask(self.workdir, 'judge this')['verdict'], 'new')
        last = server.requests[2][2]
        self.assertEqual(last['messages'][-1]['content'], NO_ANSWER)
        self.assertNotIn('tools', last)
        self.assertEqual(last['response_format']['json_schema']['name'], 'verdict')

    def test_ollama(self):
        server = self.serve([
            {'message': {'role': 'assistant', 'content': '', 'thinking': 'hmm',
                         'tool_calls': [{'function': {'name': 'list_files',
                                                      'arguments': {'pattern': '*.log'}}}]}},
            {'message': {'role': 'assistant', 'content': json.dumps(VERDICT)}}])
        agent = make_agent('g', {'g': {'provider': 'ollama', 'model': 'gemma4', 'files': True,
                                       'url': server.url, 'context': 4096}})
        answer = agent.ask(self.workdir, 'judge this')
        self.assertNotIn('cost', answer)
        path, _, body = server.requests[1]
        self.assertEqual((path, body['options'], body['stream'], body['think']),
                         ('/api/chat', {'num_ctx': 4096}, False, False))
        self.assertEqual(body['messages'][-1], {'role': 'tool', 'tool_name': 'list_files',
                                                'content': 'teuthology.log 48'})
        # Its thinking is kept in the .out file, but not sent back.
        self.assertNotIn('thinking', body['messages'][1])
        with open(os.path.join(self.workdir, 'judge-g.out')) as f:
            self.assertEqual(json.load(f)[1]['thinking'], 'hmm')

    def test_no_answer(self):
        server = self.serve([{'message': {'role': 'assistant', 'content': 'no idea'}},
                             {'message': {'role': 'assistant', 'content': ''},
                              'done_reason': 'length'}])
        agent = make_agent('g', {'g': {'provider': 'ollama', 'model': 'g', 'url': server.url,
                                       'think': 'low'}})
        with self.assertRaisesRegex(JudgeError, 'gave no answer; see'):
            agent.ask(self.workdir, 'judge this')
        with self.assertRaisesRegex(JudgeError, 'gave no answer \\(it ran out of context\\)'):
            agent.ask(self.workdir, 'judge this')
        self.assertEqual(server.requests[0][2]['think'], 'low')

    def test_big_tool_results_are_cut_and_dropped(self):
        for name, text in (('job.yaml', 'job: 1\n'), ('big.log', ('x' * 1000 + '\n') * 3)):
            with open(os.path.join(self.workdir, name), 'w') as f:
                f.write(text)

        def read(path):
            return {'message': {'role': 'assistant', 'content': '',
                                'tool_calls': [{'function': {'name': 'read_file',
                                                             'arguments': {'path': path}}}]}}
        server = self.serve([read('job.yaml'), read('big.log'), read('big.log'),
                             {'message': {'role': 'assistant', 'content': json.dumps(VERDICT)}}])
        # 10000 tokens: a result is cut at 2300 characters, and a prompt of 13000
        # leaves room for one of them, not two.
        agent = make_agent('g', {'g': {'provider': 'ollama', 'model': 'g', 'files': True,
                                       'url': server.url, 'context': 10000}})
        agent.ask(self.workdir, 'p' * 13000)
        tools = [m['content'] for m in server.requests[3][2]['messages'] if m['role'] == 'tool']
        self.assertEqual(tools[0], '1: job: 1')
        self.assertTrue(tools[1].startswith('[qa_triage dropped this result of 2377 '))
        self.assertTrue(tools[2].startswith('1: xxx'))
        self.assertIn('cut at 2300 characters', tools[2])

    def test_a_repeated_call_is_dropped_first(self):
        for name, text in (('small.log', ('x' * 1000 + '\n') * 2),
                           ('big.log', ('y' * 1000 + '\n') * 3)):
            with open(os.path.join(self.workdir, name), 'w') as f:
                f.write(text)

        def read(path):
            return {'message': {'role': 'assistant', 'content': '',
                                'tool_calls': [{'function': {'name': 'read_file',
                                                             'arguments': {'path': path}}}]}}
        server = self.serve([read('small.log'), read('big.log'), read('small.log'),
                             {'message': {'role': 'assistant', 'content': json.dumps(VERDICT)}}])
        agent = make_agent('g', {'g': {'provider': 'ollama', 'model': 'g', 'files': True,
                                       'url': server.url, 'context': 10000}})
        agent.ask(self.workdir, 'p' * 11000)
        tools = [m['content'] for m in server.requests[3][2]['messages'] if m['role'] == 'tool']
        # The first read of small.log goes, though big.log's result is bigger.
        self.assertTrue(tools[0].startswith('[qa_triage dropped this result of 2007 '))
        self.assertEqual([t[:6] for t in tools[1:]], ['1: yyy', '1: xxx'])


class TestTools(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='qa_triage-tools-')
        os.makedirs(os.path.join(self.root, 'requested'))
        for name, text in (('a.log', 'one\ntwo\nthree\n'), ('requested/1-1.txt', 'two\n')):
            with open(os.path.join(self.root, name), 'w') as f:
                f.write(text)
        self.tools = Tools(self.root)

    def test_tools(self):
        self.assertEqual(self.tools.call('list_files', {'pattern': 'a*'}), 'a.log 14')
        self.assertEqual(self.tools.call('grep', {'regex': 'two'}),
                         'a.log:2: two\nrequested/1-1.txt:1: two')
        self.assertEqual(self.tools.call('read_file', {'path': 'a.log', 'offset': 2,
                                                       'limit': 1}),
                         '2: two\n... more from line 3')
        self.assertEqual(self.tools.call('read_file', {'path': 'a.log', 'offset': 9}),
                         'a.log has no line 9: it has 3 lines')
        self.assertEqual(self.tools.call('read_file', {'path': 'ansible.log'}),
                         'error: there is no ansible.log; list_files shows the files there are')
        self.assertIn('outside', self.tools.call('read_file', {'path': '../x'}))
        self.assertIn('error', self.tools.call('rm', {}))
        with open(os.path.join(self.root, 'big.log'), 'w') as f:
            f.write('x' * 1500 + '\n' * 1 + ('y' * 1500 + '\n') * 399)
        text = self.tools.call('read_file', {'path': 'big.log'})
        self.assertTrue(text.endswith('cut at 20000 characters: ask for fewer lines, or a '
                                      'narrower regex or glob'))

    def test_linked_source(self):
        source = tempfile.mkdtemp(prefix='qa_triage-source-')
        os.makedirs(os.path.join(source, 'src', 'osd'))
        with open(os.path.join(source, 'src', 'osd', 'PG.cc'), 'w') as f:
            f.write('void PG::start() {}\n')
        os.symlink(source, os.path.join(self.root, 'source'))
        # Only a glob that names it reaches into it.
        self.assertNotIn('PG.cc', self.tools.call('list_files', {}))
        self.assertEqual(self.tools.call('list_files', {'pattern': 'source/**'}),
                         'source/src/osd/PG.cc 20')
        self.assertEqual(self.tools.call('grep', {'regex': 'start', 'path': 'source/**/*.cc'}),
                         'source/src/osd/PG.cc:1: void PG::start() {}')
        self.assertEqual(self.tools.call('read_file', {'path': 'source/src/osd/PG.cc'}),
                         '1: void PG::start() {}')
        self.assertIn('outside', self.tools.call('read_file', {'path': 'source/../../x'}))

    def test_a_carriage_return_does_not_end_a_line(self):
        with open(os.path.join(self.root, 'cr.log'), 'w', newline='') as f:
            f.write('progress 1%\rprogress 100%\nFAILED\n')
        self.assertEqual(self.tools.call('grep', {'regex': 'FAILED', 'path': 'cr.log'}),
                         'cr.log:2: FAILED')
        self.assertEqual(self.tools.call('read_file', {'path': 'cr.log', 'offset': 2}),
                         '2: FAILED')

    def test_mcp(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                     'params': {'protocolVersion': '2025-03-26'}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                     'params': {'name': 'grep', 'arguments': {'regex': 'thr', 'path': '*.log'}}}]
        p = subprocess.run([sys.executable, '-m', 'qa_triage', 'mcp', self.root],
                           input=''.join(json.dumps(r) + '\n' for r in requests),
                           capture_output=True, text=True, cwd=ROOT, timeout=60)
        replies = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-03-26')
        self.assertEqual([t['name'] for t in replies[1]['result']['tools']],
                         ['list_files', 'read_file', 'grep'])
        self.assertEqual(replies[2]['result']['content'][0]['text'], 'a.log:3: three')


if __name__ == '__main__':
    unittest.main()
