"""Asking a person before the library changes: what the tool got wrong, what the
analyst would change, and what that would change of what it decided before, with
the chance to talk it over with the analyst in its own program first.

Every change to the records, the tree's nodes or the settled jobs comes
from a doubt or a lesson (learn.Doubt). The library is brought up to date before
the analyst starts, and again before its change is written; the change is checked
against what is there; and, with approval asked, the person sees it and decides:
approved, it is written and published to the library's remote for everyone at
once.
"""

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile

import yaml

from . import db, share
from .learn import held_writes

# The approval setting: ask the person; approve every change that passes the
# checks; or keep each for a person to approve later.
ASK, ALWAYS, HOLD = 'ask', 'always', 'hold'
CHOICES = (ASK, ALWAYS, HOLD)
DIFF_LINES = 60
# What the analyst tells the person, when they talk in its own program, of how to have
# it change how the tool decides.
HOWTO = ('tell you the right answer and why, how far the change should reach, and what kind '
         'of change they want, and then ask you to write your proposal to proposal.json.')
# What the analyst is told before the person talks with it about its change.
LIVE = ('A person must approve your change to the tool\'s records and tree before it is '
        'kept, and will now talk with you about it in your own program, in this directory. '
        'Explain what was wrong, what your change does, and why, plainly and briefly; '
        'quote the log lines you rely on. If they ask for a different change, write it, '
        'the JSON object the instructions describe, to proposal.json in this directory, '
        'and tell them it is written: the tool checks it when they leave you. Change no '
        'other file. To begin, say in a few lines what was wrong and what you would change; '
        'then, in a line or two, how they can have you change it: ' + HOWTO)
PROPOSAL = 'proposal.json'
# What approve says of a change it did not keep: the screen shows why from it.
NOT_KEPT = 'not kept'


def diff(path, text, context=2):
    """The lines of a unified diff of the file at path, as it is, to text."""
    try:
        with open(path) as f:
            old = f.read().splitlines()
    except OSError:
        old = []
    name = os.path.basename(path)
    return list(difflib.unified_diff(old, text.splitlines(), f'{name} (now)',
                                     f'{name} (proposed)', n=context, lineterm=''))


def effect(report):
    """What the checks' report says the change does to jobs triaged before: (the
    lines of jobs whose result changes, whether every check passed)."""
    moved = [line[6:] for line in report if line.startswith('ok    ') and
             ('(not settled):' in line or 'moves with it' in line)]
    return moved, not any(line.startswith('FAIL') for line in report)


def failing(report):
    """The checks of a report that fail, each with the lines under it, such as the
    quotes that were not found: in plain words, where it can."""
    found, under = [], False
    for line in report:
        if line.startswith('FAIL'):
            found.append(plain(line[6:].strip()))
            under = True
        elif under and line.startswith('      '):
            found.append('  ' + line.strip())
        else:
            under = False
    return found


def plain(line):
    """A check that failed, as a sentence a person reads easily; the line as it is,
    if it is not one this knows."""
    lost = re.match(r'record (\S+) loses examples: (.+)$', line)
    if lost:
        return (f'It drops {lost.group(2)} from record {lost.group(1)}\'s examples, the jobs '
                'the record must place; another lesson may have added them since this change '
                'was made.')
    job = re.match(r'(\S+) \((.*?)\): (.+?); should be (.+?)'
                   r'(?:, or a record that lists it under also)?$', line)
    if not job:
        return line
    path, where, got, want = job.groups()
    want = 'new' if want == 'no record' else want
    got = re.sub(r', (un)?matched$', '', re.sub(r'\s*\([^()]*->[^()]*\)', '', got))
    got = re.sub(r'^(duplicate|probable-duplicate) (\S+)$', r'placed in \2', got)
    got = re.sub(r'^seen-after-fix (\S+)$', r'placed in \1, seen after its fix', got)
    got = 'new, placed in no record' if got == 'new' else got
    whose = (f'which {where[len("settled by "):]} settled as {want}'
             if where.startswith('settled by ') else
             f'an example of record {where[len("example of "):]}'
             if where.startswith('example of ') else
             'the job this change is for' if where == 'doubted' else where)
    return f'With this change, {path}, {whose}, would be {got}, not {want}.'


