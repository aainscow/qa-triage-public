"""qa_triage review: work through a run, and spend AI only when asked.

The run is triaged with the tree and the records alone when the review starts.
Then each command either looks at what they found, which is free, or asks an
agent, which costs: the judge about a group (judge), the analyst to learn from a
group (learn), to answer a doubt about a job (doubt), or to talk it over, in the
analyst's own program (talk) or here (discuss).

Nothing paid for is thrown away. Every answer an agent gives is kept, with how
it got there, what it cost and its session, in review.json in the output
directory, as soon as it is given; a review of the same run, with the same
output directory, starts from it. Asking the judge or the analyst about a group
again shows the answer it gave before, unless asked again on purpose; a
discussion continues the analyst's last session about the job, and so knows what
was said in it.
"""

import cmd
import concurrent.futures
import contextlib
import datetime
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error

import yaml

from . import approve, baseline, config, db, editor, pack, report, setup, share
from .agents import JudgeError, Stopped, each_at_once, installed, make_agent, readable, units
from .fingerprint import to_regex
from .graph import FOUND, describe, placed
from .learn import (LESSON, NOT_BEGUN, Doubt, answer_of, held_writes, keep_account,
                    learn_from_run, lessons, next_tag, problem_id, recorded)
from .sources import (QA_PROXY, TRACKER, ArchiveError, Job, comment_issue, comment_pr,
                      file_issue, pr_title, tracker_key, update_issue, use_tracker_key)
from .sources import tracker_issue as read_issue
from .tree import learned

MAX_HITS = 20
# The settings the review cannot take up while it is open: the run was triaged with
# them, or they are for when it opens.
LATER = ('archive', 'git', 'db', 'problems', 'nodes', 'labels', 'cache', 'worktrees',
         'deep', 'at_once', 'corpus', 'judge_on_open')
STATE = 'review.json'
KINDS = {'judge': 'the judge', 'lesson': 'a lesson', 'doubt': 'a doubt',
         'discussion': 'a discussion', 'track': 'tracking', 'proposal': 'a proposal',
         'filed': 'filing', 'posted': 'posting', 'revision': 'a revision'}
# The commands that change the results: once each is done, the report, the drafts and
# results.json are written as they are then.
CHANGES = ('judge', 'learn', 'doubt', 'propose', 'retriage', 'file', 'track', 'approve',
           'revise', 'post', 'sightings', 'qa')
# The tracker's status of a QA batch the reviewers approved.
QA_APPROVED = 25
# The ways to update the records outside qa_triage about a failure (see Review.ways).
WAYS = {'pr': 'a comment on a PR', 'jobs': 'a comment on a tracker issue: only its jobs',
        'tracker': 'a comment on a tracker issue', 'issue': 'a new tracker issue'}
DISCUSS = ('A person is reviewing this failed job and wants to discuss it with you. Answer '
           'their questions in plain text, not JSON, and briefly; quote the log lines you '
           'rely on, with the file each is in. The files in the current directory are the '
           'evidence. You can also change how the tool decides about jobs like this one, '
           'its records and its tree of questions, as the instructions say; but give such '
           'an answer, the JSON object they describe, only when the person asks for your '
           'proposal. Until then, discuss.')
LIVE = ('The person will now talk with you in your own program, in this directory. When '
        'they ask for your proposal, write it, the JSON object that the instructions '
        'describe, to proposal.json in this directory, and tell them it is written: the '
        'tool checks it when they leave you, and keeps it only if it passes the checks. '
        'Change no other file. To begin, say in a few lines what you make of this failure '
        'so far, and what you would look at next; then, in a line or two, how they can have '
        'you change how the tool decides about jobs like it: ' + approve.HOWTO)
# The analyst, asked to make a change that waits for approval again, for the library
# as others have changed it since.
REVISE = ('Your change to the tool\'s records and tree waited for a person\'s approval, and '
          'meanwhile others changed the library. Checked again, against the library as it '
          'is now, it cannot be kept:\n{why}\n\nRead the records and nodes it changes as they '
          'are now, and make your change again for them: keep what others changed, unless '
          'the logs show it is wrong. Then reply with the JSON object the instructions '
          'describe, as before.')
# The analyst, once a person who doubted a result has read its response, and wants to
# discuss it in the analyst's own program; and when they leave it.
RESPONSE_TALK = ('The person who doubted the result has read your response, and wants to '
                 'discuss it with you, in your own program, in this directory, where all '
                 'the evidence still is, with the records and the tree. Discuss it: answer '
                 'their questions and their objections from what you found, briefly, quoting '
                 'the log lines you rely on, with the file each is in. Do not start a new '
                 'analysis, or read further, unless they ask you to. Write no JSON answer '
                 'and change no file: when they leave, you will be asked once more for your '
                 'final answer, with any change the discussion calls for. To begin, say in '
                 'two or three lines what you concluded, and what you are least sure of; '
                 'then wait for them.')
DIGEST = ('The person has finished discussing your response with you. From what you found '
          'and what was discussed, reply now with only the JSON object that the '
          'instructions describe: your final answer, with the changes to the records '
          '("changes") or to the tree ("nodes") that it needs, including any the discussion '
          'called for. If they convinced you, answer as they say; if not, answer what the '
          'logs show, and say why in the summary. It is checked as before, and the person '
          'decides whether it is kept.')
PROPOSAL = 'proposal.json'
PROPOSE = ('The person asks for your proposal. From what you found and what was discussed, '
           'reply now with only the JSON object that the instructions describe: your '
           'assessment, the problem this job should be placed in, or null, and the changes to '
           'the records ("changes") or to the tree ("nodes") that make the tool get jobs like '
           'this one right by itself. They are checked as the instructions say before they '
           'are kept.')


def analysis(workdir):
    """The analyst's last answer in a lesson's or a doubt's analysis.md: what it
    found, its changes and the checks'."""
    try:
        with open(os.path.join(workdir, 'analysis.md')) as f:
            lines = f.read().rstrip('\n').split('\n')
    except OSError:
        return []
    starts = [i for i, line in enumerate(lines)
              if re.match(r'\S+: (the result is|it cannot tell|it needs more)', line)]
    return lines[starts[-1]:] if starts else lines


def last_checks(workdir):
    """The report of the checks of the analyst's last answer in workdir's analysis.md,
    a line each; [] if there is none."""
    lines = analysis(workdir)
    at = max((i for i, line in enumerate(lines) if line == 'Checks:'), default=None)
    return [] if at is None else lines[at + 1:]


def expand(answer):
    """An answer that only points at an analysis, as one kept before a lesson's
    answer was, with the analysis and its summary."""
    pointer = re.search(r' see (\S+)/analysis\.md$', answer['lines'][-1]) \
        if len(answer['lines']) == 1 else None
    if not pointer:
        return answer
    workdir = pointer.group(1)
    summary = answer.get('summary')
    try:
        with open(os.path.join(workdir, 'lesson.json')) as f:
            summary = summary or json.load(f).get('summary')
    except (OSError, ValueError):
        pass
    return dict(answer, lines=answer['lines'] + analysis(workdir), summary=summary,
                workdir=answer.get('workdir') or workdir)


def short_line(text, limit=120):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


class State:
    """What the review has paid for, kept in a file: {job id: [answer]}, where an
    answer is {kind, agent, when, cost, lines, steps, session, workdir}; every
    cost, [{kind, agent, cost, jobs}]; the jobs whose groups were judged; and each
    job's discussion, {agent, session, workdir, turns: [{who, text}]}. A workdir
    is kept relative to the file's directory."""

    def __init__(self, path):
        self.path = path
        self.base = os.path.dirname(os.path.abspath(path)) if path else None
        data = {}
        if path and os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
        self.answers = data.get('answers', {})
        self.spent = data.get('spent', [])
        self.judged = data.get('judged', [])
        # {job id: the judge's answer about its group}, as it gave it.
        self.verdicts = data.get('verdicts', {})
        # {job id: [discussion]}, the one going on last.
        self.discussions = {job: talks if isinstance(talks, list) else [talks]
                            for job, talks in data.get('discussions', {}).items()}
        for item in self._items(self.answers, self.discussions):
            item['workdir'] = self._used(item.get('workdir'))
        # How many of the costs were before this review began.
        self.before = len(self.spent)

    @staticmethod
    def _items(*kept):
        return [item for jobs in kept for items in jobs.values() for item in items]

    def _used(self, workdir):
        """A workdir as kept, as a path, if it is in the review's directory. One
        elsewhere, as in a review copied from another directory, is not used: an
        agent's session continued there would change the review copied."""
        if not (workdir and self.base):
            return workdir
        path = os.path.normpath(os.path.join(self.base, workdir))
        return path if path.startswith(self.base + os.sep) else None

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        answers, discussions = json.loads(json.dumps([self.answers, self.discussions]))
        for item in self._items(answers, discussions):
            if item.get('workdir'):
                item['workdir'] = os.path.relpath(item['workdir'], self.base)
        with open(self.path + '.new', 'w') as f:
            json.dump({'answers': answers, 'spent': self.spent, 'judged': self.judged,
                       'verdicts': self.verdicts, 'discussions': discussions}, f, indent=1)
        os.replace(self.path + '.new', self.path)

    def keep(self, rs, kind, agent, cost, lines, steps=(), session=None, workdir=None,
             paid=True, unit=None, summary=None, account=None, response=None):
        """Keep an answer about the jobs of rs, with its summary, and, if paid, what
        it cost, in unit; and account, the analyst's account of the failure, as
        keep_account gave it, if it gave one; and response, what a doubt came to, for
        the person who doubted (see Review.response_lines)."""
        answer = {'kind': kind, 'agent': agent, 'cost': cost or 0, 'lines': list(lines),
                  'steps': list(steps), 'session': session, 'workdir': workdir,
                  'summary': summary,
                  'when': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                  **({'account': account} if account else {}),
                  **({'response': response} if response else {})}
        for r in rs:
            self.answers.setdefault(r.job.id, []).append(answer)
        if paid:
            self.spent.append({'kind': kind, 'agent': agent, 'cost': cost or 0,
                               'unit': unit, 'jobs': [r.job.id for r in rs]})
        self.save()

    def latest(self, job_id, *kinds):
        found = [a for a in self.answers.get(job_id, []) if a['kind'] in kinds]
        if not found:
            return None
        answer = expand(found[-1])
        # Its pointer may name another review's directory, as a copy's does.
        return dict(answer, workdir=self._used(answer.get('workdir')))


