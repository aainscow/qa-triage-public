"""The shared library, as a git repository: taking it from where everyone shares
it, and sharing back what was learned.

The db setting names the library: a directory, or a git repository's URL, which is
cloned into the cache and brought up to date when qa_triage starts. qa_triage
share commits what has changed in it, the records, the tree's nodes, the
settled jobs and the scripts, on a branch of its own, pushes it, and proposes it as
a pull request, for a person to review there before everyone has it.

A URL may end in #branch, for a branch of the repository, or #branch:directory,
for a library in a directory of a branch, as a pull request into a bigger
repository holds it: such as ceph's, which anyone with write access to ceph may
push to. Only that directory is fetched, and what is learned is pushed to that
branch, code or not: those who may push to it cannot make branches beside it, and
the pull request is where it is reviewed.
"""

import datetime
import fcntl
import getpass
import os
import re
import shutil
import subprocess
import tempfile

# What a library holds, as the settings that name each part; and the parts that are
# code, which runs on everyone's machine, and so is reviewed before it is used.
PARTS = ('problems', 'nodes', 'labels')


def is_code(path):
    """True if a file of the library is code, which runs on everyone's machine: a
    node, or a script; not nodes/wording.yaml, which only says how the tree's
    answers read."""
    part = path.split('/', 1)[0]
    return part == 'scripts' or part == 'nodes' and not path.endswith('.yaml')


def _git(directory, *args, check=True):
    p = subprocess.run(['git', '-C', directory, *args], capture_output=True, text=True,
                       timeout=300)
    if check and p.returncode:
        raise RuntimeError(f'git {" ".join(args)}: {p.stderr.strip() or p.stdout.strip()}')
    return p.stdout.strip()


# Where the commit of each change that waits for a person's approval is kept, by
# name, for them to inspect it.
PENDING_REFS = 'refs/qa-triage/pending/'


def pending_commit(directory, writes, message, name):
    """A commit of writes ({path in the library: text}) onto the library's HEAD, for
    a person to inspect with git show before approving the change: made without
    touching the library's checkout, and kept by the ref PENDING_REFS + name.
    (the repository it is in, its id); None if the library is not in a git
    repository, or git could not make it."""
    top = root(directory)
    if not top:
        return None
    prefix = os.path.relpath(os.path.realpath(directory), os.path.realpath(top))
    prefix = '' if prefix == '.' else prefix + '/'
    with tempfile.TemporaryDirectory() as tmp:
        # An index of its own: the library's checkout, and what is staged in it, stay.
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, 'index'))

        def git(*args, given=None):
            p = subprocess.run(['git', '-C', top, *args], input=given, capture_output=True,
                               text=True, timeout=120, env=env)
            if p.returncode:
                raise RuntimeError(p.stderr.strip())
            return p.stdout.strip()

        try:
            git('read-tree', 'HEAD')
            for path, text in sorted(writes.items()):
                blob = git('hash-object', '-w', '--stdin', given=text)
                git('update-index', '--add', '--cacheinfo', f'100644,{blob},{prefix}{path}')
            # A partial clone has not every file the tree names, nor needs them.
            tree = git('write-tree', '--missing-ok')
            commit = git('commit-tree', tree, '-p', 'HEAD', '-m', message)
            git('update-ref', PENDING_REFS + name, commit)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return None
    return top, commit


def drop_pending_commit(repository, name):
    """Forget the commit of a change that no longer waits for approval."""
    subprocess.run(['git', '-C', repository, 'update-ref', '-d', PENDING_REFS + name],
                   capture_output=True, timeout=60)


def is_url(db):
    return bool(re.match(r'^(\w+://|git@)', str(db)))


def parse(db):
    """(the repository's URL, its branch or None, the library's directory in it or
    '') of a db setting that is a URL."""
    url, _, rest = str(db).partition('#')
    branch, _, inside = rest.partition(':')
    return url, branch or None, inside.strip('/')


def _clone(db, cache):
    url, branch, _ = parse(db)
    name = re.sub(r'\.git$', '', url.rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1])
    return os.path.join(cache, 'db', (name or 'library') + (f'-{branch}' if branch else ''))