def explain(doubt, answer, writes, report, full=False):
    """Clear text for a person: what was wrong, what the analyst would change, and
    what that changes."""
    now = getattr(doubt, 'result', None)
    was = (now.problem.id if now is not None and now.problem else
           now.verdict if now is not None and now.verdict else
           (getattr(doubt, 'recorded', None) or {}).get('problem') or 'new')
    said = answer.get('problem') or 'no known problem (new)'
    lines = [f'The analyst ({doubt.analyst.name}) proposes to change how the tool decides '
             f'jobs like {doubt.job.path}.', '',
             f'What was wrong: the tool said {was}; the analyst says {said}' +
             (f', as {doubt.by} expected' if doubt.expected else '') + '.']
    why = answer.get('explanation') or answer.get('summary') or ''
    if why:
        lines += ['', 'Why, in its words:'] + [f'  {line}' for line in _wrap(why, 88)]
    lines += ['', 'What it would change:']
    for name, text in sorted(writes.items()):
        path = doubt._path(name)
        kind = 'node' if name.startswith('nodes/') else 'record'
        lines.append(f'  {"a new " if not os.path.exists(path) else ""}{kind} {name}')
        changed = diff(path, text)
        shown = changed if full else changed[:DIFF_LINES]
        lines += [f'    {line}' for line in shown]
        if len(changed) > len(shown):
            lines.append(f'    ... {len(changed) - len(shown)} more lines ([f] shows them)')
    moved, passed = effect(report)
    lines += ['', 'What that changes of what the tool decided before:']
    lines += [f'  {line}' for line in moved[:12]] or ['  no other job triaged before']
    if len(moved) > 12:
        lines.append(f'  ... and {len(moved) - 12} more')
    lines.append('  Every check passed.' if passed else '  Some checks failed: see above.')
    return lines


def _wrap(text, width):
    words, line, out = text.split(), '', []
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f'{line} {word}' if line else word
    return out + ([line] if line else [])


def guidance(program, after):
    """For the person, before they talk with the analyst in its own program: how to
    have it change how the tool decides jobs like this one. after says what is done
    with its proposal when they leave it."""
    return [f'How to have {program} change how qa_triage decides jobs like this one:',
            '  1. Settle the answer first. Say which tracker issue the job belongs in, or that '
            'it is a',
            '     new problem, and why: the log line that shows it, if it got that wrong.',
            '  2. Say how far the change should reach: which other jobs to decide the same way',
            '     ("every job whose mgr log says Port 8443 is not free, whatever test failed '
            'after"),',
            '     and which not ("only in the dashboard suite").',
            "  3. Say what kind of change you want, or let it choose: a record's, the lines "
            'that identify',
            "     a known problem (problems/<issue>.yaml), or the tree's, a question it asks of "
            'every job,',
            '     such as "did a daemon crash first?" (a node, nodes/<name>.py). Ask to '
            'see it first',
            '     if you want to.',
            f'  4. Then ask it to write its proposal to {PROPOSAL}. Only that file is read: '
            'what it edits',
            '     anywhere else is not kept.',
            f'When you leave {program}, {after}']


def terminal(ask=input, say=print, run=None):
    """An approval for Doubt that asks the person at the terminal: approve
    (doubt, answer, writes, report) -> (approved, answer, writes, report), the
    answer, its writes and their checks being those of a revision written while
    they talked with the analyst, if they took it."""
    def approve(doubt, answer, writes, report):
        full = False
        while True:
            say('\n'.join(explain(doubt, answer, writes, report, full)))
            program = getattr(doubt.analyst, 'program', None)
            choices = ['[y]es, keep it and push it', '[n]o', '[f]ull diff'] + \
                ([f'[d]iscuss it with the analyst in {program}'] if program else [])
            try:
                reply = ask(', '.join(choices) + ': ').strip().lower()
            except EOFError:
                reply = 'n'
            if reply in ('y', 'yes'):
                return True, answer, writes, report
            if reply in ('n', 'no', 'q'):
                return False, answer, writes, report
            if reply in ('f', 'full'):
                full = True
            elif reply in ('d', 'discuss') and program:
                say('\n'.join(guidance(program, 'its proposal is checked against every job '
                                                 'triaged before, and shown here for you to keep '
                                                 'or not.')))
                revised = doubt.talk_live(LIVE, PROPOSAL, run)
                if revised:
                    ok, report2, writes2 = doubt.check(revised)
                    if ok:
                        answer, writes, report = revised, writes2, report2
                        say('Its revised change passes the checks: here it is.')
                    else:
                        say('Its revised change does not pass the checks, so the first one '
                            'stands:\n' + '\n'.join(l for l in report2 if l.startswith('FAIL')))
    return approve


