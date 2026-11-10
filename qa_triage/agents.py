"""Judges: AI agents that look at the failures the log checks could not place.

A judge works in a directory holding the evidence for one job (see pack.py)
and returns the verdict JSON described in prompts/verdict.schema.json. The same
agents analyse doubted results (see learn.py), answering with
prompts/doubt.schema.json and continuing one session over several turns.

By default a judge answers in a single turn from a prompt that includes the
compacted evidence, without any tools. A judge spec ending in "+files" instead
lets the agent read the evidence directory, including teuthology.log, which
costs more and suits a second, stronger judge in a cascade.

The agents here are commands: Bob Shell, Claude Code, goose, or any command.
chat.py has the ones qa_triage talks to over HTTP: Ollama and OpenAI-compatible
servers.
"""

import concurrent.futures
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading

PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')


class JudgeError(Exception):
    """An agent gave no answer. If it can be resumed, session continues it, and
    cost is what it has cost so far, in its own unit. refused if it would not, as
    its safety filter can: asking it again in the same session will not help."""

    def __init__(self, message, session=None, cost=None, refused=False):
        super().__init__(message)
        self.session, self.cost, self.refused = session, cost, refused


class Stopped(JudgeError):
    """A person stopped the agent while it worked."""


class PerThread:
    """An attribute of an agent that has a value for each thread: lessons and judges
    at once ask one agent, each from a thread of its own, and each hears, and
    follows, its own call."""

    _values = threading.local()

    def __init__(self, default=None):
        self.default = default

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, agent, owner=None):
        if agent is None:
            return self
        return self._values.__dict__.get((id(agent), self.name), self.default)

    def __set__(self, agent, value):
        self._values.__dict__[(id(agent), self.name)] = value


class Stoppable:
    """What every agent has: a person can stop it while it works; and, as lessons
    may ask one agent at once, each from a thread of its own, what it says as it
    works goes to the progress of the thread that asked, and what it writes, as it
    writes it, to its written."""

    stopped = False
    # Held while the processes it runs are counted.
    _counting = threading.Lock()
    progress = PerThread()
    written = PerThread()

    def stop(self):
        """Stop what it runs now, and refuse what it is asked next, until go()."""
        self.stopped = True
        with self._counting:
            running = list(getattr(self, '_running', ()))
        for p in running:
            p.kill()

    def go(self):
        self.stopped = False

    def _stop_here(self):
        if self.stopped:
            raise Stopped(f'{self.name}: stopped')


def each_at_once(do, items, at_once=1):
    """do(item) for each of items, at_once of them at a time (a number, or all), as
    judges and lessons ask their agents: one after the other with 1. Once all are
    done, the first error any raised, a person's stop before any other."""
    items = list(items)
    at_once = len(items) if at_once == 'all' else min(at_once or 1, len(items))
    if at_once <= 1:
        for item in items:
            do(item)
        return
    with concurrent.futures.ThreadPoolExecutor(at_once) as pool:
        done = [pool.submit(do, item) for item in items]
    failed = [f.exception() for f in done if f.exception()]
    if failed:
        raise next((e for e in failed if isinstance(e, Stopped)), failed[0])


# The key that every answer of a kind has.
KEYS = {'verdict': 'verdict', 'doubt': 'assessment'}


def schema(kind='verdict'):
    with open(os.path.join(PROMPTS, f'{kind}.schema.json')) as f:
        return f.read()


def _find_verdict(obj, key='verdict'):
    """The last object with key in obj, looking inside wrappers and strings. The
    key's value is a word, as an answer's is: not a schema of one, which an agent
    that prints its prompt prints too."""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), str):
            return obj
        obj = list(obj.values())
    if isinstance(obj, list):
        found = None
        for item in obj:
            found = _find_verdict(item, key) or found
        return found
    if isinstance(obj, str) and '{' in obj:
        return extract_verdict(obj, key)
    return None


