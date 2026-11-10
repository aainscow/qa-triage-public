"""Agents that qa_triage talks to over HTTP: a model served by Ollama, or by
anything with an OpenAI-compatible chat completions API (OpenAI, vLLM,
llama.cpp's server, LM Studio, LiteLLM, OpenRouter and others).

An inline judge gets one call, with the answer's JSON schema as the response
format where the server supports it. One that reads files gets the read-only
evidence tools of tools.py, which qa_triage runs itself, for up to max_turns
rounds of tool calls. The conversation is kept here, so a later turn of a doubt
continues it. If the agent's context is known, each tool result is kept to a
share of it, and earlier results are dropped from the conversation before it
outgrows it.
"""

import json
import os
import urllib.error
import urllib.request

from .agents import KEYS, JudgeError, Stoppable, extract_verdict, schema
from .tools import MAX_OUTPUT, TOOLS, Tools

LAST_TURN = 'You have used all your tool calls: reply now with only the JSON object.'
NO_ANSWER = 'Your reply had no JSON object in it: reply now with only the JSON object.'
# Logs are dense: about 2.3 characters a token for Gemma 4 on teuthology logs.
CHARS_PER_TOKEN = 2.3
# The share of the context the conversation may fill, leaving the rest for the
# tool definitions and the reply, and the share one tool result may fill.
PROMPT_SHARE = 0.7
RESULT_SHARE = 0.1
DROPPED = ('[qa_triage dropped this result of {} characters to keep the conversation '
           'within the context; call the tool again if you still need it]')
# Keys of a message that are kept for the .out file, and not sent back.
NOTES = ('thinking', 'done_reason')