def doubting(settings, cache, library=None, interactive=None):
    """What Doubt needs, from the settings, to ask before changing the library and
    to publish the change, and to keep a change for an answer that disputes the
    people's: {approve, approve_edits, library, publish, dispute}. interactive is
    False where no one can be asked now, as on the review's screen; None, if a
    terminal is.

    A change that only adds to the library, a record or a node, or a job to a
    record's examples, is approved as the approve setting says. One that edits what
    is there is asked about even where additions are kept by themselves, unless the
    approve_edits setting says always too: an addition that passes every check adds
    what the library knows, and an edit can lose some of it, in ways the checks
    cannot see."""
    how = (settings or {}).get('approve') or ASK
    edit = (settings or {}).get('approve_edits') or (ASK if how == ALWAYS else how)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    def asking(how, directory, why=''):
        return None if how == ALWAYS else terminal() if how == ASK and interactive else \
            hold(directory, why)

    # Edits wait apart only where additions are kept without a person: where every
    # change waits, they wait together.
    return {'approve': asking(how, pending(cache)),
            'approve_edits': asking(edit, edits(cache), 'it edits what the library has, '
                                                        'which a person approves')
                             if how == ALWAYS else asking(edit, pending(cache)),
            'library': library, 'publish': bool(library),
            'dispute': keep_disputed(disputed(cache))}


def pending(cache):
    """Where the changes that wait for a person's approval are kept."""
    return os.path.join(cache, 'pending')


def edits(cache):
    """Where the changes that edit the library, not only add to it, wait for a
    person, apart from those a person or a training's applier approves alike."""
    return os.path.join(pending(cache), 'edits')


def disputed(cache):
    """Where the changes are kept that the analyst made for an answer that disputes
    the people's, for a person who agrees with the analyst: qa_triage settle."""
    return os.path.join(pending(cache), 'disputed')


def kept(directory, job):
    """Where the change for job is kept in directory."""
    return os.path.join(directory, re.sub(r'[^\w.-]', '_', job.path) + '.json')


def _keep(directory, doubt, answer, **more):
    os.makedirs(directory, exist_ok=True)
    path = kept(directory, doubt.job)
    with open(path, 'w') as f:
        json.dump(dict(answer, job=doubt.job.path, why=doubt.why, session=doubt.session,
                       analyst=getattr(doubt.analyst, 'name', None),
                       workdir=os.path.abspath(doubt.workdir), **more), f, indent=1)
    return path


def hold(directory, why=''):
    """An approval for Doubt that keeps each change for a person to approve later,
    in directory: the answer, to give `qa_triage doubt <job> --proposal <file>`; why
    says why it waits, if not only for approval."""
    def approve(doubt, answer, writes, report):
        # As a commit too, for the person to inspect the code it changes.
        made = share.pending_commit(library_of(doubt.problems_dir), writes,
                                    commit_message(doubt.job.path, answer),
                                    ref_name(kept(directory, doubt.job)))
        # With the files as they were, which it was made on: approving it makes it
        # again on them as they are then.
        path = _keep(directory, doubt, answer, by=doubt.by, expected=doubt.expected,
                     writes=writes, before={name: _read(doubt._path(name)) for name in writes},
                     **({'repository': made[0], 'commit': made[1]} if made else {}))
        doubt.held = path
        doubt.say(f'{doubt.job.path}: the change waits for approval' +
                  (f' ({why})' if why else '') + f': qa_triage doubt {doubt.job.path} '
                  f'--proposal {path}' +
                  (f'; to inspect it, git -C {made[0]} show {made[1]}' if made else ''))
        return False, answer, writes, report
    return approve


def library_of(problems_dir):
    """The library a problems/ directory is in."""
    return os.path.dirname(os.path.abspath(problems_dir))


def ref_name(path):
    """The name of the ref that keeps the commit of the change kept at path."""
    return os.path.basename(path)[:-len('.json')]


def commit_message(job_path, answer):
    return (f'Proposed from {job_path}: {(answer.get("summary") or "")[:100]}\n\n'
            'It waits for a person\'s approval: qa_triage pending lists it, and the '
            'review\'s A key asks about it.')


def held_in(directory):
    """[(path, change)] of the changes kept in directory for a person's approval."""
    names = sorted(n for n in os.listdir(directory) if n.endswith('.json')) \
        if os.path.isdir(directory) else []
    found = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            with open(path) as f:
                found.append((path, json.load(f)))
        except (OSError, ValueError):
            continue
    return found


