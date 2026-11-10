"""The evidence directory a judge works in."""

import os
import re
import shutil
import string
from collections import deque

import yaml

from .agents import PROMPTS, schema
from .db import under
from .fingerprint import compact
from .sources import ArchiveError, size_bytes

FULL_LOG_LIMIT = 20 << 20
HEAD_LINES = 2000
TAIL_LINES = 20000
_ERRORS = re.compile(r'FAILED|Failure|Traceback|Exception|Error|error:|assert|abort|'
                     r'Segmentation|[Tt]imed out|timeout|[Ff]ailed', re.S)
_WORDS = re.compile(r'[a-z0-9_]{3,}')
MAX_EXCERPTS = 2000
MAX_TEMPLATES = 120
TAIL_TEMPLATES = 60
LINE_LIMIT = 400
CANDIDATES = 6
# The most tracker issues a search gives the judge, and the most files of each PR
# under test it is told of.
TRACKERS, MOST_FILES = 15, 40
# The names of tests in a job's description and failure reason, which a tracker
# search looks for: test_ and TEST_ functions, scripts, and singleton tests.
TEST_NAME = re.compile(r'\b(?:test_\w+|TEST_\w+|[\w.-]+\.(?:sh|py))\b|(?<=\ball/)[\w.-]+')
SEARCHES = 3


def _words(text):
    return set(_WORDS.findall(text.lower()))


def near(finding, problems):
    """The problems that hang in the tree on the finding's path, above or below
    where it stopped."""
    if not finding:
        return []
    where = finding.where
    return [p for p in problems if any(under(where, top) or under(top, where)
                                       for top in p.trees)]


def similar(job, problems, n=CANDIDATES):
    """The n problems whose text shares most words with the job's."""
    words = _words(job.description + ' ' + job.reason)

    def score(p):
        theirs = _words(' '.join([p.title, p.notes] +
                                 [r.pattern for r in p.match.values()]))
        return len(words & theirs) / (len(words | theirs) or 1)

    return sorted(problems, key=score, reverse=True)[:n]


def search_words(job):
    """The tracker searches for the tests a job ran: the names in its description
    and failure reason, each once, at most SEARCHES of them."""
    names = TEST_NAME.findall(f'{job.description} {job.reason}')
    return list(dict.fromkeys(names))[:SEARCHES]


def mentioned(path, text):
    """True if text names the file at path: by its last two parts, without the
    extension, as a log or a job's description does (mon/misc.sh, all/ec-hinfo)."""
    key = os.path.splitext('/'.join(path.split('/')[-2:]))[0]
    # As a whole name: tasks/ceph is not tasks/ceph_test_rados.
    return bool(key) and re.search(rf'(?<![\w-]){re.escape(key)}(?![\w-])', text) is not None


def _write(workdir, name, text):
    with open(os.path.join(workdir, name), 'w') as f:
        f.write(text)


# The evidence made from teuthology.log, which reading the whole log makes.
LOG_EVIDENCE = ('teuthology.log', 'excerpts.txt')


def _log_evidence(files, workdir):
    """Put in workdir teuthology.log, or its start and end if it is big, and the
    excerpts of it: as they are there already, or were kept in the cache, beside the
    archive's answers about the job, as a job's log does not change once it has
    ended; or else read from the archive, and kept."""
    if all(os.path.exists(os.path.join(workdir, name)) for name in LOG_EVIDENCE):
        return
    kept = getattr(files, 'kept', None)
    cache = kept and os.path.join(os.path.dirname(kept), 'evidence')
    if cache and all(os.path.exists(os.path.join(cache, name)) for name in LOG_EVIDENCE):
        for name in LOG_EVIDENCE:
            shutil.copyfile(os.path.join(cache, name), os.path.join(workdir, name))
        return
    errors, tail = _copy_log(files, workdir)
    _write(workdir, 'excerpts.txt',
           '# The last error-looking lines of teuthology.log (line: text)\n' +
           '\n'.join(line[:LINE_LIMIT] for line in errors) +
           '\n\n# The last lines of teuthology.log\n' +
           '\n'.join(line[:LINE_LIMIT] for line in tail) + '\n')
    if cache and all(os.path.exists(os.path.join(workdir, name)) for name in LOG_EVIDENCE):
        os.makedirs(cache, exist_ok=True)
        for name in LOG_EVIDENCE:
            shutil.copyfile(os.path.join(workdir, name), os.path.join(cache, name))


