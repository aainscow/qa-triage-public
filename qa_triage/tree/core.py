"""The decision tree that explains a failed job, before any record or agent
looks at it.

A person triaging a job asks one question after another: what went wrong
first? If a daemon crashed, which one, and on what assert? If it was an assert
seen before, what else must be true for it to be the same problem? Each node is
one such question: a function that reads what the job's logs show and answers
with one of

    go(branch, ...)       the answer, and the node it leads to asks the next question;
    done(branch, ...)     the answer, and it explains the failure: nothing more to ask;
    unknown(...)          no answer: the logs do not show one that this node knows;
    judge(question, ...)  the logs hold the answer, but finding it needs judgement.

The branches taken are the job's path, such as "crash/assert", and what each
node said, with the log lines it read, is the explanation. It is conclusive if a
node said done; otherwise it is inconclusive, and a judge decides, given the
explanation. Every answer but unknown cites the lines it was decided by: they are
what a person reading the result checks it against (see Finding.uncited).

The tree grows where it is inconclusive: an unknown or judge answer that names a
branch leads on to the node at that path, if there is one. It also grows where it
stopped at a symptom: a
node at the path of a done answer can explain more, with another done, or leave
the explanation as it was. Where the walk ended without a node is where the next
one would go: Finding.grows_at.

A node is registered by its path: the node at "crash" asks what follows the
root's answer "crash". The nodes are files of the library (see learned.py), which
an analyst may add to or change. Nodes read the landmarks, the lines of teuthology.log
that one pass over it finds for every node (see LANDMARKS), and can search the
job's other files, such as a daemon's log, for more.
"""

import functools
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..fingerprint import fingerprint, template
from ..sources import ArchiveError

LOG = 'teuthology.log'
# How many of each landmark's first and last lines a scan keeps.
FIRST = LAST = 40
# The landmark "reason" is where the log gives this much of the failure reason.
REASON = 80
# A path longer than this is a loop between nodes.
MAX_STEPS = 12

# {name: (regex, [literal])}: the lines of teuthology.log the nodes read, all
# found in one pass. Every line a regex finds must have one of its literals, plain
# strings that grep looks for first: a big log is read many times faster so.
LANDMARKS = {}
# {path: Node}
NODES = {}
# {node's path: {answer: sentence}}: how the tree's answers read to a person, in
# place of the nodes' own sentences: the library's nodes/wording.yaml, which an
# analyst may improve (see worded).
WORDING = {}


@dataclass
class Node:
    path: str
    question: str
    ask: object


def node(path, question):
    """Register a function as the node at path, which asks question."""
    def register(ask):
        if path in NODES:
            raise ValueError(f'two nodes at "{path}"')
        NODES[path] = Node(path, question, ask)
        return ask
    return register


def landmarks(**patterns):
    """Add landmarks: name=(regex, literal, ...)."""
    for name, (pattern, *literals) in patterns.items():
        if name in LANDMARKS:
            raise ValueError(f'two landmarks named {name}')
        if not literals or not all(literals):
            raise ValueError(f'landmark {name} needs literals')
        re.compile(pattern)
        LANDMARKS[name] = (pattern, literals)


@dataclass
class Answer:
    kind: str
    branch: str = ''
    says: str = ''
    evidence: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    # What tells this failure apart from others that took the same branch, such
    # as the assert; jobs with the same path and keys are judged as one.
    key: str = ''
    question: str = ''
    options: list = field(default_factory=list)


def _evidence(hits):
    return [h for h in hits if h]


# A field of a sentence of the wording, {name}, or a brace written twice, as itself.
FIELD = re.compile(r'\{\{|\}\}|\{(\w+)\}')


def worded(at, answer, facts):
    """The sentence a person reads for the answer the node at path at gave: the
    wording's for it, with {name} the fact of that name the tree found by then and
    {says} the node's own sentence; or the node's own, if there is none, or it
    names a fact that was not found."""
    sentence = (WORDING.get(at) or {}).get(answer.branch or answer.kind)
    if not isinstance(sentence, str):
        return answer.says
    values = dict(facts, says=answer.says)
    if any(name and name not in values for name in FIELD.findall(sentence)):
        return answer.says
    return FIELD.sub(lambda m: str(values[m.group(1)]) if m.group(1) else m.group(0)[0],
                     sentence)


def go(branch, says, *evidence, key='', **facts):
    return Answer('go', branch, says, _evidence(evidence), facts, key)


