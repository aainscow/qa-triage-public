"""Write a run's triage: a report, results.json and draft tracker comments.

Nothing is posted anywhere: the drafts are for a human to check and post.
"""

import json
import os
import re
from collections import Counter, defaultdict

import yaml

from . import pack
from .agents import readable
from .db import label, link, record_id
from .fingerprint import to_regex
from .graph import ESCALATED, FOUND, VERDICTS, describe, fits, named, placed
from .sources import PULPITO, QA_PROXY
from .tree.common import short

REASON_LIMIT = 200
# What to do about a problem seen again after its fix.
RECURRED = 'reopen {label} with these jobs if the cause is the same; if not, file a new issue'
DRAFT_NOTE = ('<!-- Draft from qa_triage: check each line against the report '
              'before posting. Nothing has been posted. -->\n')


def _ids(results):
    return '[' + ', '.join(f"'{r.job.id}'" for r in results) + ']'


def _by_problem(results):
    groups = defaultdict(list)
    for r in results:
        groups[r.problem.id].append(r)
    return groups


def suspected(results):
    """Lines for the jobs a PR under test may have failed, one for each PR and
    file; [] for none."""
    found, blamed = defaultdict(list), defaultdict(list)
    for r in results:
        for pr, who, why in r.blamed:
            blamed[(pr, who, why)].append(r)
        for pr, path in [] if r.blamed else r.suspects:
            found[(pr, path)].append(r)
    lines = [f'{_ids(rs)} - {pr}: {who} found that it caused them: {why}'
             for (pr, who, why), rs in blamed.items()]
    lines += [f'{_ids(rs)} - {pr} changes {path}, which they ran' +
              (f'; otherwise {verdict(rs[0])}' if rs[0].verdict else '') +
              ('; but the base branch failed it the same way'
               if any(same for _, _, same in rs[0].baseline) else '')
              for (pr, path), rs in found.items()]
    return [f'{n}. {line}' for n, line in enumerate(lines, 1)]


def verdict(r):
    """r's verdict, with the problem it names."""
    return f'{r.verdict} {r.problem.label}' if r.problem else r.verdict


SUSPECTED = ('Possibly caused by PRs under test (an analyst found the cause in the PR, or '
             'the PR changes a file the jobs ran; check it before calling them unrelated):')


def qa_comment(run, results):
    """Paste-ready text for the batch's QA tracker, in the RADOS wiki's format. The
    jobs a PR under test may have failed come first, and in no other list."""
    out = [DRAFT_NOTE, f'{PULPITO}/{run}/', '']
    lines = suspected(results)
    if lines:
        out += [SUSPECTED, *lines, '']
    results = [r for r in results if not (r.suspects or r.blamed)]
    known = [r for r in results if r.verdict == 'duplicate']
    probable = [r for r in results if r.verdict == 'probable-duplicate']
    other = [r for r in results if r.escalated]

    def problems(title, group):
        if not group:
            return
        out.append(title)
        for n, rs in enumerate(_by_problem(group).values(), 1):
            p = rs[0].problem
            out.append(f'{n}. {_ids(rs)} - {p.tracker} - {p.title}{also_tracked(p)}')
        out.append('')

    problems('Failures, unrelated:', known)
    problems('Probably unrelated (matched by a judge, or by what people answered for the '
             'same failure before; check):', probable)
    if other:
        out.append('Needs investigation:')
        groups = defaultdict(list)
        for r in other:
            groups[r.group or r.signature].append(r)
        for n, rs in enumerate(groups.values(), 1):
            r = rs[0]
            why = ''
            if r.verdict == 'seen-after-fix':
                why = f'looks like {r.problem.tracker}, but {r.notes[-1]}'
            elif r.verdict == 'ambiguous':
                why = '; '.join(r.notes)
            reason = r.job.reason or r.job.status
            if r.finding and r.finding.steps:
                reason = f'{r.finding.where or "tree"}: {r.finding.steps[-1].says}'
            found = [n[len(FOUND):] for n in r.notes if n.startswith(FOUND)]
            if found:
                reason = found[-1]
            if len(reason) > REASON_LIMIT:
                reason = reason[:REASON_LIMIT] + '...'
            reason += start(r)
            out.append(f'{n}. {_ids(rs)} - {reason}' +
                       (f' ({why})' if why else ''))
        out.append('')
    return '\n'.join(out)


def also_tracked(p):
    """What a draft adds of the other tracker issues people place a record's
    failures in, which may be duplicates of its own, for the reviewer to choose."""
    return (f' (also tracked as {", ".join(link(a) for a in p.also)}: possibly the same '
            'problem)' if p.also else '')


def sighting(run, results):
    """A comment for a known problem's tracker, listing the new occurrences."""
    return DRAFT_NOTE + ''.join(f'/a/{run}/{r.job.id}\n' for r in results)