def _copy_log(files, workdir):
    """Copy teuthology.log (only its start and end if it is big); return its lines'
    error excerpts and its tail."""
    found = files.glob('teuthology.log')
    if not found:
        return [], []
    rel, size = found[0]
    big = size_bytes(size) > FULL_LOG_LIMIT
    errors, tail = deque(maxlen=MAX_EXCERPTS), deque(maxlen=TAIL_LINES)
    with open(os.path.join(workdir, 'teuthology.log'), 'w') as out:
        for n, line in enumerate(files.lines(rel, size), 1):
            if _ERRORS.search(line):
                errors.append((n, line))
            if not big or n <= HEAD_LINES:
                out.write(line + '\n')
            if not big or n > HEAD_LINES:
                tail.append((n, line))
        if big and tail:
            out.write(f'[... cut by qa_triage; the last {len(tail)} lines follow, from line '
                      f'{tail[0][0]} ...]\n')
            out.writelines(line + '\n' for _, line in tail)
    return compact(errors)[-MAX_TEMPLATES:], compact(list(tail)[-150:])[-TAIL_TEMPLATES:]


# The inline prompt gives the files in this order, so that the part that is the
# same for every job of a run (the instructions and index.txt) comes first and
# can be cached.
INLINE_FILES = ('index.txt', 'candidates.yaml', 'trackers.txt', 'checks.txt', 'job.yaml',
                'explanation.txt', 'under-test.txt', 'excerpts.txt', 'needed.txt')
# The evidence that only some jobs have, which the caller gives.
EXTRA_FILES = ('trackers.txt', 'under-test.txt', 'needed.txt')
# The files of the archive a judge that could not decide says it needs, as it names
# them; and the most of them it is given, and of the lines of each.
NEEDED_NAME = re.compile(r'[\w.*@+-]+(?:/[\w.*@+-]+)*\.(?:log|txt|yaml|json|out)(?:\.gz)?')
NEEDED_FILES, NEEDED_LINES = 4, 60
# Plain strings that every line _ERRORS finds has, for a scan to look for first.
ERROR_WORDS = ('FAILED', 'Failure', 'Traceback', 'Exception', 'Error', 'error:', 'assert',
               'abort', 'Segmentation', 'imed out', 'timeout', 'ailed')


# The most of the jobs that passed, the most like the failed one first, that are tried
# for one whose teuthology.log the archive has, to compare the failed job's with.
PASSING_TRIED = 3
# What excerpts.txt says of the lines it leaves out.
LEFT_OUT = ('# Left out: {count} lines that {job}, a job of the same run that passed, logs '
            'too: they are not what failed')
# What varies between two jobs' logs of the same line: numbers, hashes and hosts; the
# longest stretch of a line between them is its key, if it is at least KEY_LEAST long.
VARIES = re.compile(r'[0-9a-fA-F]{6,}|\d+')
KEY_LEAST = 20


def kept(jobs, files_for):
    """Of jobs, of one run, those the archive still has, as far as it says: it prunes
    most of the jobs that passed sooner than those that failed."""
    siblings = getattr(files_for(jobs[0]), 'siblings', None) if jobs else None
    try:
        there = set(siblings()) if siblings else None
    except ArchiveError:
        there = None
    return [j for j in jobs if there is None or j.id in there]


def alike(job, jobs):
    """jobs, the most like job first, by the words their descriptions share."""
    mine = set(re.findall(r'[\w.-]+', job.description))
    return sorted(jobs, key=lambda j: -len(mine & set(re.findall(r'[\w.-]+', j.description))))


def key(text):
    """What any job that logs the line text logs too: its longest stretch without
    numbers or hashes, after its timestamp; '' if none is long enough to tell."""
    text = re.sub(r'^\S*\d{4}-\d\d-\d\dT\S+\s+', '', SIMILAR.sub('', text))
    found = max((part.strip() for part in VARIES.split(text)), key=len, default='')
    return found if len(found) >= KEY_LEAST else ''