class Review(cmd.Cmd):
    """session is what cli.start gives: the run, its jobs, triage and results.
    learning() gives what learn_from_run needs besides the results and the
    analyst; retriage(results) triages the jobs again with the records and the
    tree as they are now, with no AI; agents() makes the judge and the analyst again,
    as the settings now say."""

    intro = ('Each group of jobs has a number: show, files and grep look at its first '
             'job, for free; judge, learn, doubt and discuss ask an agent, which costs. '
             'help lists the commands.')

    def __init__(self, session, judge=None, analyst=None, learning=None, retriage=None,
                 units=None, args=None, stdout=None, progress=None, agents=None):
        super().__init__(stdout=stdout)
        self.session, self.judge, self.analyst = session, judge, analyst
        self.learning, self.retriage, self.agents = learning, retriage, agents
        self.units, self.args = units or {}, args
        # The day each tracker issue the run saw was resolved, if after the run began.
        self._resolved = {}
        self.state = State(os.path.join(session.out, STATE) if session.out else None)
        # Where a command done on another thread, while the screen is in use, says
        # what it says.
        self._local = threading.local()
        # Held while lessons and judges at once keep what they found.
        self._keeping = threading.Lock()
        # While judge all asks about several groups at once: {job id: what was said
        # about its group so far}, for each group the judge works on now.
        self.at_work = None
        # {judge or analyst: the turns of the AI}, as watch's runs share them, or None
        # for no limit but the review's own; and the first jobs of the groups that wait
        # for a turn, or for the groups before them, to be asked about.
        self.turns, self.waiting_for_ai = None, set()
        # {PR number: [how it is known]} of the PRs a watch of PRs found the run tests.
        self.followed = {}
        # {job id: what the judge has written so far, as it is} for each group it is
        # asked about now: what a screen shows while it works.
        self.writing = {}
        # Whether the report is written as the results are now.
        self.saved = False
        # A run still being triaged, as on a screen that opens at once, is restored
        # once it is.
        if getattr(session, 'triaged', True):
            self.restore(progress)

    # The state of the review.

    @property
    def results(self):
        return self.session.results

    def restore(self, progress=None):
        """Give the groups judged before the judge's verdicts, from those kept by
        their evidence: without asking the judge again; and the groups an analyst
        found the cause of their accounts of it. progress, if given, hears how far it
        has got."""
        self.restore_accounts()
        if not (self.judge and self.state.judged):
            return
        triage = self.session.triage
        triage.judge, triage.kept_only = self.judge, True
        # The answers as they were kept: taken as they are, with no evidence
        # gathered again.
        triage.answers = dict(self.kept_answers(set(self.state.judged)),
                              **self.state.verdicts)
        try:
            for n, job_id in enumerate(self.state.judged, 1):
                if progress:
                    progress(f'Reading the judge\'s verdicts from the cache: {n} of '
                             f'{len(self.state.judged)}')
                for _, rs in report.groups(self.results):
                    if any(r.job.id == job_id for r in rs) and not (rs[0].agent or
                                                                    rs[0].opinion):
                        if rs[0].sure:
                            triage.check_group(rs, self.session.files[rs[0].job.id])
                        else:
                            triage.judge_group(rs, self.session.files[rs[0].job.id])
        finally:
            triage.judge, triage.kept_only, triage.answers = None, False, {}

    def restore_accounts(self):
        """Give each job an analyst found the cause of the failure of its account of
        it, as the review kept it, or, for a lesson kept before the review kept
        accounts, as the lesson's directory has it: without asking the analyst
        again."""
        for r in self.results:
            latest = self.state.latest(r.job.id, 'lesson', 'doubt', 'proposal')
            if r.analysis or not latest:
                continue
            account = latest.get('account') or self.recovered(latest)
            if not account:
                continue
            r.analysis = account
            if latest.get('summary') and FOUND + latest['summary'] not in r.notes:
                r.notes.append(FOUND + latest['summary'])
            # The PR under test it found caused it, once its change is kept.
            pr = account.get('pr')
            if pr and self.standing(account) is None and pr not in (b[0] for b in r.blamed):
                r.blamed.append((pr, f'the analyst ({account.get("analyst")})',
                                 account.get('summary', '')))

    @staticmethod
    def recovered(latest):
        """The analyst's account in the directory of a lesson or a doubt kept as
        latest, if its answer passed the checks, or its change waited for approval
        (and so passed them); None if not."""
        workdir = latest.get('workdir')
        if not (workdir and os.path.isdir(workdir)):
            return None
        said = '\n'.join(latest.get('lines') or [])
        try:
            with open(os.path.join(workdir, LESSON)) as f:
                outcome = json.load(f).get('outcome')
        except (OSError, ValueError):
            outcome = None
        held = re.search(r'the change waits for approval.*?--proposal (\S+\.json)', said)
        if not (outcome in ('learned', 'nothing') or ', settled' in said or held):
            return None
        try:
            analyst = make_agent(latest['agent'])
        except (ValueError, KeyError):
            analyst = None
        answer = answer_of(workdir, analyst)
        if not pack.told(answer):
            return None
        # Where each line it quotes is in the logs, as its evidence says.
        pack.locate(workdir, answer)
        settled = outcome in ('learned', 'nothing') or ', settled' in said
        return dict(answer, analyst=latest['agent'], job=os.path.basename(workdir),
                    **({'held': held.group(1)} if held and not settled else {}))

    def standing(self, account):
        """What became of the change of the analyst whose account this is, if it is
        not kept yet, in a line; None if it is kept, or it changed nothing."""
        held = account.get('held')
        if not held:
            return None
        if os.path.exists(held):
            return 'Its change to the records or the tree waits for your approval: A asks.'
        cache = getattr(self.args, 'cache', None)
        if cache and os.path.exists(os.path.join(approve.rejected(cache),
                                                 os.path.basename(held))):
            return 'Its change to the records or the tree was rejected; this is what it found.'
        return None

    def kept_answers(self, jobs):
        """{job id: the judge's answer} of jobs, as the run's results.json kept them
        when the review last wrote it."""
        path = os.path.join(self.session.out or '', 'results.json')
        try:
            with open(path) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            return {}
        return {e['job']: e.get('judge') or e.get('check') for e in entries
                if e.get('job') in jobs and (e.get('judge') or e.get('check'))}

    def spent(self):
        """What the agents have cost: in all, and in this review."""
        totals, counts, now = {}, {}, {}
        for n, entry in enumerate(self.state.spent):
            if not entry['cost'] and entry['kind'] in ('lesson', 'track'):
                continue
            unit = entry.get('unit') or self.units.get(entry['agent'], 'of its own unit')
            key = (entry['agent'], unit)
            totals[key] = totals.get(key, 0) + entry['cost']
            counts.setdefault(key, {}).setdefault(entry['kind'], 0)
            counts[key][entry['kind']] += 1
            if n >= self.state.before:
                now[unit] = now.get(unit, 0) + entry['cost']
        if not totals:
            return 'Cost: nothing; no judge or analyst was asked.'
        parts = []
        for (agent, unit), total in totals.items():
            what = ', '.join(f'{n} {kind}{"" if n == 1 else "s"}'
                             for kind, n in counts[(agent, unit)].items())
            parts.append(f'{readable(agent)} {total:.2f} {unit} ({what})')
        in_all = {}
        for (agent, unit), total in totals.items():
            in_all[unit] = in_all.get(unit, 0) + total
        return ('Cost: ' + '; '.join(parts) + '. In all: ' +
                ', '.join(f'{total:.2f} {unit}' for unit, total in in_all.items()) +
                ('; in this review: ' + ', '.join(f'{c:.2f} {u}' for u, c in now.items())
                 if now else '') + '.')

    def total(self):
        """What the agents have cost in all, in a few words, for the prompt."""
        found = re.search(r'In all: (.*?)(?:; in this review|\.$)', self.spent())
        return found.group(1) if found else 'no AI yet'

    @property
    def prompt(self):
        tag = self.session.run.split('-distro-')[0][-40:]
        return f'{tag} [{self.total()}]> '

    def say(self, *lines):
        out = getattr(self._local, 'stdout', None) or self.stdout
        for line in lines:
            print(line, file=out)

    def in_background(self, line, out):
        """Do a command on this thread, saying what it says to out: for one done
        while the screen is in use."""
        self._local.stdout = out
        try:
            return self.onecmd(line)
        finally:
            self._local.stdout = None

    def onecmd(self, line):
        try:
            done = super().onecmd(line)
            # The report, the drafts and results.json, as each change is done.
            if not self.saved and getattr(self.session, 'triaged', True) and \
                    line.split(' ', 1)[0] in CHANGES:
                self.write()
            return done
        except Stopped:
            self.say('Stopped: nothing it was doing is kept, and what it had cost by then is '
                     'spent.')
            return False
        except (ArchiveError, JudgeError, ValueError, OSError, re.error) as e:
            self.say(f'error: {e}')
            return False

    def emptyline(self):
        return False

    def default(self, line):
        self.say(f'No command "{line.split()[0]}": help lists them.')

    def group(self, arg):
        """The results a command's first argument names: a group's number, or a job
        id; and the rest of the arguments."""
        words = arg.split(None, 1)
        if not words:
            raise ValueError('give a group\'s number, or a job id')
        which, rest = words[0], words[1] if len(words) > 1 else ''
        found = report.groups(self.results)
        if which.isdigit() and 1 <= int(which) <= len(found):
            return found[int(which) - 1][1], rest
        jobs = [r for r in self.results if r.job.id == which]
        if not jobs:
            raise ValueError(f'no group {which}, and no job {which}: summary lists them')
        return jobs, rest

    def group_of(self, r):
        """The results of the group r is in: [r] if it is in none."""
        return next((rs for _, rs in report.groups(self.results) if r in rs), [r])

    def listening(self, say=None):
        """([lines], a function that keeps a line in them and says it)."""
        lines = []

        def listen(line):
            lines.append(line)
            (say or self.say)(line)
        return lines, listen

    def again(self):
        """Triage the jobs again with what the records and the tree are now."""
        self.session.triage.problems = db.load(self.args.problems)
        learned.load(self.args.nodes)
        self.session.results = self.retriage(self.results)
        self.saved = False
        self.restore()

    def asked(self, rs, kind, word):
        """Say the answer kept from asking about rs before, if there is one: so
        that it is not paid for again."""
        before = self.state.latest(rs[0].job.id, kind)
        if not before:
            return False
        self.say(f'Asked before, at {before["when"]}, for {before["cost"]:.2f}: its answer '
                 f'follows. "{word} {rs[0].job.id} again" asks again.', '', *before['lines'])
        return True

    # Free: what the tree and the records found, and what the agents said.

    def do_summary(self, arg):
        """summary: the groups of jobs, numbered, and what the agents have cost."""
        lines = report.summary(self.session.run, self.results, self.session.header,
                               numbered=True)
        self.say(*lines[:-1], self.spent())
        if self.session.known:
            self.say('', *report.compared(self.results, self.session.known,
                                          'Against the known answers'))

    do_s = do_summary

    def answers(self, job_id):
        """The lines for what the agents said about a job, newest first, and the
        discussion of it."""
        lines = []
        for talk in reversed(self.state.discussions.get(job_id, [])):
            if not talk['turns']:
                continue
            lines += [f'The discussion with {readable(talk["agent"])}:', '']
            for turn in talk['turns']:
                lines += [f'{turn["who"]}:', *[f'  {line}' for line in turn['text'].split('\n')],
                          '']
        for answer in reversed(self.state.answers.get(job_id, [])):
            if answer['kind'] == 'discussion':
                continue
            answer = expand(answer)
            lines += [f'From {KINDS[answer["kind"]]} ({readable(answer["agent"])}), '
                      f'{answer["when"]}, '
                      f'for {answer["cost"]:.2f}:', '', *answer['lines']]
            if answer['steps']:
                lines += ['', 'How it got there:', *answer['steps']]
            lines.append('')
        return lines

    def do_show(self, arg):
        """show N: group N's first job: first what it failed on, in a line, and why, as
        the analyst or the judge told it, step by step; then where its logs are, what
        the agents said, and what the tree and the records decided, with the lines
        they read. N can also be a job id."""
        rs, _ = self.group(arg)
        r = rs[0]
        self.say(report.headline(r), '')
        told = next((a for a in (r.analysis, r.agent, r.opinion)
                     if pack.told(a) or (a or {}).get('summary')), None)
        if told:
            standing = self.standing(told) if told is r.analysis else None
            self.say(f'Why, as the analyst ({readable(told.get("analyst"))}) tells it:'
                     if told is r.analysis else
                     f'Why, as the judge ({readable(told.get("judge"))}) tells it:',
                     *([standing] if standing else []), *report.told_text(told), '')
        else:
            self.say('Run judge (j) to see summary: why it is this, step by step.', '')
        start = report.start(r).strip(' ()')
        self.say('─' * 40, '',
                 f'Jobs: {", ".join(o.job.id for o in rs)}',
                 f'Description: {r.job.description}',
                 f'Failure: {r.job.reason or r.job.status}',
                 f'Archive: {report.QA_PROXY}/{self.session.run}/{r.job.id}/',
                 *([f'Start reading at {start}'] if start else []), '')
        said = self.answers(r.job.id)
        if said:
            self.say('What the agents said, newest first:', *said, '─' * 40, '')
        if r.finding:
            self.say('What the tree found:', *r.finding.lines(200), '')
        # What the records decided, with the lines their checks found: after the
        # tree's answers, which the explanation has given.
        decided = report.decisions(r)
        first = next((i for i, line in enumerate(decided)
                      if line.startswith(('  record', '  no record', '  =>'))), len(decided))
        self.say(*decided[first:])
        self.say(*[f'Note: {n}' for n in r.notes])

    def do_files(self, arg):
        """files N [glob]: the files in the archive of group N's first job, such as
        files 3 remote/*/log/*. Without a glob, the top and the daemons' logs."""
        rs, pattern = self.group(arg)
        files = self.session.files[rs[0].job.id]
        for glob in [pattern] if pattern else ['*', 'remote/*/log/*']:
            self.say(*[f'{rel}  {size}' for rel, size in files.glob(glob)])

    def do_grep(self, arg):
        """grep N REGEX [glob]: the lines of the files of group N's first job that
        REGEX finds (teuthology.log, or the files glob matches), up to 20 a file."""
        rs, rest = self.group(arg)
        words = shlex.split(rest)
        if not words:
            raise ValueError('give a regex to look for')
        regex, glob = re.compile(words[0]), words[1] if len(words) > 1 else 'teuthology.log'
        hits = self.session.files[rs[0].job.id].search(glob, regex, MAX_HITS)
        self.say(*[f'{h.file}:{h.line}: {h.text[:300]}' for h in hits] or ['nothing found'])

    def opening(self, arg):
        """The command that opens group N's first job's FILE at LINE in the editor, from
        "N [FILE:LINE]": by default, where its result starts."""
        rs, rest = self.group(arg)
        r = rs[0]
        words = shlex.split(rest)
        where = words[0] if words else report.start(r).strip(' ()')
        if not where:
            raise ValueError('give the file and line, such as teuthology.log:1234')
        found = re.fullmatch(r'(.+):(\d+)', where)
        rel, line = (found.group(1), int(found.group(2))) if found else (where, 1)
        files = self.session.files[r.job.id]
        if not files.glob(rel):
            raise ValueError(f'{rel} is not in the archive of job {r.job.id}: files '
                             f'{arg.split()[0]} lists them')
        settings = getattr(self.args, 'settings', None) or {}
        return editor.argv(files, r.job, rel, line, settings.get('editor'),
                           settings.get('edit_on') == 'here',
                           getattr(self.args, 'cache', None) or self.session.out)

    def do_open(self, arg):
        """open N [FILE:LINE]: open group N's first job's FILE at LINE in your editor,
        as the details cite them, such as open 3 teuthology.log:1234; by default
        where its result starts. The editor setting says which, and where."""
        subprocess.run(self.opening(arg))

    def do_comment(self, arg):
        """comment: the draft comment for the run's QA tracker."""
        self.say(report.qa_comment(self.session.run, self.results))

    def do_cost(self, arg):
        """cost: what the judge and the analyst have cost, in all and in this
        review."""
        self.say(self.spent())

    def found(self, rs):
        """What is known of the failure of a group's jobs: what the analyst said of
        it last, or else what the tree found; and the lines that show it."""
        r = rs[0]
        latest = self.state.latest(r.job.id, 'lesson', 'doubt', 'discussion')
        summary = (latest or {}).get('summary') or report.what(r)
        if summary.startswith('the analyst found: '):
            summary = summary[len('the analyst found: '):]
        elif r.finding and summary.startswith(f'{r.finding.where}: '):
            summary = summary[len(r.finding.where) + 2:]
        evidence = [f'{h.file}:{h.line}: {h.text}' for step in (r.finding.steps if r.finding
                                                                 else [])
                    for h in step.evidence]
        return summary, latest, evidence

    def issue_text(self, rs, seen=False):
        """(subject, description) of a tracker issue for a group's failure, the
        description in the tracker's Textile; with seen, the description is a
        comment for the issue it is, that it was seen again. Anyone can read the
        tracker: it names no agent, and has no costs or local paths."""
        summary, latest, evidence = self.found(rs)
        r, run = rs[0], self.session.run
        found = [line for line in (latest or {}).get('lines', []) if line.strip() and not (
            line.startswith(('---', '+++', '@@', '+', '-', 'ok ', 'FAIL', 'Checks:',
                             'Asking ', 'Settled ', 'Wrote ', f'{r.job.id}: ')) or
            '(cost ' in line or 'analysis.md' in line)]
        told = next((a for a in (r.analysis, r.agent, r.opinion) if pack.told(a)), None)
        judged = '\n'.join(report.told_text(told)) if told else None
        lines = [f'Seen {"again " if seen else ""}in {len(rs)} '
                 f'job{"" if len(rs) == 1 else "s"} of {run}:',
                 *[f'* {report.PULPITO}/{run}/{o.job.id}/' for o in rs], '',
                 f'Failure: {r.job.reason or r.job.status}', '']
        if found or judged:
            # The account of the failure, if there is one: the lines a lesson said
            # before its answer are its work, not what it found.
            lines += ['What the analysis found:', '', *([judged] if judged else found), '']
        if r.finding:
            lines += ['What qa_triage\'s checks found:', '', '<pre>', *r.finding.lines(300),
                      '</pre>', '']
        if evidence:
            lines += ['The lines that show it:', '', '<pre>', *evidence, '</pre>', '']
        return self.public(rs, short_line(summary)), self.public(rs, '\n'.join(lines))

    def public(self, rs, text):
        """text, for anyone to read: naming no agent that said it."""
        r, latest = rs[0], self.state.latest(rs[0].job.id, 'lesson', 'doubt', 'discussion')
        names = {n for n in ((latest or {}).get('agent'), (r.agent or {}).get('judge'),
                             (r.opinion or {}).get('judge'), (r.analysis or {}).get('analyst'),
                             self.analyst and self.analyst.name,
                             self.judge and self.judge.name) if n}
        for name in sorted(names, key=len, reverse=True):
            text = text.replace(name, 'the analysis')
        return text

    # Updating the records outside qa_triage: the PRs and the tracker.

    def ways(self, rs):
        """The ways to update the records outside qa_triage about the failure of rs's
        jobs, the recommended one first: [{way, number, says, recommended, posted}],
        way pr (a comment on a PR under test), tracker (a comment on a tracker issue,
        that it was seen again) or issue (a new tracker issue); number the PR's or the
        issue's, or None for one the person names; posted, when it was posted, if it
        was. A PR the analyst found caused it is told first; then the tracker issue it
        is, and for a new failure, a new issue."""
        r, found = rs[0], []
        pr = self.blamed_pr(r)
        if pr:
            found.append({'way': 'pr', 'number': pr, 'says': f'Comment on PR #{pr}, which '
                          'the analyst found caused it: explain it to its developer'})
        if r.problem and db.is_pr(r.problem.id):
            found.append({'way': 'pr', 'number': r.problem.id[2:],
                          'says': f'Comment on PR #{r.problem.id[2:]}, whose record placed it: '
                                  'that it was seen again, and why'})
        issue = self.tracker_issue(r)
        if issue:
            # As people mostly write it: the jobs, as /a/ paths; or with what was found.
            found.append({'way': 'jobs', 'number': issue,
                          'says': f'Comment on tracker issue {issue}, which it is: only its '
                                  'jobs, as /a/ paths'})
            found.append({'way': 'tracker', 'number': issue,
                          'says': f'Comment on tracker issue {issue}, which it is: its jobs, '
                                  'and what was found (the analysis, the checks and the lines)'})
        if not r.problem:
            found.append({'way': 'issue', 'number': None,
                          'says': 'File a new tracker issue for it, in '
                                  f'{self.tracker_project()}'})
        if not any(w['way'] == 'pr' for w in found) and self.under_test():
            found.append({'way': 'pr', 'number': None,
                          'says': 'Comment on a PR under test, which you choose'})
        if not issue:
            found.append({'way': 'tracker', 'number': None,
                          'says': 'Comment on a tracker issue, which you name'})
        posted = {a.get('summary'): a['when'] for a in self.state.answers.get(r.job.id, [])
                  if a['kind'] in ('posted', 'filed')}
        for w in found:
            # Either comment on a tracker issue is the comment on it.
            w['posted'] = posted.get(f'{"tracker" if w["way"] == "jobs" else w["way"]} '
                                     f'{w["number"]}') or \
                (posted.get('issue None') if w['way'] == 'issue' else None)
        # The first not posted already, of those for a PR or an issue it names.
        first = next((w for w in found if w['number'] or w['way'] == 'issue'), None)
        for w in found:
            w['recommended'] = w is first and not w['posted']
        return found

    def blamed_pr(self, r):
        """The number of the PR under test an analyst found caused r's failure, or None."""
        for pr, _, _ in r.blamed:
            number = re.search(r'(\d+)', str(pr))
            if number:
                return number.group(1)
        pr = (r.analysis or {}).get('pr')
        number = re.search(r'(\d+)', str(pr)) if pr and not self.standing(r.analysis) else None
        return number.group(1) if number else None

    def tracker_issue(self, r):
        """The number of the tracker issue r's failure is, as its record or an analyst
        says; None if none is known."""
        if r.problem and str(r.problem.id).isdigit():
            return r.problem.id
        said = str((r.analysis or r.agent or {}).get('problem') or '')
        return said if said.isdigit() else None

    def draft(self, rs, way, number=None):
        """(subject, text) of way's update about rs's failure (see ways), to post:
        subject only for a new issue."""
        if way == 'issue':
            return self.issue_text(rs)
        if way == 'tracker':
            return None, self.issue_text(rs, seen=True)[1]
        if way == 'jobs':
            return None, self.jobs_text(rs)
        if way == 'pr':
            return None, self.pr_text(rs, number)
        raise ValueError(f'the ways are {", ".join(WAYS)}')

    def pr_text(self, rs, number):
        """A comment for PR number about rs's failure, for its developer, in GitHub's
        Markdown: what failed in the run, why, and how to check it, with the lines of
        the logs that show it."""
        r, run = rs[0], self.session.run
        caused = number and number == self.blamed_pr(r)
        jobs = ', '.join(f'[{o.job.id}]({report.PULPITO}/{run}/{o.job.id}/)' for o in rs[:5])
        lines = [(f'A QA run that tests this PR failed, and the failure looks to be caused by '
                  'it' if caused else 'A QA run that tests this PR failed in a way that may '
                  'concern it') + f': [{run}]({report.PULPITO}/{run}/), '
                 f'{len(rs)} job{"" if len(rs) == 1 else "s"} ({jobs}'
                 f'{", ..." if len(rs) > 5 else ""}).', '',
                 f'**The failure:** `{r.job.reason or r.job.status}`', '']
        told = next((a for a in (r.analysis, r.agent, r.opinion) if pack.told(a)), None)
        if told:
            lines += ['**Why:**', '', str(told.get('explanation') or told.get('summary') or
                                          '').strip(), '']
            steps = pack.steps(told)
            if steps:
                lines += ['**How to check it:**', '']
            for n, step in enumerate(steps, 1):
                title = str(step.get('title') or '').strip().rstrip('.')
                lines.append(f'{n}. ' + (f'**{title}.** ' if title else '') + ' '.join(
                    t for t in (str(step.get('do') or '').strip(),
                                str(step.get('finds') or '').strip()) if t))
                quoted = []
                for q in step.get('evidence') or []:
                    if isinstance(q, dict):
                        rows = q.get('lines') or []
                        where = q.get('at') or (f'{rows[0][0]}:{rows[0][1]}' if rows
                                                else q.get('file'))
                        quoted.append(f'{where}: {short_line(rows[0][2] if rows else str(q.get("text", "")), 300)}')
                if quoted:
                    lines += ['   ```', *[f'   {q}' for q in quoted], '   ```']
            lines.append('')
        else:
            summary, _, _ = self.found(rs)
            lines += [f'**What was found:** {summary}', '']
        lines += [f'The job\'s logs: {QA_PROXY}/{run}/{r.job.id}/', '',
                  '_Found by qa_triage\'s automated triage of the run: check the lines it '
                  'quotes in the logs._']
        return self.public(rs, '\n'.join(lines))

    def post(self, rs, way, number=None, text=None, subject=None, kind='product'):
        """Post way's update about rs's failure (see ways), where anyone can read it:
        its draft, or text (and subject, for a new issue) if given. A line that says
        where it went."""
        if way == 'issue':
            return self.file(rs, kind, subject, text)
        number = str(number or '').lstrip('#')
        if not number.isdigit():
            raise ValueError(f'give the number of the {"PR" if way == "pr" else "tracker issue"}')
        text = text or self.draft(rs, way, number)[1]
        if way == 'pr':
            try:
                url = self.comment_pr(number, text)
            except (urllib.error.URLError, OSError, subprocess.SubprocessError) as e:
                raise ValueError(f'GitHub did not take it: {e}') from e
            line = f'Commented on PR #{number}: {url}'
        elif way in ('tracker', 'jobs'):
            key = self.tracker_key()
            if not key:
                raise ValueError('no API key for the tracker: set REDMINE_API_KEY, or name a '
                                 'file with it in the tracker_key setting, or put it in '
                                 '~/.redmine_key')
            try:
                url = self.comment_issue(key, number, text)
            except (urllib.error.URLError, OSError) as e:
                raise ValueError(f'the tracker did not take it: {e}') from e
            line = f'Commented on tracker issue {number}: {url}'
        else:
            raise ValueError(f'the ways are {", ".join(WAYS)}')
        self.say(line)
        self.state.keep(rs, 'posted', 'a reviewer', 0, [line], paid=False,
                        summary=f'{"tracker" if way == "jobs" else way} {number}')
        return line

    def do_update(self, arg):
        """update N: the ways to update the records outside qa_triage about group N's
        failure, the recommended one first: a comment on the PR under test that caused
        it, explaining it to its developer; a comment on the tracker issue it is, that
        it was seen again, and why; or a new tracker issue. draft N WAY [NUMBER] shows
        each, and post N WAY [NUMBER] posts it. Free."""
        rs, _ = self.group(arg)
        self.say(f'The ways to update the records about group {arg.split()[0]}, the '
                 'recommended one first:')
        for w in self.ways(rs):
            self.say(f'  {w["way"]}{" " + w["number"] if w["number"] else ""}: {w["says"]}' +
                     (' (recommended)' if w['recommended'] else '') +
                     (f' (posted {w["posted"]})' if w['posted'] else ''))

    def do_draft(self, arg):
        """draft N pr|tracker|issue [NUMBER]: what that update about group N's failure
        says (see update), as it would be posted: to the PR or the tracker issue
        NUMBER, by default the one update names. Free."""
        rs, way, number = self._way(arg)
        subject, text = self.draft(rs, way, number)
        self.say(*([f'Subject: {subject}', ''] if subject else []), *text.split('\n'))

    def do_post(self, arg):
        """post N pr|tracker|issue [NUMBER] [product|test|infra]: post that update
        about group N's failure (see update and draft), where anyone can read it: a
        comment on the PR on GitHub (with gh, signed in, or a GitHub token), or on the
        tracker issue, or a new issue, as file does (with the tracker's API key)."""
        rs, way, number = self._way(arg)
        kind = next((w for w in arg.split()[2:] if w in ('product', 'test', 'infra')),
                    'product')
        self.post(rs, way, number, kind=kind)

    def _way(self, arg):
        """(results, way, number) that "N WAY [NUMBER]" names: by default the number
        update names for the way."""
        rs, rest = self.group(arg)
        words = rest.split()
        if not words or words[0] not in WAYS:
            raise ValueError(f'give the way: {", ".join(WAYS)} (update lists them)')
        way = words[0]
        number = next((w.lstrip('#') for w in words[1:] if w.lstrip('#').isdigit()), None) or \
            next((w['number'] for w in self.ways(rs) if w['way'] == way and w['number']), None)
        if way != 'issue' and not number:
            raise ValueError(f'give the number of the {"PR" if way == "pr" else "tracker issue"}')
        return rs, way, number

    # The whole run's updates at once: every known issue it saw again, and the PRs.

    def sightings(self, news=False):
        """[(tracker issue, [the groups' numbers], their results, the comment, when it
        was posted or None)] of the tracker issues the run's failures are, apart from
        those a PR under test was found to cause: a comment for each, that the run saw
        it again, with its jobs as /a/ paths, as people write them. With news, those
        resolved after the run began are left out too: it saw them before their fix,
        which tells their people nothing (see resolved_after_run)."""
        issues = {}
        for n, (_, rs) in enumerate(report.groups(self.results), 1):
            r = rs[0]
            issue = self.tracker_issue(r)
            if issue and not self.blamed_pr(r) and not (news and
                                                        self.resolved_after_run(issue)):
                issues.setdefault(issue, []).append((n, rs))
        found = []
        for issue, groups in issues.items():
            text = self.jobs_text([r for _, rs in groups for r in rs])
            posted = next((a['when'] for _, rs in groups
                           for a in self.state.answers.get(rs[0].job.id, [])
                           if a['kind'] == 'posted' and a.get('summary') == f'tracker {issue}'),
                          None)
            found.append((issue, [n for n, _ in groups], [rs for _, rs in groups], text,
                          posted))
        return found

    def resolved_after_run(self, issue):
        """The day tracker issue was resolved (closed), if that was after the run
        began, so that the run saw it before its fix; None if it is open, was closed
        before the run, or the tracker cannot say. Asked once a review."""
        if issue not in self._resolved:
            began = re.search(r'-(\d{4}-\d{2}-\d{2})_(\d{2}:\d{2}:\d{2})-', self.session.run)
            day = None
            try:
                got = self.read_issue(issue) if began else None
                closed = got and (got.get('status') or {}).get('is_closed') and \
                    got.get('closed_on')
                if closed and closed[:10] + 'T' + closed[11:19] > \
                        began.group(1) + 'T' + began.group(2):
                    day = closed[:10]
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
                pass
            self._resolved[issue] = day
        return self._resolved[issue]

    def jobs_text(self, results):
        """A comment for the tracker issue results' failure is, that the run saw it
        again: only its jobs, as /a/ paths, as people write them, and if it was seen
        again after its fix, that."""
        lines = [f'/a/{self.session.run}/{r.job.id}' for r in results]
        after = next((r.notes[-1] for r in results
                      if r.verdict == 'seen-after-fix' and r.notes), None)
        return '\n'.join(lines + (['', f'Seen again after its fix: {after}'] if after else []))

    def do_sightings(self, arg):
        """sightings [post]: the comment for each tracker issue the run's failures are,
        apart from those a PR under test caused: that the run saw it again, with its
        jobs, as /a/ paths. With post, each not posted yet is posted, where anyone can
        read it, with the tracker's API key. Free."""
        found = self.sightings()
        if not found:
            self.say('No group is placed in a tracker issue, apart from any a PR under test '
                     'caused.')
            return
        if arg.strip() != 'post':
            for issue, numbers, _, text, posted in found:
                before = self.resolved_after_run(issue)
                self.say(f'Tracker issue {issue}, group{"" if len(numbers) == 1 else "s"} '
                         f'{", ".join(map(str, numbers))}' +
                         (f': posted {posted}' if posted else
                          f': left out, resolved on {before}, after the run began, so the '
                          'run saw it before its fix' if before else ':'), *(
                             f'    {line}' for line in text.split('\n')), '')
            self.say('sightings post posts those not posted yet.')
            return
        found = self.sightings(news=True)
        key = self.tracker_key()
        if not key:
            raise ValueError('no API key for the tracker: the setup page (o) takes one, and '
                             'says how to make it')
        done, failed = 0, []
        for issue, _, groups, text, posted in found:
            if posted:
                continue
            try:
                url = self.comment_issue(key, issue, text)
            except (urllib.error.URLError, OSError, ValueError) as e:
                failed.append(f'Tracker issue {issue}: the tracker did not take it: {e}')
                continue
            line = f'Commented on tracker issue {issue}: {url}'
            self.say(line)
            for rs in groups:
                self.state.keep(rs, 'posted', 'a reviewer', 0, [line], paid=False,
                                summary=f'tracker {issue}')
            done += 1
        self.say(*failed, f'Posted {done} of the {sum(1 for f in found if not f[4])} '
                          'comments for the tracker issues the run saw again.')

    def approval(self):
        """(the comment, [(PR, why it is approved or not, True if it is)]) for the PRs
        the run tests, "Rados approved", with a link to the run's section of the RADOS
        wiki, as people write it: those merged into the build it ran, if the ceph clone
        shows them, else those its QA tracker lists, apart from any an analysis found
        caused a failure."""
        text = f'Rados approved: {self.wiki_link()}'
        prs = self.under_test()
        merged = {n for n, how in prs.items() if any(h.startswith('merged into') for h in how)}
        caused = {}
        for n, (_, rs) in enumerate(report.groups(self.results), 1):
            pr = self.blamed_pr(rs[0])
            if pr:
                caused.setdefault(pr, []).append(n)
        suspected, rows = self.suspected(), []
        for number, how in prs.items():
            if merged and number not in merged:
                rows.append((number, 'its QA tracker lists it, but it is not merged into the '
                                     'build the run tested: left out', False))
            elif number in caused:
                groups = caused[number]
                rows.append((number, f'an analysis found it caused group'
                                     f'{"" if len(groups) == 1 else "s"} '
                                     f'{", ".join(map(str, groups))}: left out', False))
            else:
                ran = [g for g in suspected.get(number, []) if g not in caused.get(number, [])]
                rows.append((number, '; '.join(how) + (
                    f'; the jobs of group{"" if len(ran) == 1 else "s"} '
                    f'{", ".join(map(str, ran))} ran a file it changes' if ran else ''), True))
        return text, rows

    def wiki_page_name(self):
        """The RADOS wiki's page for the run's base branch: MAIN, TENTACLE, ..."""
        found = baseline.parts(self.session.run)
        return baseline.base_of(found[2] if found else 'main').upper()

    def wiki_link(self):
        """The link to the run's section of the RADOS wiki, as people write it: the
        section is headed by the run's QA tracker."""
        qa = self.qa_tracker()
        if not qa:
            raise ValueError('no QA tracker is known for this run: the run\'s section of '
                             'the RADOS wiki is headed by it')
        return (f'{TRACKER}/projects/rados/wiki/{self.wiki_page_name()}'
                f'#httpstrackercephcomissues{qa}')

    def posted_for_run(self):
        """{what: (where it was posted, when)} of the run's own updates, posted once
        each: the approval on a PR ("pr N"), and its QA tracker's approval ("qa")."""
        try:
            with open(os.path.join(self.session.out, 'posted.json')) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _posted(self, what, where):
        done = self.posted_for_run()
        done[what] = (where, datetime.datetime.now().isoformat(timespec='seconds'))
        os.makedirs(self.session.out, exist_ok=True)
        with open(os.path.join(self.session.out, 'posted.json'), 'w') as f:
            json.dump(done, f, indent=1)

    def approved(self):
        """{PR: (where the approval was posted, when)} of those posted for this run."""
        return {what[3:]: done for what, done in self.posted_for_run().items()
                if what.startswith('pr ')}

    def do_approvals(self, arg):
        """approvals [post]: "Rados approved", with a link to the run's section of the
        RADOS wiki, for each PR the run tests: those merged into the build it ran,
        apart from any an analysis found caused a failure. With post, it is posted on
        each not posted yet, where anyone can read it, with gh or a GitHub token. Post
        the run's wiki section (wiki) first: the link goes to it. Free."""
        text, rows = self.approval()
        done = self.approved()
        if arg.strip() != 'post':
            self.say(f'The comment: {text}', '')
            for number, why, ok in rows:
                self.say(f'#{number}  {"approve" if ok else "leave out"}: {why}' +
                         (f' (posted {done[number][1]})' if number in done else ''))
            self.say('', 'approvals post posts it on those to approve, not posted yet. The '
                         'link goes to the run\'s wiki section: post that first (wiki).')
            return
        posted, failed = 0, []
        todo = [number for number, _, ok in rows if ok and number not in done]
        for number in todo:
            try:
                url = self.comment_pr(number, text)
            except (urllib.error.URLError, OSError, ValueError,
                    subprocess.SubprocessError) as e:
                failed.append(f'PR #{number}: GitHub did not take it: {e}')
                continue
            self._posted(f'pr {number}', url)
            self.say(f'Approved PR #{number}: {url}')
            posted += 1
        self.say(*failed, f'Posted the approval on {posted} of the {len(todo)} PRs to approve.')

    def do_issue(self, arg):
        """issue N: a draft tracker issue for group N's failure: its subject, what
        failed and why, as the analysis or the tree found it, the lines that show it,
        and the jobs. file N files it."""
        rs, _ = self.group(arg)
        subject, description = self.issue_text(rs)
        self.say(f'Subject: {subject}', '', 'Description:', '', *description.split('\n'))

    # Filing it.
    file_issue = staticmethod(file_issue)
    comment_issue = staticmethod(comment_issue)
    comment_pr = staticmethod(comment_pr)

    def tracker_key(self):
        settings = getattr(self.args, 'settings', None) or {}
        return tracker_key(settings.get('tracker_key'))

    def tracker_project(self):
        return (getattr(self.args, 'settings', None) or {}).get('tracker_project') or 'rados'

    def do_file(self, arg):
        """file N [product|test|infra]: file the draft issue for group N's failure
        (see issue) on the tracker, where anyone can read it, in the project the
        tracker_project setting names (default: rados); then keep a record for it, as
        track does. Needs the tracker's API key: $REDMINE_API_KEY, or the file the
        tracker_key setting names, or ~/.redmine_key."""
        rs, kind = self.group(arg)
        self.file(rs, kind.strip() or 'product')

    def file(self, rs, kind='product', subject=None, description=None):
        """File a new issue for the failure of rs's jobs, its draft (see issue), or
        subject and description if given; then keep a record for it, as track does.
        A line that says where it went."""
        if kind not in ('product', 'test', 'infra'):
            raise ValueError('the kind is product, test or infra')
        if rs[0].problem:
            raise ValueError(f'it is placed in {rs[0].problem.id} already')
        key = self.tracker_key()
        if not key:
            raise ValueError('no API key for the tracker: set REDMINE_API_KEY, or name a file '
                             'with it in the tracker_key setting, or put it in ~/.redmine_key')
        drafted = self.issue_text(rs)
        subject, description = subject or drafted[0], description or drafted[1]
        try:
            number = self.file_issue(key, self.tracker_project(), subject, description)
        except (urllib.error.URLError, KeyError, ValueError) as e:
            raise ValueError(f'the tracker did not take it: {e}') from e
        line = f'Filed {TRACKER}/issues/{number}: {subject}'
        self.say(line)
        self.state.keep(rs, 'filed', 'a reviewer', 0, [line], paid=False,
                        summary='issue None')
        self._track(rs, number, kind)
        return line

    def do_wiki(self, arg):
        """wiki: the run's section for the RADOS team's wiki of reviewed runs
        (https://tracker.ceph.com/projects/rados/wiki/MAIN, or the page of the run's
        base branch), in its Textile, to paste there, at the top of its runs; it is in
        drafts/wiki.textile too."""
        self.say(*report.wiki(self.session.run, self.results, self.session.header).split('\n'))

    # The tracker's issues, as the review changes them: for tests to fake.
    update_issue = staticmethod(update_issue)
    read_issue = staticmethod(read_issue)

    def needs_tracker_key(self):
        key = self.tracker_key()
        if not key:
            raise ValueError('no API key for the tracker: the setup page (o) takes one, and '
                             'says how to make it')
        return key

    def qa_approval(self):
        """(the run's QA tracker issue, its author: (id, name) or None, the comment, and
        the PRs an analysis found caused a failure): to approve the run on it, as
        people do, "Rados approved", with a link to the run's wiki section; set QA
        Approved; and assigned back to its author, who built the batch."""
        qa = self.qa_tracker()
        text = f'Rados approved: {self.wiki_link()}'
        try:
            author = (self.read_issue(qa) or {}).get('author') or {}
        except (urllib.error.URLError, OSError, ValueError):
            author = {}
        caused = sorted({self.blamed_pr(rs[0]) for _, rs in report.groups(self.results)} -
                        {None}, key=int)
        return qa, ((author['id'], author.get('name')) if author.get('id') else None), text, \
            caused

    def do_qa(self, arg):
        """qa [approve|comment]: what approving the run on its QA tracker does: a
        comment, "Rados approved", with a link to the run's section of the RADOS wiki;
        its status set to QA Approved; and assigned back to its author, who built the
        batch. With approve, that is done; with comment, the comment alone is posted.
        Paste the wiki section (wiki) in first. Needs the tracker's API key."""
        qa, author, text, caused = self.qa_approval()
        how = arg.strip()
        if how not in ('approve', 'comment'):
            self.say(f'QA tracker {TRACKER}/issues/{qa}:', f'    the comment: {text}',
                     '    its status: QA Approved',
                     f'    assigned to: {author[1] if author else "(its author is not known)"}',
                     *([f'PRs under test an analysis found caused a failure: '
                        f'{", ".join("#" + n for n in caused)}'] if caused else []),
                     'qa approve does all that; qa comment posts the comment alone.')
            return
        key = self.needs_tracker_key()
        try:
            url = self.update_issue(
                key, qa, notes=text, status_id=QA_APPROVED if how == 'approve' else None,
                assigned_to_id=author[0] if how == 'approve' and author else None)
        except (urllib.error.URLError, OSError) as e:
            raise ValueError(f'the tracker did not take it: {e}') from e
        line = (f'Approved the run on its QA tracker: {url}' if how == 'approve' else
                f'Commented on the QA tracker: {url}')
        self._posted('qa', url)
        self.say(line)

    def do_track(self, arg):
        """track N TRACKER [product|test|infra]: once a tracker issue is filed for
        group N's failure, a record for it, hung where the tree explains the failure,
        with the group's jobs as its examples. It is kept only if it passes the checks
        that every change to the records passes. No AI."""
        rs, rest = self.group(arg)
        words = rest.split()
        if not words or not words[0].lstrip('#').isdigit():
            raise ValueError('give the number of the tracker issue filed for it')
        number, kind = words[0].lstrip('#'), words[1] if len(words) > 1 else 'product'
        if kind not in ('product', 'test', 'infra'):
            raise ValueError('the kind is product, test or infra')
        self._track(rs, number, kind)

    def _track(self, rs, number, kind):
        r, context = rs[0], self.learning()
        if not (r.finding and r.finding.conclusive):
            raise ValueError('the tree does not explain it: learn or doubt it first')
        issue = context['tracker'](number)
        summary, latest, _ = self.found(rs)
        facts = {name: to_regex(str(value)) for name, value in r.finding.facts.items()
                 if name in report.TELLING and value}
        record = {'id': number, 'tracker': f'https://tracker.ceph.com/issues/{number}',
                  'title': issue.get('subject', ''), 'kind': kind, 'tree': r.finding.where}
        if facts:
            record['facts'] = facts
        record['notes'] = summary + '\n'
        record['examples'] = [r.job.path for r in rs]
        doubt = Doubt(r.job, self.session.files[r.job.id], context['problems_dir'],
                      context['labels_dir'], None, f'{context["workdir"]}/track-{r.job.id}',
                      context['files_for'], context['jobs_for'], context['tracker'],
                      expected=number, by='a reviewer', said_by_people=True, corpus=context['corpus'],
                      say=self.say, nodes_dir=context['nodes_dir'],
                      history=context['history'],
                      holdout=context.get('holdout', ()), **self.approving(asked=True))
        doubt.setup()
        text = yaml.safe_dump(record, sort_keys=False, width=100, allow_unicode=True)
        answer = {'assessment': 'wrong', 'problem': number, 'summary': summary,
                  'explanation': 'the tracker filed for it',
                  'evidence': [{'file': 'explanation.txt', 'text': r.finding.steps[-1].says}],
                  'changes': [{'id': number, 'yaml': text}]}
        ok, lines, writes = doubt.check(answer)
        self.say(text, 'Checks:', *lines)
        if not ok:
            self.say('Not kept: the record did not pass the checks.')
            return
        doubt._apply(answer, writes)
        if doubt.publish and doubt.library and doubt.write:
            # Shared at once, as a lesson's change is, under its own name: not left for
            # the next lesson to take with it.
            try:
                self.say(share.publish(doubt.library, f'Tracked {r.job.path} as {number}: '
                                       f'{record["title"][:100]}\n\nKept by '
                                       f'{getpass.getuser()}, who named the tracker issue.',
                                       self.say, own=doubt.written))
            except (OSError, subprocess.SubprocessError, RuntimeError) as e:
                self.say(f'Kept here, but not shared: {e}')
        self.state.keep(rs, 'track', 'a reviewer', 0, [f'Tracked as {number}: '
                                                         f'{record["title"]}', *lines],
                        paid=False)
        self.again()

    def do_retriage(self, arg):
        """retriage: triage the jobs again with the records and the tree as they are
        now, brought up to date with the library's remote first, such as after others
        taught it, or after editing a record by hand; and say which jobs' results
        changed. No AI."""
        before = {r.job.id: describe(r) for r in self.results}
        library = getattr(self.args, 'library', None)
        moved = bool(library) and share.sync(library, self.say)
        self.again()
        changed = [f'  {r.job.id}: {before.get(r.job.id, "?")} -> {describe(r)}'
                   for r in self.results if before.get(r.job.id) != describe(r)]
        self.say(f'Triaged the {len(self.results)} jobs again with the records and the tree '
                 'as they are now' + (', the library brought up to date first' if moved else
                                       '') + ': ' +
                 (f'{len(changed)} changed.' if changed else 'no result changed.'),
                 *changed, '')
        self.do_summary('')

    def write(self):
        """Write the report, the drafts and results.json to the output directory:
        results.json makes the run one that later changes are checked against."""
        if not getattr(self.session, 'triaged', True):
            # Not a run of some of its jobs.
            raise ValueError('the run is still being triaged: it is written once it is')
        report.write(self.session.out, self.session.run, self.results,
                     skipped=getattr(self.session, 'skipped', ()), header=self.session.header)
        self.saved = True

    # Costs: the agents.

    def unjudged(self):
        """The groups that have no verdict of the judge's: those the tree and the
        records could not decide, and those they did, for it to check. A verdict
        given before is one, if it was on the same evidence."""
        return [rs for _, rs in report.groups(self.results)
                if not (rs[0].agent or rs[0].opinion)]

    def to_learn(self):
        """[(group, why)] for the groups the analyst could learn from and has not:
        those that needed the judge, or that it disagrees about, or new ones nobody
        has looked for the cause of; the likeliest to teach something first."""
        found = []
        for r, _, why, _ in lessons(self.results, self.session.known):
            if self.state.latest(r.job.id, 'lesson'):
                continue
            rs = next((rs for _, rs in report.groups(self.results) if r in rs), [r])
            found.append((rs, why))
        return found

    def recommend(self):
        """What the analyst could learn from, in a few lines; [] for nothing."""
        todo = self.to_learn()
        if not todo:
            return []
        numbers = {id(rs[0]): n for n, (_, rs) in enumerate(report.groups(self.results), 1)}
        return [f'The analyst could learn from {len(todo)} group'
                f'{"" if len(todo) == 1 else "s"}, so that the tree and the records decide '
                'jobs like them by themselves next time: learn all asks it about each, or '
                'learn N about one; each lesson costs more than a judge.',
                *[f'  [{numbers.get(id(rs[0]), "?")}] {rs[0].job.id}: {short_line(why, 160)}'
                  for rs, why in todo]]

    def do_judge(self, arg):
        """judge N [again]: the judge's verdict on group N. Costs a judge call, the
        first time: after that, it shows the verdict it gave, unless again.
        judge all: the judge's verdict on each group that the tree and the records
        could not decide, and it was not asked about. Costs a judge call each. The
        judges_at_once setting says how many groups are judged at once."""
        if not self.judge:
            raise ValueError('no judge: give --judge, or set judge in a config file')
        if arg.strip() == 'all':
            todo = self.unjudged()
            if not todo:
                self.say('Every group has been judged.')
            at_once = getattr(self.args, 'judges_at_once', None) or 1
            at_once = min(len(todo), len(todo) if at_once == 'all' else at_once)
            if at_once > 1:
                self.say(f'{len(todo)} groups, {at_once} at once; each line of one begins '
                         'with its job.')
                self.at_work = {}
            out = getattr(self._local, 'stdout', None)
            begun = iter(range(1, len(todo) + 1))
            queued = {rs[0].job.id for rs in todo}
            self.waiting_for_ai |= queued

            def judge(rs):
                # Said where the command's own lines go.
                self._local.stdout = out
                job_id = rs[0].job.id
                with self._keeping:
                    if self.at_work is not None:
                        self.at_work[job_id] = []
                    self.say(f'Group {next(begun)} of {len(todo)}, job {job_id}:')
                try:
                    self._judge(rs, False, before=False,
                                tag=f'{job_id}: ' if at_once > 1 else '')
                finally:
                    if self.at_work is not None:
                        self.at_work.pop(job_id, None)

            try:
                each_at_once(judge, todo, at_once)
            finally:
                self.at_work = None
                self.waiting_for_ai -= queued
            self.say('', *self.recommend())
            return
        rs, rest = self.group(arg)
        self._judge(rs, rest.strip() == 'again')

    @contextlib.contextmanager
    def turn(self, kind, job_id):
        """Wait for a turn of the AI, the judge's or the analyst's, as watch's runs share
        them: the group of job_id waits for it meanwhile, and a person's stop stops the
        wait."""
        slots = (self.turns or {}).get(kind)
        agent = self.judge if kind == 'judge' else self.analyst
        if slots:
            self.waiting_for_ai.add(job_id)
            try:
                while not slots.acquire(timeout=0.5):
                    if getattr(agent, 'stopped', False):
                        raise Stopped(f'{agent.name}: stopped')
            finally:
                self.waiting_for_ai.discard(job_id)
        self.waiting_for_ai.discard(job_id)
        try:
            yield
        finally:
            if slots:
                slots.release()

    @contextlib.contextmanager
    def heard(self, agent, say=None, written=None):
        """While it works, what agent says it does, for one that says, is said, by say
        if given: as it thinks, and what it reads; and what it writes, as it is, is
        added to written, a list, if given, for one that streams it."""
        agents = getattr(agent, 'judges', [agent])
        before = [(getattr(a, 'progress', None), getattr(a, 'written', None)) for a in agents]
        for a in agents:
            a.progress, a.written = say or self.say, written
        try:
            yield
        finally:
            for a, (progress, was) in zip(agents, before):
                a.progress, a.written = progress, was

    def _judge(self, rs, again, before=True, tag=''):
        """Ask the judge about rs; if it was asked before, and not again, show what it
        said then, if before. With tag, as groups judged at once are, each line it
        says begins with it, and is kept in at_work too."""
        say = self.say
        if tag:
            kept = (self.at_work or {}).get(rs[0].job.id, [])

            def say(*lines):
                lines = [line for text in lines for line in str(text).split('\n')]
                kept.extend(lines)
                self.say(*(tag + line for line in lines))
        if before and not again and self.asked(rs, 'judge', 'judge'):
            return
        triage = self.session.triage.judging(self.judge, fresh=again)
        # The tree and the records decided it: the judge checks that, and explains.
        decided = rs[0].sure and not rs[0].agent
        writing = self.writing[rs[0].job.id] = []
        try:
            with self.turn('judge', rs[0].job.id), \
                    self.heard(self.judge, say, writing):
                if decided:
                    triage.check_group(rs, self.session.files[rs[0].job.id])
                else:
                    triage.judge_group(rs, self.session.files[rs[0].job.id])
        finally:
            self.writing.pop(rs[0].job.id, None)
            self.session.triage.spent.extend(triage.spent)
        r = rs[0]
        a = r.opinion if decided else r.agent
        cost = sum(c for _, c in triage.spent)
        ids = ','.join(o.job.id for o in rs)
        # For a person: what the judge explains first, then the verdicts.
        answer = [*report.told_text(a), ''] if a else []
        if decided:
            said = ('gave no answer' if not a else
                    {True: 'agrees', None: 'is not sure'}.get(
                        a.get('agrees'), f'disagrees: it says {a.get("verdict")} '
                                         f'{a.get("problem") or ""}'.rstrip()))
            answer.append(f'{ids}: {describe(r).split(" (")[0]}, as the tree and the records '
                          f'decided; the judge {said} (cost {cost:.2f})')
        else:
            answer.append(f'{ids}: {r.verdict}{" " + r.problem.label if r.problem else ""}, '
                          f'{"matched" if r.sure else "unmatched"} (cost {cost:.2f})')
        if a:
            answer += [f'The judge ({readable(a.get("judge"))}) said {a.get("verdict")} '
                       f'{a.get("problem") or ""}: {a.get("summary", "")}']
            answer += [f'  {q.get("file")}: {str(q.get("text", ""))[:300]}'
                       for q in pack.quotes(a)]
        answer += [f'Note: {n}' for n in r.notes]
        with self._keeping:
            say(*answer)
            if r.job.id not in self.state.judged:
                self.state.judged.append(r.job.id)
            if a:
                self.state.verdicts[r.job.id] = a
            self.state.keep(rs, 'judge', self.judge.name, cost, answer,
                            unit=self.units.get(self.judge.name),
                            summary=(a or {}).get('summary'))
            self.saved = False

    def do_learn(self, arg):
        """learn N [again]: ask the analyst to learn from group N: to place it, to
        teach the tree to explain it, or, for a new failure, to find its cause. Its
        changes are kept only if they pass the checks. Costs a lesson, the first
        time: after that, it shows what it learned, unless again.
        learn all: a lesson from each group that judge all recommends. Costs a lesson
        each. The lessons_at_once setting says how many ask the analyst at once."""
        if not self.analyst:
            raise ValueError('no analyst: give --analyst, or set analyst in a config file')
        if arg.strip() == 'all':
            todo = self.to_learn()
            if not todo:
                self.say('Nothing to learn from: every group the analyst could learn from '
                         'has a lesson.')
            at_once = getattr(self.args, 'lessons_at_once', None) or 1
            at_once = min(len(todo), len(todo) if at_once == 'all' else at_once)
            queued = {rs[0].job.id for rs, _ in todo}
            self.waiting_for_ai |= queued
            if at_once <= 1:
                try:
                    for n, (rs, _) in enumerate(todo, 1):
                        self.say(f'Group {n} of {len(todo)}, job {rs[0].job.id}:')
                        self._learn(rs, False)
                finally:
                    self.waiting_for_ai -= queued
                return
            self.say(f'{len(todo)} lessons, {at_once} at once; each line of one begins with '
                     'its job. Each change is checked and kept in turn.')
            out = getattr(self._local, 'stdout', None)

            def learn(rs):
                # Said where the command's own lines go.
                self._local.stdout = out
                self._learn(rs, False, tag=f'{rs[0].job.id}: ', retriage=False)

            try:
                with concurrent.futures.ThreadPoolExecutor(at_once) as pool:
                    done = [pool.submit(learn, rs) for rs, _ in todo]
            finally:
                self.waiting_for_ai -= queued
            # The jobs triaged again once, with what all of them learned.
            self.again()
            failed = [f.exception() for f in done if f.exception()]
            if failed:
                raise next((e for e in failed if isinstance(e, Stopped)), failed[0])
            return
        rs, rest = self.group(arg)
        self._learn(rs, rest.strip() == 'again')

    def stop(self):
        """Stop the judge and the analyst, from another thread, while they work."""
        for agent in (self.judge, self.analyst):
            getattr(agent, 'stop', lambda: None)()

    def go(self):
        """Let them work again, once stopped."""
        for agent in (self.judge, self.analyst):
            getattr(agent, 'go', lambda: None)()

    def _learn(self, rs, again, tag='', retriage=True):
        """A lesson from the jobs of rs; with tag, each line it says begins with it,
        as lessons at once do, and, unless retriage, the jobs are not triaged again
        after it."""
        say = self.say if not tag else lambda *lines: self.say(*(
            '\n'.join(line if line.startswith(tag) else tag + line
                      for line in str(text).split('\n')) for text in lines))
        if not again and self.asked(rs, 'lesson', 'learn'):
            return
        context = dict(self.learning(), limit=1, relearn=again, approving=self.approving())
        answer, show = self.listening(say)
        steps, progress = self.listening(say)
        account = rs[0].analysis
        with self.turn('analyst', rs[0].job.id):
            lines = learn_from_run(rs, analyst=self.analyst, say=say, show=show,
                                   progress=progress, **context)
        # Its account of the failure, if it gave one.
        account = rs[0].analysis if rs[0].analysis is not account else None
        if not lines:
            say('Nothing to learn from it: the tree and the records decided it, and not as '
                'new. To question that, use doubt.')
            return
        if NOT_BEGUN in lines[-1]:
            # Kept only for a lesson begun: one that was not is asked again.
            return
        costs = [float(c) for line in lines for c in re.findall(r'\(cost (-?[\d.]+)\)', line)]
        workdir = os.path.join(context['workdir'], rs[0].job.id)
        before = re.search(r' see (\S+)/analysis\.md$', lines[-1]) if not answer else None
        if before:
            # Taught before: what it found then, not a pointer to it.
            workdir = before.group(1)
            answer = analysis(workdir)
            say(*answer)
        with self._keeping:
            self.state.keep(rs, 'lesson', self.analyst.name, sum(costs), [*lines, *answer],
                            steps, self._session(workdir), workdir, paid=not before,
                            unit=self.units.get(self.analyst.name),
                            summary=self._summary(workdir), account=account)
        if retriage:
            self.again()

    @staticmethod
    def _summary(workdir):
        try:
            with open(os.path.join(workdir, 'lesson.json')) as f:
                return json.load(f).get('summary') or None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _session(workdir):
        try:
            with open(os.path.join(workdir, 'lesson.json')) as f:
                return json.load(f).get('session')
        except (OSError, ValueError):
            return None

    @staticmethod
    def _session_cost(answer):
        """What the session an answer came from had cost by then, for an agent that
        reports a session's costs so far."""
        try:
            with open(os.path.join(answer['workdir'], 'lesson.json')) as f:
                return json.load(f).get('session_cost') or answer['cost']
        except (OSError, ValueError):
            return answer['cost']

    def do_doubt(self, arg):
        """doubt N [--says ID|new] [-i] WHY: ask the analyst whether the result for
        group N's first job is right, and to change the records or the tree if not.
        --says gives the answer you expect; -i lets you talk with the analyst before
        anything is kept. Costs an analyst conversation."""
        if not self.analyst:
            raise ValueError('no analyst: give --analyst, or set analyst in a config file')
        rs, rest = self.group(arg)
        words, says, interactive = shlex.split(rest), None, False
        while words and words[0] in ('--says', '-i'):
            if words.pop(0) == '-i':
                interactive = True
            elif words:
                says = words.pop(0)
        self._doubt(rs, says, ' '.join(words), interactive)
        if not interactive:
            self.say(f'Its response: response {rs[0].job.id} shows it; accept takes it, '
                     'dismiss drops it, and talk ... response discusses it with the analyst.')

    def _doubt(self, rs, says, why, interactive=False, talk=None):
        """Ask the analyst about the doubt of a person who says the job is says (or
        says nothing), and why; and keep what it came to, its response, for them. With
        talk, its discussion of the response, the analyst gives its final answer, in
        the session they talked in."""
        context, r = self.learning(), rs[0]
        answer, show = self.listening()
        steps, progress = self.listening()
        doubt = Doubt(r.job, self.session.files[r.job.id], context['problems_dir'],
                      context['labels_dir'], self.analyst,
                      talk['workdir'] if talk else f'{context["workdir"]}/doubt-{r.job.id}',
                      context['files_for'], context['jobs_for'], context['tracker'],
                      expected=says, why=why, by='a reviewer', said_by_people=True, firm=True,
                      recorded=recorded(r),
                      corpus=context['corpus'], interactive=interactive, say=show,
                      search=context['search'], source=context['source'],
                      nodes_dir=context['nodes_dir'], history=context['history'],
                      holdout=context.get('holdout', ()),
                      budget=context['budget'], progress=progress,
                      session=talk['session'] if talk else None,
                      **self.approving(asked=interactive))
        try:
            with self.turn('analyst', r.job.id):
                settled = doubt.run(DIGEST if talk else None)
        except Stopped:
            # Nothing was done to keep.
            raise
        except JudgeError as e:
            settled = False
            show(f'Not settled: {e}')
        account = self._account(r, doubt, settled)
        cost = doubt.cost() or 0
        if talk and self.analyst.cumulative_cost:
            cost, talk['paid'] = max(0, cost - (talk.get('paid') or 0)), cost
        if talk:
            talk['session'] = doubt.session or talk['session']
        line = (f'{r.job.id}: {"its final answer" if talk else "doubted"}, ' +
                ('settled' if settled else 'its change waits for your approval' if doubt.held
                 else 'not settled') +
                (f', wrote {", ".join(doubt.written)}' if doubt.written else '') +
                self._blame(rs, doubt, settled) +
                f' (cost {cost:.2f}); see {doubt.workdir}/analysis.md')
        self.say(line)
        given = doubt.answer or {}
        response = {'says': says, 'why': why, 'settled': bool(settled), 'held': doubt.held,
                    'kept': doubt.kept, 'disputed': getattr(doubt, 'disputed', None),
                    'problem': given.get('problem'), 'assessment': given.get('assessment'),
                    'written': list(doubt.written), 'answer': given or None,
                    'final': bool(talk)}
        self.state.keep(rs, 'doubt', self.analyst.name, cost, [line, *answer], steps,
                        doubt.session, doubt.workdir, unit=self.units.get(self.analyst.name),
                        summary=given.get('summary'), account=account, response=response)
        if doubt.written:
            self.again()

    def response_of(self, job_id):
        """(the doubt about job_id that has a response, its response); None if none."""
        latest = self.state.latest(job_id, 'doubt')
        if not (latest and latest.get('response')):
            return None
        return latest, latest['response']

    def response_lines(self, job_id):
        """(lines, what the person can do with it: some of accept and dismiss) of the
        analyst's response to the doubt about job_id, for the person who doubted it;
        None if there is none."""
        found = self.response_of(job_id)
        if not found:
            return None
        latest, got = found
        name = readable(latest.get('agent') or 'the analyst')
        problems = {p.id: p for p in self.session.triage.problems}

        def named(pid):
            if not pid or str(pid).lower() in ('new', 'none'):
                return 'new, a problem no record has'
            pid = db.record_id(pid)
            p = problems.get(pid)
            return db.label(pid) + (f', {p.title}' if p else '')
        says, problem = got.get('says'), got.get('problem')
        lines = [f'This is my response{", after our discussion" if got.get("final") else ""}'
                 f', as {name} gives it:', '',
                 str(latest.get('summary') or 'It gave no summary.').strip(), '',
                 f'It says the job is {named(problem)}.' if got.get('answer') else
                 'It gave no answer that could be checked.']
        if says:
            agrees = got.get('answer') and str(db.record_id(says)) == str(problem) or \
                str(says).lower() == 'new' and not problem and got.get('answer')
            lines.append(f'You said {named(says)}: it ' +
                         ('agrees with you.' if agrees else 'disagrees with you.'))
        can = []
        held = got.get('held') if got.get('held') and os.path.exists(got['held']) else None
        kept = got.get('kept') if got.get('kept') and os.path.exists(got['kept']) else None
        if got.get('accepted'):
            lines += ['', 'You accepted it.']
        elif got.get('dismissed'):
            lines += ['', 'You dismissed it.']
        elif held:
            with open(held) as f:
                change = json.load(f)
            lines += ['', 'Its change waits for your approval:',
                      *self.held_lines(held, change)[4:]]
            can = ['accept', 'dismiss']
        elif got.get('disputed') is not None and got.get('answer'):
            lines += ['', 'It answers otherwise than you, and its answer passed the checks: '
                          'accept it if it convinced you, and the job is settled as it says' +
                          (', with its change, which is kept aside until then.' if kept else
                           '; it changed nothing.')]
            can = ['accept', 'dismiss']
        elif got.get('settled'):
            lines += ['', 'It is kept: ' + (f'it wrote {", ".join(got["written"])}.'
                                            if got.get('written') else 'it changed nothing.')]
        else:
            failed = approve.failing(last_checks(latest.get('workdir') or ''))
            if failed:
                # Even where it agrees with the person, nothing is kept until the records
                # place the job themselves: what stops them is what to discuss.
                lines += ['', 'Nothing is kept: its answer failed these checks:', '',
                          *failed, '', 'Discuss it (so that it changes what failed), or '
                          'dismiss it and doubt it again.']
            else:
                lines += ['', 'Its answer did not pass the checks, or it was not sure: '
                              'nothing is kept. Discuss it, or dismiss it, and doubt it again.']
            can = ['dismiss']
        return lines, can

    def response_whole(self, job_id):
        """The analyst's response to the doubt about job_id, whole: what it said, what it
        found, as analysis.md has it, and the change that waits, if one does."""
        latest, got = self.response_of(job_id)
        lines = [*self.response_lines(job_id)[0], '', 'What it found:', '',
                 *analysis(latest.get('workdir') or '')]
        held = got.get('held') or got.get('kept')
        if held and os.path.exists(held):
            with open(held) as f:
                lines += ['', *self.change_lines(held, json.load(f))]
        return lines

    def do_response(self, arg):
        """response N: the analyst's response to your doubt about group N's first job:
        what it says, whether it agrees with you, and what waits for you. Free."""
        rs, _ = self.group(arg)
        found = self.response_lines(rs[0].job.id)
        if not found:
            raise ValueError(f'{rs[0].job.id}: no doubt about it has a response')
        lines, can = found
        self.say(*lines, '', ' '.join(f'{c} {rs[0].job.id}' for c in can) +
                 ('; ' if can else '') + f'talk {rs[0].job.id} response discusses it')

    def do_accept(self, arg):
        """accept N: take the analyst's response to your doubt about group N's first
        job: its change, if one waits for approval, is checked again and kept; if it
        answered otherwise than you, the job is settled as it says, by you, with its
        change. Free: no agent is asked."""
        rs, _ = self.group(arg)
        r, found = rs[0], self.response_of(rs[0].job.id)
        latest, got = found or (None, {})
        if not found or not got.get('answer') or got.get('accepted') or got.get('dismissed'):
            raise ValueError(f'{r.job.id}: no response waits to be accepted')
        path = next((p for p in (got.get('held'), got.get('kept')) if p and os.path.exists(p)),
                    None)
        if path:
            with open(path) as f:
                held = json.load(f)
            if got.get('kept') == path:
                # The person agrees with the analyst: the job is theirs, as it says.
                held.update(by=getpass.getuser(), why=f'agreed with {latest["agent"]}\'s '
                                                      'response to their doubt')
        elif got.get('disputed') is not None:
            # It changed nothing: the job is settled as it says, by the person.
            held = dict(got['answer'], job=r.job.path, expected=got.get('problem') or 'new',
                        by=getpass.getuser(), why=f'agreed with {latest["agent"]}\'s response '
                                                  'to their doubt',
                        session=latest.get('session'), workdir=latest.get('workdir'))
        else:
            raise ValueError(f'{r.job.id}: its response is kept already')
        doubt = self._doubting(held)
        settled, _ = doubt.settle(held)
        if settled:
            if path:
                approve.approved(path, held)
            got['accepted'] = True
            self.state.save()
        if not settled:
            raise ValueError(f'{r.job.id}: its answer no longer passes the checks, so nothing '
                             'is kept: dismiss drops it')
        self.say(f'Kept the response to the doubt about {r.job.id}' +
                 (f': wrote {", ".join(doubt.written)}' if doubt.written else '') + '.')
        self.again()

    def do_dismiss(self, arg):
        """dismiss N: drop the analyst's response to your doubt about group N's first
        job: its change, if it made one, is set aside in the cache, and the library is
        left as it is. Free."""
        rs, _ = self.group(arg)
        r, found = rs[0], self.response_of(rs[0].job.id)
        if not found or found[1].get('accepted') or found[1].get('dismissed'):
            raise ValueError(f'{r.job.id}: no response waits to be dismissed')
        got, cache = found[1], getattr(self.args, 'cache', None) or '.'
        for path in (got.get('held'), got.get('kept')):
            if path and os.path.exists(path):
                approve.reject(path, cache)
        got['dismissed'] = True
        self.state.save()
        self.say(f'{r.job.id}: its response is dismissed; nothing it made is kept')

    def do_digest(self, arg):
        """digest N: once you have discussed the analyst's response to your doubt about
        group N's first job (talk N response), ask it once more, in that discussion,
        for its final answer, with any change the discussion called for; it is checked
        and kept, or waits for you, as the doubt's was. Costs a turn of the
        analyst's."""
        rs, _ = self.group(arg)
        r = rs[0]
        found, talk = self.response_of(r.job.id), self._talk(r.job.id)
        if not (found and talk and talk.get('response')):
            raise ValueError(f'{r.job.id}: discuss its response first: talk {r.job.id} '
                             'response')
        got = found[1]
        if not (got.get('accepted') or got.get('dismissed')):
            # What it answered before gives way to its final answer.
            for path in (got.get('held'), got.get('kept')):
                if path and os.path.exists(path):
                    approve.reject(path, getattr(self.args, 'cache', None) or '.')
        self._doubt(rs, got.get('says'), got.get('why') or '', talk=talk)

    def do_discuss(self, arg):
        """discuss N MESSAGE: say MESSAGE to the analyst about group N's first job, and
        show its reply. The discussion continues the analyst's last session about the
        job, if it can, so it knows what it found; otherwise the first message gives
        it what was said so far. Costs a turn of the analyst's."""
        if not self.analyst:
            raise ValueError('no analyst: give --analyst, or set analyst in a config file')
        rs, message = self.group(arg)
        if not message.strip():
            raise ValueError('say something to the analyst')
        self._discuss(rs, message)

    def _discuss(self, rs, message, who='you', shown=None):
        """Say message to the analyst about rs's first job; who said it, and what
        the discussion shows of it, if not message."""
        r, analyst = rs[0], self.analyst
        talks = self.state.discussions.setdefault(r.job.id, [])
        talk = talks[-1] if talks and talks[-1]['agent'] == analyst.name else None
        prompt = message
        if not talk:
            before = self.state.latest(r.job.id, 'lesson', 'doubt')
            if before and before.get('session') and before.get('workdir') and \
                    analyst.resumes and os.path.isdir(before['workdir']):
                # The analyst's own session: it knows what it found.
                talk = {'agent': analyst.name, 'session': before['session'],
                        'workdir': before['workdir'], 'turns': [],
                        'paid': self._session_cost(before)}
                prompt = f'{DISCUSS}\n\n{message}'
            else:
                context = self.learning()
                workdir = os.path.join(context['workdir'], f'discuss-{r.job.id}')
                doubt = Doubt(r.job, self.session.files[r.job.id], context['problems_dir'],
                              context['labels_dir'], analyst, workdir, context['files_for'],
                              context['jobs_for'], context['tracker'], say=lambda line: None,
                              source=context['source'], nodes_dir=context['nodes_dir'])
                said = self.answers(r.job.id)
                doubt.setup()
                prompt = '\n\n'.join([DISCUSS, doubt.instructions()] +
                                     (['What was said about it before:\n' + '\n'.join(said)]
                                      if said else []) + [message])
                talk = {'agent': analyst.name, 'session': None, 'workdir': workdir,
                        'turns': []}
            talks.append(talk)
        elif not analyst.resumes:
            # An agent that cannot continue a session hears the conversation again.
            prompt = '\n\n'.join([DISCUSS] + [f'{t["who"]}: {t["text"]}' for t in talk['turns']]
                                 + [f'{who}: {message}'])
        steps, progress = self.listening()
        before, analyst.progress = getattr(analyst, 'progress', None), progress
        try:
            reply = analyst.chat(talk['workdir'], prompt, talk['session'],
                                 tag=next_tag(talk['workdir'], 'discuss'))
        finally:
            analyst.progress = before
        talk['session'] = reply.get('session') or talk['session']
        talk['turns'] += [{'who': who, 'text': shown or message},
                          {'who': analyst.name, 'text': reply['text']}]
        cost = reply.get('cost') or 0
        if analyst.cumulative_cost and talk.get('paid') is not None and reply.get('cost'):
            cost = max(0, reply['cost'] - talk['paid'])
        if analyst.cumulative_cost and reply.get('cost') is not None:
            talk['paid'] = reply['cost']
        self.say('', f'{readable(analyst.name)}:',
                 *[f'  {line}' for line in reply['text'].split('\n')])
        self.state.keep(rs, 'discussion', analyst.name, cost, [reply['text']], steps,
                        talk['session'], talk['workdir'], unit=self.units.get(analyst.name))

    def do_propose(self, arg):
        """propose N [NOTE]: ask the analyst, in its discussion of group N's first job,
        for the change to the records or the tree that the discussion found, with
        NOTE if given. It is kept only if it passes the checks, as a lesson's is; if
        not, the analyst is told why, and asked again, as many times as for a doubt.
        Costs a turn of the analyst's, or a few."""
        if not self.analyst:
            raise ValueError('no analyst: give --analyst, or set analyst in a config file')
        rs, note = self.group(arg)
        r, analyst = rs[0], self.analyst
        talk = self._talk(r.job.id)
        if not (talk and talk.get('session')):
            raise ValueError(f'discuss it with {analyst.name} first: propose asks for what '
                             'the discussion found')
        answer, show = self.listening()
        steps, progress = self.listening()
        doubt = self._proposing(r, talk, show, progress)
        try:
            with self.turn('analyst', r.job.id):
                settled = doubt.run(PROPOSE + (f'\n\nThe person adds: {note}' if note else ''))
        except JudgeError as e:
            settled = False
            show(f'Not settled: {e}')
        cost = doubt.cost() or 0
        if analyst.cumulative_cost:
            cost, talk['paid'] = max(0, cost - (talk.get('paid') or 0)), cost
        talk['session'] = doubt.session or talk['session']
        line = self._proposed(r, doubt, settled, 'proposed') + f' (cost {cost:.2f})'
        self.say(line)
        # The whole answer is kept once, as the proposal's.
        talk['turns'].append({'who': 'you', 'text': '/propose' + (f' {note}' if note else '')})
        self._keep_proposal(rs, talk, doubt, line, answer, steps, cost)

    def _talk(self, job_id):
        """The analyst's discussion of a job, if it has one."""
        talks = self.state.discussions.get(job_id, [])
        if talks and self.analyst and talks[-1]['agent'] == self.analyst.name:
            return talks[-1]
        return None

    def _proposing(self, r, talk, show, progress):
        """A doubt that checks the analyst's proposal from its discussion of r's job."""
        context, analyst = self.learning(), self.analyst
        return Doubt(r.job, self.session.files[r.job.id], context['problems_dir'],
                     context['labels_dir'], analyst, talk['workdir'], context['files_for'],
                     context['jobs_for'], context['tracker'], why='what was discussed',
                     # The analyst's answer, not the reviewer's: a lesson's label.
                     by=f'qa_triage ({analyst.name})', label_none=False,
                     recorded=recorded(r), corpus=context['corpus'],
                     say=show, search=context['search'], source=context['source'],
                     nodes_dir=context['nodes_dir'], history=context['history'],
                     holdout=context.get('holdout', ()),
                     budget=context['budget'], progress=progress, session=talk['session'],
                     **self.approving())

    def approving(self, asked=False):
        """What a doubt needs to ask before changing the library and to publish the
        change: on the full screen, which cannot ask in the middle, each change
        waits for approval (qa_triage pending); at the prompt, the person is asked.
        asked is True when the person asked for the change themselves."""
        args = getattr(self, 'args', None)
        found = approve.doubting(getattr(args, 'settings', None) or {},
                                 getattr(args, 'cache', '.'), getattr(args, 'library', None),
                                 None if getattr(args, 'plain', False) else False)
        return dict(found, approve=None, approve_edits=None) if asked else found

    def _blame(self, rs, doubt, settled):
        """Give the group the PR under test that the analyst found caused it, if its
        answer passed the checks; what to add to the line that says so."""
        pr = (doubt.answer or {}).get('pr') if settled else None
        if not pr:
            return ''
        for r in rs:
            if pr not in (b[0] for b in r.blamed):
                r.blamed.append((pr, f'the analyst ({self.analyst.name})',
                                 doubt.answer.get('summary', '')))
        return f'; {pr}, under test, caused it'

    def _account(self, r, doubt, settled):
        """Give r's group the analyst's account of the failure, if its answer passed
        the checks: kept, or waiting for approval; the account, or None."""
        if not (settled or doubt.held):
            return None
        before = r.analysis
        keep_account(self.results, r, doubt.answer, self.analyst.name, doubt.files,
                     doubt.workdir, None if settled else doubt.held)
        return r.analysis if r.analysis is not before else None

    def _proposed(self, r, doubt, settled, how):
        self._account(r, doubt, settled)
        return f'{r.job.id}: {how}, ' + (
            f'kept, wrote {", ".join(doubt.written)}' if settled and doubt.written else
            'no change to the records or the tree' if settled else 'not kept') + \
            self._blame(self.group_of(r), doubt, settled)

    def _keep_proposal(self, rs, talk, doubt, line, said, steps=(), cost=None):
        analyst, summary = self.analyst, (doubt.answer or {}).get('summary')
        talk['turns'].append({'who': analyst.name,
                              'text': '\n'.join([line] + ([summary] if summary else []))})
        said = [s for s in said if not s.startswith(f'Asking {analyst.name}')]
        account = rs[0].analysis if rs[0].analysis and \
            rs[0].analysis.get('summary') == summary else None
        self.state.keep(rs, 'proposal', analyst.name, cost, [line, *said], steps,
                        talk['session'], talk['workdir'], paid=cost is not None,
                        unit=self.units.get(analyst.name), summary=summary, account=account)
        if doubt.written:
            self.again()

    # The PRs the run tests.

    def qa_tracker(self):
        """The number of the run's QA tracker issue, or None."""
        found = next((re.search(r'/issues/(\d+)', h) for h in self.session.header
                      if h.startswith('QA tracker')), None)
        return found.group(1) if found else None

    def under_test(self):
        """{PR number: how it is known to be under test} of the PRs the run tests, in
        their order: those its QA tracker lists, those merged into its branch, as the
        ceph clone shows them, and those a watch of PRs found it tests; {} for a run of
        a base branch, or before they are found."""
        triage = self.session.triage
        found = {n: ['its QA tracker lists it']
                 for n in sorted(getattr(triage, 'batch_prs', None) or (), key=int)}
        for merged in list((getattr(triage, 'branches', None) or {}).values()):
            for what, _, _, files in merged:
                pr = re.match(r'PR #(\d+)$', what)
                if pr:
                    found.setdefault(pr.group(1), []).append(
                        f'merged into its branch, changing {len(files)} '
                        f'file{"" if len(files) == 1 else "s"}')
        for number, how in dict(self.followed).items():
            found.setdefault(number, []).extend(h for h in how if h not in found[number])
        return dict(sorted(found.items(), key=lambda item: int(item[0])))

    def suspected(self):
        """{PR number: [group numbers]} of the groups whose jobs ran a file a PR under
        test changes, or that an analyst found it caused."""
        found = {}
        for n, (_, rs) in enumerate(report.groups(self.results), 1):
            for r in rs:
                for what in [pr for pr, _, _ in r.blamed] + [what for what, _ in r.suspects]:
                    pr = re.search(r'#(\d+)', str(what))
                    if pr and n not in found.setdefault(pr.group(1), []):
                        found[pr.group(1)].append(n)
        return found

    def do_prs(self, arg):
        """prs: the PRs the run tests, from its QA tracker and its branch, each with its
        title, from GitHub, and the groups whose jobs ran a file it changes. Free."""
        prs = self.under_test()
        if not prs:
            self.say('No PRs under test: the run is of a base branch, or its QA tracker '
                     'and branch name none, or they are not found yet.')
            return
        tracker = self.qa_tracker()
        self.say(f'{len(prs)} PR{"" if len(prs) == 1 else "s"} under test' +
                 (f', of QA tracker {TRACKER}/issues/{tracker}' if tracker else '') + ':', '')
        if not (self.args and getattr(self.args, 'settings', None) or {}).get('git'):
            from .cli import NO_CLONE
            self.say(NO_CLONE, '')
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            titles = dict(zip(prs, pool.map(pr_title, prs)))
        suspected = self.suspected()
        for number, how in prs.items():
            title = titles.get(number)
            groups = suspected.get(number)
            self.say(f'#{number}  ' + (f'{title[0]} ({title[1]})' if title else
                                       '(GitHub did not say its title)'),
                     f'    {"; ".join(how)}: https://github.com/ceph/ceph/pull/{number}',
                     *([f'    the jobs of group{"" if len(groups) == 1 else "s"} '
                        f'{", ".join(map(str, groups))} ran a file it changes'] if groups else []))

    # The changes that wait for a person's approval.

    def waiting(self):
        """[(path, change)] of the changes to the records and the tree that wait for
        a person's approval, as qa_triage pending lists them."""
        return approve.waiting(getattr(self.args, 'cache', None) or '.')

    def held_lines(self, path, held):
        """What a change that waits for approval is, for a person deciding whether to
        keep it: what failed, as the analyst found it; what the change does; and the
        commit to inspect its code in."""
        problems = self.args.problems
        library, writes = approve.library_of(problems), held_writes(held, problems)
        added = [n for n in writes if not os.path.exists(os.path.join(library, n))]
        edited = [n for n in writes if n not in added]
        problem = held.get('problem')
        title = next((p.title for p in self.session.triage.problems if p.id == str(problem)),
                     None) if problem else None
        for change in held.get('changes') or []:
            if str(change.get('id')) == str(problem):
                with contextlib.suppress(yaml.YAMLError, AttributeError):
                    title = yaml.safe_load(change.get('yaml', '')).get('title') or title
        does = ((f'It places this job, and those that fail the same way, in {problem}' +
                 (f': {title}' if title else '')) if problem else
                'It decides this job, and those that fail the same way, as new: no record '
                'has its problem yet')
        made = approve.commit_of(path, held, problems)
        return [f'Job {held.get("job")}', '',
                str(held.get('summary') or 'The analyst gave no summary.').strip(), '',
                f'{does}.' + (f' It adds {", ".join(added)}.' if added else '') +
                (f' It changes {", ".join(edited)}.' if edited else ''), '',
                'Its checks passed when the analyst made it. Keeping it checks it again, '
                'against the library as it is now, and then pushes it to the shared library.',
                '', f'To inspect the code: git -C {made[0]} show {made[1][:12]}' if made else
                'No commit of it could be made, to inspect: change shows it.']

    def change_lines(self, path, held):
        """A change that waits for approval, whole: what the analyst found, and the
        commit of what it writes, as git shows it."""
        made = approve.commit_of(path, held, self.args.problems)
        shown = []
        if made:
            p = subprocess.run(['git', '-C', made[0], 'show', '--stat', '--patch', made[1]],
                               capture_output=True, text=True, timeout=120)
            shown = p.stdout.splitlines() if p.returncode == 0 else \
                [f'git show {made[1]}: {p.stderr.strip()}']
        else:
            for name, text in held_writes(held, self.args.problems).items():
                shown += [f'{name}:', *text.splitlines(), '']
        return [*self.held_lines(path, held), '', 'What the analyst found:', '',
                str(held.get('explanation') or '').strip(), '', *shown]

    def _held(self, arg):
        """[(path, change)] of the changes that wait for approval that arg names: their
        numbers, as pending lists them, or all."""
        held, words = self.waiting(), arg.split()
        if not held:
            raise ValueError('No change waits for approval.')
        if words == ['all']:
            return held
        if not words or not all(w.isdigit() and 1 <= int(w) <= len(held) for w in words):
            raise ValueError(f'give the numbers of changes that wait, from 1 to {len(held)}, '
                             'or all: pending lists them')
        return [held[int(w) - 1] for w in dict.fromkeys(words)]

    def do_pending(self, arg):
        """pending: the changes to the records and the tree that wait for your
        approval, numbered: change N shows one, approve N keeps it, reject N drops it.
        Free."""
        held = self.waiting()
        if not held:
            self.say('No change waits for approval.')
            return
        for n, (path, change) in enumerate(held, 1):
            lines = self.held_lines(path, change)
            self.say(f'[{n}] {change.get("job")}', *(f'    {line}' for line in lines[2:]
                                                     if line), '')
        self.say('change N shows one whole; approve N (or all) checks it again and keeps '
                 'it; reject N drops it.')

    def do_change(self, arg):
        """change N: the change N that waits for approval, whole: what the analyst
        found, and the commit of what it writes. Free."""
        for path, held in self._held(arg):
            self.say(*self.change_lines(path, held))

    def do_approve(self, arg):
        """approve N... | all: keep the changes that wait for approval, numbered as
        pending lists them: each is made again onto the library as others have
        changed it meanwhile, keeping what both did, checked again, and if it still
        passes, written and pushed to the shared library. Free: no agent is asked."""
        todo, kept = self._held(arg), 0
        # One context for them all: making it reads every run triaged before.
        context = self.learning() if todo else None
        for path, held in todo:
            doubt, took = self._doubting(held, context), []
            settled, report, clash = self._settle_again(doubt, held, context, took)
            if settled:
                approve.approved(path, held)
                kept += 1
                self.say(f'{held.get("job")}: kept' +
                         (f' (made again onto what others changed meanwhile: '
                          f'{", ".join(took)})' if took else '') +
                         (f', wrote {", ".join(doubt.written)}' if doubt.written else ''))
                continue
            failing = approve.failing(report)
            needless = self._needless(doubt, held)
            number = next((n for n, (p, _) in enumerate(self.waiting(), 1) if p == path), None)
            self.say(f'{held.get("job")}: {approve.NOT_KEPT}, so it still waits: reject drops '
                     'it. ' + needless + (
                         'Others changed what it changes meanwhile, in ways that clash with '
                         'it:' if clash else
                         'Checked again, against the library as it is now, it fails these '
                         'checks, which keep a change from undoing what the library gets '
                         'right:' if failing
                         else 'It passed the checks, but the library kept changing while it '
                              'was checked: A tries again.'),
                     *(f'    {line}' for line in clash or failing))
            if (clash or failing) and number:
                self.say(f'revise {number} asks the analyst to make it again for the library '
                         'as it is now (costs AI).')
            self.say('')
        self.say(f'Kept {kept} of the {len(todo)} change{"" if len(todo) == 1 else "s"}.')
        if kept:
            self.again()

    def _settle_again(self, doubt, held, context, took):
        """(settled, the checks' report, what clashes) of a change that waits, checked
        again and kept if it passes: made again onto what others changed meanwhile.
        One that does not know what it was made on, kept before changes did, is
        merged only where the two sides do not clash; where they do, it is checked as
        it was, and what clashes is said only if that fails too."""
        def rebased(answer):
            return self._rebase(answer, context, took)
        try:
            settled, report = doubt.settle(held, rebase=rebased)
            return settled, report, []
        except approve.Clash as e:
            if held.get('before') is not None:
                return False, [], e.clashes
            clash = e.clashes
        settled, report = doubt.settle(held)
        return settled, report, [] if settled else clash

    @staticmethod
    def _rebase(answer, context, took):
        change, found = approve.rebase(answer, context['problems_dir'], context['nodes_dir'])
        took.extend(found)
        return change

    @staticmethod
    def _needless(doubt, held):
        """What to say of a change that waits, if the library as it is now places its
        job as the change would: it may not be needed any more; else ''."""
        want = problem_id(held.get('problem'))
        result = getattr(doubt, 'result', None)
        if want and result is not None and placed(result) == want:
            return (f'The library places the job in {want} already, as the change would: it '
                    'may not be needed any more. ')
        return ''

    def do_revise(self, arg):
        """revise N | <run>/<job>: ask the analyst to make change N again, numbered as pending lists
        them, for the library as others have changed it since: one that no longer
        passes the checks. Its new change waits for approval in place of the old; if
        it does not pass either, the old one still waits. Costs AI."""
        found = [(p, h) for p, h in self.waiting() if h.get('job') == arg.strip()] or \
            self._held(arg)
        if len(found) != 1:
            raise ValueError('give the number of one change that waits, or its job: pending '
                             'lists them')
        [(path, held)] = found
        context, took = self.learning(), []
        doubt = self._doubting(held, context, asked=False)
        doubt.setup()
        answer = dict(held, problem=problem_id(held.get('problem')))
        try:
            change = self._rebase(answer, context, took)
            ok, report, _ = doubt.check(change)
            why = approve.failing(report)
        except approve.Clash as e:
            # As approve decides: one that knows what it was made on clashes; one that
            # does not is checked as it was.
            ok, why = False, e.clashes
            if held.get('before') is None:
                ok, report, _ = doubt.check(answer)
                why += approve.failing(report)
        if ok:
            self.say(f'{held.get("job")}: it passes the checks against the library as it is '
                     'now: approve keeps it, with no need to ask the analyst.')
            return
        note = REVISE.format(why='\n'.join(why))
        run, _, jid = str(held.get('job')).rpartition('/')
        # Its own session, if the analyst is the one that made it; else a new one, told
        # the doubt first, in a directory of its own: the change that waits keeps the
        # evidence it quotes.
        made_by = held.get('analyst') or (self.state.latest(jid, 'lesson', 'doubt') or {}).get(
            'agent')
        resumable = held.get('session') and made_by == self.analyst.name
        doubt.session = held['session'] if resumable else None
        if not resumable:
            doubt.workdir = f'{doubt.workdir.rstrip(os.sep)}-revised'
        doubt.held = None
        try:
            with self.turn('analyst', jid):
                settled = doubt.run(note if resumable else
                                    (lambda: f'{doubt.instructions()}\n\n{note}'))
        except JudgeError as e:
            settled = False
            self.say(f'Not revised: {e}')
        if doubt.held or settled:
            approve.superseded(path, held, doubt.held)
        cost = doubt.cost() or 0
        line = (f'{held.get("job")}: revised, ' + (
            'its new change waits for your approval in place of the old: A asks about it'
            if doubt.held else 'kept' if settled else
            'but its revision does not pass the checks either, so the old change still '
            'waits: reject drops it') + f' (cost {cost:.2f}); see {doubt.workdir}/analysis.md')
        self.say(line)
        rs = [r for r in self.results if r.job.path == held.get('job')]
        if rs:
            self.state.keep(rs, 'revision', self.analyst.name, cost, [line], (), doubt.session,
                            doubt.workdir, unit=self.units.get(self.analyst.name),
                            summary=(doubt.answer or {}).get('summary'))
        if settled:
            self.again()

    def do_reject(self, arg):
        """reject N... | all: drop changes that wait for approval, numbered as pending
        lists them: the library is left as it is, and each is set aside in the cache,
        its commit still there to inspect. Free."""
        cache = getattr(self.args, 'cache', None) or '.'
        for path, held in self._held(arg):
            approve.reject(path, cache)
            self.say(f'{held.get("job")}: rejected; it is set aside in '
                     f'{approve.rejected(cache)}')

    def _doubting(self, held, context=None, asked=True):
        """A doubt that checks again a change kept for approval, and keeps it: the
        person approved it; without asked, one whose change waits for approval again,
        as a new change does. context is what learning gives, if made already."""
        if not self.analyst:
            raise ValueError('no analyst: give --analyst, or set analyst in a config file; '
                             'the change is checked again as its analyst\'s answer')
        context = context or self.learning()
        run, _, jid = str(held.get('job')).rpartition('/')
        job = context['jobs_for'](run).get(jid) or Job(run, jid, '', '', '')
        by = held.get('by') or getpass.getuser()
        doubt = Doubt(job, context['files_for'](job), context['problems_dir'],
                      context['labels_dir'], self.analyst,
                      held.get('workdir') or os.path.join(context['workdir'], jid),
                      context['files_for'], context['jobs_for'], context['tracker'],
                      expected=held.get('expected'), why=held.get('why') or '', by=by,
                      corpus=context['corpus'], say=self.say, search=context['search'],
                      source=context['source'], nodes_dir=context['nodes_dir'],
                      history=context['history'], holdout=context.get('holdout', ()),
                      said_by_people=not str(by).startswith('qa_triage'),
                      **self.approving(asked=asked))
        doubt.session = held.get('session') or doubt.session
        return doubt

    # Talking with the analyst in its own program.

    def program(self, job_id):
        """(the command that opens the analyst's own program on its discussion of a
        job, the directory it runs in), once the discussion is ready for it; or
        None."""
        talk = self._talk(job_id)
        if not (talk and talk.get('live') and talk.get('session')):
            return None
        workdir = os.path.abspath(talk['workdir'])
        return self.analyst.interactive(workdir, talk['session']), workdir

    def do_ready(self, arg):
        """ready N: get the analyst's discussion of group N's first job ready to go on
        in the analyst's own program, as talk does, without opening it; and say the
        command that opens it. Costs a turn of the analyst's, unless it is ready."""
        rs, rest = self.group(arg)
        self._ready(rs, response=rest.strip() == 'response')

    def _ready(self, rs, response=False):
        """Get the analyst's discussion of rs's first job ready for its own program;
        with response, to discuss its response to the person's doubt, going on from
        the doubt's own session."""
        if response:
            self.respond(rs, live=bool(self.analyst and self.analyst.program))
            if not self.analyst.program:
                self.say(f'Ready: discuss {rs[0].job.id} MESSAGE talks with it here, and '
                         f'digest {rs[0].job.id} then asks for its final answer.')
                return
        elif not (self.analyst and self.analyst.program):
            raise ValueError(f'{self.analyst.name if self.analyst else "no analyst"} has no '
                             'program of its own to talk in: discuss talks with it here')
        job_id = rs[0].job.id
        if not self.program(job_id):
            self._discuss(rs, LIVE, who='qa_triage',
                          shown=f'(Asked it to get ready to talk in {self.analyst.program}, '
                                f'and to write its proposal to {PROPOSAL}.)')
            self._talk(job_id)['live'] = True
            self.state.save()
        argv, workdir = self.program(job_id)
        self.say(f'Ready: {shlex.join(argv)}  (in {workdir})')

    def respond(self, rs, live=True):
        """Begin a discussion of the analyst's response to the person's doubt about
        rs's first job: in the doubt's own session, which has all it found, told to
        discuss rather than look further. live, for its own program; otherwise here,
        with discuss. Costs a turn of the analyst's."""
        r, analyst = rs[0], self.analyst
        found = self.response_of(r.job.id)
        if not (analyst and found):
            raise ValueError(f'{r.job.id}: no doubt about it has a response to discuss')
        latest = found[0]
        if latest['agent'] != analyst.name or not (latest.get('session') and analyst.resumes):
            raise ValueError(f'the response is {latest["agent"]}\'s, whose session '
                             f'{analyst.name} cannot go on from: choose it as the analyst (o)')
        talk = {'agent': analyst.name, 'session': latest['session'],
                'workdir': latest['workdir'], 'turns': [], 'paid': self._session_cost(latest),
                'response': True}
        self.state.discussions.setdefault(r.job.id, []).append(talk)
        self._discuss(rs, RESPONSE_TALK, who='qa_triage',
                      shown='(Asked it to discuss its response with you' +
                            (f', in {analyst.program}.)' if live else '.)'))
        talk['live'] = bool(live)
        self.state.save()

    def do_talk(self, arg):
        """talk N: talk with the analyst about group N's first job in the analyst's own
        interactive program, if it has one, on this terminal, until you leave it. It goes on
        from the discussion of the job, with the evidence and the instructions; when
        you leave, the proposal it wrote, if any, is checked, as back says. Getting
        ready costs a turn of the analyst's, and what is said there costs too."""
        rs, rest = self.group(arg)
        response = rest.strip() == 'response'
        self._ready(rs, response=response)
        argv, workdir = self.program(rs[0].job.id)
        since = time.time()
        subprocess.run(argv, cwd=workdir, env=getattr(self.analyst, 'env', lambda: None)())
        if response:
            # Then the analyst gives its final answer, from the discussion.
            self.do_digest(rs[0].job.id)
        else:
            self._back(rs, since)

    def do_back(self, arg):
        """back N SINCE: what talk does when you leave the analyst's program, SINCE
        (seconds since 1970) being when you went to it: check the proposal it wrote
        there, in proposal.json, and keep it if it passes the checks, as propose does.
        Asks the analyst nothing."""
        rs, since = self.group(arg)
        self._back(rs, float(since or 0))

    def _back(self, rs, since):
        r, analyst = rs[0], self.analyst
        talk = self._talk(r.job.id)
        if not talk:
            raise ValueError(f'{r.job.id} has no discussion with '
                             f'{analyst.name if analyst else "an analyst"}')
        talk['turns'].append({'who': 'you', 'text': f'(Talked with it in {analyst.program}' +
                              (f', {max(1, round((time.time() - since) / 60))} min' if since
                               else '') + '.)'})
        path = os.path.join(talk['workdir'], PROPOSAL)
        if not (os.path.exists(path) and os.path.getmtime(path) >= since):
            line = f'{r.job.id}: talked in {analyst.program}; it wrote no proposal'
            self.say(line)
            talk['turns'].append({'who': 'qa_triage', 'text': line})
            self.state.keep(rs, 'discussion', analyst.name, None, [line], (), talk['session'],
                            talk['workdir'], paid=False)
            return
        # Checked once: the next one it writes is new.
        kept = os.path.join(talk['workdir'], next_tag(talk['workdir'], 'proposal', '.json') +
                            '.json')
        os.replace(path, kept)
        said, show = self.listening()
        doubt = self._proposing(r, talk, show, None)
        try:
            with open(kept) as f:
                answer = json.load(f)
            if not isinstance(answer, dict):
                raise ValueError('not a JSON object')
        except ValueError as e:
            line = (f'{r.job.id}: proposed in {analyst.program}, not kept: its {PROPOSAL} '
                    f'does not load ({e}); it is {kept}')
        else:
            settled, _ = doubt.settle(answer)
            line = self._proposed(r, doubt, settled, f'proposed in {analyst.program}') + (
                '' if settled else f'; the checks are at the end of {talk["workdir"]}/'
                                   'analysis.md, to tell it about')
        self.say(line)
        self._keep_proposal(rs, talk, doubt, line, said)

    def choices(self):
        """The analysts to choose from: the config's agents, and each kind of agent
        installed here, with its own default model."""
        agents = (getattr(self.args, 'settings', None) or {}).get('agents', {})
        return list(dict.fromkeys([*agents, *(spec for spec, _ in installed())]))

    def described(self):
        """[(name, the name of the analyst it makes, its own program or None)] for the
        analysts to choose from, the one asked now among them."""
        agents = (getattr(self.args, 'settings', None) or {}).get('agents', {})
        found = []
        for name in self.choices():
            try:
                agent = make_agent(name, agents, files=True)
            except ValueError:
                continue
            found.append((name, agent.name, agent.program))
        now = self.analyst
        if now and now.name not in [made for _, made, _ in found]:
            found.insert(0, (now.name, now.name, getattr(now, 'program', None)))
        return found

    def do_analyst(self, arg):
        """analyst [NAME]: the analyst that learn, doubt, talk, discuss and propose
        ask: one of the config's agents, or any agent, as --analyst takes. With no
        name, the analyst now, and the ones to choose from."""
        if not arg.strip():
            found = self.described()
            width = max(len(name) for name, _, _ in found)
            self.say(f'The analyst is {self.analyst.name if self.analyst else "none"}. '
                     'To choose another: analyst NAME, one of',
                     *[f'  {name:<{width}}  talk: {program or "here, with discuss"}'
                       for name, _, program in found])
            return
        settings = getattr(self.args, 'settings', None) or {}
        self.analyst = make_agent(arg.strip(), settings.get('agents', {}), files=True)
        self.units.update(units(self.analyst))
        self.say(f'The analyst is now {readable(self.analyst.name)}.')

    # Settings changed while it is open.

    def do_settings(self, arg):
        """settings: the setup page, as qa_triage setup shows it, to change the settings
        in your config file; what is saved there, the review takes up at once where it
        can."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError('the setup page needs a terminal')
        page = setup.Setup()
        setup.run(page=page)
        self.say(self.take(page.since()))

    def take(self, changed):
        """Take up settings saved while the review is open, {setting: its value, None
        for its default}, as config.load gives them: those it can, from now on; the
        rest are the triage's, done with the ones it had, for the next run. What was
        taken up and what was not, in a sentence. The verdicts given stay."""
        if not changed:
            return 'No settings were changed.'
        args, settings = self.args, self.args.settings
        titles = dict(setup.TITLES, agents='agent keys')
        now, later, notes = [], [], []
        for name, value in changed.items():
            value = config.DEFAULTS.get(name) if value is None else value
            if name in LATER or name not in config.DEFAULTS:
                later.append(titles.get(name, name))
                continue
            if name == 'agents':
                # Each agent's settings, as they are now.
                value = dict(settings.get('agents') or {}, **value)
            settings[name] = value
            if name in ('judge', 'analyst', 'learn_max', 'learn_budget', 'lessons_at_once',
                        'judges_at_once'):
                setattr(args, name, value)
            if name == 'cited_lines':
                self.session.triage.cited_lines = value
            if name == 'tracker_key':
                try:
                    use_tracker_key(tracker_key(value))
                except ValueError as e:
                    notes.append(f'Not using the tracker key: {e}.')
            now.append(titles.get(name, name))
        if {'judge', 'analyst', 'agents'} & set(changed) and self.agents:
            judge, analyst = self.agents()
            if {'judge', 'agents'} & set(changed):
                self.judge = judge
            if {'analyst', 'agents'} & set(changed):
                self.analyst = analyst
            self.units.update(units(self.judge, self.analyst))
        said = [f'Taken up now: {", ".join(now)}.' if now else '',
                f'From the next review: {", ".join(later)}.' if later else '']
        if {'judge', 'analyst'} & set(changed):
            said.append(f'The judge is {readable(self.judge.name) if self.judge else "none"}, '
                        'the analyst '
                        f'{readable(self.analyst.name) if self.analyst else "none"}.')
        return ' '.join(part for part in said + notes if part)

    # Leaving.

    def do_quit(self, arg):
        """quit: leave the review, writing the report as it is now. What the agents
        said is kept already."""
        if not self.saved:
            self.write()
        self.say(f'Report: {self.session.out}/report.md', self.spent())
        return True

    do_exit = do_quit

    def do_EOF(self, arg):
        self.say('')
        return self.do_quit(arg)