def against(results, known):
    """(the number of jobs with a known answer whose result is the same, and
    [(result, the known answer)] for those whose result differs)."""
    same, differ = 0, []
    for r in results:
        if r.job.id in known:
            if fits(r, known[r.job.id]):
                same += 1
            else:
                differ.append((r, known[r.job.id]))
    return same, differ


def compared(results, known, when):
    """Lines for the report: the results compared with the known answers."""
    same, differ = against(results, known)
    lines = [f'{when}, {same} of the {same + len(differ)} jobs with a known answer got it.']
    if differ:
        lines += ['', 'The others:', '']
        lines += [f'- {r.job.id}: known {answer}; the tool said {describe(r)}, '
                  f'{"matched" if r.sure else "unmatched"}' for r, answer in differ]
    return lines + ['']


def checked(results, known):
    """Lines for the report: the judge's checks of what the tree and the records
    decided, against the known answers: the wrong results it caught, by kind, and
    the right ones it doubted."""
    decided = [r for r in results if r.job.id in known and r.sure and not r.agent]
    if not decided:
        return []

    def said(r):
        o = r.opinion
        if not o:
            return 'gave no answer'
        return {True: 'agreed', None: 'was not sure'}.get(
            o.get('agrees'), f'disagreed: it said {o.get("verdict")} '
                             f'{o.get("problem") or ""}'.rstrip() +
            (f', caused by {o["pr"]} under test' if o.get('pr') else ''))

    def counts(rs):
        found = defaultdict(int)
        for r in rs:
            found[(r.opinion or {}).get('agrees', 'none')] += 1
        naming = sum(1 for r in rs if r.opinion and r.opinion.get('agrees') is False and
                     named(r.opinion.get('problem')) == known[r.job.id])
        return (f'disagreed with {found[False]} ({naming} naming the known answer), agreed '
                f'with {found[True]}, was not sure of {found[None]} and gave no answer for '
                f'{found["none"]}')

    wrong = [r for r in decided if not fits(r, known[r.job.id])]
    right = [r for r in decided if fits(r, known[r.job.id])]
    hits = [r for r in wrong if placed(r)]
    misses = [r for r in wrong if not placed(r)]
    doubted = [r for r in right if (r.opinion or {}).get('agrees') is False]
    lines = [' '.join(
        ['The judge checked what the tree and the records decided.'] +
        [f'Of the {len(rs)} jobs {what}, it {counts(rs)}.' for rs, what in (
            (hits, 'they placed on a record the known answer does not name'),
            (misses, 'they said were new, which the known answer places on a record'))
         if rs] +
        [f'Of the {len(right)} they got right, it disagreed with {len(doubted)}.'])]
    if wrong or doubted:
        lines += ['', 'The wrong ones, and the right ones it doubted:', '']
        lines += [f'- {r.job.id}: known {known[r.job.id]}; the tool said {describe(r)}; the '
                  f'judge ({(r.opinion or {}).get("judge", "")}) {said(r)}'
                  for r in wrong + doubted]
    return lines + ['']


# The most lines the console shows under each of the tree's answers.
CONSOLE_LINES = 3


def decisions(r):
    """What the tree and the records decided about r's job, and why, a line each,
    with the lines of the logs each answer was decided by under it: for the console,
    so that a person can follow the logic and check it."""
    lines = []
    finding = r.finding
    if finding:
        for step in finding.steps:
            lines.append(f'  {step.question} {step.answer}: {short(step.says, 150)}')
            lines += [f'      {h.file}:{h.line}: {short(h.text, 150)}'
                      for h in step.evidence[:CONSOLE_LINES]]
        lines.append(f'  {"conclusive" if finding.conclusive else "inconclusive"} at '
                     f'{finding.where or "the root"}')
    found = defaultdict(list)
    for pid, kind, check, hits in r.checks:
        found[pid].append((kind, check, hits))
    for p in r.candidates:
        wrong = [f'{kind} /{check.pattern.pattern}/ {"found" if hits else "not found"}'
                 for kind, check, hits in found.get(p.id, []) if bool(hits) == (kind == 'exclude')]
        lines.append(f'  record {p.label}: ' + (
            f'{short(wrong[0], 150)}' if wrong else
            'its checks found the lines they look for' if found.get(p.id) else
            'it hangs where the tree explained the job, and its facts fit'))
        if not wrong:
            lines += [f'      {h.file}:{h.line}: {short(h.text, 150)}'
                      for kind, _, hits in found.get(p.id, []) if kind != 'exclude'
                      for h in hits[:1]][:CONSOLE_LINES]
    if not r.candidates:
        lines.append('  no record fits it')
    lines.append(f'  => {r.verdict}{" " + r.problem.label if r.problem else ""}, '
                 f'{"matched" if r.sure else "unmatched"}: {why(r)}' +
                 ''.join(f'; {n}' for n in r.notes if 'fix' in n))
    return lines


