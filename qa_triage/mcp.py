"""The evidence tools (tools.py) as an MCP server on stdin and stdout, for agents
that take their tools from MCP servers, such as goose:

    python3 -m qa_triage mcp <evidence directory>
"""

import json
import sys

from .tools import TOOLS, Tools

PROTOCOL = '2025-06-18'


def serve(root, stdin=sys.stdin, stdout=sys.stdout):
    tools = Tools(root)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {'id': None, 'error': {'code': -32700, 'message': 'not JSON'}}
        else:
            if 'id' not in msg:
                continue
            reply = {'id': msg['id']}
            reply.update(_handle(tools, msg.get('method'), msg.get('params') or {}))
        stdout.write(json.dumps(dict(reply, jsonrpc='2.0')) + '\n')
        stdout.flush()


def _handle(tools, method, params):
    if method == 'initialize':
        return {'result': {'protocolVersion': params.get('protocolVersion', PROTOCOL),
                           'capabilities': {'tools': {}},
                           'serverInfo': {'name': 'qa_triage', 'version': '1'}}}
    if method == 'ping':
        return {'result': {}}
    if method == 'tools/list':
        return {'result': {'tools': [{'name': t['name'], 'description': t['description'],
                                      'inputSchema': t['parameters']} for t in TOOLS]}}
    if method == 'tools/call':
        text = tools.call(params.get('name'), params.get('arguments') or {})
        return {'result': {'content': [{'type': 'text', 'text': text}],
                           'isError': text.startswith('error: ')}}
    return {'error': {'code': -32601, 'message': f'no method {method}'}}