def done(branch, says, *evidence, key='', **facts):
    return Answer('done', branch, says, _evidence(evidence), facts, key)


def unknown(says, *evidence, branch='', **facts):
    """No answer; with a branch, a node added there later can give one."""
    return Answer('unknown', branch, says, _evidence(evidence), facts)


def judge(question, says, *evidence, branch='', options=(), key='', **facts):
    """A question for the judge; with a branch, the path takes it, so that the
    records there are still candidates, and a node added there later can answer
    the question, but until then the explanation stays inconclusive."""
    return Answer('judge', branch, says, _evidence(evidence), facts, key, question,
                  list(options))


@dataclass
class Step:
    node: str
    question: str
    answer: str
    says: str
    evidence: list
    # go, done, judge or unknown.
    kind: str = 'unknown'


@dataclass
class Finding:
    """What the tree found for one job."""
    path: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    keys: list = field(default_factory=list)
    conclusive: bool = False
    # For a judge, if a node asked one.
    question: str = ''
    options: list = field(default_factory=list)
    # Where a node added to the tree would be asked next, if the walk could go on.
    grows_at: str = ''

    @property
    def where(self):
        return '/'.join(self.path)

    def uncited(self):
        """The steps that decided something, with go, done or judge, without citing
        a line of the logs: a person cannot check them."""
        return [s for s in self.steps if s.kind != 'unknown' and not s.evidence]

    def signature(self, job):
        """What groups the jobs that failed the same way: the path, and what the
        nodes said that tells it apart, with numbers, hosts and hashes masked;
        with the failure reason too if the nodes said nothing that does."""
        keys = [template(k) for k in self.keys] or [fingerprint(job)]
        return ' | '.join([self.where or '-'] + keys)

    def progress(self):
        """How far the tree got, in a sentence or two."""
        where = f'"{self.where}"' if self.path else 'its root'
        answered = f'{len(self.path)} question{"" if len(self.path) == 1 else "s"}'
        if self.conclusive:
            text = f'The tree explained it conclusively, answering {answered}, at {where}.'
            if self.grows_at:
                text += (f' A node at "{self.grows_at}" would be asked to explain more, such '
                         'as the cause behind a symptom.')
            return text
        text = f'The tree answered {answered}, to {where}, and stopped, inconclusive' + \
            (f': it asks "{self.question}".' if self.question else '.')
        return text + (f' A node at "{self.grows_at}" would be asked next.' if self.grows_at
                       else ' The tree cannot grow there.')

    def lines(self, limit=300):
        """The explanation as text: what each node asked, answered and read."""
        out = []
        for n, step in enumerate(self.steps, 1):
            out.append(f'{n}. {step.question} {step.answer}: {step.says}')
            out += [f'   {h.file}:{h.line}: {h.text[:limit]}' for h in step.evidence]
        out.append(('Conclusive' if self.conclusive else 'Inconclusive') +
                    (f': {self.where}' if self.path else '') + '.')
        if self.facts:
            out.append('Facts: ' + '; '.join(f'{name}={str(value)[:limit]}'
                                             for name, value in self.facts.items()))
        if self.question:
            out.append(f'Question for a judge: {self.question}' +
                       (f' ({", ".join(self.options)})' if self.options else ''))
        if self.grows_at and not self.conclusive:
            out.append(f'No node at "{self.grows_at}" answers it yet.')
        return out

    def to_json(self):
        return {'path': self.where, 'code': code(), 'conclusive': self.conclusive,
                'facts': self.facts,
                'keys': self.keys, 'question': self.question, 'options': self.options,
                'grows_at': self.grows_at,
                'steps': [{'node': s.node, 'answer': s.answer, 'kind': s.kind,
                           'says': s.says,
                           'evidence': [[h.file, h.line, h.text] for h in s.evidence]}
                          for s in self.steps]}


@functools.cache
def code():
    """What tells the tree's engine from other code: a digest of its modules, and
    of the searches the archive's host makes for them (the nodes are not in it: a
    lesson walks again the jobs a change to them can reach). A path that other code found
    may not be the path this finds, so a path is kept only with the code it took."""
    here = os.path.dirname(os.path.abspath(__file__))
    names = [os.path.join(here, n) for n in sorted(os.listdir(here)) if n.endswith('.py')]
    digest = hashlib.sha256()
    for name in names + [os.path.join(os.path.dirname(here), 'archive_host.py')]:
        with open(name, 'rb') as f:
            digest.update(os.path.basename(name).encode() + b'\0' + f.read())
    return digest.hexdigest()[:12]