def local(db, cache):
    """Where a library is here: its directory, or, for a URL, its clone's, or the
    directory in its clone that it is in."""
    if not is_url(db):
        return os.path.expanduser(db)
    inside = parse(db)[2]
    return os.path.join(_clone(db, cache), *inside.split('/')) if inside else _clone(db, cache)


def nested(directory):
    """True if the library at directory is a directory of a bigger repository's
    branch, as a pull request's is."""
    return bool(_git(directory, 'rev-parse', '--show-prefix', check=False))


def update(db, cache, say=print):
    """Clone the library, or bring its clone up to date; its directory. A clone
    with changes not yet shared is left as it is."""
    directory = local(db, cache)
    if not is_url(db):
        return directory
    url, branch, inside = parse(db)
    clone = _clone(db, cache)
    os.makedirs(os.path.dirname(clone), exist_ok=True)
    # One qa_triage at a time clones it, or brings it up to date: several started at
    # once on an empty cache would each clone into the same place.
    with open(f'{clone}.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _update(directory, clone, url, branch, inside, say)


def _update(directory, clone, url, branch, inside, say):
    if not os.path.isdir(os.path.join(clone, '.git')):
        argv = ['git', 'clone', '-q'] + (['--single-branch', '--branch', branch] if branch
                                         else [])
        if inside:
            # Of a big repository, only the directory, as it is now.
            argv += ['--depth', '1', '--filter=blob:none', '--sparse']
        subprocess.run(argv + [url, clone], check=True, timeout=1800)
        if inside:
            _git(clone, 'sparse-checkout', 'set', inside)
        return directory
    if _git(directory, 'status', '--porcelain'):
        say(f'The library in {directory} has changes not shared yet: qa_triage share '
            'proposes them; it is not updated until then.')
        return directory
    # What was shared and merged since drops out, however it was merged.
    p = subprocess.run(['git', '-C', directory, 'pull', '-q', '--rebase'], capture_output=True,
                       text=True, timeout=300)
    if p.returncode:
        say(f'Could not update the library in {directory}: {p.stderr.strip()}')
    return directory


def changes(directory):
    """{part: [paths]} of what has changed in the library since its last commit, its
    paths relative to the library."""
    found = {}
    prefix = _git(directory, 'rev-parse', '--show-prefix', check=False)
    for line in _git(directory, 'status', '--porcelain', '--untracked-files=all',
                     '--', '.').splitlines():
        path = line[3:].strip().strip('"')
        path = path[len(prefix):] if prefix and path.startswith(prefix) else path
        part = path.split('/', 1)[0]
        found.setdefault(part, []).append(path)
    return found


def summary(found):
    words = {'problems': 'records', 'nodes': 'nodes', 'labels': 'settled-job files',
             'paths': 'tree-path files', 'memory': 'memory files', 'scripts': 'script files'}
    order = list(words)
    return ', '.join(f'{len(found[part])} {words.get(part, part)}' for part in
                     sorted(found, key=lambda part: (order.index(part) if part in order
                                                     else len(order), part)))


def root(path):
    """The top of the git repository that path is in, or None."""
    p = subprocess.run(['git', '-C', path, 'rev-parse', '--show-toplevel'], capture_output=True,
                       text=True, timeout=60)
    return p.stdout.strip() if p.returncode == 0 else None


def sync(directory, say=print):
    """Bring the library at directory up to date with its remote, before it is
    changed, so that a change is made to what everyone has: changes not committed
    yet are set aside and put back. True if it moved."""
    if not _git(directory, 'remote', check=False):
        return False
    before = _git(directory, 'rev-parse', 'HEAD', check=False)
    p = subprocess.run(['git', '-C', directory, 'pull', '-q', '--rebase', '--autostash'],
                       capture_output=True, text=True, timeout=300)
    if p.returncode:
        say(f'Could not bring the library in {directory} up to date: '
            f'{(p.stderr or p.stdout).strip()[-300:]}')
        return False
    return _git(directory, 'rev-parse', 'HEAD', check=False) != before


class Moved(Exception):
    """Someone pushed to the library's branch after a change was checked against
    it: the change was taken back, to be checked again against what they pushed."""


def publish(directory, message, say=print, gh=None, checked=False, own=None):
    """Commit the library's changes, bring it up to date, and push them to its
    branch, for everyone to have at once; if the remote does not take the push, as
    when the person may not write to it, propose them as share does. A change to
    code, nodes or scripts, which runs on everyone's machine, is always
    proposed, for a person to review first. What was done.

    A checked change, one checked against the library as it is here, is pushed only
    onto that: not merged with what others pushed since, which no one checked it
    with. If someone did, the commit is taken back and Moved raised, saying what
    they pushed.

    own, if given, is the paths this change wrote, relative to the library: any other
    change not shared yet goes with it, named in the message, for no commit to say it
    is what it is not. The tree-path files are bookkeeping, and go unnamed."""
    found = changes(directory)
    if not found:
        return 'Nothing to publish: the library has no changes.'
    if own is not None:
        others = sorted(path for paths in found.values() for path in paths
                        if path not in own and path.split('/', 1)[0] != 'paths')
        if others:
            message += ('\n\nAlso in this commit, made here before and not shared until '
                        'now:\n' + '\n'.join(f'  {path}' for path in others))
    if not _git(directory, 'remote', check=False):
        return share(directory, message, say, gh)
    if any(is_code(path) for paths in found.values() for path in paths) and \
            not nested(directory):
        return _propose_code(directory, message, found, gh)
    _git(directory, 'add', '-A', '.')
    _git(directory, 'commit', '-q', '-m', message)
    if checked:
        pushed = subprocess.run(['git', '-C', directory, 'push', '-q'], capture_output=True,
                                text=True, timeout=300)
        if pushed.returncode and re.search(r'fetch first|non-fast-forward', pushed.stderr):
            _git(directory, 'reset', '-q', '--hard', 'HEAD~1')
            subprocess.run(['git', '-C', directory, 'fetch', '-q'], capture_output=True,
                           timeout=300)
            theirs = _git(directory, 'log', '--format=%h %an: %s', 'HEAD..@{u}',
                          check=False).splitlines()
            raise Moved('the library changed while this change was checked: ' +
                        ('; '.join(theirs[:3]) + (f' and {len(theirs) - 3} more'
                                                   if len(theirs) > 3 else '')
                         if theirs else 'someone pushed to it'))
        if pushed.returncode == 0:
            url = _git(directory, 'remote', 'get-url', _git(directory, 'remote').split()[0])
            return f'Pushed {summary(found)} to {url}.'
        say(f'The library\'s remote did not take the push ({pushed.stderr.strip()[-200:]}); '
            'proposing it instead.')
        return _propose(directory, message, found, gh)
    pulled = subprocess.run(['git', '-C', directory, 'pull', '-q', '--rebase'],
                            capture_output=True, text=True, timeout=300)
    if pulled.returncode:
        subprocess.run(['git', '-C', directory, 'rebase', '--abort'], capture_output=True)
        return (f'Committed {summary(found)}, but could not bring the library up to date '
                f'to push it: {pulled.stderr.strip()[-300:]}')
    pushed = subprocess.run(['git', '-C', directory, 'push', '-q'], capture_output=True,
                            text=True, timeout=300)
    if pushed.returncode == 0:
        url = _git(directory, 'remote', 'get-url', _git(directory, 'remote').split()[0])
        return f'Pushed {summary(found)} to {url}.'
    say(f'The library\'s remote did not take the push ({pushed.stderr.strip()[-200:]}); '
        'proposing it instead.')
    return _propose(directory, message, found, gh)


def share(directory, message=None, say=print, gh=None):
    """Commit the library's changes, and push the commit as a branch of its own, to
    propose as a pull request; with no remote, only commit them. The commit stays
    where it is, so that the changes are used here until everyone has them. What
    was done."""
    found = changes(directory)
    if not found:
        return 'Nothing to share: the library has no changes.'
    if nested(directory):
        # A pull request's branch is shared already: it is pushed to.
        return publish(directory, message or f'Learned: {summary(found)}', say, gh)
    what = summary(found)
    message = message or f'Learned: {what}'
    remote = _git(directory, 'remote', check=False).split()
    _git(directory, 'add', '-A')
    _git(directory, 'commit', '-q', '-m', message)
    if not remote:
        return (f'Committed {what} in {directory}, which has no remote to share it with: '
                'git remote add origin <URL> gives it one.')
    return _propose(directory, message, found, gh)


def _branch():
    return f'learned/{getpass.getuser()}-{datetime.datetime.now().strftime("%Y%m%d-%H%M%S")}'


def _propose(directory, message, found, gh=None):
    """Push the commit at HEAD as a branch of its own, and propose it as a pull
    request. What was done."""
    return _push_and_propose(directory, _branch(), message, found, gh)


def _propose_code(directory, message, found, gh=None):
    """Commit a change to code on a branch of its own, and propose it, leaving the
    library here as everyone has it: the code runs here too only once a person has
    reviewed and merged it. What was done."""
    branch, start = _branch(), _git(directory, 'rev-parse', '--abbrev-ref', 'HEAD')
    _git(directory, 'checkout', '-q', '-b', branch)
    _git(directory, 'add', '-A')
    _git(directory, 'commit', '-q', '-m', message)
    try:
        said = _push_and_propose(directory, branch, message, found, gh)
    finally:
        _git(directory, 'checkout', '-q', start)
    return (said + ' It changes code (nodes or scripts), so it is used, here too, '
            'once someone has reviewed and merged it.')


def _push_and_propose(directory, branch, message, found, gh=None):
    """Push HEAD as branch, to the library's remote, or, if that does not take it, to
    the person's own fork of it on GitHub; and propose it there as a pull request.
    What was done."""
    what = summary(found)
    remote = _git(directory, 'remote', check=False).split()
    url = _git(directory, 'remote', 'get-url', remote[0])
    gh = gh if gh is not None else shutil.which('gh')
    head, where = branch, url
    pushed = subprocess.run(['git', '-C', directory, 'push', '-q', remote[0],
                             f'HEAD:refs/heads/{branch}'], capture_output=True, text=True,
                            timeout=300)
    if pushed.returncode:
        fork = _fork(url, gh) if gh else None
        if not fork:
            return (f'Committed {what}, but could not push it ({pushed.stderr.strip()[-200:]}), '
                    'nor to a fork: it stays here.')
        me, where = fork
        pushed = subprocess.run(['git', '-C', directory, 'push', '-q', where,
                                 f'HEAD:refs/heads/{branch}'], capture_output=True, text=True,
                                timeout=300)
        if pushed.returncode:
            return f'Committed {what}, but could not push it to {where}: it stays here.'
        head = f'{me}:{branch}'
    said = f'Pushed {what} as {branch} to {where}.'
    if gh and 'github.com' in url:
        body = 'What qa_triage learned, for review:\n\n' + '\n'.join(
            f'- {path}' for paths in found.values() for path in paths)
        p = subprocess.run([gh, 'pr', 'create', '--title', message.splitlines()[0],
                            '--body', body, '--head', head], capture_output=True, text=True,
                           timeout=300, cwd=directory)
        said += (f' Proposed as {p.stdout.strip()}' if p.returncode == 0 else
                 f' Could not propose it: {p.stderr.strip()}')
    return said


def _fork(url, gh):
    """(the person's GitHub login, the URL of their fork) of the GitHub repository
    at url, forked if they have none; None if it is theirs, or cannot be forked."""
    found = re.search(r'github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$', url)
    if not found:
        return None
    owner, repo = found.groups()
    me = subprocess.run([gh, 'api', 'user', '--jq', '.login'], capture_output=True,
                        text=True, timeout=120).stdout.strip()
    if not me or me == owner:
        return None
    p = subprocess.run([gh, 'repo', 'fork', f'{owner}/{repo}', '--clone=false',
                        '--remote=false'], capture_output=True, text=True, timeout=300)
    if p.returncode and 'already exists' not in (p.stderr + p.stdout):
        return None
    return me, url.replace(f'{owner}/{repo}', f'{me}/{repo}')
