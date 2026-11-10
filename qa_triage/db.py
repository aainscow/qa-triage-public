"""The problem database: one YAML record per known problem, in problems/.

A record is of a tracker issue, and its id is the issue's number, such as "76295";
or, for a problem nobody filed an issue for, of the pull request that fixed or
caused it, and its id is "PR" and the PR's number, such as "PR68305". People see
them as TR 76295 and PR 68305."""

import os
import re
from dataclasses import dataclass, field

import yaml

KINDS = ('product', 'test', 'infra')
MATCH_KEYS = ('description', 'reason')
TRACKER_ISSUES = 'https://tracker.ceph.com/issues'
PULLS = 'https://github.com/ceph/ceph/pull'


def record_id(text):
    """The id of the record text names, as people and agents name them: a tracker
    issue's number, URL, "#76295" or "TR 76295"; or a pull request's URL, "PR68305"
    or "PR 68305". Anything else, as it is."""
    text = str(text).strip()
    found = re.fullmatch(r'(?:TR[ -]?|#)?(\d+)|.*/issues/(\d+)/?', text, re.I)
    if found:
        return found.group(1) or found.group(2)
    found = re.fullmatch(r'PR[ -]?#?(\d+)|.*github\.com/ceph/ceph/pull/(\d+)/?', text, re.I)
    if found:
        return f'PR{found.group(1) or found.group(2)}'
    return text


def link(pid):
    """The tracker issue, or the pull request, that a record's id names."""
    return f'{PULLS}/{pid[2:]}' if is_pr(pid) else f'{TRACKER_ISSUES}/{pid}'


def label(pid):
    """A record's id as people see it: TR 76295, or PR 68305."""
    return f'PR {pid[2:]}' if is_pr(pid) else f'TR {pid}' if str(pid).isdigit() else str(pid)


def is_pr(pid):
    return bool(re.fullmatch(r'PR\d+', str(pid)))


@dataclass
class Check:
    file: str
    pattern: re.Pattern

    def __str__(self):
        return f'{self.file}: /{self.pattern.pattern}/'


@dataclass
class Fix:
    pr: str
    branch: str = 'main'
    # `git describe` of the merge, as in the tracker's "Fixed In" field.
    fixed_in: str = ''


@dataclass
class Problem:
    id: str
    # The tracker issue's URL, or the pull request's for a record of a PR.
    tracker: str
    title: str
    kind: str
    match: dict
    confirm: list
    # Where the record hangs in the tree (tree/): it applies to the jobs whose
    # path is one of these or under one, and whose facts its facts' regexes find.
    # Most hang in one place; a cause that shows first as one symptom in some jobs
    # and as another in others, such as a timeout or a cluster-log warning, hangs
    # in each.
    trees: tuple = ()
    facts: dict = field(default_factory=dict)
    deep: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    notes: str = ''
    examples: list = field(default_factory=list)
    # Other tracker issues that people place the same failures in, such as an older
    # issue for the same problem: a job placed in one of them is placed rightly.
    also: list = field(default_factory=list)
    source: str = ''

    def prefilter(self, job, finding=None):
        """True if the tree's finding for the job is at or under the record's path
        and has its facts, and every match key's regex is found in the job's
        pulpito data."""
        if self.trees:
            if not finding or not self.hangs(finding.where):
                return False
            if not all(regex.search(str(finding.facts.get(name, '')))
                       for name, regex in self.facts.items()):
                return False
        return all(regex.search(getattr(job, key))
                   for key, regex in self.match.items())

    def hangs(self, path):
        """True if path is at or under one of the record's places in the tree."""
        return any(under(path, top) for top in self.trees)

    @property
    def label(self):
        return label(self.id)


def under(path, top):
    """True if path is top or below it."""
    return path == top or path.startswith(top + '/')