def waiting(cache):
    """[(path, change)] of the changes that wait for a person's approval: those that
    wait together, then those that edit the library, where they wait apart."""
    return held_in(pending(cache)) + held_in(edits(cache))


def count_waiting(cache):
    """How many changes wait for a person's approval, without reading them."""
    return sum(len([n for n in os.listdir(d) if n.endswith('.json')])
               for d in (pending(cache), edits(cache)) if os.path.isdir(d))


def commit_of(path, held, problems_dir):
    """(repository, commit) of the change held, kept at path, for a person to inspect:
    made now, and kept with it, for one held before commits were made; None if none
    can be."""
    if held.get('repository') and held.get('commit'):
        return held['repository'], held['commit']
    made = share.pending_commit(library_of(problems_dir), held_writes(held, problems_dir),
                                commit_message(held.get('job', '?'), held), ref_name(path))
    if made:
        held.update(repository=made[0], commit=made[1])
        with open(path + '.new', 'w') as f:
            json.dump(held, f, indent=1)
        os.replace(path + '.new', path)
    return made


def rejected(cache):
    """Where the changes a person rejected are kept, rather than lost."""
    return os.path.join(pending(cache), 'rejected')


def reject(path, cache):
    """Set aside the change kept at path, which a person rejected: its commit stays,
    to inspect."""
    os.makedirs(rejected(cache), exist_ok=True)
    os.replace(path, os.path.join(rejected(cache), os.path.basename(path)))


def approved(path, held):
    """Forget the change kept at path, and its commit, once it is kept."""
    if os.path.exists(path):
        os.remove(path)
    if held.get('repository'):
        share.drop_pending_commit(held['repository'], ref_name(path))


def superseded(path, held, new):
    """Forget the change kept at path, which a revision, kept at new or written,
    takes the place of: its commit too, unless the revision's is kept by the same
    name."""
    if new and os.path.abspath(new) == os.path.abspath(path):
        return
    if os.path.exists(path):
        os.remove(path)
    if held.get('repository') and not (new and ref_name(new) == ref_name(path)):
        share.drop_pending_commit(held['repository'], ref_name(path))


def keep_disputed(directory):
    """What Doubt calls with a change that passed the checks for an answer that is
    not the people's: kept in directory, its path returned. Kept so, it answers as
    the analyst did, for whoever takes it up; who doubted the job, and what they
    expected, are kept apart."""
    def keep(doubt, answer):
        return _keep(directory, doubt, answer, expected=answer['problem'] or 'new',
                     disputes={'expected': doubt.expected, 'by': doubt.by})
    return keep


# Rebasing a change that waits onto the library as it has moved since it was made.
# What a record gains from both sides, rather than either's alone.
GROWS = ('examples', 'also', 'learned')


class Clash(Exception):
    """A change that waits, whose files the library changed too since it was made, in
    ways that cannot be put together: [what clashes]."""

    def __init__(self, clashes):
        super().__init__('; '.join(clashes))
        self.clashes = clashes


def rebase(held, problems_dir, nodes_dir):
    """The change held, made again onto the library as it is now: (the change, [what
    it takes now from what others changed meanwhile]). Where others changed a file it
    writes, since it was made, it keeps what both did: a record has the jobs and
    issues either added, and what only one side changed; a node's code is merged as
    git merges. Clash if both changed the same thing differently. A change keeps the
    files it was made on; for one kept before it did, they are not known, and any
    key the two sides give differently clashes. (Its commit's parent is no guide: a
    change kept before changes had commits was given one when it was first shown.)"""
    before, writes = held.get('before'), held.get('writes') or {}

    def base(name):
        return (before or {}).get(name)

    # The record files the change writes, by id.
    named = {}
    for name, text in writes.items():
        if name.startswith('problems/'):
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                data = None
            if isinstance(data, dict) and data.get('id') is not None:
                named[str(data['id'])] = name
    now = {p.id: p.source for p in db.load(problems_dir)}
    change, took, clashes = dict(held), [], []
    records = []
    for item in held.get('changes') or []:
        pid, theirs = str(item.get('id')), str(item.get('yaml', ''))
        was = base(named[pid]) if pid in named else \
            base(f'problems/{os.path.basename(now[pid])}') if pid in now else None
        try:
            text, keys = merge_record(was, _read(now.get(pid)), theirs,
                                      known=before is not None)
        except Clash as e:
            clashes += [f'record {pid}: {c}' for c in e.clashes]
            records.append(item)
            continue
        if keys:
            took.append(f'record {pid} ({", ".join(keys)})')
        records.append(dict(item, yaml=text))
    nodes = []
    for item in held.get('nodes') or []:
        name = str(item.get('file', ''))
        name = name[len('nodes/'):] if name.startswith('nodes/') else name
        theirs = str(item.get('code', ''))
        mine = _read(os.path.join(nodes_dir, name)) if nodes_dir else None
        was = base(f'nodes/{name}')
        if mine is None or mine == was or _same(mine, theirs):
            if mine is None and was is not None:
                clashes.append(f'nodes/{name}: removed meanwhile')
            nodes.append(item)
            continue
        merged = None if was is None else merge_text(mine, was, theirs)
        if merged is None:
            clashes.append(f'nodes/{name}: its code was changed meanwhile too')
            nodes.append(item)
            continue
        took.append(f'nodes/{name}')
        nodes.append(dict(item, code=merged))
    if clashes:
        raise Clash(clashes)
    if held.get('changes'):
        change['changes'] = records
    if held.get('nodes'):
        change['nodes'] = nodes
    if took:
        # What it writes is made again from its records and nodes, as they are now.
        change.pop('writes', None)
    return change, took


