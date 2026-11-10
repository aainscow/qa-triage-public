"""Run on a host that has the teuthology archive, such as a sepia machine with
/a mounted, so that a job's files are searched where they are instead of being
downloaded. qa_triage sends this file over ssh as the program of `python3 -c`,
and one JSON request on stdin:

    {"op": "list", "root": job directory}
        -> {"files": {path: size}}, or {"missing": true}
    {"op": "siblings", "root": job directory}
        -> {"jobs": [the job directories of its run that are there]}
    {"op": "search", "root": ..., "files": [path], "pattern": regex, "flags": int,
     "max_hits": n, "literals": [literal]} -> {"hits": [[path, line number, text]],
     "gone": [path]}
        (literals is optional: plain strings that every line the regex finds has;
        gone, the files that are not there, as when the archive has pruned them)
    {"op": "read", "root": ..., "path": path, "limit": bytes} -> {"text": text}
    {"op": "cat", "root": ..., "path": path} -> the file, gzip-compressed
    {"op": "scan", "root": ..., "path": path,
     "patterns": {name: [regex, [literal]]}, "first": n, "last": n}
        -> {"marks": {name: {"count": n, "hits": [[n, text]]}}}
    {"op": "tail", "root": ..., "path": path, "lines": n} -> {"hits": [[n, text]]}
    {"op": "range", "root": ..., "path": path, "start": n, "end": n}
        -> {"hits": [[n, text]]}

Paths are relative to root. Lines end only at \\n, and .gz files are read as
they decompress, as qa_triage reads the files it downloads, so that line numbers
agree. It needs only the standard library of Python 3.6.
"""

import collections
import gzip
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import tempfile
import sys
import zlib

# A search returns at most this much of a line: daemon log lines can be huge.
MAX_TEXT = 10000
PROCESSES = 8
# What a truncated .gz file raises, as a job that died can leave one.
TRUNCATED = (EOFError, zlib.error, getattr(gzip, 'BadGzipFile', OSError))


def _path(root, rel):
    rel = os.path.normpath(rel)
    if os.path.isabs(rel) or rel.startswith('..'):
        raise ValueError('%s is outside the job directory' % rel)
    return os.path.join(root, rel)


def lines(path):
    """The decoded lines of a file; a truncated .gz file ends where it breaks."""
    with (gzip.open(path, 'rb') if path.endswith('.gz') else open(path, 'rb')) as f:
        try:
            for line in f:
                yield line.decode('utf-8', 'replace').rstrip('\n')
        except TRUNCATED:
            return


def _list(root):
    found = {}
    for top, dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(top, name)
            try:
                found[os.path.relpath(path, root)] = os.stat(path).st_size
            except OSError:
                pass
    return found


def _search(args):
    """The (line number, text) of the lines that pattern finds; None if there is no
    file."""
    path, pattern, flags, max_hits, literals = args
    if not os.path.exists(path):
        return None
    regex, hits = re.compile(pattern, flags), []
    try:
        for n, text in (literals and grepped(path, literals)) or enumerate(lines(path), 1):
            if regex.search(text):
                hits.append((n, text[:MAX_TEXT]))
                if len(hits) == max_hits:
                    break
    except OSError:
        pass
    return hits


def scan(numbered, patterns, first, last):
    """{name: {"count": n, "hits": [[n, text]]}} for each of patterns, a dict of
    [regex, [literal]], over numbered, an iterable of (line number, text): how many
    lines it found, and the first and the last of them. A line is matched against
    a regex only if it has one of the regex's literals, plain strings that every
    line it finds has."""
    landmarks = [(name, re.compile(regex), literals)
                 for name, (regex, literals) in patterns.items()]
    every = [literal for _, _, literals in landmarks for literal in literals]
    marks = {name: [0, [], collections.deque(maxlen=last)] for name in patterns}
    for n, text in numbered:
        if not any(literal in text for literal in every):
            continue
        for name, regex, literals in landmarks:
            if any(literal in text for literal in literals) and regex.search(text):
                mark = marks[name]
                mark[0] += 1
                hit = [n, text[:MAX_TEXT]]
                if len(mark[1]) < first:
                    mark[1].append(hit)
                else:
                    mark[2].append(hit)
    return {name: {'count': count, 'hits': head + list(tail)}
            for name, (count, head, tail) in marks.items()}


