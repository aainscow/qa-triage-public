"""The machines of a run that failed many of its jobs in different ways.

A test that fails the same way on any machine is the test's or the product's
problem; a machine whose jobs fail in several different ways, none of them a known
problem of the test or the product, is more likely the machine's: a bad disk or
network card, a full file system, a reimage gone wrong. The machines of a job are
the targets its config names.
"""

from collections import defaultdict

import yaml

# The least number of a machine's failed jobs, each failing a different way, that
# make it suspect.
LEAST = 3


def machines(files):
    """The short names of the machines a job ran on, from its config's targets."""
    for name in ('config.yaml', 'orig.config.yaml'):
        try:
            config = yaml.safe_load(files.text(name) or '') or {}
        except yaml.YAMLError:
            continue
        targets = config.get('targets') if isinstance(config, dict) else None
        if targets:
            return sorted({str(t).split('@')[-1].split('.')[0] for t in targets})
    return []


def crowded(results, machines_of, least=LEAST):
    """{machine: [results]} of the machines that failed at least `least` jobs that no
    record of the test or the product explains, in at least `least` different ways,
    the most first."""
    found = defaultdict(list)
    for r in results:
        if r.problem and r.problem.kind in ('product', 'test'):
            continue
        for machine in machines_of(r):
            found[machine].append(r)
    busy = {m: rs for m, rs in found.items()
            if len(rs) >= least and len({r.signature for r in rs}) >= least}
    return dict(sorted(busy.items(), key=lambda item: -len(item[1])))


def note(busy):
    """Note on each result of a crowded machine that it may be at fault; lines for the
    report."""
    lines = []
    for machine, rs in busy.items():
        ways = len({r.signature for r in rs})
        for r in rs:
            r.notes.append(f'{machine} failed {len(rs)} jobs of this run in {ways} different '
                           'ways: the machine may be at fault')
        lines.append(f'{machine} failed {len(rs)} jobs in {ways} different ways, none a known '
                     'problem of the test or the product: ' + ', '.join(r.job.id for r in rs))
    return (['Machines that may be at fault:'] + [f'  {line}' for line in lines] + ['']
            if lines else [])
