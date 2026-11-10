"""Tools that read the evidence directory and nothing else.

Agents that take tools from qa_triage get only these: the chat providers (see
chat.py) call them directly, and agents such as goose get them from an MCP server
(see mcp.py). Paths are relative to the directory, and nothing outside it can be
read, but for the directories that qa_triage links into it, such as source/: a
glob reaches into one of those only if it starts with its name.
"""

import os
import re

MAX_FILES = 500
MAX_LINES = 400
MAX_HITS = 100
LINE_LIMIT = 2000
# A model with a small context must not be swamped by one call.
MAX_OUTPUT = 20000

TOOLS = [
    {'name': 'list_files',
     'description': 'List the files whose paths match a glob, with their sizes. '
                    '* matches within one path part and ** any number of parts.',
     'parameters': {'type': 'object',
                    'properties': {'pattern': {'type': 'string',
                                               'description': 'a glob; default **'}}}},
    {'name': 'read_file',
     'description': f'Read up to {MAX_LINES} numbered lines of a file, from line '
                    'offset (1 is the first).',
     'parameters': {'type': 'object',
                    'properties': {'path': {'type': 'string'},
                                   'offset': {'type': 'integer'},
                                   'limit': {'type': 'integer'}},
                    'required': ['path']}},
    {'name': 'grep',
     'description': f'Find the lines that match a Python regex in the files whose '
                    f'paths match a glob (default **); at most {MAX_HITS} lines.',
     'parameters': {'type': 'object',
                    'properties': {'regex': {'type': 'string'},
                                   'path': {'type': 'string'}},
                    'required': ['regex']}},
]


def glob_regex(pattern):
    """A regex for a glob over relative paths: * and ? stay within a path part."""
    out = ''
    for part in re.split(r'(\*\*/?|\*|\?)', pattern):
        out += {'**': '.*', '**/': '(?:.*/)?', '*': '[^/]*', '?': '[^/]'}.get(
            part, re.escape(part))
    return re.compile(out + r'\Z')


class Tools:
    def __init__(self, root, max_output=MAX_OUTPUT):
        self.root = os.path.realpath(root)
        self.max_output = max_output

    def call(self, name, args):
        """The tool's result as text; errors start with "error: "."""
        tools = {'list_files': self.list_files, 'read_file': self.read_file,
                 'grep': self.grep}
        if name not in tools:
            return f'error: there is no tool {name}'
        try:
            text = tools[name](**args)
        except FileNotFoundError:
            return f'error: there is no {args.get("path")}; list_files shows the files there are'
        except (TypeError, ValueError, OSError, re.error) as e:
            return f'error: {e}'
        if len(text) > self.max_output:
            text = text[:self.max_output] + f'\n... cut at {self.max_output} characters: ' \
                                            'ask for fewer lines, or a narrower regex or glob'
        return text

    def _links(self):
        """{name: target} of the directories linked into the evidence directory."""
        found = {}
        for name in sorted(os.listdir(self.root)):
            path = os.path.join(self.root, name)
            if os.path.islink(path) and os.path.isdir(path):
                found[name] = os.path.realpath(path)
        return found

    def _path(self, rel):
        path = os.path.realpath(os.path.join(self.root, str(rel)))
        for top in [self.root, *self._links().values()]:
            if path == top or path.startswith(top + os.sep):
                return path
        raise ValueError(f'{rel} is outside the evidence directory')

    def _files(self, pattern):
        pattern = str(pattern or '**')
        regex = glob_regex(pattern)
        tops = [(self.root, '')] + [(target, name) for name, target in self._links().items()
                                    if pattern.startswith(name + '/')]
        found = []
        for root, prefix in tops:
            for top, dirs, names in os.walk(root):
                dirs.sort()
                for name in sorted(names):
                    path = os.path.join(top, name)
                    rel = os.path.join(prefix, os.path.relpath(path, root))
                    if regex.match(rel) and not os.path.islink(path):
                        found.append(rel)
        return found

    def list_files(self, pattern='**'):
        found = self._files(pattern)
        lines = [f'{rel} {os.path.getsize(self._path(rel))}' for rel in found[:MAX_FILES]]
        if len(found) > MAX_FILES:
            lines.append(f'... and {len(found) - MAX_FILES} more')
        return '\n'.join(lines) or 'no files match'

    def read_file(self, path, offset=1, limit=MAX_LINES):
        offset, limit = max(1, int(offset)), max(1, min(int(limit), MAX_LINES))
        lines, n = [], 0
        # Lines end only at \n, as they do for the log checks and excerpts.txt, so
        # a lone \r in a log does not shift the line numbers.
        with open(self._path(path), errors='replace', newline='\n') as f:
            for n, line in enumerate(f, 1):
                if n >= offset + limit:
                    lines.append(f'... more from line {n}')
                    break
                if n >= offset:
                    lines.append(f'{n}: {line.rstrip()[:LINE_LIMIT]}')
        return '\n'.join(lines) or f'{path} has no line {offset}: it has {n} lines'

    def grep(self, regex, path='**'):
        compiled = re.compile(regex)
        hits = []
        for rel in self._files(path):
            with open(self._path(rel), errors='replace', newline='\n') as f:
                for n, line in enumerate(f, 1):
                    if compiled.search(line):
                        hits.append(f'{rel}:{n}: {line.rstrip()[:LINE_LIMIT]}')
                        if len(hits) == MAX_HITS:
                            return '\n'.join(hits + ['... stopped at the limit'])
        return '\n'.join(hits) or 'no lines match'