class ChatAgent(Stoppable):
    resumes = True
    cumulative_cost = False
    # What the costs it reports are in: it reports none unless it has a price.
    unit = 'dollars'
    # It has no program of its own for people to talk with it in.
    program = None

    def interactive(self, workdir, session):
        return None

    def __init__(self, model, url, files=False, max_turns=30, timeout=900, name=None,
                 context=None):
        self.model, self.url, self.files = model, url.rstrip('/'), files
        self.max_turns, self.timeout, self.context = max_turns, timeout, context
        self.name = name or f'{self.provider}:{model}' + ('+files' if files else '')
        self.sessions = {}

    def __call__(self, workdir, prompts):
        return self.ask(workdir, prompts['files' if self.files else 'inline'])

    def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
        workdir = os.path.abspath(workdir)
        tools = Tools(workdir, self._result_limit()) if self.files else None
        messages = list(self.sessions.get(session, [])) + [{'role': 'user', 'content': prompt}]
        costs, left, reminded = [], self.max_turns, False
        try:
            while True:
                self._stop_here()
                use_tools = tools is not None and left > 0
                self._fit(messages)
                message, calls, cost = self._chat([_sent(m) for m in messages], kind,
                                                  use_tools)
                costs.append(cost)
                # A small model may give its answer as the arguments of a call to a
                # tool named after it.
                given = next((args for _, _, args in calls if KEYS[kind] in args), None)
                if given is not None:
                    message = {k: v for k, v in message.items() if k != 'tool_calls'}
                    message['content'], calls = json.dumps(given), []
                messages.append(message)
                if use_tools and calls:
                    left -= 1
                    for call_id, name, args in calls:
                        messages.append(self._tool_message(call_id, name,
                                                           tools.call(name, args)))
                    if not left:
                        messages.append({'role': 'user', 'content': LAST_TURN})
                    continue
                if use_tools and not reminded and \
                        extract_verdict(message.get('content') or '', KEYS[kind]) is None:
                    # It stopped calling tools without answering: ask once more,
                    # without tools, so that the server holds it to the schema.
                    reminded, left = True, 0
                    messages.append({'role': 'user', 'content': NO_ANSWER})
                    continue
                break
        finally:
            out = os.path.join(workdir, (tag or 'judge-' + self.name.replace(':', '-')) + '.out')
            with open(out, 'w') as f:
                json.dump(messages, f, indent=1)
        answer = extract_verdict(message.get('content') or '', KEYS[kind])
        if answer is None:
            why = ' (it ran out of context)' if message.get('done_reason') == 'length' else ''
            raise JudgeError(f'{self.name} gave no answer{why}; see {out}')
        session = f'{self.name}/{len(self.sessions) + 1}'
        self.sessions[session] = messages
        answer.update(judge=self.name, session=session)
        if None not in costs:
            answer['cost'] = sum(costs)
        return answer

    def chat(self, workdir, prompt, session=None, tag=None):
        """A turn of a conversation: the reply as text, which is not checked for an
        answer; {'text', 'session', 'cost'}. It reads the files in workdir if it
        reads files."""
        workdir = os.path.abspath(workdir)
        tools = Tools(workdir, self._result_limit()) if self.files else None
        messages = list(self.sessions.get(session, [])) + [{'role': 'user', 'content': prompt}]
        costs, left = [], self.max_turns
        try:
            while True:
                self._stop_here()
                use_tools = tools is not None and left > 0
                self._fit(messages)
                message, calls, cost = self._chat([_sent(m) for m in messages], 'chat',
                                                  use_tools)
                costs.append(cost)
                messages.append(message)
                if not (use_tools and calls):
                    break
                left -= 1
                for call_id, name, args in calls:
                    if getattr(self, 'progress', None):
                        self.progress(f'  {name}: {args}')
                    messages.append(self._tool_message(call_id, name, tools.call(name, args)))
        finally:
            with open(os.path.join(workdir, (tag or 'chat') + '.out'), 'w') as f:
                json.dump(messages, f, indent=1)
        session = f'{self.name}/{len(self.sessions) + 1}'
        self.sessions[session] = messages
        return {'text': message.get('content') or '', 'session': session,
                'cost': None if None in costs else sum(costs)}

    def _result_limit(self):
        """The most characters one tool result may have."""
        if not self.context:
            return MAX_OUTPUT
        return min(MAX_OUTPUT, int(self.context * RESULT_SHARE * CHARS_PER_TOKEN))

    def _fit(self, messages):
        """Drop tool results, but never the latest, until the conversation fits
        PROMPT_SHARE of the context: first those of a call that was made again
        later, which lose nothing, then the biggest. Small results, such as the
        job's description, are kept: a model that loses them reads them again."""
        if not self.context:
            return
        limit = self.context * PROMPT_SHARE * CHARS_PER_TOKEN
        size = sum(len(m.get('content') or '') for m in messages)
        results = _results(messages)
        seen, repeated = set(), []
        for call, m in reversed(results):
            if call in seen:
                repeated.append(m)
            seen.add(call)
        rest = [m for _, m in results[:-1] if not any(m is r for r in repeated)]
        for m in repeated + sorted(rest, key=lambda m: len(m['content']), reverse=True):
            if size <= limit:
                break
            stub = DROPPED.format(len(m['content']))
            if len(m['content']) > len(stub):
                size -= len(m['content']) - len(stub)
                m['content'] = stub

    def _post(self, path, body, headers=()):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers=dict({'Content-Type': 'application/json'},
                                                  **dict(headers)))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise JudgeError(f'{self.name}: HTTP {e.code} from {self.url}{path}: '
                             f'{e.read()[:300].decode("utf-8", "replace")}') from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise JudgeError(f'{self.name}: {self.url}{path}: {e}') from e


def _results(messages):
    """(the call, as text, and the message) for each tool result in messages."""
    results, calls = [], []
    for m in messages:
        if m['role'] == 'assistant':
            calls = [json.dumps([c['function']['name'],
                                 _arguments(c['function'].get('arguments'))], sort_keys=True)
                     for c in m.get('tool_calls') or []]
        elif m['role'] == 'tool':
            results.append((calls.pop(0) if calls else id(m), m))
    return results