_TIME = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)')


def when(hit):
    """The time teuthology logged hit at, or None."""
    m = _TIME.match(hit.text) if hit else None
    return datetime.fromisoformat(m.group(1)) if m else None


class Context:
    """What a node reads: the job's pulpito data, its landmarks, its files, and
    the facts the nodes before it found."""

    def __init__(self, job, files, marks):
        self.job = job
        self.files = files
        self.marks = marks
        self.facts = {}
        # What a node found that the nodes after it need too, by name: found once.
        self.found = {}

    def hits(self, name):
        """The first and last lines found for a landmark."""
        return self.marks.get(name, (0, []))[1]

    def count(self, name):
        return self.marks.get(name, (0, []))[0]

    def first(self, name):
        found = self.hits(name)
        return found[0] if found else None

    def last(self, name):
        found = self.hits(name)
        return found[-1] if found else None

    def match(self, name, hit):
        """The landmark's match in hit, for its groups."""
        return re.search(LANDMARKS[name][0], hit.text) if hit and name in LANDMARKS else None

    def search(self, pattern, regex, *literals, max_hits=3):
        """Lines that regex finds in the files matching pattern, such as
        remote/*/log/ceph-osd.2.log.gz. Give literals, plain strings that every
        line it finds has, so that a big log is searched fast."""
        return self.files.search(pattern, re.compile(regex), max_hits, literals)

    def glob(self, pattern):
        return self.files.glob(pattern)

    def tail(self, rel=LOG, count=50):
        return self.files.tail(rel, count)

    def between(self, start, end, rel=LOG):
        return self.files.between(rel, start, end)


def explain(job, files):
    """The Finding for a job: the tree's path through it, and why."""
    patterns = dict(LANDMARKS)
    reason = job.reason.strip().splitlines()[0][:REASON] if job.reason.strip() else ''
    if reason:
        patterns['reason'] = (re.escape(reason), [reason])
    # The cluster log's line that a reason quotes, where teuthology.log has it, without
    # the quotes, and with its runs of spaces, which the reason has as one: the line the
    # check of the cluster log found. Its time, to the microsecond, is the literal.
    logged = re.match(r'"([^\n]+?)" in cluster log', job.reason.strip())
    words = logged.group(1)[:REASON].split() if logged else []
    if words:
        patterns['cluster_line'] = (r'\s+'.join(map(re.escape, words)), [words[0]])
    marks = files.scan(LOG, patterns, FIRST, LAST) if files.glob(LOG) else {}
    ctx = Context(job, files, marks)
    finding = Finding(facts=ctx.facts)
    at, unsure, refining = '', False, False
    while len(finding.steps) < MAX_STEPS:
        found = NODES.get(at)
        if not found:
            if not unsure and not refining:
                finding.steps.append(Step(at, f'What follows "{at}"?', 'unknown',
                                          'No node asks yet.', []))
            finding.grows_at = at
            return finding
        failed = False
        try:
            answer = found.ask(ctx)
        except ArchiveError:
            raise
        except Exception as e:  # noqa: BLE001 - one node's bug must not stop a run
            answer, failed = unknown(f'the node failed: {type(e).__name__}: {e}'), True
        if refining and answer.kind in ('unknown', 'judge'):
            # A node below a conclusive answer that does not know this job leaves
            # the explanation as it was; one that failed says so.
            if failed:
                finding.steps.append(Step(at, found.question, 'unknown', answer.says, []))
            return finding
        finding.steps.append(Step(at, found.question, answer.branch or answer.kind,
                                  worded(at, answer, {**ctx.facts, **answer.facts}),
                                  answer.evidence, answer.kind))
        ctx.facts.update(answer.facts)
        if answer.key:
            finding.keys.append(answer.key)
        if answer.branch:
            finding.path.append(answer.branch)
        at = finding.where
        if answer.kind == 'done':
            finding.conclusive = True
            finding.question, finding.options = '', []
            # A node below it, if there is one, can explain more.
            refining = True
            continue
        unsure = answer.kind in ('unknown', 'judge')
        if unsure:
            # A node that cannot tell keeps the question the one before it asked.
            if answer.question:
                finding.question, finding.options = answer.question, answer.options
            if not answer.branch:
                return finding
    finding.steps.append(Step(at, '', 'unknown', f'the path is over {MAX_STEPS} steps', []))
    return finding