# Log lines of several times, and one with the time's digits gone: a check that finds
# one of the first and not the last looks at when the job ran, not at what happened.
DATED = [f'{when} INFO:teuthology.run:x' for when in (
    '2025-03-20T19:28:08.012', '2026-01-01T00:00:00.000', '2026-05-05T05:05:05.555',
    '2026-10-17T15:16:41.890', '2026-12-31T23:59:59.999', '2027-07-07T07:07:07.777')]
UNDATED = re.sub(r'\d', 'x', DATED[0])


def _checks(items, where):
    if not isinstance(items, list):
        raise ValueError(f'{where} must be a list')
    checks = []
    for item in items:
        if set(item) != {'file', 'pattern'}:
            raise ValueError(f'{where}: each check needs exactly "file" and "pattern"')
        pattern = re.compile(item['pattern'])
        if any(pattern.search(line) for line in DATED) and not pattern.search(UNDATED):
            raise ValueError(f'{where}: /{item["pattern"]}/ finds the time a line was '
                             'logged, which tells when a job ran, not what failed: two '
                             'problems are told apart by what the logs show; if they are '
                             'one, they are one record, with the other issues under also')
        checks.append(Check(item['file'], pattern))
    return checks


def _trees(value):
    """A record's places in the tree: its tree key is one path, or a list of them."""
    paths = value if isinstance(value, list) else [value] if value else []
    if not all(isinstance(p, str) and p.strip('/') for p in paths):
        raise ValueError('"tree" must be a path, or a list of paths')
    return tuple(p.strip('/') for p in paths)


def parse(data, source=''):
    try:
        for key in ('id', 'title', 'kind'):
            if not data.get(key):
                raise ValueError(f'missing "{key}"')
        pid = str(data['id'])
        # A record of a tracker issue gives its URL; one of a pull request, the PR's.
        if is_pr(pid):
            if not re.fullmatch(rf'{re.escape(PULLS)}/{pid[2:]}/?', str(data.get('pr', ''))):
                raise ValueError(f'a record of a pull request gives "pr": {link(pid)}')
        elif not pid.isdigit():
            raise ValueError(f'its id is its tracker issue\'s number, or PR and the number '
                             f'of a pull request, such as PR68305; not "{pid}"')
        elif not data.get('tracker'):
            raise ValueError('missing "tracker"')
        # A record placed in the tree is found by its path and facts; one that is
        # not needs match keys and confirm checks.
        for key in () if data.get('tree') else ('match', 'confirm'):
            if not data.get(key):
                raise ValueError(f'missing "{key}" (or "tree")')
        if data.get('facts') and not data.get('tree'):
            raise ValueError('"facts" needs "tree"')
        if data['kind'] not in KINDS:
            raise ValueError(f'kind must be one of {", ".join(KINDS)}')
        unknown = set(data.get('match') or {}) - set(MATCH_KEYS)
        if unknown:
            raise ValueError(f'unknown match keys {sorted(unknown)}')
        return Problem(
            id=pid,
            tracker=data['pr'] if is_pr(pid) else data['tracker'],
            title=data['title'],
            kind=data['kind'],
            match={k: re.compile(v) for k, v in (data.get('match') or {}).items()},
            confirm=_checks(data.get('confirm') or [], 'confirm'),
            trees=_trees(data.get('tree')),
            facts={k: re.compile(str(v)) for k, v in (data.get('facts') or {}).items()},
            deep=_checks(data.get('deep', []), 'deep'),
            exclude=_checks(data.get('exclude', []), 'exclude'),
            fixes=[Fix(**f) for f in data.get('fixes', [])],
            notes=data.get('notes', ''),
            examples=[str(e) for e in data.get('examples') or []],
            also=[str(a) for a in data.get('also') or []],
            source=source)
    except (ValueError, TypeError, re.error) as e:
        raise ValueError(f'{source or "record"}: {e}') from e


def load(directory):
    problems = []
    for name in sorted(os.listdir(directory)):
        if name.endswith('.yaml'):
            path = os.path.join(directory, name)
            with open(path) as f:
                problems.append(parse(yaml.safe_load(f), path))
    ids = [p.id for p in problems]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f'duplicate problem ids: {sorted(dupes)}')
    return problems
