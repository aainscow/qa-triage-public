"""Opening a file of a job's archive at a line, such as a result cites, in the
person's own editor or viewer: qa_triage has no viewer of its own.

The editor setting is a command with {file} and {line} in it, such as
lnav -c ':goto {line}' {file}; a command without {file} is given +{line} {file},
as vi, vim, emacs, nano and less take them. Without the setting it is $VISUAL, or
else $EDITOR, or else vim.

With the archive on a host, as trial200:/a is, the file is opened there, over ssh,
where it is: nothing is copied, and a log the archive has gzipped is opened as it
is, which vim and lnav read. There the editor is the host's own $VISUAL or $EDITOR,
unless the setting names one. With edit_on: here, or the files downloaded over
https, the file is opened here, in a copy decompressed into the cache.
"""

import os
import posixpath
import shlex

from .sources import SshJobFiles

DEFAULT = 'vim'
AT_LINE = '+{line} {file}'


def _command(editor):
    return editor if '{file}' in editor else f'{editor} {AT_LINE}'


def argv(files, job, rel, line=1, editor=None, here=False, cache='.', environ=os.environ):
    """The command that opens rel, a file of job's archive, files, at line: on the
    archive's host, unless here is true, or the archive is not on a host."""
    real = files.real(rel)
    if isinstance(files, SshJobFiles) and not here:
        # The host's shell expands its own editor.
        command = _command(editor or '${VISUAL:-${EDITOR:-%s}}' % DEFAULT)
        path = shlex.quote(posixpath.join(files.root, real))
        return files.ssh + ['-t', files.host,
                            command.replace('{file}', path).replace('{line}', str(line))]
    command = _command(editor or environ.get('VISUAL') or environ.get('EDITOR') or DEFAULT)
    path = copy(files, job, rel, real, cache)
    return [word.replace('{file}', path).replace('{line}', str(line))
            for word in shlex.split(command)]


def copy(files, job, rel, real, cache):
    """A copy of rel, which the archive has as real, decompressed, in the cache; its
    path. It is made once: a job's files do not change once it has ended."""
    plain = real[:-3] if real.endswith('.gz') else real
    path = os.path.join(cache, 'opened', job.run, job.id, *plain.split('/'))
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = f'{path}.{os.getpid()}'
        with open(part, 'w', errors='replace') as f:
            for line in files.lines(rel):
                f.write(line + '\n')
        os.replace(part, path)
    return path
