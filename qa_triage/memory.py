"""What people answered for each way a job can fail: the library's memory.

A job's failure has a signature: where the tree's explanation of it ended, and
what told it apart there, with hosts, numbers and hashes masked. When people placed
jobs that failed one way in a tracker issue (the RADOS wiki's and the tracker's
answers, `qa_triage answers`), a later job that fails the same way is likely the
same problem. `qa_triage remember` writes, for each run triaged with answers,
memory/<run>.yaml in the library: each signature with the issues people placed its
jobs in, and each issue's title. Triage then gives a job no record places the answer
the memory agrees on, with no judge, whether a record names that issue or not yet:
the jobs people answered teach every later run at once, before any lesson does.

A run is given only what people answered for the runs before it, as when it was
new: so that triaging an older run again does not answer it from what was learned
after it. An answer counts for less as it ages, halving every HALF_LIFE days, so
that where people placed a failure in one issue and later in another, the later
answer wins.

Signatures are kept as the version of how they are made that made them,
NORMALISATION: when the masking of fingerprint.template or signature_of changes, so
does it, and a memory kept by another is left out, until qa_triage remember writes
it again, instead of being looked up by signatures that no longer arise. A mask that
only joins what the masks told apart before, such as a client's name, needs no new
version: the signatures kept are masked again as they are read.
"""

import datetime
import os
import re
from collections import Counter

import yaml

from .db import Problem, link
from .fingerprint import template

# The least number of people's answers for a signature, and the least share of
# them that must agree, for the memory to answer; a signature that is only the
# failure reason, which says less, needs more of them.
LEAST, AGREE, LEAST_REASON = 2, 0.9, 3
# How many days it takes an answer to count for half as much.
HALF_LIFE = 180
# How signatures are made: raise it when fingerprint.template or signature_of change
# what signature a failure has.
NORMALISATION = 1
RUN_DATE = re.compile(r'-(\d{4}-\d\d-\d\d)_\d\d[:_]\d\d[:_]\d\d-')



def run_date(run):
    """The day a run was scheduled, from its name; None if its name does not say."""
    found = RUN_DATE.search(run or '')
    return datetime.date.fromisoformat(found.group(1)) if found else None


class Memory(dict):
    """{signature: Counter of issues, each answer weighed by its age}; jobs:
    {signature: Counter of issues, by how many jobs people placed there}; titles:
    {issue: its title on the tracker}; and stale, the runs whose memory was kept by
    another NORMALISATION."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.titles, self.jobs, self.stale = {}, {}, []


def remasked(signature):
    """A signature as the masks make it now: its keys masked again, which changes
    only what a mask added since then masks; the tree's path is not."""
    path, tree, keys = signature.partition(' | ')
    if not tree:
        return template(signature)
    return ' | '.join([path] + [template(k) for k in keys.split(' | ')])


def signature_of(entry):
    """The signature of a job's failure, as results.json keeps the job, the same as
    its Result's: the tree's path and keys, or else its failure reason."""
    tree = entry.get('tree')
    reason = template((entry.get('reason') or '').strip()) or \
        f"<{entry.get('status')} with no reason>"
    if not tree:
        return reason
    keys = [template(k) for k in tree.get('keys') or []] or [reason]
    return ' | '.join([tree.get('path') or '-'] + keys)


def build(triaged, canonical=lambda number: number):
    """{run: {signature: {issue: number of jobs}}} of the answered jobs of triaged,
    as score.runs gives it: {} for a run none of whose jobs is answered."""
    found = {}
    for run, answers, results in triaged:
        mine = found.setdefault(run, {})
        for r in results:
            signature = signature_of(r)
            if r['job'] in answers:
                answer = canonical(answers[r['job']])
                per = mine.setdefault(signature, {})
                per[answer] = per.get(answer, 0) + 1
    return found


def write(directory, found, titles=None):
    """Write each run's memory to <directory>/<run>.yaml, with the titles, of
    titles ({issue: title}), of the issues it names, and remove the memory of a run
    with none; the files written."""
    os.makedirs(directory, exist_ok=True)
    written = []
    for run, signatures in sorted(found.items()):
        path = os.path.join(directory, run.replace(':', '_') + '.yaml')
        if not signatures:
            if os.path.exists(path):
                os.remove(path)
            continue
        named = {i: (titles or {}).get(i) for answers in signatures.values() for i in answers}
        data = {'run': run, 'normalisation': NORMALISATION, 'signatures': signatures}
        if any(named.values()):
            data['titles'] = {i: t for i, t in named.items() if t}
        with open(path, 'w') as f:
            yaml.safe_dump(data, f, sort_keys=True, width=1000, allow_unicode=True)
        written.append(path)
    return written


def forget(directory, runs):
    """Remove the memory of runs from directory: the files removed."""
    removed = []
    for run in runs:
        path = os.path.join(directory, run.replace(':', '_') + '.yaml')
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed


def load(directory, leave_out=(), before=None):
    """The Memory of every run's memory in directory, but the runs left out; with
    before, a run's name, only of the runs scheduled before it, each answer weighed
    by how long before."""
    memory = Memory()
    if not directory or not os.path.isdir(directory):
        return memory
    day = run_date(before)
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.yaml'):
            continue
        with open(os.path.join(directory, name)) as f:
            data = yaml.safe_load(f) or {}
        run = data.get('run')
        if run in leave_out:
            continue
        if data.get('normalisation', 1) != NORMALISATION:
            memory.stale.append(run)
            continue
        weight, when = 1.0, run_date(run)
        if day and when:
            if when >= day:
                continue
            weight = 0.5 ** ((day - when).days / HALF_LIFE)
        for signature, answers in (data.get('signatures') or {}).items():
            signature = remasked(signature)
            memory.setdefault(signature, Counter()).update(
                {str(k): weight * int(v) for k, v in answers.items()})
            memory.jobs.setdefault(signature, Counter()).update(
                {str(k): int(v) for k, v in answers.items()})
        memory.titles.update({str(k): str(v) for k, v in (data.get('titles') or {}).items()})
    return memory


def recall(memory, signature, specific=True):
    """(issue, jobs) of what people placed the jobs that failed this way in, if
    enough of them agree; else None. specific is False for a signature that is the
    failure reason alone."""
    answers = memory.get(signature) if signature else None
    if not answers:
        return None
    issue, weight = answers.most_common(1)[0]
    # How many jobs people answered, as many as there were, however old; and the
    # share of them that agree, the older counting for less.
    counted = getattr(memory, 'jobs', {}).get(signature) or answers
    least = LEAST if specific else LEAST_REASON
    return (issue, round(counted[issue])) \
        if sum(counted.values()) >= least and weight >= AGREE * sum(answers.values()) else None


def unrecorded(issue, title=None):
    """A stand-in record for a tracker issue that people placed jobs in and no record
    names yet: it has no checks, and places only the jobs the memory places."""
    return Problem(id=str(issue), tracker=link(str(issue)),
                   title=title or 'no record yet', kind='', match={}, confirm=[],
                   source='memory')