def _sent(message):
    """message as it is sent back to the server: without the keys in NOTES."""
    return {k: v for k, v in message.items() if k not in NOTES}


def _arguments(value):
    if isinstance(value, str):
        try:
            value = json.loads(value or '{}')
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _functions():
    return [{'type': 'function', 'function': t} for t in TOOLS]


class OllamaAgent(ChatAgent):
    """A model served by Ollama. Ollama cuts prompts that are longer than the
    context it was asked for, so ask for enough. A model that can think, such as
    Gemma 4, thinks unless told not to, and its thinking fills the same context:
    think is off unless the config turns it on (true, or a level such as "low")."""

    provider = 'ollama'

    def __init__(self, model, url=None, context=32768, options=None, think=False, **kw):
        super().__init__(model, url or os.environ.get('OLLAMA_HOST') or
                         'http://localhost:11434', context=context, **kw)
        if '://' not in self.url:
            self.url = 'http://' + self.url
        self.options = dict({'num_ctx': context}, **(options or {}))
        self.think = think

    def _chat(self, messages, kind, use_tools):
        body = {'model': self.model, 'messages': messages, 'stream': False,
                'think': self.think, 'options': self.options}
        if use_tools:
            body['tools'] = _functions()
        elif kind != 'chat':
            body['format'] = json.loads(schema(kind))
        data = self._post('/api/chat', body)
        msg = data['message']
        calls = [(None, c['function']['name'], _arguments(c['function'].get('arguments')))
                 for c in msg.get('tool_calls') or []]
        message = {'role': 'assistant', 'content': msg.get('content') or ''}
        if msg.get('tool_calls'):
            message['tool_calls'] = msg['tool_calls']
        if msg.get('thinking'):
            message['thinking'] = msg['thinking']
        if data.get('done_reason') not in (None, 'stop'):
            message['done_reason'] = data['done_reason']
        return message, calls, None

    def _tool_message(self, call_id, name, result):
        return {'role': 'tool', 'tool_name': name, 'content': result}


class OpenAIAgent(ChatAgent):
    """A model behind an OpenAI-compatible chat completions API. The key is read
    from the environment variable api_key_env; price, in dollars per million
    input and output tokens, gives the cost."""

    provider = 'openai'

    def __init__(self, model, url=None, api_key_env='OPENAI_API_KEY', json_schema=True,
                 price=None, **kw):
        super().__init__(model, url or os.environ.get('OPENAI_BASE_URL') or
                         'https://api.openai.com/v1', **kw)
        self.api_key_env, self.json_schema, self.price = api_key_env, json_schema, price

    def _chat(self, messages, kind, use_tools):
        body = {'model': self.model, 'messages': messages}
        if use_tools:
            body['tools'] = _functions()
        elif self.json_schema and kind != 'chat':
            body['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': kind, 'schema': json.loads(schema(kind))}}
        headers = {}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise JudgeError(f'{self.name}: set {self.api_key_env} to the API key')
            headers['Authorization'] = f'Bearer {key}'
        data = self._post('/chat/completions', body, headers)
        msg = data['choices'][0]['message']
        calls = [(c.get('id'), c['function']['name'], _arguments(c['function'].get('arguments')))
                 for c in msg.get('tool_calls') or []]
        message = {'role': 'assistant', 'content': msg.get('content') or ''}
        if msg.get('tool_calls'):
            message['tool_calls'] = msg['tool_calls']
        cost = None
        if self.price and data.get('usage'):
            usage = data['usage']
            cost = (usage.get('prompt_tokens', 0) * self.price.get('input', 0) +
                    usage.get('completion_tokens', 0) * self.price.get('output', 0)) / 1e6
        return message, calls, cost

    def _tool_message(self, call_id, name, result):
        return {'role': 'tool', 'tool_call_id': call_id, 'content': result}