def _fix_escapes(text):
    """text with backslashes that JSON does not allow, such as the \\S or \\' an
    agent writes in a regex, escaped."""
    return re.sub(r'\\(.)', lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu'
                  else '\\\\' + m.group(1), text)


def extract_verdict(text, key='verdict'):
    """The last JSON object with key in text, or None."""
    found = _extract(text, key)
    return found if found is not None else _extract(_fix_escapes(text), key)


def _extract(text, key):
    decoder = json.JSONDecoder()
    found = None
    i = text.find('{')
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i = text.find('{', i + 1)
            continue
        found = _find_verdict(obj, key) or found
        i = text.find('{', end)
    return found


def report_of(stdout):
    """What an agent reports of itself, such as its session and cost: its output,
    if that is one JSON object, or else the last line that is one; or None."""
    for text in [stdout] + stdout.strip().splitlines()[::-1]:
        try:
            found = json.loads(text)
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    return None


class CommandJudge(Stoppable):
    """Any command that reads the prompt on stdin, runs in the evidence
    directory and prints the verdict JSON somewhere in its output."""

    name = 'cmd'
    # What the costs it reports are in, for the report.
    unit = 'of its own unit'
    timeout = 1800
    # Whether a later turn can continue the session of an earlier one; if not,
    # learn.py repeats the conversation so far in each prompt.
    resumes = False
    cumulative_cost = False
    # Whether it prints its work as it goes, a line at a time (see describe); what
    # to give a line a person can follow, for each step, is its progress.
    streams = False
    # The agent's own program, for people to talk with it in, if it has one; and the
    # models it offers by name, for a menu.
    program = None
    models = ()

    def __init__(self, command=None, files=False, timeout=None):
        self.argv = shlex.split(command) if isinstance(command, str) else command
        self.files = files
        self.timeout = timeout or self.timeout

    def command(self, workdir, kind='verdict', session=None):
        return self.argv

    def interactive(self, workdir, session):
        """The command that opens the agent's own program on session, in workdir, for
        a person to talk with it there; None if it has none."""
        return None

    # The environment variable it takes its API key from, if it needs one; and a
    # file to read the key from when that is not set.
    key_env = None
    key_file = None

    def env(self):
        """The environment to run it in: this one, with its key from key_file if the
        environment does not have it; None for this one as it is."""
        if not (self.key_env and self.key_file) or os.environ.get(self.key_env):
            return None
        try:
            with open(os.path.expanduser(self.key_file)) as f:
                return dict(os.environ, **{self.key_env: f.read().strip()})
        except OSError:
            return None

    # The program that runs it.
    def executable(self):
        return self.argv[0] if self.argv else None

    def missing(self):
        """Why the agent cannot run here, or None."""
        name = self.executable()
        if name and not shutil.which(name):
            return f'{self.name}: there is no {name} command here'
        return None

    def cost(self, output):
        """The cost the agent reports in its output, in its own unit."""
        return None

    def refusal(self, stdout, stderr, report):
        """What the agent said if it refused to answer, as its safety filter can;
        None if it did not."""
        return None

    def trouble(self, stderr):
        """Why the agent would not run at all, from what it said, if it says
        something a person can do about; None if not."""
        return None

    def _check_refusal(self, stdout, stderr, report, found):
        why = self.refusal(stdout, stderr, report or {})
        if why:
            raise JudgeError(f'{self.name} refused to answer: {why}. Safety filters flag '
                             'safe requests now and then: ask again, or choose another agent '
                             'or model', found.get('session'), found.get('cost'), refused=True)

    def session(self, output):
        """The id that continues this conversation, from the agent's output."""
        return None

    def describe(self, line):
        """Lines a person can follow for a line of the agent's output, as it works:
        what it thinks, and what it looks at."""
        return []

    def wrote(self, line):
        """What the agent wrote, as it is, in a line of its output, as it works."""
        return line

    def hear(self, line):
        """Tell the thread that asked of a line of the agent's output, as it works:
        its progress the lines a person can follow, and add what it wrote to its
        written, a list."""
        for said in self.describe(line) if self.progress else []:
            self.progress(said)
        text = self.wrote(line) if self.written is not None else ''
        if text:
            self.written.append(text)

    def answer_text(self, stdout):
        """The text of the agent's output that holds its answer."""
        return stdout

    def reply_text(self, stdout):
        """What the agent said, in a turn of a conversation."""
        return self.answer_text(stdout)

    def chat(self, workdir, prompt, session=None, tag=None):
        """A turn of a conversation: the reply as text, which is not checked for an
        answer; {'text', 'session', 'cost'}."""
        workdir = os.path.abspath(workdir)
        self._stop_here()
        try:
            stdout, stderr, status = self._run(self.command(workdir, 'chat', session), prompt,
                                               workdir)
        except (OSError, subprocess.TimeoutExpired) as e:
            self._stop_here()
            raise JudgeError(f'{self.name}: {e}') from e
        self._stop_here()
        out = os.path.join(workdir, tag or 'chat')
        with open(out + '.out', 'w') as f:
            f.write(stdout)
        with open(out + '.err', 'w') as f:
            f.write(stderr)
        found, report = {'text': self.reply_text(stdout).strip()}, report_of(stdout)
        for field, get in (('cost', self.cost), ('session', self.session)):
            try:
                found[field] = get(report)
            except (ValueError, AttributeError, KeyError, TypeError):
                found[field] = None
        self._check_refusal(stdout, stderr, report, found)
        if not found['text']:
            said = stderr.strip().splitlines()[-1:] or stdout.strip().splitlines()[-1:]
            raise JudgeError(f'{self.name} said nothing (exit {status}): ' +
                             (self.trouble(stderr) or (said[0] if said else 'no output')) +
                             f'; see {out}.out and .err', found['session'], found['cost'])
        return found

    def __call__(self, workdir, prompts):
        return self.ask(workdir, prompts['files' if self.files else 'inline'])

    def _run(self, argv, prompt, workdir):
        """(stdout, stderr, exit status) of the agent; a line at a time, to
        progress, if it streams."""
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=workdir, text=True, env=self.env())
        # What stop() kills.
        with self._counting:
            self._running = getattr(self, '_running', set()) | {p}
        if self.stopped:
            p.kill()
        try:
            return self._wait(argv, p, prompt)
        finally:
            with self._counting:
                self._running = self._running - {p}

    def _wait(self, argv, p, prompt):
        if not self.streams:
            try:
                stdout, stderr = p.communicate(prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                raise
            return stdout, stderr, p.returncode
        stopped, errors = [], []
        timer = threading.Timer(self.timeout, lambda: (stopped.append(1), p.kill()))
        reader = threading.Thread(target=lambda: errors.append(p.stderr.read()), daemon=True)
        timer.start()
        reader.start()
        try:
            p.stdin.write(prompt)
            p.stdin.close()
            lines = []
            for line in p.stdout:
                lines.append(line)
                self.hear(line)
            p.wait()
            reader.join()
        finally:
            timer.cancel()
            p.stdout.close()
            p.stderr.close()
        if stopped:
            raise subprocess.TimeoutExpired(argv, self.timeout)
        return ''.join(lines), ''.join(errors), p.returncode

    def ask(self, workdir, prompt, kind='verdict', session=None, tag=None):
        """Run the agent on prompt in workdir; return its answer, an object with
        the key KEYS[kind]."""
        # The agent runs in workdir, so a relative path would be read from there.
        workdir = os.path.abspath(workdir)
        self._stop_here()
        try:
            stdout, stderr, status = self._run(self.command(workdir, kind, session), prompt,
                                               workdir)
        except (OSError, subprocess.TimeoutExpired) as e:
            self._stop_here()
            raise JudgeError(f'{self.name}: {e}') from e
        self._stop_here()
        out = os.path.join(workdir, tag or 'judge-' + self.name.replace(':', '-'))
        with open(out + '.out', 'w') as f:
            f.write(stdout)
        with open(out + '.err', 'w') as f:
            f.write(stderr)
        answer = extract_verdict(self.answer_text(stdout), KEYS[kind])
        found, report = {}, report_of(stdout)
        for field, get in (('cost', self.cost), ('session', self.session)):
            try:
                found[field] = get(report)
            except (ValueError, AttributeError, KeyError, TypeError):
                pass
        self._check_refusal(stdout, stderr, report, found)
        if answer is None:
            said = stderr.strip().splitlines()[-1:] or stdout.strip().splitlines()[-1:]
            raise JudgeError(f'{self.name} gave no answer (exit {status}): ' +
                             (self.trouble(stderr) or (said[0] if said else 'no output')) +
                             f'; see {out}.out and .err', found.get('session'),
                             found.get('cost'))
        answer['judge'] = self.name
        answer.update(found)
        return answer


# The oldest Bob Shell whose bob run qa_triage runs as it does.
BOB_VERSION = (2, 0, 6)


def bob_version():
    """The version of the bob here, as a tuple of numbers; None if it does not say."""
    if 'bob' not in _versions:
        try:
            said = subprocess.run(['bob', '--version'], capture_output=True, text=True,
                                  timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            said = ''
        found = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', said)
        _versions['bob'] = tuple(int(n) for n in found.groups() if n) if found else None
    return _versions['bob']


_versions = {}


class BobJudge(CommandJudge):
    """IBM Bob Shell in headless mode, which approves its own tool calls, so it
    is given only tools that read."""

    resumes = True
    # A resumed session reports what the whole session has cost so far.
    cumulative_cost = True
    unit = 'Bob coins'
    # What it has said so far of a thought, and where its evidence is; when its turn
    # began, whether it resumed a session, its prompt, and whether its turn has begun
    # in what it printed: of the call of the thread that asked.
    _thinking, _workdir = PerThread(''), PerThread('')
    _since, _resumed, _prompt, _begun = PerThread(''), PerThread(False), PerThread(), \
        PerThread(True)

    key_env = 'BOB_API_KEY'

    def __init__(self, files=False, max_turns=None, timeout=None, key_file=None):
        self.files, self.max_turns, self.key_file = files, max_turns, key_file
        self.timeout = timeout or self.timeout
        self.name = 'bob' + ('+files' if files else '')

    # It prints each step as a JSON object: what it says, a word or two at a
    # time, the tools it calls, and at the end what it cost.
    streams = True

    def command(self, workdir, kind='verdict', session=None):
        self._thinking, self._workdir = '', workdir.rstrip('/') + '/'
        # A resumed session prints the steps of the turns before it again, first:
        # only the steps from now on are this turn's. Its timestamps are UTC, to
        # the millisecond; a second's grace allows for the clock.
        self._since = (datetime.datetime.now(datetime.timezone.utc) -
                       datetime.timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]
        # Some versions stamp the steps printed again with the time they print
        # them: then this turn's steps are the ones after its prompt.
        self._resumed, self._prompt, self._begun = bool(session), None, not session
        # Tool definitions cost tokens on every turn, so an inline judge gets
        # none that act, and a +files judge only the ones that read.
        groups = 'edit,execute,browser,mcp,skill,artifact,subagent'
        # With no tools at all Bob Shell repeats its system prompt, so an inline
        # judge keeps its todo list. One that reads files does not need it, and
        # it has been seen calling it in a loop instead of answering.
        groups = groups + ',todo' if self.files else 'read,' + groups
        turns = str(self.max_turns or (50 if kind in ('doubt', 'chat') else 30 if self.files
                                       else 3))
        argv = ['bob', 'run', '--format', 'stream-json', '--workspace', workdir, '--trust',
                '--disable-mcp', '--disable-subagents', '--disable-tool-groups', groups,
                '--max-turns', turns]
        return argv + (['--resume', session] if session else [])

    program = 'Bob Shell'

    def executable(self):
        return 'bob'

    def missing(self):
        if not (os.environ.get('BOB_API_KEY') or self.env()):
            return (f'{self.name}: BOB_API_KEY is not set, and Bob Shell needs it: export '
                    'BOB_API_KEY=<your key>, or give a file with it in qa_triage setup')
        why = super().missing()
        if why:
            return why
        version = bob_version()
        if version and version < BOB_VERSION:
            return (f'{self.name}: this Bob Shell is version {".".join(map(str, version))}, '
                    f'and qa_triage needs {".".join(map(str, BOB_VERSION))} or later: '
                    'update it the way it was installed')
        return None

    def interactive(self, workdir, session):
        # It asks the person before it uses a tool that writes or runs a command.
        return ['bob', 'chat', '--workspace', workdir, '--trust', '--disable-mcp',
                '--disable-subagents', '--disable-tool-groups', 'browser,skill,artifact,subagent',
                '--resume', session]

    def cost(self, output):
        return output['stats']['session_costs']

    def trouble(self, stderr):
        if re.search(r'Unknown arguments?: .*\bformat\b', stderr):
            return (f'this Bob Shell is older than version 2, and has no bob run: qa_triage '
                    f'needs {".".join(map(str, BOB_VERSION))} or later; update it the way it '
                    'was installed')
        if 'license agreement is required' in stderr:
            return ('Bob Shell says a license agreement is required, which it says as well '
                    'when it does not know the API key: check the key (BOB_API_KEY, or the '
                    'key file qa_triage setup names); if it is right, run bob by itself once, '
                    'and accept its license')
        return None

    def refusal(self, stdout, stderr, report):
        if 'blocked by the content filter' in stderr:
            return 'its content filter blocked the request, twice'
        return None

    def session(self, output):
        return output['stats']['task_id']

    def _short(self, value, limit=200):
        text = ' '.join(str(value).replace(self._workdir, '').split())
        return text if len(text) <= limit else text[:limit - 3] + '...'

    def _thoughts(self, texts):
        """What it said, as lines, but not its answer's JSON."""
        return [f'  thinks: {self._short(text, 400)}' for text in texts
                if text.strip() and not text.strip().startswith(('{', '}', '"', '[', ']', '`'))]

    def _run(self, argv, prompt, workdir):
        self._prompt = prompt.strip()[:200]
        stdout, stderr, status = super()._run(argv, prompt, workdir)
        if 'blocked by the content filter' in stderr:
            # Its content filter has blocked a prompt that passed, unchanged, when
            # sent again; a blocked one costs nothing.
            stdout, stderr, status = super()._run(argv, prompt, workdir)
        return stdout, stderr, status

    def _asked(self, event):
        """True if event is this turn's prompt, printed."""
        return event.get('type') == 'message' and event.get('role') == 'user' and \
            str(event.get('content', '')).strip().startswith(self._prompt or '\0')

    def _now(self, event):
        """True if event is of this turn, not one of the turns before it, as far as
        the events so far tell."""
        if not getattr(self, '_begun', True):
            self._begun = self._asked(event)
            return False
        return str(event.get('timestamp', '9')) >= getattr(self, '_since', '')

    def _event(self, line):
        """The event a line of its output is, if it is one of this turn; None if not."""
        try:
            event = json.loads(line)
        except ValueError:
            return None
        return event if isinstance(event, dict) and self._now(event) else None

    def hear(self, line):
        # A line is read once, for whether it is of this turn: what it said and wrote
        # are of the event it is.
        event = self._event(line) if self.progress or self.written is not None else None
        if event is None:
            return
        for said in self._steps(event) if self.progress else []:
            self.progress(said)
        if self.written is None:
            return
        if event.get('type') == 'message' and event.get('role') == 'user':
            # A turn begins: what it wrote before was of a turn before it, printed again.
            self.written.clear()
        text = self._wrote(event)
        if text:
            self.written.append(text)

    def describe(self, line):
        event = self._event(line)
        return self._steps(event) if event else []

    def wrote(self, line):
        event = self._event(line)
        return self._wrote(event) if event else ''

    def _steps(self, event):
        said, kind = [], event.get('type')
        if kind == 'message' and event.get('role') == 'user':
            # A turn begins: what was said before it is done with.
            self._thinking = ''
        if kind == 'message' and event.get('role') == 'assistant':
            self._thinking += str(event.get('content', ''))
            *done, self._thinking = self._thinking.split('\n')
            return self._thoughts(done)
        if kind in ('tool_use', 'result'):
            said, self._thinking = self._thoughts([self._thinking]), ''
            if kind == 'tool_use':
                said.append(self._tool(event))
            else:
                stats = event.get('stats') or {}
                # Of the whole session, not only this turn.
                said.append(f'  done: the session has made {stats.get("tool_calls", "?")} tool '
                            f'calls and cost {stats.get("session_costs") or 0:.2f} Bob coins')
        return said

    def _wrote(self, event):
        """What it wrote in event, as it is: its words, a few at a time, and each tool
        it calls, on a line of its own."""
        if event.get('type') == 'message' and event.get('role') == 'assistant':
            return str(event.get('content', ''))
        if event.get('type') == 'tool_use':
            return f'\n{self._tool(event)}\n'
        return ''

    def _tool(self, event):
        parameters = event.get('parameters') or {}
        what = (', '.join(f'{k}={self._short(v, 120)}' for k, v in parameters.items())
                if isinstance(parameters, dict) else self._short(parameters))
        return f'  {event.get("tool_name")}: {what}'

    def answer_text(self, stdout):
        """What it said, joined up: its answer is in the last of it."""
        events = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        asked = [i for i, event in enumerate(events) if self._asked(event)]
        if getattr(self, '_resumed', False) and asked:
            # After the last time its prompt was printed: the same prompt may have
            # been asked in a turn before.
            events = events[asked[-1] + 1:]
        else:
            events = [event for event in events
                      if str(event.get('timestamp', '9')) >= getattr(self, '_since', '')]
        parts = []
        for event in events:
            if event.get('type') == 'message' and event.get('role') == 'assistant':
                parts.append(str(event.get('content', '')))
            elif event.get('type') == 'tool_use':
                parts.append('\n')
        return ''.join(parts)


class ClaudeJudge(CommandJudge):
    """Claude Code in print mode: no tools, or only file reading with +files."""

    resumes = True
    # A resumed session's total_cost_usd, like its modelUsage, is what the whole
    # session has cost so far, not the turn.
    cumulative_cost = True
    unit = 'dollars'

    def __init__(self, model=None, files=False, budget_usd=2.0, timeout=None):
        self.model = model
        self.files = files
        self.budget_usd = budget_usd
        self.timeout = timeout or self.timeout
        self.name = 'claude' + (f':{model}' if model else '') + ('+files' if files else '')

    def command(self, workdir, kind='verdict', session=None):
        argv = ['claude', '-p', '--output-format', 'json', '--max-budget-usd',
                str(self.budget_usd)]
        if kind != 'chat':
            argv += ['--json-schema', schema(kind)]
        if self.files:
            argv += ['--tools', 'Read,Grep,Glob', '--allowedTools', 'Read', 'Grep', 'Glob']
        else:
            argv += ['--tools', '']
        if self.model:
            argv += ['--model', self.model]
        return argv + (['--resume', session] if session else [])

    program = 'Claude Code'
    models = ('opus', 'sonnet', 'haiku')

    def executable(self):
        return 'claude'

    def interactive(self, workdir, session):
        # It asks the person before it uses a tool, as it always does.
        return ['claude', '--resume', session] + (['--model', self.model] if self.model else [])

    def env(self):
        # Its sessions are kept, for a later turn or the person's own talk with it to go
        # on from, even where qa_triage runs in a Claude Code session, whose children
        # otherwise keep no transcript.
        return dict(os.environ, CLAUDE_CODE_FORCE_SESSION_PERSISTENCE='1')

    def cost(self, output):
        return output['total_cost_usd']

    def session(self, output):
        return output['session_id']

    def reply_text(self, stdout):
        return str((report_of(stdout) or {}).get('result', ''))

    def refusal(self, stdout, stderr, report):
        if report.get('stop_reason') == 'refusal':
            return str(report.get('result', '')).split('\n')[0] or 'it refused'
        return None


class GooseAgent(CommandJudge):
    """goose in headless mode, with any provider and model it supports. It gets
    none of its own extensions, so no tools that run commands or write: with
    +files, only qa_triage's read-only evidence tools, as an MCP server."""

    def __init__(self, goose_provider=None, model=None, files=False, timeout=None):
        self.provider, self.model, self.files = goose_provider, model, files
        self.timeout = timeout or self.timeout
        self.name = 'goose' + (f':{goose_provider}' if goose_provider else '') + \
            (f'/{model}' if model else '') + ('+files' if files else '')

    def executable(self):
        return 'goose'

    def command(self, workdir, kind='verdict', session=None):
        argv = ['goose', 'run', '--no-session', '--no-profile', '--quiet',
                '--instructions', '-']
        if self.provider:
            argv += ['--provider', self.provider]
        if self.model:
            argv += ['--model', self.model]
        if self.files:
            package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            argv += ['--with-extension', f'PYTHONPATH={package} {sys.executable} -m '
                                         f'qa_triage mcp {workdir}']
        return argv


class CascadeJudge:
    """Ask the judges in turn, cheapest first, until one of them is sure."""

    def __init__(self, judges):
        self.judges = judges
        self.name = ','.join(j.name for j in judges)
        self.units = units(*judges)

    def stop(self):
        for judge in self.judges:
            judge.stop()

    def go(self):
        for judge in self.judges:
            judge.go()

    @property
    def stopped(self):
        return any(getattr(judge, 'stopped', False) for judge in self.judges)

    def missing(self):
        return next((why for why in (getattr(j, 'missing', lambda: None)()
                                     for j in self.judges) if why), None)

    def __call__(self, workdir, prompts):
        verdict, errors, costs = None, [], []
        for judge in self.judges:
            try:
                verdict = judge(workdir, prompts)
            except Stopped:
                raise
            except JudgeError as e:
                errors.append(str(e))
                continue
            costs.append((judge.name, verdict.get('cost')))
            if verdict.get('verdict') != 'unsure':
                break
        if verdict is None:
            raise JudgeError('; '.join(errors))
        verdict['costs'] = costs
        return verdict


# The settings each provider takes in a config file's agents.
PROVIDERS = {
    'bob': ('files', 'max_turns', 'timeout', 'key_file'),
    'claude': ('model', 'files', 'budget_usd', 'timeout'),
    'goose': ('goose_provider', 'model', 'files', 'timeout'),
    'ollama': ('model', 'url', 'context', 'options', 'think', 'files', 'max_turns',
               'timeout'),
    'openai': ('model', 'url', 'api_key_env', 'json_schema', 'price', 'files', 'max_turns',
               'timeout'),
    'cmd': ('command', 'files', 'timeout'),
}
SPECS = ('bob, claude[:<model>], goose[:<provider>/<model>], ollama:<model>, '
         'openai:<model>[@<base url>], each optionally ending in +files, or '
         '"cmd:<command>"')


def parse_spec(spec):
    """An agent's settings from a spec such as claude:haiku or ollama:gemma4+files."""
    if spec.startswith('cmd:'):
        return {'provider': 'cmd', 'command': spec[4:]}
    base, files = (spec[:-6], True) if spec.endswith('+files') else (spec, False)
    provider, _, rest = base.partition(':')
    settings = {'provider': provider, 'files': files}
    if provider == 'bob' and not rest:
        return settings
    if provider == 'claude':
        return dict(settings, model=rest or None)
    if provider == 'goose':
        goose_provider, _, model = rest.partition('/')
        return dict(settings, goose_provider=goose_provider or None, model=model or None)
    if provider == 'ollama' and rest:
        return dict(settings, model=rest)
    if provider == 'openai' and rest:
        model, _, url = rest.partition('@')
        return dict(settings, model=model, url=url or None)
    raise ValueError(f'unknown agent "{spec}": name one from the config file, or give '
                     f'one of {SPECS}')


def build(settings, name=None):
    """An agent from its settings: a config file's entry, or parse_spec's."""
    settings = {k: v for k, v in settings.items() if v is not None}
    provider = settings.pop('provider', None)
    what = f'agent {name}' if name else 'an agent'
    if provider not in PROVIDERS:
        raise ValueError(f'{what}: its provider must be one of {", ".join(PROVIDERS)}, '
                         f'not {provider}')
    unknown = set(settings) - set(PROVIDERS[provider])
    if unknown:
        raise ValueError(f'{what}: {provider} does not take {", ".join(sorted(unknown))}')
    from . import chat
    if provider in ('ollama', 'openai'):
        if not settings.get('model'):
            raise ValueError(f'{what}: {provider} needs a model')
        kind = chat.OllamaAgent if provider == 'ollama' else chat.OpenAIAgent
        return kind(name=name, **settings)
    kind = {'bob': BobJudge, 'claude': ClaudeJudge, 'goose': GooseAgent,
            'cmd': CommandJudge}[provider]
    agent = kind(**settings)
    if name:
        agent.name = name
    return agent


def installed():
    """[(agent, what it is)]: each kind of agent whose program is installed here
    and that needs no settings, with its own default model and with each model it
    offers by name; to choose from, in no order but the alphabet's."""
    found = []
    for provider in PROVIDERS:
        if provider in ('ollama', 'openai', 'cmd'):
            continue
        agent = build({'provider': provider})
        if shutil.which(agent.executable()):
            what = agent.program or provider
            found += [(provider, what)] + [(f'{provider}:{model}', f'{what} with {model}')
                                           for model in agent.models]
    return sorted(found, key=lambda found: found[1].lower())


# Each kind of agent's name, for people.
PROPER = {'bob': 'Bob', 'claude': 'Claude', 'goose': 'Goose', 'ollama': 'Ollama',
          'openai': 'OpenAI'}


def readable(name):
    """An agent's name as a person reads it: a kind of agent by its proper name, with
    the model it runs, such as Claude (opus) for claude:opus; an agent of a config by
    its name there; without "+files", since the analyst always reads the files. The
    names of a cascade's agents, each so."""
    if not name:
        return str(name)
    out = []
    for one in str(name).split(','):
        one = one.strip()
        one = one[:-len('+files')] if one.endswith('+files') else one
        kind, _, model = one.partition(':')
        out.append(PROPER[kind] + (f' ({model})' if model else '') if kind in PROPER else one)
    return ', '.join(out)


def make_agent(ref, agents=None, files=None):
    """The agent named ref in agents (a config file's), or given by the spec ref.
    files True makes it read the evidence files itself."""
    agents = agents or {}
    if ref in agents:
        settings, name = dict(agents[ref]), ref
    else:
        settings, name = parse_spec(ref), None
    if files is not None and bool(settings.get('files')) != files:
        settings['files'] = files
        name = name and name + ('+files' if files else '')
    return build(settings, name)


def units(*agents):
    """{agent name: the unit its costs are in} for agents and cascades; None
    agents are left out."""
    found = {}
    for agent in agents:
        if agent:
            found.update(getattr(agent, 'units', None) or
                         {agent.name: getattr(agent, 'unit', 'of its own unit')})
    return found


def make_judge(spec, agents=None):
    """None for none; otherwise the agents to try in turn (a cascade), as a list
    or separated by commas, each one named in agents or given by a spec. A cmd:
    spec is taken whole."""
    if not spec or spec == 'none':
        return None
    if not isinstance(spec, list):
        spec = [spec] if spec.startswith('cmd:') or spec in (agents or {}) else \
            spec.split(',')
    judges = [make_agent(ref.strip(), agents) for ref in spec]
    return judges[0] if len(judges) == 1 else CascadeJudge(judges)