def passed_too(passing, texts):
    """The indexes of texts, lines of a failed job's logs, that passing, (job, its
    files), a job that passed, logs too, by their keys."""
    keys = {str(i): key(text) for i, text in enumerate(texts)}
    patterns = {i: (re.escape(k), [k]) for i, k in keys.items() if k}
    if not passing or not patterns:
        return set()
    found = passing[1].scan('teuthology.log', patterns, first=0, last=0)
    return {int(i) for i, (count, _) in found.items() if count}


def leave_out(workdir, passing):
    """Leave out of excerpts.txt the lines that passing, (job, its files), a job of the
    same run that passed, logs too, saying so at its top: once."""
    path = os.path.join(workdir, 'excerpts.txt')
    with open(path, errors='replace') as f:
        lines = f.read().split('\n')
    if not passing or any(line.startswith('# Left out: ') for line in lines):
        return
    numbered = [i for i, line in enumerate(lines) if NUMBERED.match(line)]
    common = {numbered[i] for i in passed_too(
        passing, [NUMBERED.sub('', lines[n]) for n in numbered])}
    if common:
        _write(workdir, 'excerpts.txt', '\n'.join(
            [LEFT_OUT.format(count=len(common), job=passing[0].path)] +
            [line for i, line in enumerate(lines) if i not in common]))


def needed(files, needs):
    """needed.txt, of the files of the archive a judge said it needs, as needs, its
    words, name them: the last error-looking lines of each, and its last lines, under
    a header that says which file they are from; '' if the archive has none."""
    rels = []
    for need in needs:
        for name in NEEDED_NAME.findall(str(need)):
            for pattern in (name,) if '/' in name else (name, f'remote/*/log/{name}'):
                rels += [rel for rel, _ in files.glob(pattern) if rel not in rels]
    out = []
    for rel in rels[:NEEDED_FILES]:
        errors = files.scan(rel, {'errors': (_ERRORS.pattern, ERROR_WORDS)}, first=0,
                            last=MAX_EXCERPTS)['errors'][1]
        out += [f'# {rel}: its last error-looking lines (line: text)'] + [
            line[:LINE_LIMIT] for line in compact([(h.line, h.text) for h in errors])
            [-NEEDED_LINES:]]
        out += [f'# {rel}: its last lines (line: text)'] + [
            f'{h.line}: {h.text[:LINE_LIMIT]}' for h in files.tail(rel, NEEDED_LINES)]
    return '\n'.join(out) + '\n' if out else ''
# What the judge is asked: to decide a job that the tree and the records could not.
UNDECIDED = ('A tree of deterministic checks explained what it could of the failure, but was '
             'not\nsure of the answer, and no known problem\'s log checks matched it. Decide '
             'whether it\nis a duplicate of one of the known problems in candidates.yaml, or '
             'something new.')
# Or to decide one they did, without being told what they decided.
BLIND = ('A tree of deterministic checks explained what it could of the failure. Decide '
         'whether it is\na duplicate of one of the known problems in candidates.yaml, or of '
         'a tracker issue in\ntrackers.txt, or something new.')


def checking(result):
    """What the judge is asked about a job that the tree and the records decided: to
    check the decision, and explain the failure."""
    if result.problem:
        decision = (f'it is a duplicate of the known problem {result.problem.id} '
                    f'("{result.problem.title}"), whose log checks all found their lines '
                    '(checks.txt)' +
                    (', seen again after its fix' if result.verdict == 'seen-after-fix'
                     else ''))
    else:
        decision = ('it is a new problem: the tree explained it conclusively '
                    '(explanation.txt), and no known problem\'s record nearly fits it')
    return ('The tool decided this job by itself, with no AI: ' + decision + '. Check that '
            'decision against the logs, and explain the failure for the person reviewing '
            'the run. Give the verdict the logs support: the tool\'s, if they show it, or '
            'another if they do not; "unsure" if you cannot tell.')