def merge_record(base, now, theirs, known=True):
    """(the text of record theirs, which was made from base, with what now, the
    record as others left it, changed since; the keys it took from now). Each side's
    jobs and issues are kept; any other key one side changed is that side's. Clash
    if both changed a key differently, or now is gone. Without known, what it was
    made from is not known: base is None, and any key the two give differently
    clashes."""
    if now is None:
        if base is not None:
            raise Clash(['removed meanwhile'])
        return theirs, []
    if _same(now, base) or _same(now, theirs):
        return theirs, []
    b, o, t = (_load(text) for text in (base, now, theirs))
    merged, took, clashes = {}, [], []
    for key in dict.fromkeys([*t, *o]):
        bv, ov, tv = b.get(key), o.get(key), t.get(key)
        if key in GROWS:
            value = _merge_list(bv or [], ov or [], tv or [])
            if [str(x) for x in value] != [str(x) for x in tv or []]:
                took.append(key)
        elif ov == tv or ov == bv:
            value = tv
        elif tv == bv:
            value = ov
            took.append(key)
        else:
            clashes.append(key)
            continue
        if value is None or (value == [] and key not in t):
            continue
        merged[key] = value
    if clashes:
        raise Clash([(f'another lesson {"wrote it" if base is None else "changed it"} '
                      'meanwhile too' if known else 'another lesson may have changed it '
                      'meanwhile') + f', with a different {", ".join(clashes)}'])
    if merged == t:
        return theirs, []
    # As git merges it, if that keeps its layout and comes to the same; else written
    # out again.
    text = merge_text(now, base, theirs) if base is not None else None
    if text is None or _load(text) != merged:
        text = yaml.dump(merged, Dumper=_Dumper, sort_keys=False, width=100,
                         allow_unicode=True)
    return text, [k for k in took if k != 'learned']


def merge_text(mine, base, theirs):
    """theirs, made from base, merged with mine as git merge-file merges them; None if
    they conflict."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, text in (('mine', mine), ('base', base), ('theirs', theirs)):
            paths.append(os.path.join(tmp, name))
            with open(paths[-1], 'w') as f:
                f.write(text)
        p = subprocess.run(['git', 'merge-file', '-p', *paths], capture_output=True,
                           text=True, timeout=60)
    return p.stdout if p.returncode == 0 else None


def _merge_list(base, now, theirs):
    """theirs, without what now dropped of base, and with what now added to it."""
    b, o, t = ({str(x) for x in items} for items in (base, now, theirs))
    return [x for x in theirs if not (str(x) in b and str(x) not in o)] + \
        [x for x in now if str(x) not in b and str(x) not in t]


def _load(text):
    data = yaml.safe_load(text) if text else {}
    return data if isinstance(data, dict) else {}


def _same(a, b):
    return a is not None and b is not None and a.rstrip('\n') == b.rstrip('\n')


def _read(path):
    if not path:
        return None
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


class _Dumper(yaml.SafeDumper):
    """Records as people write them: text of several lines as a block."""


_Dumper.add_representer(str, lambda dumper, text: dumper.represent_scalar(
    'tag:yaml.org,2002:str', text, style='|' if '\n' in text else None))
