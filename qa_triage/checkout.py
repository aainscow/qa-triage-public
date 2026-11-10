"""Check out the source a job ran: a git worktree of the ceph clone, at the
commit the job's config names, for the analyst and for people to read.

A job's config gives sha1, the build it ran, and suite_sha1, the qa suite it
ran, with the repositories they came from. Each commit gets one worktree,
<worktrees>/<sha1>, shared by every job that ran it; a commit the clone does not
have is fetched first, by its sha1 alone, so that no branch or tag is added.
Remove a worktree that is no longer needed with `git worktree remove`.
"""

import os
import re
import subprocess

import yaml

SHA1 = re.compile(r'[0-9a-f]{40}')
# Where builds come from when a config does not say: wip branches are pushed here.
CEPH_CI = 'https://github.com/ceph/ceph-ci.git'
URL = re.compile(r'(https|file)://\S+')
# A PR under test, as ptl-tool merges it into a testing branch, or as git pull
# merges its branch.
MERGED = re.compile(r"Merge (?:PR #(\d+)|branch '([^']+)' of (\S+)) into wip-")
# GitHub's merge of a PR: on the base branch, where a build's own changes begin, or
# into a developer's branch.
BASE_MERGE = GITHUB_MERGE = re.compile(r'Merge pull request #(\d+) from ()()')
# A PR that a commit message names: #N, PR N, PRs N + M, .../pull/N, or N followed by
# a commit id, as "squash: PRs 72643 e0632f6a69b + 72644".
_LISTED = r'(?:\s+(?:at\s+)?[0-9a-f]{7,40}\b)?'
NAMED_PR = re.compile(r'(?:(?<![\w/&])#|\bPRs?\s+|/pull/)(\d{4,6})\b' + _LISTED +
                      r'((?:\s*(?:[+,&]|\band\b)\s*\d{4,6}\b' + _LISTED + r')*)'
                      r'|\b(\d{5,6})\s+[0-9a-f]{9,40}\b')
REVERT = re.compile(r'\brevert', re.I)
# The most of a branch's history looked through for them, the most commits a build
# adds that are read, and the most lines of each one's qa/ diff shown.
HISTORY, ADDED, DIFF_LINES = 300, 2000, 400


class CheckoutError(Exception):
    pass


def versions(files):
    """{'source': (sha1, repo), 'suite': (sha1, repo)} from the job's config, the
    suite only if it differs from the source; {} if the config gives neither."""
    for name in ('orig.config.yaml', 'config.yaml'):
        try:
            config = yaml.safe_load(files.text(name) or '') or {}
        except yaml.YAMLError:
            config = {}
        if isinstance(config, dict) and config.get('sha1'):
            break
    else:
        return {}
    found = {}
    for key, sha1, repo in (('source', 'sha1', 'repo'), ('suite', 'suite_sha1', 'suite_repo')):
        value, url = str(config.get(sha1) or ''), str(config.get(repo) or '')
        if SHA1.fullmatch(value) and value not in [v for v, _ in found.values()]:
            found[key] = (value, url if URL.fullmatch(url) else CEPH_CI)
    return found


