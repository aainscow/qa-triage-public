"""qa_triage score: how often the tree and the records gave the answers people
gave, from runs triaged before and the answers `qa_triage answers` wrote.

For each run of the answers' index that has a results.json, each job people
placed is counted right if the tool placed it there, or in a problem whose record
says people place it there too (`also`); else wrong, and sure or not. Each record
is scored by how often its placements were right, and the disagreements and the
problems the tool did not know are counted, for the analyst to learn from first:
the commonest are where a lesson, or an `also`, gains most. A job the memory alone
placed rightly, in an issue no record names yet, is right, and still a lesson: a
record places such jobs the first time they fail, which the memory cannot.

It also says what decided the jobs (report.STAGES), how much of each suite was
placed rightly, and which records' checks matched the same jobs.
"""

import json
import os
import re
from collections import Counter, defaultdict

import yaml

from .memory import RUN_DATE
from .report import STAGES, decided, stage_of

# The records a result says matched, when more than one did.
MATCHED = re.compile(r'^matched (.+)$')


def runs(answers_dir, runs_dir, settled=None):
    """[(run, {job id: answer}, [results.json entries])] of the index's runs that
    were triaged in runs_dir. settled, {<run>/<job>: problem id or None} of the jobs
    a person settled, is their answer instead: a job settled in none has none."""
    with open(os.path.join(answers_dir, 'index.yaml')) as f:
        index = yaml.safe_load(f) or {}
    found = []
    for run, name in sorted(index.items()):
        path = os.path.join(runs_dir, run, 'results.json')
        if not os.path.exists(path):
            continue
        with open(os.path.join(answers_dir, name)) as f:
            answers = {str(j): str(p) for j, p in (yaml.safe_load(f) or {}).items()}
        for where, problem in (settled or {}).items():
            mine, _, job = where.rpartition('/')
            if mine != run:
                continue
            if problem:
                answers[job] = problem
            else:
                answers.pop(job, None)
        with open(path) as f:
            found.append((run, answers, json.load(f)))
    return found


def score(triaged, problems, canonical=lambda number: number):
    """The counts and the lists that lines() reports, for triaged as runs() gives it;
    problems are the records, for each one's `also`, and canonical gives the issue
    an answer's issue duplicates or backports."""
    also = {p.id: set(p.also) for p in problems}
    total, by_record, by_suite = Counter(), defaultdict(Counter), defaultdict(Counter)
    # How many jobs each stage decided, and how many of those as people did.
    stages, stages_right, overlaps = Counter(), Counter(), Counter()
    pairs, unknown, examples = Counter(), Counter(), defaultdict(list)
    # {issue: [(run, results.json entry)]} of the jobs the memory alone placed
    # rightly, in an issue no record names.
    recordless = defaultdict(list)
    for run, answers, results in triaged:
        for r in results:
            if r['job'] not in answers:
                continue
            answer, said = canonical(answers[r['job']]), r.get('problem')
            right = said == answer or answer in also.get(said, ())
            stages[stage_of(r)] += 1
            stages_right[stage_of(r)] += right
            suite = by_suite[suite_of(run)]
            suite['jobs'] += 1
            suite['right'] += right
            suite['placed'] += bool(said and r.get('sure'))
            suite['placed right'] += bool(said and r.get('sure') and right)
            if r.get('verdict') == 'ambiguous':
                for note in r.get('notes') or []:
                    found = MATCHED.match(note)
                    if found:
                        overlaps[found.group(1)] += 1
            total['jobs'] += 1
            total['right'] += right
            total['also'] += right and said != answer
            if not r.get('sure'):
                total['unsure'] += 1
            elif not right:
                total['sure, wrong'] += 1
            if said:
                by_record[said]['placed'] += 1
                by_record[said]['right'] += right
            if right and said and said not in also:
                total['memory'] += 1
                recordless[answer].append((run, r))
            if not right:
                if said:
                    pairs[(said, answer)] += 1
                elif r.get('sure'):
                    unknown[answer] += 1
                examples[(said, answer)].append((run, r))
    return {'total': total, 'records': by_record, 'pairs': pairs, 'unknown': unknown,
            'examples': examples, 'recordless': recordless, 'suites': by_suite,
            'stages': stages, 'stages right': stages_right, 'overlaps': overlaps}


def by_stage(stages, right):
    """A line of how many of the jobs each stage decided it placed as people did:
    how right the judge is, apart from the rest."""
    parts = [f'{what} {right[name]} of {stages[name]} ({100 * right[name] // stages[name]}%)'
             for name, what in STAGES if stages[name]]
    return ('Placed as people did, of the jobs each decided: ' + ', '.join(parts) + '.'
            if parts else '')


def suite_of(run):
    """The suite a run ran, from its name, such as rados or orch: the first part of
    the suite's path."""
    found = RUN_DATE.search(run)
    rest = run[found.end():] if found else run
    return rest.split('-', 1)[0].split(':', 1)[0] or '?'