HOW = {
    'files': 'Everything you need is in the current directory: read only these files, '
             'run no network commands and change nothing.',
    'inline': 'The files are included at the end of this prompt, so you need no tools.',
}


def gather(workdir, files, result, problems, extra=None, blind=False, passing=None):
    """Fill workdir with the evidence for result.job, and the extra files, {name:
    text}; blind, with nothing that tells what the records' checks found; with
    passing, (job, its files), a job of the same run that passed, without the lines
    of the excerpts that it logs too."""
    job = result.job
    # A workdir that held another job's evidence, such as a lesson's taught again with
    # another of its jobs, starts afresh: nothing in it is this job's.
    if _evidence_of(workdir) not in (None, job.path):
        shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)
    _write(workdir, 'job.yaml', yaml.safe_dump({
        'run': job.run, 'job': job.id, 'status': job.status,
        'description': job.description, 'failure_reason': job.reason,
        'archive': files.base}, sort_keys=False, width=1000))

    _log_evidence(files, workdir)
    try:
        leave_out(workdir, passing)
    except (ArchiveError, OSError):
        pass

    finding = result.finding
    _write(workdir, 'explanation.txt',
           '\n'.join(finding.lines(LINE_LIMIT) if finding else
                     ['The tree did not look at this job.']) + '\n')

    lines = []
    for pid, kind, check, hits in result.checks:
        lines.append(f'{pid} {kind} {check}: ' + ('found' if hits else 'not found'))
        lines += [f'    {h.file}:{h.line}: {h.text[:LINE_LIMIT]}' for h in hits]
    if blind:
        lines = ['Not given: decide from the logs and the records.']
    _write(workdir, 'checks.txt', '\n'.join(lines or ['No candidate checks ran.']) + '\n')

    chosen = {}
    for p in result.candidates + near(finding, problems) + similar(job, problems):
        chosen.setdefault(p.id, p)
    # Blind, the order says nothing of which records' checks matched.
    order = sorted(chosen) if blind else list(chosen)
    texts = []
    for pid in order:
        with open(chosen[pid].source) as f:
            texts.append(f.read())
    _write(workdir, 'candidates.yaml', '\n---\n'.join(texts))
    _write(workdir, 'index.txt', ''.join(f'{p.id} [{p.kind}] {p.title}\n' for p in problems))
    for name in EXTRA_FILES:
        if (extra or {}).get(name):
            _write(workdir, name, extra[name])
        elif os.path.exists(os.path.join(workdir, name)):
            os.remove(os.path.join(workdir, name))


def _evidence_of(workdir):
    """<run>/<job> of the job whose evidence is in workdir; None if none is."""
    try:
        with open(os.path.join(workdir, 'job.yaml')) as f:
            found = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    return f'{found.get("run")}/{found.get("job")}'


def build(workdir, files, result, problems, task=UNDECIDED, extra=None, blind=False,
          passing=None):
    """Fill workdir with the evidence for result.job, as gather does.

    Returns {'inline': prompt, 'files': prompt}: the first includes the evidence,
    the second asks the agent to read it from workdir. task is what the judge is
    asked to do.
    """
    gather(workdir, files, result, problems, extra, blind, passing)
    with open(os.path.join(PROMPTS, 'judge.md')) as f:
        template = string.Template(f.read())
    evidence = ''
    for name in INLINE_FILES:
        if name in EXTRA_FILES and not os.path.exists(os.path.join(workdir, name)):
            continue
        with open(os.path.join(workdir, name)) as f:
            evidence += f'\n=== {name} ===\n{f.read()}'
    prompts = {mode: template.substitute(task=task, how=HOW[mode], schema=schema(),
                                         evidence=evidence if mode == 'inline' else '')
               for mode in HOW}
    for mode, prompt in prompts.items():
        _write(workdir, f'prompt-{mode}.md', prompt)
    return prompts


def quotes(answer):
    """The lines an agent's answer quotes: each {file, text}, of its evidence and of
    each step of its walkthrough; any other thing it gave as evidence is left out."""
    found = []
    for part in [answer] + steps(answer):
        evidence = part.get('evidence')
        found += [q for q in evidence if isinstance(q, dict)] if isinstance(evidence, list) \
            else []
    return found


