"""Stand-ins for the network and the judges."""

import os
import tempfile
from fnmatch import fnmatch

import yaml

from qa_triage import db, learn
from qa_triage.sources import Job, LineReader, found_in
from qa_triage.tree import learned

RUN = 'someone-2026-10-12_23:32:34-rados-wip-test-main-distro-default-trial'

# The tree's nodes that the tests are written for, from the library's nodes/, or
# from QA_TRIAGE_NODES: a lesson's checks run the tests on the nodes it changed.
# Their examples are real jobs, which the tests have no files of: they have none.
NODES = os.environ.get('QA_TRIAGE_NODES') or \
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'library', 'nodes')
TREE = {}
for name in ('root.py', 'crash.py', 'crash-assert.py', 'crash-abort.py',
             'crash-abort-bluestore-error.py', 'crash-signal.py', 'crash-client.py', 'infra.py',
             'valgrind.py', 'cluster-log.py', 'test.py', 'command.py', 'hung.py', 'timeout.py',
             'workunit.py'):
    with open(os.path.join(NODES, name)) as f:
        TREE[name] = f.read()
    learned._register(name, TREE[name]).examples = {}


# A lesson's checks run the tool's tests on a change to the nodes: in the tests, they
# pass, rather than run the tests again (see test_learn's TestRunTests).
REAL_RUN_TESTS = learn.run_tests
learn.run_tests = lambda nodes: []


def forget_learned():
    """Unregister the node files a test registered, and register the tree's again."""
    for name, f in list(learned.FILES.items()):
        if TREE.get(name) != f.code:
            learned._unregister(f)
    for name, code in TREE.items():
        if name not in learned.FILES:
            learned._register(name, code).examples = {}


class FakeFiles(LineReader):
    """A job archive held in memory: {path: [lines]}."""

    def __init__(self, files):
        self.files = files
        self.base = f'https://example.invalid/{RUN}/1/'

    def glob(self, pattern):
        parts = pattern.split('/')
        return sorted((path, sum(len(line) + 1 for line in lines))
                      for path, lines in self.files.items()
                      if len(path.split('/')) == len(parts)
                      and all(fnmatch(a, b) for a, b in zip(path.split('/'), parts)))

    def lines(self, rel, size=None):
        yield from self.files[rel]

    def search(self, pattern, regex, max_hits=3, literals=()):
        return self.search_files(self.glob(pattern), regex, max_hits)

    def search_files(self, found, regex, max_hits=3, literals=()):
        hits = []
        for rel, _ in found:
            found = [found_in(rel, n, text, regex) for n, text in enumerate(self.files[rel], 1)
                     if regex.search(text)]
            hits += found[:max_hits]
        return hits

    def text(self, rel, limit=1 << 20):
        return '\n'.join(self.files.get(rel, []))[:limit]


class FakeJudge:
    """Returns a fixed verdict and records what it was given."""

    def __init__(self, verdict, name='fake'):
        self.verdict = verdict
        self.name = name
        self.calls = []

    def __call__(self, workdir, prompts):
        self.calls.append((workdir, prompts))
        return dict(self.verdict, judge=self.name)


def job(id='1', reason='Command failed (workunit test rados/test.sh) on trial1 with status 1',
        description='rados/basic/{tasks/rados_api_tests}', status='fail'):
    return Job(RUN, id, status, description, reason)


def problems(*records):
    """Write records to a temporary problems/ directory and load them."""
    directory = tempfile.mkdtemp(prefix='qa_triage-test-')
    for i, record in enumerate(records):
        with open(os.path.join(directory, f'{i}-{record["id"]}.yaml'), 'w') as f:
            yaml.safe_dump(record, f)
    return db.load(directory)


def record(id='100', reason='rados/test\\.sh', pattern='objecter.op_reply', **extra):
    return dict({'id': id, 'tracker': f'https://tracker.ceph.com/issues/{id}',
                 'title': f'problem {id}', 'kind': 'test', 'match': {'reason': reason},
                 'confirm': [{'file': 'teuthology.log', 'pattern': pattern}]}, **extra)
