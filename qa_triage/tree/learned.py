"""The tree's nodes: Python files in the library's nodes directory, one for each
node, from the root's ("root.py") to those a lesson added below where the tree
stopped. An analyst may add a file, or change any of them, as a person may.

A node is registered at its path, such as "hung/command", and answers the
question asked there for the failures it knows. A node that has to tell several
failures apart is one file, which later lessons extend.

Each file lists the jobs it was written or checked for, with the path each takes
through the tree with it:

    EXAMPLES = {'<run>/<job>': 'hung/command/cephadm-rm-cluster'}

Every later change to the files must still take each of them there, conclusively.

A node file is code that an agent may write from logs that anyone can write to, so
it is checked before it is run: it may import only re and the tree's engine,
and may not use the names and attributes that reach files, processes or the
interpreter (see check()); it runs with only the builtins in BUILTINS. That keeps
mistakes out, not a determined attacker: read a node file as you would any
code before you commit it.
"""

import ast
import builtins
import contextlib
import os
import re
from dataclasses import dataclass, field

import yaml

from . import core

MODULES = {'re'} | {f'qa_triage.tree.{name}' for name in ('core', 'common', 'marks')}
# Names a node file may not use: the tree's own registries, and the builtins
# that reach files, code or the interpreter.
HIDDEN = {'NODES', 'LANDMARKS', 'Context', 'explain', 'open', 'exec', 'eval',
          'compile', 'globals', 'locals', 'vars', 'getattr', 'setattr', 'delattr', 'input',
          'breakpoint', 'exit', 'quit', 'help', 'memoryview', 'super', 'type', 'object',
          'classmethod', 'staticmethod', 'property'}
# Attributes it may not use: a job's files (nodes read them through the context),
# formatting that reaches attributes by name, and the interpreter's frames and code.
HIDDEN_ATTRIBUTES = re.compile(r'_.*|files|ssh|format|format_map|mro|'
                               r'(gi|cr|ag|f|tb|co)_\w+')
BUILTINS = {name: getattr(builtins, name) for name in (
    'abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'filter', 'float', 'frozenset', 'int',
    'isinstance', 'iter', 'len', 'list', 'map', 'max', 'min', 'next', 'range', 'reversed',
    'round',
    'set', 'sorted', 'str', 'sum', 'tuple', 'zip', 'Exception', 'AttributeError',
    'IndexError', 'KeyError', 'TypeError', 'ValueError', 'StopIteration')}
NAME = re.compile(r'[a-z0-9][a-z0-9_-]*\.py')


class LearnedError(ValueError):
    pass


@dataclass
class Learned:
    name: str
    code: str
    paths: list = field(default_factory=list)
    landmarks: list = field(default_factory=list)
    # {'<run>/<job>': the path it takes}
    examples: dict = field(default_factory=dict)


# {file name: Learned}, for the files registered now.
FILES = {}


def check(name, code):
    """What is wrong with a node file: [line], empty if nothing."""
    if not NAME.fullmatch(name):
        return [f'{name}: name a node file like hung-command.py']
    try:
        parsed = ast.parse(code, name)
    except SyntaxError as e:
        return [f'{name}:{e.lineno}: {e.msg}']
    wrong = []

    def bad(n, what):
        wrong.append(f'{name}:{getattr(n, "lineno", "?")}: {what}')

    for n in ast.walk(parsed):
        if isinstance(n, ast.Import):
            for alias in n.names:
                if alias.name != 're':
                    bad(n, f'it may not import {alias.name}')
        elif isinstance(n, ast.ImportFrom):
            if n.level or n.module not in MODULES:
                bad(n, f'it may not import from {"." * n.level}{n.module or ""}; only from '
                       're and qa_triage.tree\'s modules')
            for alias in n.names:
                if alias.name == '*' or alias.name in HIDDEN or alias.name.startswith('_'):
                    bad(n, f'it may not import {alias.name}')
        elif isinstance(n, ast.Name) and (n.id in HIDDEN or n.id.startswith('_')):
            bad(n, f'it may not use {n.id}')
        elif isinstance(n, ast.Attribute) and HIDDEN_ATTRIBUTES.fullmatch(n.attr):
            bad(n, f'it may not use the attribute {n.attr}')
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bad(n, 'it may not use global or nonlocal')
        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target]
            if any(isinstance(t, ast.Attribute) for t in targets):
                bad(n, 'it may not set attributes')
    return wrong


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in MODULES:
        raise ImportError(f'a node may not import {name}')
    return builtins.__import__(name, globals, locals, fromlist, level)