def why(r):
    """Why r has its verdict, in a few words."""
    if 'memory' in r.path:
        return next((n for n in r.notes if 'the memory places it' in n),
                    'the memory places it there')
    if r.verdict == 'duplicate' or r.verdict == 'seen-after-fix' and not r.agent:
        if r.problem.trees and not r.problem.confirm:
            return (f'record {r.problem.label} hangs where the tree explained it, and its '
                    'facts fit what the tree found')
        return f'record {r.problem.label}\'s checks all passed'
    if r.verdict == 'ambiguous':
        return 'the checks of several records passed'
    if r.sure and not r.agent:
        return 'the tree is conclusive, and no record nearly fits'
    if r.agent:
        return f'the judge ({readable(r.agent.get("judge"))}) said {r.agent.get("verdict")}'
    if r.candidates:
        return 'records nearly fit it, so a judge must decide'
    return 'the tree was inconclusive, so a judge must decide'


def jobs(n):
    return f'{n} job{"" if n == 1 else "s"}'


SECTIONS = (('duplicate', 'Known problems, placed by their records'),
            ('probable-duplicate', 'Probably known problems, placed by the judge, or by '
                                   'what people answered for the same failure before; '
                                   'check them'),
            ('seen-after-fix', 'Known problems seen again after their fix'),
            ('explained', 'New problems the tree explains: each needs a tracker filed'),
            ('other', 'For a person to look into'))


def groups(results):
    """[(section, [results])]: the known problems, one group for each; the new
    problems the tree and the records are sure of, which need a tracker filed; then
    the jobs that need a person to look into, one group for each failure; in the
    summary's order."""
    found = []
    for verdict, _ in SECTIONS[:3]:
        found += [(verdict, rs) for rs in
                  _by_problem([r for r in results if r.verdict == verdict]).values()]
    explained, other = defaultdict(list), defaultdict(list)
    for r in results:
        if r.verdict == 'new' and r.sure and not r.agent:
            explained[r.signature].append(r)
        elif r.verdict in ('new', 'ambiguous'):
            other[r.group or r.signature].append(r)
    return found + [('explained', rs) for rs in explained.values()] + \
        [('other', rs) for rs in other.values()]


def wiki(run, results, header=()):
    """The run's section of the RADOS team's wiki of reviewed runs
    (https://tracker.ceph.com/projects/rados/wiki/MAIN), in its Textile: the run's QA
    tracker, then its new problems, then the known ones, one line each, with their
    jobs. A problem is new if its record's examples are all of this run's jobs, as
    one filed in its review is; a new failure with no tracker yet, and a job still to
    look into, are listed for the reviewer to deal with first."""
    tracker = next((m.group(1) for m in map(re.compile(r'QA tracker (\S+?),?(?:\s|$)').match,
                                            header) if m), None)
    jobs = {r.job.path for r in results}

    def line(rs, link, title, note=''):
        return (f'# {[r.job.id for r in rs]} - {link} - {title}' +
                (f' - [{note}]' if note else ''))

    new, known, todo = [], [], []
    suspects, found = defaultdict(list), defaultdict(list)
    for r in results:
        for pr, who, why in r.blamed:
            found[(pr, who, why)].append(r)
        for pr, path in [] if r.blamed else r.suspects:
            suspects[(pr, path)].append(r)
    blamed = [line(rs, pr, f'{who} found that it caused them: {short(why, 200)}')
              for (pr, who, why), rs in found.items()]
    blamed += [line(rs, pr, f'changes {path}, which they ran', verdict(rs[0]))
               for (pr, path), rs in suspects.items()]
    for section, rs in groups([r for r in results if not (r.suspects or r.blamed)]):
        r = rs[0]
        if r.problem:
            p = r.problem
            note = ('placed by what people answered for the same failure before'
                    if r.verdict == 'probable-duplicate' and 'memory' in r.path else
                    'placed by the judge' if r.verdict == 'probable-duplicate' else
                    f'seen again after its fix: {r.notes[-1]}; '
                    f'{RECURRED.format(label=p.label)}' if r.verdict == 'seen-after-fix'
                    else '')
            if not note and track_record(p.id):
                note = f'placed by its record, {track_record(p.id)}'
            # A record of this run alone is a problem it found.
            fresh = p.examples and set(p.examples) <= jobs
            (new if fresh else known).append(line(rs, p.tracker,
                                                  p.title + also_tracked(p), note))
        elif section == 'explained':
            new.append(line(rs, 'no tracker yet', short(what(r), 200)))
        else:
            todo.append(line(rs, f'{r.verdict}, {"judged" if r.sure else "unmatched"}',
                             short(what(r), 200)))
    out = [f'h3. {tracker or f"{PULPITO}/{run}/"}', '']
    for title, lines in (('Possibly caused by PRs under test:', blamed),
                         ('New failures, unrelated:', new), ('Failures, unrelated:', known),
                         ('Still to look into:', todo)):
        if lines:
            out += [title, *sorted(lines, key=lambda text: text.split("'")[1]), '']
    return '\n'.join(out)