def grepped(path, literals):
    """(line number, text) of the lines of path that have any of literals, found
    by grep -F, which reads a big log many times faster than Python does, with a
    .gz file decompressed by gzip; or None if there is no grep or gzip."""
    grep, gunzip = shutil.which('grep'), shutil.which('gzip')
    zipped = path.endswith('.gz')
    if not grep or (zipped and not gunzip) or not literals or not all(literals):
        return None
    argv = [grep, '-a', '-n', '-F'] + [arg for literal in literals for arg in ('-e', literal)]

    def found():
        with open(os.devnull, 'wb') as quiet, tempfile.TemporaryFile() as said:
            # A truncated .gz file ends where it breaks, as lines() reads it.
            source = subprocess.Popen([gunzip, '-dc', '--', path], stdout=subprocess.PIPE,
                                      stderr=quiet) if zipped else None
            with subprocess.Popen(argv + ([] if zipped else ['--', path]),
                                  stdin=source.stdout if zipped else quiet,
                                  stdout=subprocess.PIPE, stderr=said) as p:
                if source:
                    source.stdout.close()
                try:
                    for line in p.stdout:
                        n, _, text = line.partition(b':')
                        yield int(n), text.decode('utf-8', 'replace').rstrip('\n')
                finally:
                    # Whoever reads this may stop early, as a search does.
                    for process in (p, source):
                        if process and process.poll() is None:
                            process.kill()
            if source:
                source.wait()
            said.seek(0)
            why = said.read(500).decode('utf-8', 'replace').strip()
        if p.returncode > 1:
            raise OSError('grep failed on %s%s' % (path, ': ' + why if why else ''))
    return found()


def tail(numbered, count):
    return [[n, text[:MAX_TEXT]] for n, text in collections.deque(numbered, maxlen=count)]


def between(numbered, start, end):
    """The lines numbered start to end, inclusive."""
    found = []
    for n, text in numbered:
        if n > end:
            break
        if n >= start:
            found.append([n, text[:MAX_TEXT]])
    return found


def handle(request, out):
    """Answer request, writing the answer to the binary stream out."""
    root, op = request['root'], request['op']
    if op == 'cat':
        path = _path(root, request['path'])
        with open(path, 'rb') as f:
            if path.endswith('.gz'):
                shutil.copyfileobj(f, out)
                return
            packer = zlib.compressobj(1, zlib.DEFLATED, 31)
            for chunk in iter(lambda: f.read(1 << 20), b''):
                out.write(packer.compress(chunk))
            out.write(packer.flush())
        return
    if op == 'list':
        answer = {'files': _list(root)} if os.path.isdir(root) else {'missing': True}
    elif op == 'siblings':
        run = os.path.dirname(os.path.normpath(root))
        answer = {'jobs': sorted(name for name in os.listdir(run)
                                 if os.path.isdir(os.path.join(run, name)))}
    elif op == 'read':
        path = _path(root, request['path'])
        try:
            with (gzip.open(path, 'rb') if path.endswith('.gz') else open(path, 'rb')) as f:
                text = f.read(request['limit']).decode('utf-8', 'replace')
        except (OSError, *TRUNCATED):
            text = ''
        answer = {'text': text}
    elif op in ('scan', 'tail', 'range'):
        path = _path(root, request['path'])
        numbered = enumerate(lines(path), 1)
        if op == 'scan':
            every = [literal for _, literals in request['patterns'].values()
                     for literal in literals]
            answer = {'marks': scan(grepped(path, every) or numbered, request['patterns'],
                                    request['first'], request['last'])}
        elif op == 'tail':
            answer = {'hits': tail(numbered, request['lines'])}
        else:
            answer = {'hits': between(numbered, request['start'], request['end'])}
    elif op == 'search':
        work = [(_path(root, rel), request['pattern'], request['flags'], request['max_hits'],
                 request.get('literals'))
                for rel in request['files']]
        if len(work) > 1:
            # fork, so that the workers have this program, which is not in a file.
            with multiprocessing.get_context('fork').Pool(min(PROCESSES, len(work))) as pool:
                found = pool.map(_search, work, chunksize=1)
        else:
            found = [_search(w) for w in work]
        answer = {'hits': [[rel, n, text] for rel, hits in zip(request['files'], found)
                           for n, text in hits or []],
                  'gone': [rel for rel, hits in zip(request['files'], found) if hits is None]}
    else:
        raise ValueError('unknown op %s' % op)
    out.write(json.dumps(answer).encode())


if __name__ == '__main__':
    try:
        handle(json.load(sys.stdin), sys.stdout.buffer)
    except (OSError, ValueError, KeyError, re.error) as e:
        sys.stderr.write('%s: %s\n' % (type(e).__name__, e))
        sys.exit(1)