def _register(name, code):
    wrong = check(name, code)
    if wrong:
        raise LearnedError('; '.join(wrong))
    nodes, marks = set(core.NODES), set(core.LANDMARKS)
    module = re.sub(r'\W', '_', name[:-3])
    space = {'__name__': f'qa_triage.tree.learned_{module}',
             '__builtins__': dict(BUILTINS, __import__=_import)}
    learned = Learned(name, code)
    try:
        exec(compile(code, name, 'exec'), space)
        examples = space.get('EXAMPLES')
        if not isinstance(examples, dict) or not examples or \
                not all(isinstance(k, str) and isinstance(v, str) for k, v in examples.items()):
            raise LearnedError('give EXAMPLES = {"<run>/<job>": "<path>"}: the jobs it was '
                               'written for, and the path each takes')
        learned.examples = examples
    except Exception as e:  # noqa: BLE001 - whatever a node file does wrong
        for path in set(core.NODES) - nodes:
            del core.NODES[path]
        for mark in set(core.LANDMARKS) - marks:
            del core.LANDMARKS[mark]
        raise LearnedError(f'{name}: {type(e).__name__}: {e}') from e
    learned.paths = sorted(set(core.NODES) - nodes)
    learned.landmarks = sorted(set(core.LANDMARKS) - marks)
    if not learned.paths:
        _unregister(learned)
        raise LearnedError(f'{name}: it registers no node')
    FILES[name] = learned
    return learned


def _unregister(learned):
    for path in learned.paths:
        core.NODES.pop(path, None)
    for mark in learned.landmarks:
        core.LANDMARKS.pop(mark, None)
    FILES.pop(learned.name, None)


# The file of the nodes directory that says how the tree's answers read (core.WORDING).
WORDING = 'wording.yaml'


def wording(text):
    """{node's path: {answer: sentence}} from the text of a wording file; LearnedError
    if it is not one. A sentence's fields are only {name}, a fact, and {says}."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise LearnedError(f'{WORDING} is not YAML: {e}') from None
    if not isinstance(data, dict):
        raise LearnedError(f'{WORDING} maps each node\'s path to its answers\' sentences')
    for path, answers in data.items():
        if not isinstance(answers, dict) or not all(
                isinstance(a, str) and isinstance(s, str) and s.strip()
                for a, s in answers.items()):
            raise LearnedError(f'{WORDING}: "{path}" must map each answer to a sentence')
        for answer, sentence in answers.items():
            fields = re.findall(r'\{([^{}]*)\}', sentence.replace('{{', '').replace('}}', ''))
            wrong = [f for f in fields if not re.fullmatch(r'\w+', f)]
            if wrong or re.search(r'(?<!\{)\{(?!\{)[^}]*$', sentence):
                raise LearnedError(f'{WORDING}: "{path}" {answer}: a sentence\'s fields are '
                                   'only {a fact\'s name} and {says}')
    return {str(path): dict(answers) for path, answers in data.items()}


def load_wording(directory):
    """Read the wording of the tree's answers from directory into core.WORDING."""
    core.WORDING.clear()
    path = os.path.join(directory or '', WORDING)
    if directory and os.path.exists(path):
        with open(path) as f:
            core.WORDING.update(wording(f.read()))


def load(directory):
    """Register the node files in directory, in place of any of the same names
    registered before, and read its wording of the tree's answers; [Learned] for each
    file."""
    if not directory or not os.path.isdir(directory):
        return []
    load_wording(directory)
    loaded = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.py'):
            continue
        with open(os.path.join(directory, name)) as f:
            code = f.read()
        if name in FILES:
            if FILES[name].code == code:
                loaded.append(FILES[name])
                continue
            _unregister(FILES[name])
        loaded.append(_register(name, code))
    return loaded


@contextlib.contextmanager
def trying(codes):
    """Run with the node files {name: code}, in place of those of the same names;
    yields [Learned] for them. Raises LearnedError if one is wrong."""
    saved = [FILES[name] for name in codes if name in FILES]
    for learned in saved:
        _unregister(learned)
    added = []
    try:
        for name, code in codes.items():
            added.append(_register(name, code))
        yield added
    finally:
        for learned in added:
            _unregister(learned)
        for learned in saved:
            _register(learned.name, learned.code)


def version():
    """What tells one set of node files from another."""
    return tuple(sorted((name, learned.code) for name, learned in FILES.items()))