def what(r):
    """What is known of the failure of r's job, that needs a person, in a line."""
    found = [n[len(FOUND):] for n in r.notes if n.startswith(FOUND)]
    if r.verdict == 'ambiguous':
        return '; '.join(r.notes)
    if found:
        return 'the analyst found: ' + found[-1]
    if r.agent and r.agent.get('summary'):
        return 'the judge said: ' + r.agent['summary']
    if r.finding and r.finding.steps:
        return f'{r.finding.where or "tree"}: {r.finding.steps[-1].says}' + start(r)
    return r.job.reason or r.job.status


def start(r):
    """Where to start reading the logs of r's job: the line the tree's last answer
    that cited one was decided by, as " (file:line)"; '' if none did."""
    cited = [s.evidence[0] for s in r.finding.steps if s.evidence] if r.finding else []
    return f' ({cited[-1].file}:{cited[-1].line})' if cited else ''


def group_lines(section, rs, number=None):
    """A group of the summary: its jobs and what they failed on."""
    n = f'[{number}] ' if number else ''
    r = rs[0]
    ids = ', '.join(o.job.id for o in rs)
    if section in ('explained', 'other'):
        return [f'  {n}{r.verdict}, {"matched" if r.sure else "unmatched"}: {ids}',
                f'          {short(what(r), 300)}']
    lines = [f'  {n}{r.problem.label:>9}  {ids}  {short(r.problem.title, 110)}']
    if section == 'seen-after-fix':
        lines.append(f'          {r.notes[-1]}: {RECURRED.format(label=r.problem.label)}')
    return lines


def summary(run, results, header=(), lessons=(), spent=None, units=None, analyst=None,
            numbered=False):
    """The run's results as a reviewer reads them, for the console: the known
    problems, each with its jobs, then the failures that need a person, what the
    analyst learned (lessons, the lines learn_from_run gave), and the counts and
    what the agents cost: spent is [(judge, cost)] for every judge call, units
    {agent: the unit of its costs}, and analyst the analyst's name. numbered
    gives each group the number groups() gives it, from 1."""
    lines = [f'Triage of {run}'] + [h for h in header if h.startswith('QA tracker')]
    blamed = suspected(results)
    if blamed:
        lines += ['', SUSPECTED] + [f'  {line}' for line in blamed]
    titles = dict(SECTIONS)
    last = None
    for number, (section, rs) in enumerate(groups(results), 1):
        if section != last:
            size = sum(len(g) for s, g in groups(results) if s == section)
            lines += ['', f'{titles[section]} ({jobs(size)}):']
            last = section
        lines += group_lines(section, rs, number if numbered else None)
    if lessons:
        lines += ['', 'What the analyst learned:']
        lines += ['  ' + short(re.sub(r'; see \S+$', '', line), 300) for line in lessons]
    counts = defaultdict(int)
    for r in results:
        counts[r.verdict] += 1
    lines += ['', f'{len(results)} failed or dead jobs: ' +
              ', '.join(f'{counts[v]} {v}' for v in VERDICTS if counts[v]) + '. ' +
              decided(Counter(stage(r) for r in results))]
    return lines + [cost_line(results, lessons, spent, units or {}, analyst)]


def cost_line(results, lessons, spent, units, analyst):
    """What the judge and the analyst cost, each in its own unit, and in all."""
    if spent is None:
        judged = judge_costs(results)
    else:
        judged = defaultdict(lambda: (0.0, 0))
        for name, cost in spent:
            total, calls = judged[name]
            judged[name] = (total + cost, calls + 1)
    parts, in_all = [], defaultdict(float)
    for name, (cost, calls) in judged.items():
        unit = units.get(name, 'of its own unit')
        parts.append(f'judge {readable(name)} {cost:.2f} {unit} in {calls} '
                     f'call{"" if calls == 1 else "s"}')
        in_all[unit] += cost
    costs = [float(c) for line in lessons for c in re.findall(r'\(cost (-?[\d.]+)\)', line)]
    if costs:
        unit = units.get(analyst, 'of its own unit')
        parts.append(f'analyst {readable(analyst)} {sum(costs):.2f} {unit} in {len(costs)} '
                     f'lesson{"" if len(costs) == 1 else "s"}')
        in_all[unit] += sum(costs)
    if not parts:
        return 'Cost: nothing; no judge or analyst was asked.'
    return 'Cost: ' + '; '.join(parts) + '. In all: ' + \
        ', '.join(f'{cost:.2f} {unit}' for unit, cost in in_all.items()) + '.'