def told(answer):
    """True if an agent's answer tells a person what failed: an explanation or a
    walkthrough."""
    return bool(answer) and bool(str(answer.get('explanation') or '').strip() or
                                 steps(answer))


def steps(answer):
    """The steps of an answer's walkthrough: each {title, do, finds, evidence}."""
    walk = answer.get('walkthrough')
    return [s for s in walk if isinstance(s, dict)] if isinstance(walk, list) else []


# Where the evidence files say a line is from: the tree's and the checks' citations
# (file:line: text), the numbered lines of excerpts.txt, daemon-log.txt and
# requested/ (line: text), under a header that names their file (# file...), and
# the start of the end of a cut teuthology.log.
CITATION = re.compile(r'^\s*(?:- )?([\w.@+*/-]+):(\d+): ')
NUMBERED = re.compile(r'^(\d+): ')
HEADER = re.compile(r'^# ([\w.@+*/-]+)(?:[:,]|$)')
CUT = re.compile(r'^\[\.\.\. cut by qa_triage; .* from line (\d+) \.\.\.\]$')


def located(workdir, quote):
    """(file, line) in the job's archive of a line an agent quoted from the evidence
    in workdir, as the evidence file says where its lines are from; None if it does
    not say, or the quote is not found."""
    rel = os.path.normpath(quote.get('file', ''))
    first = next((t.strip() for t in str(quote.get('text', '')).splitlines() if t.strip()), '')
    first = NUMBERED.sub('', first)
    path = os.path.join(workdir, rel)
    if not first or rel.startswith('..') or os.path.isabs(rel) or not os.path.isfile(path):
        return None
    with open(path, errors='replace', newline='\n') as f:
        lines = f.read().split('\n')
    at = next((i for i, line in enumerate(lines) if first in line), None)
    if at is None:
        return None
    cited = CITATION.match(lines[at])
    if cited and rel in ('explanation.txt', 'checks.txt'):
        return cited.group(1), int(cited.group(2))
    if rel == 'teuthology.log':
        cut = next((i for i in range(at, -1, -1) if CUT.match(lines[i])), None)
        return ('teuthology.log', at + 1 if cut is None else
                int(CUT.match(lines[cut]).group(1)) + at - cut - 1)
    header = next((i for i in range(at, -1, -1) if HEADER.match(lines[i])), None)
    source = 'teuthology.log' if rel == 'excerpts.txt' else \
        HEADER.match(lines[header]).group(1) if header is not None else None
    numbered = NUMBERED.match(lines[at])
    if not source:
        return None
    if numbered:
        return source, int(numbered.group(1))
    # A file copied whole into requested/, under its header.
    return (source, at - header) if rel.startswith('requested/') else None


def locate(workdir, answer, files=None, most=None):
    """Give each line an agent's answer quotes, that the evidence says where it is
    from, its place in the job's archive, as "at": "file:line"; and, given files,
    the job's archive, the lines of the logs that have it (see shown). The answer."""
    for quote in quotes(answer):
        found = 'at' not in quote and located(workdir, quote)
        if found:
            quote['at'] = f'{found[0]}:{found[1]}'
    if files is not None:
        shown(workdir, answer, files, most or MOST_SHOWN)
    return answer


# The most lines of the logs a result shows for a line a judge quotes, of those that
# have it (the setting cited_lines).
MOST_SHOWN = 10


# What excerpts.txt adds after a line that stands for others like it.
SIMILAR = re.compile(r'\s+\(\+\d+ similar\)$')


def _as_logged(text):
    """The first line of a quote as the logs have it: without the file:line or the
    line number that the evidence put before it, or the count of similar lines after
    it."""
    first = next((t.strip() for t in str(text).splitlines() if t.strip()), '')
    cited = CITATION.match(first)
    return SIMILAR.sub('', NUMBERED.sub('', first[cited.end():] if cited else first))