def precision(found):
    """{problem id: {'placed': n, 'right': n}} from score's counts: how often the jobs
    people answered that the tool placed in each problem were placed rightly, for
    people to weigh its placements by (report.PRECISION)."""
    return {pid: {'placed': c['placed'], 'right': c['right']}
            for pid, c in sorted(found['records'].items()) if c['placed']}


def held_out(directory):
    """{run: {job id: answer}} of the answers in directory, kept like `qa_triage
    answers` keeps them: the runs held out of learning, which nothing learns from,
    so that how the library does on them is how it does on runs it has not seen.
    {} without one."""
    if not directory or not os.path.exists(os.path.join(directory, 'index.yaml')):
        return {}
    with open(os.path.join(directory, 'index.yaml')) as f:
        index = yaml.safe_load(f) or {}
    found = {}
    for run, name in index.items():
        with open(os.path.join(directory, name)) as f:
            found[run] = {str(j): str(p) for j, p in (yaml.safe_load(f) or {}).items()}
    return found


def curriculum(found, known=None):
    """[(said, answer, [(run, results.json entry)])] of what the tool did not get
    right, sure or not, the commonest first: each a lesson that teaches every job
    of it at once. With known, the issues the records name, the jobs placed in an
    issue no record names are one lesson, whatever the tool said of each: the
    record it needs places them all; the tool's commonest saying names it. The jobs
    the memory alone placed in such an issue come after the ones the tool got wrong:
    in its lesson, and as lessons of their own after every other."""
    lessons, unnamed = [], {}
    for (said, answer), rs in found['examples'].items():
        if known is None or answer in known:
            lessons.append((said, answer, rs))
        else:
            unnamed.setdefault(answer, []).append((said, rs))
    recordless = dict(found.get('recordless') or {})
    for answer, parts in unnamed.items():
        parts.sort(key=lambda part: -len(part[1]))
        lessons.append((parts[0][0], answer, [r for _, rs in parts for r in rs] +
                        recordless.pop(answer, [])))
    lessons.sort(key=lambda item: -sum(1 for _, r in item[2] if r.get('problem') != item[1]))
    return lessons + sorted(((None, answer, rs) for answer, rs in recordless.items()),
                            key=lambda item: -len(item[2]))


def lines(found, most=10):
    """The score as lines for a person."""
    t = found['total']
    if not t['jobs']:
        return ['No triaged run has answers to score.']
    by_memory = ''.join([
        f', {t["also"]} of them in a record whose also names the people\'s issue'
        if t['also'] else '',
        f', {t["memory"]} of them by the memory alone, in issues no record names yet'
        if t['memory'] else ''])
    out = [f'{t["right"]} of {t["jobs"]} jobs people placed were placed the same '
           f'({100 * t["right"] // t["jobs"]}%){by_memory}; {t["sure, wrong"]} were placed '
           f'otherwise without asking the judge, and {t["unsure"]} were left unmatched, for the '
           'judge.', '']
    if found.get('stages'):
        out += [decided(found['stages']), by_stage(found['stages'],
                                                   found.get('stages right') or Counter()), '']
    suites = found.get('suites') or {}
    if len(suites) > 1:
        out += ['By suite: of its jobs people placed, how many were placed the same; of '
                'those the tool matched, how many rightly:']
        out += [f'  {name}: {c["right"]} of {c["jobs"]} ({100 * c["right"] // c["jobs"]}%); '
                f'{c["placed right"]} of {c["placed"]} matched' +
                (f' ({100 * c["placed right"] // c["placed"]}%)' if c['placed'] else '')
                for name, c in sorted(suites.items(), key=lambda item: -item[1]['jobs'])[:most]]
        out += ['']
    if found.get('overlaps'):
        out += ['Records whose checks all matched the same jobs, which then went to the judge '
                '(an exclude check, or a narrower check, tells them apart):']
        out += [f'  {n} jobs: {which}' for which, n in found['overlaps'].most_common(most)]
        out += ['']
    worst = sorted(((c['right'] / c['placed'], pid, c) for pid, c in found['records'].items()
                    if c['placed'] > c['right']), key=lambda x: (x[0], -x[2]['placed']))
    if worst:
        out += ['The records whose placements people disagreed with most:']
        out += [f'  {pid}: {c["right"]} of {c["placed"]} right' for _, pid, c in worst[:most]]
        out += ['']
    if found['pairs']:
        out += ['The commonest disagreements (a lesson, or the record\'s also, settles each):']
        out += [f'  {n} jobs: the tool said {said}, people {answer}'
                for (said, answer), n in found['pairs'].most_common(most)]
        out += ['']
    if found['unknown']:
        out += ['The problems the tool matched as new, which people placed (each needs '
                'a record):']
        out += [f'  {n} jobs: {answer}' for answer, n in found['unknown'].most_common(most)]
        out += ['']
    recordless = Counter({a: len(rs) for a, rs in (found.get('recordless') or {}).items()})
    if recordless:
        out += ['The issues the memory alone placed jobs in rightly (a record would place '
                'them the first time they fail):']
        out += [f'  {n} jobs: {answer}' for answer, n in recordless.most_common(most)]
        out += ['']
    return out