def headline(r):
    """What r's job failed on, in a line: its record's tracker issue or PR and title,
    or a new issue, and how sure that is."""
    how = f'{r.verdict}, {certainty(r)}'
    if r.problem:
        rightly = track_record(r.problem.id)
        return f'{r.problem.label}: {r.problem.title} ({how}' + \
            (f'; {rightly})' if rightly else ')')
    said = next((a for a in (r.analysis, r.agent) if a and a.get('problem')), None)
    if said:
        return (f'{label(record_id(said["problem"]))}, which has no record yet: '
                f'{short(said.get("summary") or "", 150)} ({how})')
    return f'New issue: {short(what(r), 150)} ({how})'


# What decided a result, for people to weigh it, and to count how much is decided
# without AI: a record's checks, the memory of people's answers, the tree alone (a
# new problem, for sure), or the judge; or nothing, yet.
STAGES = (('records', 'the records'), ('memory', 'the memory'), ('tree', 'the tree'),
          ('judge', 'the judge'))
# {problem id: {'placed': n, 'right': n}}: how often the tool placed jobs people
# answered in each problem, and how often rightly (see score.precision), from the
# library, if it has it.
PRECISION = {}


def stage(r):
    """What decided r: one of STAGES' names, or 'unsure'."""
    if not r.sure:
        return 'unsure'
    if r.agent:
        return 'judge'
    if 'memory' in r.path:
        return 'memory'
    return 'records' if r.problem else 'tree'


def stage_of(entry):
    """What decided a job, as results.json keeps it: as it says, or, from before it
    said, as stage() would have."""
    if entry.get('stage'):
        return entry['stage']
    if not entry.get('sure'):
        return 'unsure'
    if entry.get('judge'):
        return 'judge'
    if 'memory' in (entry.get('path') or []):
        return 'memory'
    return 'records' if entry.get('problem') else 'tree'


def decided(stages):
    """A line of how many jobs each stage decided, from a Counter of stages."""
    total = sum(stages.values())
    if not total:
        return 'No jobs.'
    free = sum(stages[name] for name, _ in STAGES if name != 'judge')
    return ('Decided by ' + ', '.join(f'{what} {stages[name]}' for name, what in STAGES) +
            f'; unmatched {stages["unsure"]}. Without AI: {free} of {total} '
            f'({100 * free // total}%).')


def track_record(pid):
    """How often the tool placed rightly the jobs people answered that it placed in
    problem pid, as a phrase; '' if that is not known."""
    known = PRECISION.get(str(pid)) or {}
    if not known.get('placed'):
        return ''
    return f'right {known["right"]} of the {known["placed"]} times people answered'


def certainty(r):
    return {'unsure': 'unmatched', 'judge': 'matched by the judge',
            'memory': 'matched by the memory', 'records': 'matched by its record',
            'tree': 'matched by the tree'}[stage(r)]


# A probable duplicate that the memory, not the judge, placed.
REMEMBERED = 'a known problem: people placed the jobs that failed the same way in it before'


# The most of a cited line the report shows, around what was found in it; and the
# most lines of a quote that no line of the logs has.
CITED, CITED_LINES = 2000, 8


def _archived(run, job_id, rel):
    """The URL of a file of a job's archive."""
    return f'{QA_PROXY}/{run}/{job_id}/{rel}'


def code(text):
    """text as Markdown code, as it is, spaces and backticks and all, so that it can
    be searched for."""
    fence = '`' * (max(map(len, re.findall('`+', text)), default=0) + 1)
    return f'{fence} {text} {fence}'


