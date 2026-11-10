"""Is a problem's fix already in the build a job tested?"""

import re
import subprocess

# 21.3.0-4162-gf64c8d74 (git describe) or 21.3.0-4162.gf64c8d74 (rpm).
VERSION = re.compile(r'v?(\d+\.\d+\.\d+)-(\d+)[-.]g([0-9a-f]{7,40})')


def parse_version(text):
    """(tag, commits since tag, sha) from the first version string in text."""
    m = VERSION.search(text or '')
    return (m.group(1), int(m.group(2)), m.group(3)) if m else None


def pr_number(url):
    m = re.search(r'/pull/(\d+)', url)
    return m.group(1) if m else None


def applies(fix, branch):
    """A fix on 'main' applies to main and to QA branches ending in '-main'."""
    return branch == fix.branch or branch.endswith('-' + fix.branch)


def fix_in_build(fix, build, git_dir=None):
    """True, False, or None if it cannot be told.

    If the build has fewer commits since the same tag than the fix's merge, it
    cannot contain the merge. Otherwise ask a local git clone if there is one.
    """
    fixed = parse_version(fix.fixed_in)
    if not fixed or not build:
        return None
    if fixed[0] == build[0] and build[1] < fixed[1]:
        return False
    if git_dir:
        rc = subprocess.run(['git', '-C', git_dir, 'merge-base', '--is-ancestor',
                             fixed[2], build[2]], capture_output=True).returncode
        if rc in (0, 1):
            return rc == 0
    return None