def _git(repo, *args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    if p.returncode:
        said = (p.stderr.strip().splitlines() or [f'exit {p.returncode}'])[-1]
        raise CheckoutError(f'git {" ".join(args[:2])}: {said}')
    return p.stdout.strip()


def fetch(repo, sha1, url, say=print):
    """Fetch sha1 from url into repo, unless repo has it already."""
    if not SHA1.fullmatch(sha1):
        raise CheckoutError(f'{sha1} is not a commit id')
    try:
        _git(repo, 'cat-file', '-e', sha1 + '^{commit}')
    except CheckoutError:
        say(f'Fetching {sha1[:10]} from {url} into {repo}...')
        _git(repo, 'fetch', '--no-tags', '--quiet', url if URL.fullmatch(url) else CEPH_CI,
             sha1)


def worktree(repo, sha1, url, directory, say=print):
    """The path of a worktree of repo at sha1, made in directory if there is none
    yet; sha1 is fetched from url first if repo does not have it."""
    if not SHA1.fullmatch(sha1):
        raise CheckoutError(f'{sha1} is not a commit id')
    path = os.path.join(directory, sha1)
    if os.path.isdir(path):
        if _git(path, 'rev-parse', 'HEAD') != sha1:
            raise CheckoutError(f'{path} is not at {sha1}: remove it')
        return path
    fetch(repo, sha1, url, say)
    os.makedirs(directory, exist_ok=True)
    say(f'Checking out {sha1[:10]} in {path}...')
    _git(repo, 'worktree', 'add', '--detach', '--quiet', path, sha1)
    return path


def under_test(repo, rev='HEAD'):
    """What a testing branch, at rev in repo, adds to the branch it was made from:
    [(what, commit, parent, [files])], newest first, for each PR merged into it as
    ptl-tool merges the PRs under test, or as git pull merges a PR's branch, and
    each commit made on it, as on a developer's own branch. The history is followed
    back to the first commit of neither kind, which is the base branch's own merge
    of a PR; [] if it has no PR merged and does not reach one, as for a run of the
    base branch itself, or a branch whose history is not of that shape."""
    found, merged, based = [], False, False
    log = _git(repo, 'log', '--first-parent', '-n', str(HISTORY), '--format=%H %P%x09%s',
               rev)
    for line in log.splitlines():
        ids, subject = line.split('\t', 1)
        commit, *parents = ids.split()
        pr = MERGED.match(subject)
        if not parents or (len(parents) > 1 and not pr):
            based = len(parents) > 1
            break
        merged = merged or bool(pr)
        what = (f'PR #{pr.group(1)}' if pr and pr.group(1) else
                f'branch {pr.group(2)} of {pr.group(3)}' if pr else f'commit "{subject}"')
        files = _git(repo, 'diff', '--name-only', parents[0], commit).split()
        found.append((what, commit, parents[0], files))
    return found if merged or based else []


def added(repo, rev):
    """[(commit, [parents], author's email, message, [files])] of every commit a build,
    at rev in repo, adds to the base branch it was made from, newest first: its history is
    followed back along its first parents to the base branch's own merge of a PR, as
    GitHub merges one, and the commits that the merges on the way bring in are added
    too. A merge's files are those it changes from its first parent. [] for a run of
    the base branch itself, or if no such merge is within HISTORY commits."""
    log = _git(repo, 'log', '--first-parent', '-n', str(HISTORY), '--format=%H%x09%s', rev)
    base = next((line.split('\t', 1)[0] for line in log.splitlines()
                 if BASE_MERGE.match(line.split('\t', 1)[-1])), None)
    if not base:
        return []
    found = []
    text = _git(repo, 'log', '-n', str(ADDED), '--format=%x01%H%x00%P%x00%ae%x00%B%x02',
                '--name-only', '--diff-merges=first-parent', rev, f'^{base}')
    for entry in text.split('\x01')[1:]:
        head, _, files = entry.partition('\x02')
        commit, parents, email, message = head.split('\x00', 3)
        found.append((commit, parents.split(), email, message.strip(), files.split()))
    return found


def fetch_all(repo, sha1s, url=CEPH_CI):
    """Fetch those of sha1s that repo does not have from url, in one fetch if it can,
    else one by one: [those it could not fetch]."""
    missing = []
    for sha1 in sha1s:
        try:
            _git(repo, 'cat-file', '-e', sha1 + '^{commit}')
        except CheckoutError:
            missing.append(sha1)
    if not missing:
        return []
    try:
        _git(repo, 'fetch', '--no-tags', '--quiet', url, *missing)
        return []
    except CheckoutError:
        pass
    failed = []
    for sha1 in missing:
        try:
            _git(repo, 'fetch', '--no-tags', '--quiet', url, sha1)
        except CheckoutError:
            failed.append(sha1)
    return failed


def named_prs(message):
    """The numbers of the PRs a commit message names, in their order: as #N, PR N, a
    list after PRs, a PR's URL, or N with a commit id after it, as one who squashes
    or merges PRs into a branch to test them writes; but none in another URL, such
    as a tracker issue's, nor any a revert names."""
    if REVERT.search(message.split('\n', 1)[0]):
        return []
    text = re.sub(r'https?://\S+', lambda m: m.group(0) if '/pull/' in m.group(0) else '',
                  message)
    found = []
    for m in NAMED_PR.finditer(text):
        found.append(m.group(1) or m.group(3))
        found += re.findall(r'(?:[+,&]|\band\b)\s*(\d{4,6})', m.group(2) or '')
    return list(dict.fromkeys(found))


def build_prs(commits, files_of, linked=lambda commits: {},
              pr_of_branch=lambda url, branch: None):
    """{PR number: [how it is known]} of the PRs a build tests, from commits, those it
    adds to its base branch as added gives them: those merged into it, as
    ptl-tool, git pull or GitHub merge them; those GitHub links its commits to, as
    linked(commits) gives them, {commit: [numbers]}; and those its commit messages
    name, as one who squashes them into a branch names them, whose files, as
    files_of(number) gives them (None for no such PR), the commit changes too: a
    tracker issue's number, or a PR named for some other reason, is not taken for
    one. pr_of_branch(url, branch) gives the number of the PR whose head a merged
    branch is."""
    found, naming = {}, {}

    def note(number, how):
        if number and how not in found.setdefault(str(number), []):
            found[str(number)].append(how)

    for commit, parents, _, message, files in commits:
        subject = message.split('\n', 1)[0]
        merged = MERGED.match(subject) or GITHUB_MERGE.match(subject)
        if merged and len(parents) > 1:
            number = merged.group(1) or pr_of_branch(merged.group(3), merged.group(2))
            note(number, 'merged into its branch')
            if number:
                continue
        for number in named_prs(message):
            if set(files) & set(files_of(number) or ()):
                naming[number] = naming.get(number, 0) + 1
    for number, n in naming.items():
        note(number, 'a commit that changes its files names it' if n == 1 else
                     f'{n} commits that change its files name it')
    for commit, numbers in linked([c for c, parents, *_ in commits
                                   if len(parents) == 1]).items():
        for number in numbers:
            note(number, 'its commits are in the build')
    return found


def naming(repo, commit, paths, names):
    """The paths, of those given, whose file at commit has any of names in it."""
    return sorted(set().union(*naming_each(repo, commit, paths, names).values()))


def naming_each(repo, commit, paths, names):
    """{name: {path}}: for each of names, the paths, of those given, whose file at
    commit has it in it. One git grep finds them all."""
    if not paths or not names:
        return {}
    p = subprocess.run(['git', '-C', repo, 'grep', '-o', '-F', *sum((['-e', n] for n in names),
                                                                      []),
                        commit, '--', *paths], capture_output=True, text=True)
    # git grep answers <commit>:<path>:<what matched>, a name or one that has it in it;
    # it exits 1 if none has them.
    found = {}
    for line in p.stdout.splitlines():
        parts = line.split(':', 2)
        if len(parts) == 3:
            for name in names:
                if name in parts[2]:
                    found.setdefault(name, set()).add(parts[1])
    return found


def touches(repo, parent, commit, path, names):
    """True if the change from parent to commit changes any of the functions names,
    in path: in a line it changes, or in the function a change is in, as git's hunk
    header names it."""
    diff = _git(repo, 'diff', '-U0', parent, commit, '--', path)
    lines = [line for line in diff.splitlines()
             if line.startswith(('@@', '+', '-')) and not line.startswith(('+++', '---'))]
    return any(re.search(rf'\b{re.escape(name)}\b', line) for name in names for line in lines)


def under_test_text(repo, changes, shown=lambda path: path.startswith('qa/'),
                    most_files=None):
    """under_test's changes, as text: the files each changes, at most most_files of
    them, and the diff of those that shown(path) is true for."""
    text, diffs = '', ''
    for what, commit, parent, files in changes:
        listed = files[:most_files] if most_files else files
        more = f', and {len(files) - len(listed)} more' if len(listed) < len(files) else ''
        text += f'{what} ({commit[:11]}): {", ".join(listed) or "no files"}{more}\n'
        paths = [path for path in files if shown(path)]
        diff = _git(repo, 'diff', parent, commit, '--', *paths).splitlines() if paths else []
        if len(diff) > DIFF_LINES:
            diff = diff[:DIFF_LINES] + [f'... {len(diff) - DIFF_LINES} more lines']
        if diff:
            diffs += f'\n{what}, its changes to {", ".join(paths)}:\n' + '\n'.join(diff) + '\n'
    return text + diffs


def check_out(files, repo, directory, say=print):
    """{'source': path, 'suite': path} of worktrees at what the job ran, the suite
    only if it differs; any that cannot be checked out are left out, and said."""
    found = {}
    if not repo:
        return found
    for name, (sha1, url) in versions(files).items():
        try:
            found[name] = worktree(repo, sha1, url, directory, say)
        except CheckoutError as e:
            say(f'Could not check out {sha1[:10]} for the {name}: {e}')
    return found