def marked(text, match=None, most=CITED):
    """A line of the logs in Markdown: whole, as code, with what found it, match,
    (start, end), in bold; a line longer than most cut to most around it."""
    start, end = match if match and len(match) == 2 and match[1] > match[0] else (0, 0)
    begin, stop = 0, len(text)
    if stop > most:
        begin = max(0, min(start - most // 4, stop - most))
        stop = begin + most
    start, end = min(max(start, begin), stop), min(max(end, begin), stop)
    # Spaces alone are not code: the bold takes them.
    if not text[begin:start].strip():
        start = begin if end > start else start
    if not text[end:stop].strip():
        end = stop if end > start else end
    parts = [code(text[begin:start]) if start > begin else '',
             f'**{code(text[start:end])}**' if end > start else '',
             code(text[end:stop]) if stop > end else '']
    return ('...' if begin else '') + ''.join(parts) + ('...' if stop < len(text) else '')


def _place(run, job_id, where, archived=True, names=None):
    """Where a line of the logs is, file:line, in Markdown, linked to its file in the
    archive if it is from there, by the name the archive has it by now."""
    rel, _, line = where.rpartition(':') if re.search(r':\d+$', where) else (where, '', '')
    rel = (names or {}).get(rel, rel)
    where = f'{rel}:{line}' if line else rel
    return f'[`{where}`]({_archived(run, job_id, rel)})' if archived else f'`{where}`'


def cited(run, job_id, hit, names=None):
    """A line of the logs a result cites, in Markdown: where it is, and the line, with
    what found it in bold."""
    return [f'   - {_place(run, job_id, f"{hit.file}:{hit.line}", names=names)}',
            f'     {marked(hit.text, hit.match)}']


def quoted(run, job_id, quote):
    """A line the judge quotes, in Markdown: what it says it shows, and the lines of
    the logs that have it, whole, with the quote in bold; or, if none was found, the
    quote, and where it is from."""
    shows = str(quote.get('shows') or '')
    lines = [row for row in quote.get('lines') or [] if isinstance(row, list) and len(row) > 2]
    archived = quote.get('archived', True)
    if not lines:
        rel = str(quote.get('file'))
        here = bool(quote.get('at')) or rel == 'teuthology.log' or rel.startswith('remote/')
        return [f'   - {_place(run, job_id, quote.get("at") or rel, here)}' +
                (f': {shows}' if shows else '')] + \
            [f'     {marked(text)}' for text in str(quote.get('text', '')).splitlines()
             if text.strip()][:CITED_LINES]
    if len(lines) == 1:
        file, line, text, *match = lines[0]
        return [f'   - {_place(run, job_id, f"{file}:{line}", archived)}' +
                (f': {shows}' if shows else ''), f'     {marked(text, *match)}']
    count = max(quote.get('count') or 0, len(lines))
    out = [f'   - {shows + "; " if shows else ""}{count} lines have it' +
           (f', the first {len(lines)} here:' if count > len(lines) else ':')]
    for file, line, text, *match in lines:
        out += [f'     - {_place(run, job_id, f"{file}:{line}", archived)}',
                f'       {marked(text, *match)}']
    return out


def _why_section(run, r):
    """Each answer of the tree and each check of the records, with the lines of the
    logs it was decided by under it, and the verdict they come to."""
    out = ['', '### Why', '']
    finding = r.finding
    if finding:
        out += ['The tree\'s questions, its answers, and the lines of the logs it decided '
                'each by, as file:line, linked to the file in the archive:', '']
        for n, step in enumerate(finding.steps, 1):
            out.append(f'{n}. {step.question} **{step.answer}**: {step.says}')
            for h in step.evidence:
                out += cited(run, r.job.id, h, r.names)
        facts = '; '.join(f'{k}={short(str(v), 120)}' for k, v in finding.facts.items())
        out += ['', ('Conclusive' if finding.conclusive else 'Inconclusive') +
                (f' at `{finding.where}`.' if finding.path else '.') +
                (f' Facts: {facts}.' if facts else '') +
                (f' Its question for a judge: {finding.question}' if finding.question else '')]
    if r.checks:
        out += ['', 'The records\' checks, and the lines they found:', '']
        for pid, kind, check, hits in r.checks:
            out.append(f'- {label(pid)} {kind} `{check.file}: /{check.pattern.pattern}/`: ' +
                       ('found' if hits else 'not found'))
            for h in hits:
                out += cited(run, r.job.id, h, r.names)
    out += ['', f'So: **{r.verdict}**' + (f' {r.problem.label}' if r.problem else '') +
            f', {"matched" if r.sure else "unmatched"}: {why(r)}.']
    return out


def _job_section(run, r):
    p = r.problem
    out = [f'## {r.job.id}: {r.verdict}, {certainty(r)}', '',
           f'- Description: `{r.job.description}`',
           f'- Failure: {r.job.reason or "(none given)"}',
           f'- Archive: {QA_PROXY}/{run}/{r.job.id}/',
           f'- Verdict: **{r.verdict}**, ' +
           (REMEMBERED if r.verdict == 'probable-duplicate' and 'memory' in r.path else
            VERDICTS[r.verdict]) +
           (f': [{p.label}]({p.tracker}) ({p.title})' if p else ''),
           f'- Path: {" -> ".join(r.path)}']
    out += [f'- Note: {n}' for n in r.notes]
    analysis = r.analysis if pack.told(r.analysis) else None
    if analysis:
        said = analysis.get('problem')
        out += ['', f'### The analyst ({readable(analysis.get("analyst"))}): ' +
                (f'{label(record_id(said))}' if said else 'no record') +
                (f', {analysis["assessment"]} result' if analysis.get('assessment') else '')]
        out += told_section(run, analysis.get('job') or r.job.id, analysis)
    for a, title in ((r.agent, 'The judge'), (r.opinion, 'The judge\'s check')):
        if not a:
            continue
        costs = a.get('costs') or [(a.get('judge'), a.get('cost'))]
        agreed = {True: ', agrees', False: ', disagrees', None: ', unsure'}.get(
            a.get('agrees'), '') if a is r.opinion else ''
        said = a.get('problem')
        out += ['', f'### {title}: {a.get("verdict")}' +
                (f' {label(record_id(said))}' if said else '') + f'{agreed} (' +
                ', '.join(f'{readable(name)}, cost {cost}' for name, cost in costs) + ')']
        if analysis:
            out += ['', f'{a.get("summary") or ""} Its account is replaced by the analyst\'s, '
                        'above.'.strip()]
        else:
            # From the archive of the job whose evidence the judge saw.
            out += told_section(run, r.group or r.job.id, a)
        for c in a.get('suggested_checks') or []:
            if isinstance(c, dict):
                out.append(f'- Suggested check: `{c.get("file")}`: `{c.get("pattern")}`')
        if r.workdir:
            out.append(f'- Evidence given to the judge: `{r.workdir}`')
    out += _why_section(run, r)
    return '\n'.join(out) + '\n'


def told_section(run, job_id, answer):
    """What an agent's answer tells a person, in Markdown: its explanation, then its
    walkthrough, each step with the lines of job_id's logs it rests on, whole, the
    quote in bold, and the command that opens the log at the first; then any other
    lines it quotes."""
    out = ['', str(answer.get('explanation') or answer.get('summary', '')).strip(), '']
    steps, in_steps = pack.steps(answer), set()
    if steps:
        out += ['How to check it, step by step, with the lines of the logs each step rests '
                'on, whole, the quote in bold:', '']
    for n, step in enumerate(steps, 1):
        title = str(step.get('title') or '').strip().rstrip('.')
        said = ' '.join(t for t in (str(step.get('do') or '').strip(),
                                    str(step.get('finds') or '').strip()) if t)
        out.append(f'{n}. ' + (f'**{title}.** ' if title else '') + said)
        quotes = [q for q in step.get('evidence') or [] if isinstance(q, dict)]
        in_steps |= {id(q) for q in quotes}
        at = next((q['at'] for q in quotes if q.get('at')), None)
        if at:
            out.append(f'   Open it: `qa_triage open {run}/{job_id} {at}`')
        for q in quotes:
            out += quoted(run, job_id, q)
    rest = [q for q in pack.quotes(answer) if id(q) not in in_steps]
    if rest:
        out += [''] * bool(steps) + [('Other lines it quotes' if steps else
                                      'The lines it quotes') +
                                     ', as the logs have them, the quote in bold:', '']
    for q in rest:
        out += quoted(run, job_id, q)
    return out


def told_text(answer, limit=300):
    """What an agent's answer tells a person, as plain lines: its explanation, then
    its walkthrough, each step with where the lines it quotes are."""
    out = [str(answer.get('explanation') or answer.get('summary', '')).strip()]
    steps = pack.steps(answer)
    if steps:
        out += ['', 'How to check it:']
    for n, step in enumerate(steps, 1):
        title = str(step.get('title') or '').strip().rstrip('.')
        out.append(f'{n}. ' + (f'{title}. ' if title else '') + ' '.join(
            t for t in (str(step.get('do') or '').strip(),
                        str(step.get('finds') or '').strip()) if t))
        for q in step.get('evidence') or []:
            if isinstance(q, dict):
                rows = q.get('lines') or []
                where = q.get('at') or (f'{rows[0][0]}:{rows[0][1]}' if rows else q.get('file'))
                text = rows[0][2] if rows else str(q.get('text', ''))
                out.append(f'   {where}: {short(text, limit)}')
    return out


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, lambda dumper, text: dumper.represent_scalar(
    'tag:yaml.org,2002:str', text, style='|' if '\n' in text else None))


# The facts that tell one failure apart from another at the same place in the
# tree: a draft record's facts start from them.
TELLING = ('check', 'level', 'message', 'source', 'function', 'test', 'command', 'error',
           'wait', 'program', 'kind', 'frames', 'signal', 'op', 'caller', 'daemon_type',
           'hung_task', 'script')


def draft_record(run, group):
    """A starting point for a new problem record for a group of jobs: placed where
    the tree put them, with their telling facts; and, unless the tree was sure of
    its explanation, with the checks the judge suggested."""
    r = group[0]
    checks = (r.agent or {}).get('suggested_checks') or []
    finding = r.finding
    record = {
        'id': 'TODO tracker number',
        'tracker': 'https://tracker.ceph.com/issues/TODO',
        'title': 'TODO the tracker\'s subject',
        'kind': 'TODO product, test or infra',
    }
    if finding and finding.path:
        record['tree'] = finding.where
        record['facts'] = {name: to_regex(str(value)) for name, value in finding.facts.items()
                           if name in TELLING and value}
    if checks or not (finding and finding.conclusive):
        record['match'] = {'reason': to_regex(r.job.reason)} if r.job.reason else {}
        record['confirm'] = [{'file': c.get('file'), 'pattern': c.get('pattern')}
                             for c in checks] or \
            [{'file': 'teuthology.log', 'pattern': 'TODO a line that shows the cause'}]
    explained = '; '.join(step.says for step in finding.steps) if finding else ''
    found = [n[len(FOUND):] for n in r.notes if n.startswith(FOUND)]
    record['notes'] = ((found or [''])[-1] or (r.agent or {}).get('summary') or explained or
                       'TODO the cause') + '\n'
    record['examples'] = [f'{run}/{x.job.id}' for x in group]
    return ('# Draft from qa_triage. Fill in the TODOs, check that the facts and any\n'
            '# confirm checks show the cause rather than the symptom, then move it to\n'
            '# problems/ and run "qa_triage verify <id>".\n' +
            yaml.dump(record, Dumper=_Dumper, sort_keys=False, width=100,
                      allow_unicode=True))


def judge_costs(results):
    """{judge: (total cost, calls)}, counting each judged group once, and not the
    verdicts kept from before."""
    spent = defaultdict(lambda: (0.0, 0))
    for r in results:
        if not r.agent or r.agent.get('kept') or (r.group and r.group != r.job.id):
            continue
        for name, cost in r.agent.get('costs') or [(r.agent.get('judge'),
                                                     r.agent.get('cost'))]:
            total, calls = spent[name]
            spent[name] = (total + (cost or 0), calls + 1)
    return dict(spent)


def write(out_dir, run, results, skipped=(), header=()):
    os.makedirs(os.path.join(out_dir, 'drafts'), exist_ok=True)
    counts = defaultdict(int)
    for r in results:
        counts[r.verdict] += 1
    summary = ', '.join(f'{counts[v]} {v}' for v in VERDICTS if counts[v])
    report = [f'# Triage of {run}', '', f'{PULPITO}/{run}/', '', *header,
              f'{len(results)} failed or dead jobs: {summary}.', '',
              decided(Counter(stage(r) for r in results)), '',
              f'To question a result: `qa_triage doubt {run}/<job> --why "..."`', '',
              f'Each result cites the lines of the logs it was decided by, as file:line, with '
              f'what was found in bold. To open the file at one in your editor: '
              f'`qa_triage open {run}/<job> <file>:<line>`', '']
    spent = judge_costs(results)
    if spent:
        report += ['Judge costs, in each judge\'s own unit: ' +
                   ', '.join(f'{name} {cost:.4f} ({calls} calls)'
                             for name, (cost, calls) in spent.items()), '']
    kept = sum(1 for r in results if r.agent and r.agent.get('kept') and
               r.group in ('', r.job.id))
    if kept:
        report += [f'Verdicts kept from judging the same evidence before, at no cost: '
                   f'{kept}.', '']
    if skipped:
        report += [f'Not triaged: {", ".join(skipped)}', '']
    order = list(VERDICTS)
    for r in sorted(results, key=lambda r: (order.index(r.verdict), r.job.id)):
        report.append(_job_section(run, r))
    with open(os.path.join(out_dir, 'report.md'), 'w') as f:
        f.write('\n'.join(report))

    with open(os.path.join(out_dir, 'drafts', 'qa-tracker-comment.md'), 'w') as f:
        f.write(qa_comment(run, results))
    with open(os.path.join(out_dir, 'drafts', 'wiki.textile'), 'w') as f:
        f.write(wiki(run, results, header))
    known = [r for r in results if r.problem and r.verdict not in ESCALATED]
    for pid, rs in _by_problem(known).items():
        with open(os.path.join(out_dir, 'drafts', f'sighting-{pid}.md'), 'w') as f:
            f.write(sighting(run, rs))

    groups = defaultdict(list)
    for r in results:
        if r.verdict == 'new':
            groups[r.group or r.signature].append(r)
    if groups:
        os.makedirs(os.path.join(out_dir, 'drafts', 'records'), exist_ok=True)
    for group in groups.values():
        with open(os.path.join(out_dir, 'drafts', 'records', f'{group[0].job.id}.yaml'),
                  'w') as f:
            f.write(draft_record(run, group))

    with open(os.path.join(out_dir, 'results.json'), 'w') as f:
        json.dump([{'run': r.job.run, 'job': r.job.id, 'status': r.job.status,
                    'description': r.job.description,
                    'reason': r.job.reason, 'verdict': r.verdict,
                    'problem': r.problem.id if r.problem else None, 'path': r.path,
                    'notes': r.notes, 'judge': r.agent, 'check': r.opinion,
                    'analysis': r.analysis, 'sure': r.sure, 'stage': stage(r),
                    'suspects': [list(s) for s in r.suspects],
                    'blamed': [list(b) for b in r.blamed],
                    'baseline': [list(b) for b in r.baseline],
                    'tree': r.finding.to_json() if r.finding else None}
                   for r in results], f, indent=1)
    return summary