def shown(workdir, answer, files, most=MOST_SHOWN):
    """Give each line an answer quotes the lines that have it, whole, for a person to
    read it as the logs have it: "lines", [[file, line, text, [start, end] of the quote
    in it]], at most most of them, the line the judge read among them, and "count",
    how many there are. They are the lines of the job's archive, files, if the quote
    is from there, named as it names them now; else of the evidence in workdir, and
    "archived" is false. The answer."""
    # {file of the archive: [(quote, its text as logged, where the judge read it)]}
    wanted = {}
    for quote in quotes(answer):
        text = _as_logged(quote.get('text', ''))
        lines = quote.get('lines')
        if not text or isinstance(lines, list) and \
                len(lines) == min(quote.get('count') or 0, most):
            continue
        at = str(quote.get('at') or '')
        rel = at.rsplit(':', 1)[0] if re.search(r':\d+$', at) else \
            'teuthology.log' if os.path.normpath(str(quote.get('file', ''))) in (
                'excerpts.txt', 'teuthology.log') else None
        if rel:
            wanted.setdefault(rel, []).append((quote, text, at))
        else:
            _from_evidence(workdir, quote, text, most)
    for rel, asked in wanted.items():
        found = _from_archive(files, rel, asked, most)
        for quote, text, at in asked:
            # One the evidence says where it is from is shown there, if no line has it.
            if not found.get(id(quote)) and not at:
                _from_evidence(workdir, quote, text, most)
    return answer


def _from_archive(files, rel, asked, most):
    """Give each quote of asked, [(quote, text, at)], the lines of rel in the archive,
    files, that have its text, in one pass over rel: {id(quote): True} of those some
    line has."""
    patterns = {f'q{i}': (re.escape(text), [text]) for i, (_, text, _) in enumerate(asked)}
    try:
        real = files.real(rel)
        marks = files.scan(rel, patterns, most, 0) if files.glob(rel) else {}
        found = {}
        for i, (quote, text, at) in enumerate(asked):
            count, hits = marks.get(f'q{i}', (0, []))
            read = int(at.rsplit(':', 1)[1]) if at else None
            if read and hits and read not in [h.line for h in hits] and count > len(hits):
                # The line the judge read, beyond the first that have the quote.
                hits = hits[:most - 1] + files.between(rel, read, read)
            if not hits:
                continue
            quote['lines'] = [[real, h.line, h.text, list(h.match or _span(h.text, text))]
                              for h in hits]
            quote['count'], quote['archived'] = count, True
            if at:
                quote['at'] = f'{real}:{read}'
            found[id(quote)] = True
        return found
    except ArchiveError:
        return {}


def _from_evidence(workdir, quote, text, most):
    """Give quote the lines of the evidence file it names that have text, if any."""
    rel = os.path.normpath(str(quote.get('file', '')))
    path = os.path.join(workdir, rel)
    if rel.startswith('..') or os.path.isabs(rel) or not os.path.isfile(path):
        return
    with open(path, errors='replace', newline='\n') as f:
        found = [[rel, n, line, list(_span(line, text))]
                 for n, line in enumerate(f.read().split('\n'), 1) if text in line]
    if found:
        quote['lines'], quote['count'], quote['archived'] = found[:most], len(found), False


def _span(line, text):
    start = line.find(text)
    return (start, start + len(text)) if start >= 0 else ()


def quote_found(workdir, quote):
    """True if quote['text'] is in the file it names inside workdir. A quote of
    several lines, such as a traceback, must be in as many lines one after
    another. A quoted line may start with the line number that the read_file
    tool puts before it."""
    rel = os.path.normpath(quote.get('file', ''))
    text = quote.get('text', '').strip()
    if not text or rel.startswith('..') or os.path.isabs(rel):
        return False
    path = os.path.join(workdir, rel)
    if not os.path.isfile(path):
        return False
    wanted = [{t, re.sub(r'^\d+: ', '', t)}
              for t in (line.strip() for line in text.splitlines()) if t]
    with open(path, errors='replace', newline='\n') as f:
        lines = f.read().split('\n')
    return any(all(any(t in lines[i + j] for t in texts) for j, texts in enumerate(wanted))
               for i in range(len(lines) - len(wanted) + 1))
