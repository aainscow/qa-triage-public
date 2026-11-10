"""The shared library's scripts: programs people wrote to pull debug data out of a
job's logs, such as the I/O that never finished, a PG's history, or a test's
operations on one object, which the analyst can ask to run on a job.

A library is a directory with problems/, nodes/, labels/ and scripts/; each script
is a directory in scripts/ with the program, and script.yaml, which says what it
does and what it reads:

    about: EC I/O that started and never finished, by object, in OSD logs
    run: find_hung_io            # the program, in this directory; it gets the files
    files: remote/*/log/ceph-osd.*.log.gz     # a glob in the job's archive
    pattern: 'ECCommon|start|finish'          # optional: only these lines are read
    args: '--oid OID'            # optional: what the analyst may add, for its prompt

The files are copied from the archive, decompressed, and only the lines the
pattern finds if it has one; the program runs here, on the copies, never on the
archive's host. What it prints is its answer.
"""

import os
import re
import shlex
import subprocess
from dataclasses import dataclass

import yaml

MANIFEST = 'script.yaml'
# The most files a script gets, the most lines of each that a pattern keeps, and
# the most bytes of a file copied whole.
MAX_FILES, MAX_LINES, COPY_LIMIT = 30, 200000, 50 << 20
TIMEOUT = 600
OUTPUT_LIMIT = 1 << 20


@dataclass
class Script:
    name: str
    about: str
    program: str
    files: str
    pattern: str = ''
    args: str = ''


def scripts_of(problems_dir):
    """The scripts of the library that problems_dir is in: {name: Script}."""
    directory = os.path.join(os.path.dirname(os.path.abspath(problems_dir)), 'scripts')
    found = {}
    if not os.path.isdir(directory):
        return found
    for name in sorted(os.listdir(directory)):
        manifest = os.path.join(directory, name, MANIFEST)
        if not os.path.isfile(manifest):
            continue
        with open(manifest) as f:
            data = yaml.safe_load(f) or {}
        program = os.path.join(directory, name, str(data.get('run', '')))
        if not (data.get('about') and data.get('files') and os.path.isfile(program)):
            continue
        found[name] = Script(name, str(data['about']), program, str(data['files']),
                             str(data.get('pattern') or ''), str(data.get('args') or ''))
    return found


def listing(scripts):
    """The scripts, as text for the analyst."""
    return ''.join(f'{s.name}: {s.about}. It reads {s.files}' +
                   (f', the lines matching /{s.pattern}/' if s.pattern else '') +
                   (f'; it takes: {s.args}' if s.args else '') + '\n'
                   for s in scripts.values())


def run(script, files, workdir, args=''):
    """Run script on a job's files, copied into workdir: what it printed."""
    inputs = os.path.join(workdir, 'script-input', script.name)
    os.makedirs(inputs, exist_ok=True)
    found = files.glob(script.files)[:MAX_FILES]
    if not found:
        return f'{script.name}: the job has no {script.files}\n'
    regex = re.compile(script.pattern) if script.pattern else None
    paths = []
    for rel, size in found:
        local = os.path.join(inputs, re.sub(r'\.gz$', '', rel).replace('/', '_'))
        with open(local, 'w') as out:
            if regex:
                for hit in files.search_files([(rel, size)], regex, max_hits=MAX_LINES):
                    out.write(hit.text + '\n')
            else:
                written = 0
                for line in files.lines(rel, size):
                    written += len(line) + 1
                    if written > COPY_LIMIT:
                        break
                    out.write(line + '\n')
        paths.append(local)
    # The analyst's arguments go to the program as words, never to a shell.
    argv = [script.program, *shlex.split(args or ''), *paths]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, cwd=inputs)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return f'{script.name}: could not run it ({e})\n'
    said = p.stdout[:OUTPUT_LIMIT]
    if p.returncode:
        said += f'\n{script.name} exited {p.returncode}: {p.stderr.strip()[-2000:]}\n'
    return said
