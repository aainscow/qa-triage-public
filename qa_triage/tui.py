"""A full-screen front end for qa_triage review, in curses.

The screen lists the run's groups of jobs, as the summary gives them, shows what
the tree and the records found for the one selected, and does a command for a
key. The commands are review.Review's, which does the work: the screen shows
their output. The keys that ask an agent say so. The judge's are done when
pressed, as a call of the judge's costs little; the analyst's ask first, saying the
most a lesson may cost. While the agent works, the screen says so, and how long it
has taken. The bottom line always says what the agents have cost.
"""

import base64
import contextlib
import curses
import datetime
import difflib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import textwrap
import traceback
from dataclasses import dataclass, replace

from . import approve, report, watch
from .agents import readable
from .sources import https_base

# The keys along the bottom: those that cost nothing, then, on a line of their own,
# those that ask an agent, which costs.
FREE_KEYS = (('↑↓', 'select'), ('enter', 'details'), ('tab', 'pane'), ('y', 'copy'),
             ('m', 'mouse'), ('f', 'files'), ('i', 'update records'), ('p', 'PRs'),
             ('w', 'wiki'), ('c', 'comment'), ('r', 'retriage'), ('W', 'done'),
             ('o', 'options'), ('?', 'help'), ('q', 'quit'))
# Beside the agents on the status line.
CHOOSE_AI = '   ([o] chooses the AI agents)'
AI_KEYS = (('j', 'judge'), ('u', 'judge all'), ('l', 'learn'), ('d', 'doubt'), ('t', 'talk'),
           ('J', 'judge again'), ('L', 'learn again'))
HELP = '''Keys

  up, down, page up, page down, home, end   choose a group of jobs
  enter                                     everything the tree and the records
                                            found for its first job, in a pager
  tab       move to the lower pane, to scroll it with the same keys, and back
  shift-up, shift-down                      scroll the lower pane
  m    the mouse, which starts on (the mouse setting): the wheel scrolls the
       list, the lower pane or a pager, whichever it is over, a click chooses a
       group and a double click shows all of it, and a drag selects text, which
       is copied to the clipboard as you let go, as Claude Code's full screen
       does. The text stays highlighted: shift and an arrow move its far end,
       and copy it again; a click anywhere, escape or another key leaves it.
       m turns the mouse off, for the terminal's own selection; holding shift,
       or option in iTerm2, selects as the terminal does while it is on.
  y    copy the lower pane, or in a pager all of it, to the clipboard
  f    files: the files in the job's archive
  c    comment: the draft comment for the run's QA tracker
  i    update records: tell the people who need to know about the group's
       failure, from a box, the recommended way first: a comment on the PR
       under test that the analyst found caused it, explaining it to its
       developer; a comment on the tracker issue it is, that it was seen
       again: only its jobs, as /a/ paths, or with what was found; or, for a
       new failure, a new tracker issue, with a record kept for it. It also
       comments on all the tracker issues the run saw again at once, as I does.
       What it posts is shown first, whole, to post, edit
       or leave: anyone can read it, and it names no agent. The box also keeps
       a record for an issue filed by hand, for the tree to place jobs like
       these in it. A PR needs gh, signed in, or a GitHub token; the tracker
       its API key: the setup page (o) takes both, and says how to make them.
  W    done: wrap the run up, step by step, each asked first: the groups still
       to look into; the changes that wait for approval; a tracker issue for
       each new failure; a comment on each PR an analysis found caused one; a
       comment on each known issue seen again (I); the run's section of the
       RADOS wiki, copied to paste in; "Rados approved" on the PRs (P); and
       the run approved on
       its QA tracker. Where a key is missing, it says how to make one, and
       takes it; or each step can be done by hand, its text copied and its
       page opened.
  I    update every tracker issue the run saw again: once you are happy with
       the issues the groups are placed in, a comment on each, with its jobs as
       /a/ paths, apart from any a PR under test caused, and any resolved after
       the run began, which it saw before their fix. All of them are shown
       first, to post or leave; those posted before are not posted again.
  P    approve the PRs: "Rados approved", with a link to the run's section of
       the RADOS wiki (post that first, w), on each PR the run tests: those
       merged into the build it ran, apart from any an analysis found caused a
       failure. The PRs, and why each is or is not approved, are shown first.
  w    wiki: the run's section for the RADOS team's wiki of reviewed runs, in
       its Textile, to paste there (drafts/wiki.textile has it too)
  p    PRs: the PRs the run tests, from its QA tracker and the PRs merged into
       its branch, each with its title from GitHub, and the groups whose jobs
       ran a file it changes; the line under the title says how many there are
  r    retriage: triage again with the records and the tree as they are now
  j    judge: the judge's verdict on the group                       costs AI
  u    judge all: the judge's verdict on every group it was not      costs AI
       asked about. It decides the groups the tree and the records could
       not, and checks and explains the ones they did; ! marks a group
       whose decision it disagrees with. Then it recommends the groups the
       analyst should learn from, and offers to ask it. It is suggested
       when the review starts.
       The judge works in the background: the screen is yours meanwhile,
       the line above the keys says how far it has got, the group it works
       on turns where the list marks the chosen one, and, while it is
       chosen, the lower pane shows what the judge writes, as it writes it;
       its answers come into the lower pane as it gives them. x stops it.
  l    learn: ask the analyst to learn from the group                costs AI
       The analyst works in the background, as the judge does: while it
       works, the lower pane shows what it is doing when its group is
       selected, and its answer, once given, is in the lower pane.
       What asks an agent while another works in the background waits for
       it: its group is marked … meanwhile, the line above the keys says
       what waits, and x stops it all.
  d    doubt: ask the analyst whether the group's result is right    costs AI
       Say why, and what the job is, if you know: a tracker number, which it
       must then place the job in, or new. Once it is done, a box shows its
       response, whether it agrees with you or not: accept it, which keeps it
       and any change it made, checked again and pushed; dismiss it; or discuss
       it with the analyst, in its own program if it has one, going on from its
       session, with all the evidence. When you leave, it is asked once more for
       its final answer, with any change the discussion called for, and the box
       shows that. d on the group shows a response that waits for you again.
  t    talk: talk with an analyst about the group's first job        costs AI
       chosen from a menu: in its own interactive program, for an agent that
       has one, on this terminal, or here for one with none. It starts
       with the evidence, what the tool found, how to change the records and
       the tree, and what was said about the job before. Ask it for its
       proposal, and it writes it to proposal.json; when you leave the program,
       the proposal is checked, and kept only if it passes. An analyst with no
       program of its own is talked with here, where /propose asks for it.
  o    options: the analyst that learn, doubt and talk ask in this review,
       from a menu: the config's agents, each kind of agent installed here, or
       any agent you type, with its model; or the setup page, as qa_triage
       setup shows it, over the review: each setting of your config file, with
       what it is for, to change, check and save. What you save the review
       takes up at once: the judge, the analyst, their keys, the limits on
       lessons, approval, the editor, the tracker and the mouse; the job files,
       the ceph clone and the library are the triage's, so they are for the
       next review. The setup page changes the analyst for every review. The
       line above the keys says which agents are asked.
  J L  judge or learn again, though asked before                     costs AI
  A    approve: the changes to the records and the tree that wait for your
       approval, one at a time in a box: what failed, as the analyst found it,
       what the change does, and the commit to inspect its code in, with git
       show. Kept, it is made again onto the library as others changed it
       meanwhile, checked again, and pushed to the shared library; if that
       fails, a box says why, and offers to have the analyst make it again
       (costs AI). Rejected, it is set aside. The review asks
       about those that wait as it opens, and about a lesson's change as soon
       as the lesson is done; the line above the keys says how many wait. A
       works while an agent does, and what you keep is checked once it is done.
  x    stop the agent at work in the background, or the checks of its
       answer; while the screen waits for the analyst, as it does to get a
       talk ready, x or escape. Nothing it was doing is kept, and what it
       had cost by then is spent. While the run is being triaged, x stops
       the triage: the jobs triaged by then stay, and the rest, such as one
       whose logs take long to read, are left for the next review.

In the list, a new problem the tree explains is one the tree's questions explained
conclusively, with no record that nearly fits: new for sure, with no judge; it
needs a tracker issue filed (i files it). For a person to look
into: "unmatched" when the judge is needed or was not sure, "judged" when the
judge said it is new.

What an agent said is kept, in review.json in the output directory, and shown in
the lower pane, newest first. The report, the drafts and results.json are written
there as each change to the results is done. j and l show it again, for nothing, once asked; a
discussion continues the analyst's session about the job. Starting the review
again, with the same output directory, starts from all of it.
  ctrl-l   redraw the screen
  q    quit

In a pager: up, down, page up, page down, home, end to scroll, y to copy it all,
q to close.
A question waits for its answer in a box in the middle of the screen: enter
answers and escape cancels; a paste of several lines is one answer; y or n, then
enter, answers yes or no.
The bottom line says what the agents have cost so far.'''
SPINNER = '|/-\\'
# The mark of a group that waits for a turn of the AI, or for the groups before it.
WAITING = '…'
# What stopping an agent means, asked before, and said after.
STOPPING = 'What it has cost so far is spent, and nothing it was doing is kept.'
STOPPED = 'Nothing it was doing is kept, and what it had cost by then is spent.'
# The keys that change the results, or talk with an agent on this terminal: they
# are refused while an agent works in the background. Those that ask an agent in
# the background wait for it instead.
BUSY = [ord(key) for key in 'tr']
# How wide a box with something to read in it is, at most.
DIALOG_WIDTH = 100
# The keys that choose yes and no in a box that asks.
YES_NO = {ord('y'): 0, ord('Y'): 0, ord('n'): 1, ord('N'): 1}
# What the person may do with a change that waits for approval.
# The tracker's pages.
TRACKER = 'https://tracker.ceph.com'
TRACKER_ISSUES = f'{TRACKER}/issues/'
TRACKER_WIKI = f'{TRACKER}/projects/rados/wiki/'
# What can be done with a change that approving did not keep.
REVISE = 'Ask the analyst to make it again, for the library as it is now (costs AI)'
REJECT_NEEDLESS = 'Reject it: the library places the job so already'
SHOW_CHECKS = 'Show every check, as it was done again'
TRY_AGAIN = 'Try again: check it again now'
LATER = 'Later: A asks again'
APPROVING = ('Keep it: check it again, and push it to the shared library',
             'Show it all: what the analyst found, and the code', 'Reject it',
             'Later: A asks again')
# Shift and an arrow: they move the far end of the text selected.
SHIFTED = (curses.KEY_SLEFT, curses.KEY_SRIGHT, curses.KEY_SR, curses.KEY_SF)
# How many lines the mouse wheel scrolls.
WHEEL = 3
# The fewest rows of the list, as the line under it is dragged.
LIST_ROWS = 3
# Where the screen's layout is kept, in the cache: the rows of the list, and the
# columns of watch's pane, as the person dragged them.
LAYOUT = 'layout.json'


def layout(cache, **changed):
    """The screen's layout, as kept in cache, with changed kept too; {} for none."""
    path = cache and os.path.join(cache, LAYOUT)
    try:
        with open(path) as f:
            kept = json.load(f)
    except (OSError, TypeError, ValueError):
        kept = {}
    if changed and path:
        kept.update(changed)
        with contextlib.suppress(OSError):
            os.makedirs(cache, exist_ok=True)
            with open(path + '.new', 'w') as f:
                json.dump(kept, f)
            os.replace(path + '.new', path)
    return kept
# The terminal's reports of the mouse, asked of it directly, as Claude Code's full
# screen does: a button pressed and let go, and the mouse's motion while one is held
# (1002), in SGR's form (1006), which tells pressing from letting go, and the wheel
# from motion, as the older form that macOS's ncurses reads does not. With them, the
# screen selects the text a drag goes over itself, and copies it as the button is let
# go, as a terminal would.
MOUSE_ON, MOUSE_OFF = '\x1b[?1002h\x1b[?1006h', '\x1b[?1006l\x1b[?1002l'
SGR = re.compile(r'\[<(\d+);(\d+);(\d+)([Mm])')
# What ncurses reports of the mouse, where it reads SGR's form itself.
DECODED = curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION
# Two clicks this many seconds apart, on the same row, are a double click.
DOUBLE = 0.4


@dataclass
class Pointer:
    """A report of the mouse's: its button (0 the left), where it is, whether it was
    pressed or let go, whether it moved with the button held, and the wheel: -1 up,
    1 down, 0 not."""
    button: int
    y: int
    x: int
    pressed: bool
    motion: bool = False
    wheel: int = 0


def sgr(text):
    """The Pointer of a report in SGR's form, after its escape: [<b;x;yM; or None."""
    found = SGR.fullmatch(text)
    if not found:
        return None
    b, x, y = (int(v) for v in found.groups()[:3])
    return Pointer(b & 3, y - 1, x - 1, found.group(4) == 'M', bool(b & 32),
                   (1 if b & 1 else -1) if b & 64 else 0)


def decoded():
    """The Pointer of what ncurses read of the mouse itself, or None."""
    try:
        _, x, y, _, state = curses.getmouse()
    except curses.error:
        return None
    let_go = curses.BUTTON1_RELEASED | curses.BUTTON1_CLICKED
    for bit, pointer in ((curses.BUTTON4_PRESSED, Pointer(0, y, x, True, wheel=-1)),
                         (getattr(curses, 'BUTTON5_PRESSED', 0), Pointer(0, y, x, True, wheel=1)),
                         (curses.BUTTON1_PRESSED, Pointer(0, y, x, True)),
                         (let_go, Pointer(0, y, x, False)),
                         (curses.REPORT_MOUSE_POSITION, Pointer(0, y, x, True, motion=True))):
        if state & bit:
            return pointer
    return None


def terminal(text):
    """Say text to the terminal itself, past curses: an escape sequence."""
    with contextlib.suppress(OSError):
        with open('/dev/tty', 'w') as tty:
            tty.write(text)


# Where what is copied is written too, for a terminal that does not take OSC 52.
COPIED = os.path.join('~', '.cache', 'qa_triage', 'copied.txt')


def copy(text):
    """Put text on the clipboard: with the system's command for it, or else by
    asking the terminal to (OSC 52, which iTerm2 and others take); how, or None.
    It is written to COPIED too, which a terminal that takes no OSC 52 needs."""
    kept = None
    try:
        kept = os.path.expanduser(COPIED)
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        with open(kept, 'w') as f:
            f.write(text)
    except OSError:
        kept = None
    for argv in (['pbcopy'], ['wl-copy'], ['xclip', '-selection', 'clipboard'],
                 ['xsel', '--clipboard', '--input']):
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, input=text.encode(), check=True, timeout=10)
                return argv[0]
            except (OSError, subprocess.SubprocessError):
                continue
    try:
        with open('/dev/tty', 'w') as tty:
            tty.write('\x1b]52;c;' + base64.b64encode(text.encode()).decode() + '\x07')
        return 'the terminal, if it takes OSC 52' + (f'; it is in {COPIED} too' if kept else '')
    except OSError:
        return None


class Model:
    """What the screen shows, without curses: the rows of the list, the details of
    a group, and the output of a command."""

    def __init__(self, review):
        self.review = review
        self.selected = 0
        self.details = {}

    def groups(self):
        return report.groups(self.review.results)

    def rows(self):
        """[(group number or None for a heading, text)] for the list."""
        titles, rows, last = dict(report.SECTIONS), [], None
        for number, (section, rs) in enumerate(self.groups(), 1):
            if section != last:
                rows.append((None, titles[section]))
                last = section
            r, size = rs[0], f'{len(rs)} job{"" if len(rs) == 1 else "s"}'
            # The judge disagrees with what the tree and the records decided.
            mark = '!' if r.opinion and r.opinion.get('agrees') is False else ' '
            if section == 'explained':
                # All of them are new, for sure: the heading says so.
                text = f'{size:<8} {report.what(r)}'
            elif section == 'other':
                text = (f'{r.verdict:<9} {"judged" if r.sure else "unmatched":<9} {size:<8} '
                        f'{report.what(r)}')
            else:
                text = f'{r.problem.label:>9}  {size:<8} {r.problem.title}'
            rows.append((number, f'{number:>3}{mark} {text}'))
        return rows

    def command(self, line, out=None):
        """Do a review command; its output, as lines. out, if given, gets the output
        as the command goes, for the screen to show while it works."""
        out, err = out or io.StringIO(), io.StringIO()
        self.review.stdout = out
        with contextlib.redirect_stderr(err):
            self.review.onecmd(line)
        if not line.startswith(('show', 'grep', 'open', 'files', 'comment', 'cost')):
            self.details.clear()
        return (out.getvalue() + err.getvalue()).rstrip('\n').split('\n')

    def call(self, do, *args):
        """do(*args), a method of the review: what it said, as lines, and the error it
        raised, if it did, as an error: line."""
        out = io.StringIO()
        self.review.stdout = out
        try:
            do(*args)
        except (ValueError, OSError) as e:
            print(f'error: {e}', file=out)
        self.details.clear()
        return out.getvalue().rstrip('\n').split('\n')

    def detail(self, number):
        if number not in self.details:
            self.details[number] = self.command(f'show {number}')
        return self.details[number]

    def status(self):
        return self.review.spent()


def wrap(lines, width):
    """lines, each one cut into lines of at most width, keeping its indent."""
    out = []
    for line in lines:
        indent = len(line) - len(line.lstrip())
        out += textwrap.wrap(line.strip(), width, initial_indent=' ' * indent,
                             subsequent_indent=' ' * (indent + 2),
                             break_on_hyphens=False) or ['']
    return out


def not_kept_by_job(output):
    """[(job, lines)] of what approve said of each change it did not keep: why, with
    the checks that fail, or what clashes, under it."""
    found, under = [], False
    for line in output:
        if f': {approve.NOT_KEPT}, ' in line:
            found.append((line.split(f': {approve.NOT_KEPT}, ')[0], [line]))
            under = True
        elif under and line.startswith('    '):
            found[-1][1].append(line)
        else:
            under = False
    return found


def not_kept(output):
    """The lines of what approve said that tell why the changes it did not keep were
    not: each change's line, and the failing checks under it."""
    return [line for n, (_, lines) in enumerate(not_kept_by_job(output))
            for line in ([''] if n else []) + lines]


def parse_draft(text, subject):
    """(subject, text) of a draft as the person edited it: with subject, its first
    line is "Subject: ..." and the rest is the text."""
    if not subject:
        return None, text.strip('\n')
    first, _, rest = text.lstrip('\n').partition('\n')
    return first.split(':', 1)[1].strip() if first.startswith('Subject:') else first.strip(), \
        rest.strip('\n')


class Screen:
    def __init__(self, stdscr, model):
        self.stdscr, self.model = stdscr, model
        self.top = 0
        # A line for the bottom of the screen, until the next key.
        self.flash = None
        # The lower pane: whether the keys scroll it, how far it is scrolled, and
        # for which group's details.
        self.focus, self.pane_top, self.pane_for, self.pane_room = 'list', 0, None, 1
        # The judge, working in the background, if it is; and the spinner's turn.
        self.bg, self.tick = None, 0
        # How many results the list showed, and the job of the group chosen then: the
        # choice stays with it as the triage adds groups.
        self.seen, self.held = 0, None
        # Where the list's rows and the lower pane are, for the mouse.
        self.rows_at, self.pane_at = {}, (0, 0)
        # The mouse: its last report; where a drag began and has got to; the text
        # it selected, (anchor, extent), kept highlighted until a click or a key
        # leaves it; the last click, (time, row), for a double click; and what the
        # screen shows, by row, for the text a drag selects.
        self.event, self.anchor, self.extent, self.kept, self.clicked, self.shadow = \
            None, None, None, None, None, {}
        # The changes that wait for approval that the person was asked about: each is
        # asked about once, as the review opens or as its lesson is done.
        self.offered = set()
        # What to do in the background once what is done there now is done, in turn:
        # [{title, line, kind, load, job}], job the first job of the group it is for,
        # if one.
        self.queued = []
        curses.curs_set(0)
        settings = getattr(getattr(model, 'review', None), 'args', None)
        self.settings = getattr(settings, 'settings', None) or {}
        # On, by the mouse setting: the wheel scrolls, a click chooses and a drag
        # selects and copies; m turns it off, for the terminal's own selection.
        self.mouse_on = False
        if model is not None:
            self.set_mouse(bool(self.settings.get('mouse', True)))
        self.colours = {}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for n, (name, fg, bg) in enumerate((('head', curses.COLOR_WHITE, curses.COLOR_BLUE),
                                                ('section', curses.COLOR_CYAN, -1),
                                                ('sure', curses.COLOR_GREEN, -1),
                                                ('unsure', curses.COLOR_YELLOW, -1),
                                                ('foot', curses.COLOR_BLACK, curses.COLOR_CYAN),
                                                ('cost', curses.COLOR_MAGENTA, -1),
                                                ('ai', curses.COLOR_BLACK, curses.COLOR_YELLOW)),
                                               1):
                curses.init_pair(n, fg, bg)
                self.colours[name] = curses.color_pair(n)

    def colour(self, name):
        return self.colours.get(name, curses.A_NORMAL)

    def put(self, y, x, text, attr=curses.A_NORMAL):
        height, width = self.stdscr.getmaxyx()
        if 0 <= y < height and x < width:
            room = width - x - (y == height - 1)
            with contextlib.suppress(curses.error):
                self.stdscr.addnstr(y, x, text, room, attr)
            text = text[:room]
            row = self.shadow.get(y, '').ljust(x)
            self.shadow[y] = row[:x] + text + row[x + len(text):]

    def clear(self):
        self.stdscr.erase()
        self.shadow = {}

    def refresh(self):
        """Show what was drawn, with the text a drag is selecting, or selected, in
        reverse."""
        span = (self.anchor, self.extent) if self.anchor and self.extent else self.kept
        if span:
            (y0, x0), (y1, x1) = sorted(span)
            width = self.stdscr.getmaxyx()[1]
            for y in range(y0, y1 + 1):
                start, end = (x0 if y == y0 else 0), (x1 + 1 if y == y1 else width)
                with contextlib.suppress(curses.error):
                    self.stdscr.chgat(y, start, max(0, min(end, width) - start),
                                      curses.A_REVERSE)
        self.stdscr.refresh()

    # The mouse.

    def set_mouse(self, on):
        """Have the terminal report the mouse, or not, for its own selection."""
        self.mouse_on, self.anchor, self.extent, self.kept = on, None, None, None
        # Where ncurses reads SGR's form itself, it reads the reports; else the
        # screen does, from the keys.
        known = (curses.tigetstr('kmous') or b'').startswith(b'\x1b[<')
        curses.mousemask(DECODED if on and known else 0)
        if known:
            curses.mouseinterval(0)
        terminal(MOUSE_ON if on else MOUSE_OFF)

    def key(self, window=None):
        """The next key, as window.getch() gives it; curses.KEY_MOUSE for a report of
        the mouse's, which is then in self.event."""
        window = window or self.stdscr
        key = window.getch()
        if key == curses.KEY_MOUSE:
            self.event = self.relative(decoded())
            return key
        if key != 27 or not self.mouse_on:
            return key
        # Escape alone, or the start of a report: ESC [ < b ; x ; y M.
        came = []
        window.timeout(30)
        try:
            while len(came) < 24:
                c = window.getch()
                if c == -1:
                    break
                came.append(c)
                if len(came) == 1 and c != ord('[') or len(came) == 2 and c != ord('<') or \
                        len(came) > 2 and c in (ord('M'), ord('m')):
                    break
        finally:
            window.timeout(-1)
        self.event = self.relative(sgr(''.join(chr(c) for c in came if 0 <= c < 0x110000)))
        if self.event:
            return curses.KEY_MOUSE
        for c in reversed(came):
            curses.ungetch(c)
        return 27

    def relative(self, event):
        """A report of the mouse's, in the rows and columns of the screen's window,
        which may be a part of the terminal, as a run's is beside watch's pane."""
        oy, ox = self.stdscr.getbegyx()
        return replace(event, y=event.y - oy, x=event.x - ox) if event and (oy or ox) else \
            event

    def resize(self):
        """Take the mouse's report if it drags the line under the list, which moves
        it: True if it did."""
        e = self.event
        if not e or e.wheel or e.button:
            return False
        if e.pressed and not e.motion and e.y == getattr(self, 'divider_at', None):
            self.event, self.dragging = None, True
            return True
        if not getattr(self, 'dragging', False):
            return False
        self.event, self.list_rows = None, max(LIST_ROWS, e.y - 2)
        if not e.pressed:
            self.dragging = False
            layout(self.cache(), list_rows=self.list_rows)
        return True

    def cache(self):
        """The cache directory, where the screen's layout is kept."""
        return getattr(getattr(getattr(self.model, 'review', None), 'args', None), 'cache', None)

    def selected(self, anchor, extent):
        """The text the screen shows from anchor to extent, (row, column) each."""
        (y0, x0), (y1, x1) = sorted([anchor, extent])
        lines = []
        for y in range(y0, y1 + 1):
            row = self.shadow.get(y, '')
            lines.append(row[x0 if y == y0 else 0:x1 + 1 if y == y1 else len(row)].rstrip())
        return '\n'.join(lines)

    # The main screen.

    def footer(self, width, free=FREE_KEYS, costly=AI_KEYS):
        """The keys along the bottom, as lines of ([(text, attribute)], the rest's
        attribute), each key in bold, then what it does: the free ones, on as few
        lines as they fit in; then, on a line of their own, those that cost."""
        def lay(hint, keys, plain):
            lines, line, used = [], [(hint, plain | curses.A_BOLD)], len(hint)
            for key, what in keys:
                size = len(key) + len(what) + 3
                if used + size + 1 > width - 1 and used > len(hint):
                    lines.append(line)
                    line, used = [(' ' * len(hint), plain)], len(hint)
                if used > len(hint):
                    line.append(('│', plain))
                    used += 1
                line += [(f' {key}', plain | curses.A_BOLD), (f' {what} ', plain)]
                used += size
            return [(pieces, plain) for pieces in lines + [line]]

        foot = self.colour('foot')
        keys = [(key, f'mouse {"on" if self.mouse_on else "off"}' if key == 'm' else what)
                for key, what in free]
        return lay('', keys, foot) + (lay(' Costs AI: ', costly,
                                          self.colours.get('ai', curses.A_REVERSE))
                                      if costly else [])

    def put_pieces(self, y, pieces, fill):
        """A line of (text, attribute) pieces, the rest of it in fill."""
        self.put(y, 0, ' ' * self.stdscr.getmaxyx()[1], fill)
        x = 0
        for text, attr in pieces:
            self.put(y, x, text, attr)
            x += len(text)

    def draw(self, busy=None, live=None):
        self.clear()
        height, width = self.stdscr.getmaxyx()
        review = self.model.review
        self.put(0, 0, f' qa_triage review  {review.session.run} '.ljust(width),
                 self.colour('head') | curses.A_BOLD)
        tracker = [h for h in review.session.header if h.startswith('QA tracker')]
        results = review.results
        matched = sum(1 for r in results if r.sure)
        skipped = getattr(review.session, 'skipped', [])
        if getattr(review.session, 'triaged', True):
            counts = (f' {len(results) + len(skipped)} failed or dead jobs, {matched} matched, '
                      f'{len(results) - matched} unmatched' +
                      (f', {len(skipped)} not triaged (the triage was stopped)' if skipped
                       else '') + '.  ')
        else:
            counts = (f' {len(results)} of {len(review.session.jobs) or "the"} failed or dead '
                      f'jobs triaged so far, {matched} matched.  ')
        prs, number = review.under_test(), review.qa_tracker()
        self.put(1, 0, counts + (f'QA tracker {number}, ' if number else '') +
                 (f'{len(prs)} PR{"" if len(prs) == 1 else "s"} under test: p lists them'
                  if prs else tracker[0] if tracker else ''))
        foot = self.footer(width)
        status = height - len(foot) - 1
        if len(results) != self.seen:
            # Groups the triage added may come before the one chosen: it stays chosen.
            self.seen = len(results)
            self.model.details.clear()
            if self.held:
                self.select(self.held)
        rows, groups = self.model.rows(), self.model.groups()
        numbers = [n for n, _ in rows if n]
        self.model.selected = min(self.model.selected, len(numbers) - 1) if numbers else 0
        chosen = numbers[self.model.selected] if numbers else None
        self.held = groups[chosen - 1][1][0].job.id if chosen else None
        list_rows = getattr(self, 'list_rows', None) or layout(self.cache()).get('list_rows')
        list_height = max(LIST_ROWS, min(list_rows or (status - 3) // 2, status - 6))
        at = next((i for i, (n, _) in enumerate(rows) if n == chosen), 0)
        if at < self.top:
            self.top = at
        elif at >= self.top + list_height:
            self.top = at - list_height + 1
        self.top = max(0, min(self.top, len(rows) - list_height))
        self.rows_at = {}
        working, waiting = self.busy(), self.waiting_for_ai() | self.queued_groups()
        for i, (number, text) in enumerate(rows[self.top:self.top + list_height]):
            y = 2 + i
            self.rows_at[y] = number
            # Each group an agent works on turns, and each that waits for one is marked
            # so, where the chosen one is marked.
            marker = WAITING if number and number in waiting else \
                SPINNER[self.tick % 4] if number and number in working else \
                '>' if number == chosen else ' '
            if number is None:
                self.put(y, 1, text, self.colour('section') | curses.A_BOLD)
            elif number == chosen and self.focus == 'pane':
                # The keys are the lower pane's: the list keeps only its marker.
                first = groups[number - 1][1][0]
                self.put(y, 0, f'{marker}{text}', self.colour('sure' if first.sure else
                                                             'unsure') | curses.A_BOLD)
            elif number == chosen:
                self.put(y, 0, f'{marker}{text}'.ljust(width), curses.A_REVERSE)
            else:
                first = groups[number - 1][1][0]
                self.put(y, 0, f'{marker}{text}', self.colour('sure' if first.sure else
                                                             'unsure'))
        y = self.divider_at = 2 + list_height
        room = max(1, status - y - 1)
        self.pane_at, self.pane_room = (y + 1, y + 1 + room), room
        self.put(y, 0, '─' * width, self.colour('section'))
        title = ' What the agent is doing '
        if live is None:
            live = self.at_work(chosen)
            if live is not None:
                written = self.written(chosen)
                title = self.working_on(chosen, bool(written))
                live = written or live
                if not any(line.strip() for line in live):
                    live = ['Nothing from it yet: the evidence it is shown is read from the '
                            'job\'s logs first, which takes a while for big ones.']
        if live is None and chosen is None and self.bg:
            # Nothing to choose yet: what the triage is doing.
            live = self.bg['out'].getvalue().rstrip('\n').split('\n')
            title = f' {self.bg["title"]}: what it is doing '
        if live is not None:
            # What the agent is doing: its latest steps, as many as fit.
            self.put(y, 2, title, self.colour('section') | curses.A_BOLD)
            for i, line in enumerate(wrap(live, width - 2)[-room:]):
                self.put(y + 1 + i, 1, line)
        elif chosen:
            detail = self.model.detail(chosen)
            if self.pane_for != (chosen, id(detail)):
                self.pane_for, self.pane_top = (chosen, id(detail)), 0
            # Above it, what of its own waits for the background, if anything does.
            waits = self.waits(chosen)
            lines = wrap(([waits, ''] if waits else []) + list(detail), width - 2)
            self.pane_top = max(0, min(self.pane_top, len(lines) - room))
            for i, line in enumerate(lines[self.pane_top:self.pane_top + room]):
                self.put(y + 1 + i, 1, line)
            where = (f' lines {self.pane_top + 1}-{min(self.pane_top + room, len(lines))} '
                     f'of {len(lines)}: ')
            if self.focus == 'pane':
                # The keys are this pane's: its top line is the one highlighted.
                self.put(y, 0, f'>{where}the keys and the wheel scroll it; tab goes back to '
                               'the list '.ljust(width), curses.A_REVERSE | curses.A_BOLD)
            elif len(lines) > room:
                self.put(y, 2, where + 'tab or the wheel scrolls it ',
                         self.colour('section') | curses.A_BOLD)
        analyst, judge = review.analyst, review.judge
        agents = (f' Analyst: {readable(analyst.name) if analyst else "none"}   Judge: '
                  f'{readable(judge.name) if judge else "none"}')
        cost = f' {self.model.status()} '
        self.tick += 1
        if self.bg and self.bg['kind'] == 'triage':
            done, total = self.progress()
            seconds = int(time.time() - self.bg['started'])
            slow = https_base(getattr(getattr(review, 'args', None), 'archive', None))
            agents = (f' {SPINNER[self.tick % 4]} ' +
                      (f'Triaging, with the tree and the records, no AI: {done} of {total} jobs'
                       if total else 'Finding the run\'s failed jobs') +
                      f', {seconds // 60}:{seconds % 60:02}; ' +
                      ('their logs downloaded over https, which is slow'
                       if slow and total else 'they come into the list as they are decided') +
                      ('; x stops it' if total else ''))
        elif self.bg:
            done, total = self.progress()
            seconds = int(time.time() - self.bg['started'])
            waiting = len(self.waiting_for_ai())
            agents = (f' {SPINNER[self.tick % 4]} {self.bg["title"]}' +
                      (f': {done} of {total}' if total else '') +
                      (f', {waiting} waiting ({WAITING}) for the AI' if waiting else '') +
                      f', {seconds // 60}:{seconds % 60:02}, ' +
                      ('stopping: it stops at its next step' if self.bg.get('stopping') else
                       'in the background; x stops it'))
        if self.bg and self.queued:
            # And what waits for it.
            then = self.queued[0]['title']
            agents += (f'; then ({WAITING}) {then[0].lower()}{then[1:]}' +
                       (f', and {len(self.queued) - 1} more' if len(self.queued) > 1 else ''))
        waiting = self.waiting_count()
        if not (self.bg or waiting) and len(agents) + len(CHOOSE_AI) + len(cost) < width:
            agents += CHOOSE_AI
        if waiting:
            # Said while an agent works too: A asks about them at any time.
            agents += (f'   [A] {waiting} change{"" if waiting == 1 else "s"} to the tree '
                       f'wait{"s" if waiting == 1 else ""} for your approval')
        said = busy or self.flash
        if said:
            # What is being done, or was just done, over the status: the keys stay.
            agents = f' {said}'
        self.put(status, 0, agents, curses.A_BOLD if self.bg or said else curses.A_NORMAL)
        # The cost after them, on the same line if it fits.
        self.put(status, max(len(agents) + 1, width - len(cost) - 1), cost, self.colour('cost'))
        for i, (pieces, fill) in enumerate(foot):
            self.put_pieces(status + 1 + i, pieces, fill)
        self.refresh()

    def mouse(self):
        """What the mouse did, from its report: ('up' or 'down' for the wheel,
        'click' or 'double' as the button is let go, 'drag' while it selects text,
        'copied' when the drag ends, or None; the row it was on). A drag selects the
        text it goes over, which is copied as the button is let go, as the terminal
        would."""
        e, self.event = self.event, None
        if not e:
            return None, -1
        if e.wheel:
            return ('up' if e.wheel < 0 else 'down'), e.y
        if e.button:
            return None, e.y
        if e.motion:
            if not self.anchor:
                return None, e.y
            self.extent = (e.y, e.x)
            return 'drag', e.y
        if e.pressed:
            # A click anywhere leaves what was selected.
            self.anchor, self.extent, self.kept = (e.y, e.x), None, None
            return None, e.y
        anchor, extent, self.anchor, self.extent = self.anchor, self.extent, None, None
        if anchor and extent and extent != anchor:
            self.kept = (anchor, extent)
            self.copy_kept()
            return 'copied', e.y
        now = time.time()
        double = bool(self.clicked) and now - self.clicked[0] < DOUBLE and \
            self.clicked[1] == e.y
        self.clicked = None if double else (now, e.y)
        return ('double' if double else 'click'), e.y

    def copy_kept(self):
        """Copy the text selected, saying so."""
        text = self.selected(*self.kept)
        how = copy(text) if text.strip() else None
        self.flash = (f'Copied {len(text)} characters, with {how}.' if how else
                      'Nothing was copied.' if not text.strip() else
                      f'Could not copy to the clipboard here: it is in {COPIED}.')

    def selecting(self, key):
        """Take a key for the text selected: shift and an arrow move its far end, as
        in a terminal or Claude Code, and copy it again; True if the key did that.
        Any other key but the mouse's leaves it."""
        if key in SHIFTED and self.kept:
            height, width = self.stdscr.getmaxyx()
            anchor, (y, x) = self.kept
            if key == curses.KEY_SLEFT:
                y, x = (y, x - 1) if x else (y - 1, width - 1)
            elif key == curses.KEY_SRIGHT:
                y, x = (y, x + 1) if x < width - 1 else (y + 1, 0)
            else:
                y += -1 if key == curses.KEY_SR else 1
            self.kept = (anchor, (max(0, min(height - 1, y)), x))
            self.copy_kept()
            return True
        if key not in (-1, curses.KEY_MOUSE, curses.KEY_RESIZE):
            self.kept = None
        return False

    def scroll_pane(self, by):
        self.pane_top = max(0, self.pane_top + by)

    def chosen(self):
        rows = [n for n, _ in self.model.rows() if n]
        return rows[self.model.selected] if rows else None

    # Pagers, questions and work.

    def pager(self, title, lines):
        height, width = self.stdscr.getmaxyx()
        whole, lines = list(lines), wrap(lines, width - 2)
        top, said = 0, ''
        while True:
            height, width = self.stdscr.getmaxyx()
            self.clear()
            self.put(0, 0, f' {title} '.ljust(width), self.colour('head') | curses.A_BOLD)
            for i, line in enumerate(lines[top:top + height - 2]):
                self.put(1 + i, 1, line)
            self.put(height - 1, 0, f' {top + 1}-{min(top + height - 2, len(lines))} of '
                                    f'{len(lines)}   ↑↓ pgup pgdn home end   y copy   q close'
                                    f'{"   " + said if said else ""} '
                     .ljust(width), self.colour('foot'))
            self.refresh()
            key = self.key()
            said = ''
            page = max(1, height - 3)
            selected = self.kept
            if self.selecting(key):
                said, self.flash = self.flash, None
                continue
            if key == 27 and selected:
                continue
            if key in (ord('q'), 27, 10, 13, curses.KEY_ENTER):
                return
            if key == ord('y'):
                how = copy('\n'.join(whole))
                said = f'copied, with {how}' if how else f'could not copy it here: it is in {COPIED}'
                continue
            if key == curses.KEY_MOUSE:
                what, _ = self.mouse()
                top += {'up': -WHEEL, 'down': WHEEL}.get(what, 0)
                if what == 'copied':
                    said, self.flash = self.flash, None
            top = {curses.KEY_DOWN: top + 1, curses.KEY_UP: top - 1,
                   curses.KEY_NPAGE: top + page, ord(' '): top + page,
                   curses.KEY_PPAGE: top - page, curses.KEY_HOME: 0,
                   curses.KEY_END: len(lines)}.get(key, top)
            top = max(0, min(top, len(lines) - (height - 2)))

    def waiting(self, window=None):
        """A key that came already, as with the rest of a paste, or None."""
        window = window or self.stdscr
        window.nodelay(True)
        try:
            return window.get_wch()
        except curses.error:
            return None
        finally:
            window.nodelay(False)

    def typed(self, key, text, came, window=None):
        """(text, done) after key, of a line being typed: done is True once enter
        ends it, and None for escape. The lines of a paste are one answer, and the
        escape sequences around it are not escape: so that none of it is taken for
        keys afterwards. A key that came with enter is kept in came."""
        if key in ('\n', '\r', curses.KEY_ENTER):
            more = self.waiting(window)
            if more is None:
                return text, True
            came.append(more)
            return text + (' ' if text and not text.endswith(' ') else ''), False
        if key == '\x1b':
            more = self.waiting(window)
            if more is None:
                return text, None
            # ESC [ ... final, ESC O x, or alt and a key.
            if more == '[':
                while (more := self.waiting(window)) is not None and \
                        not (isinstance(more, str) and '@' <= more <= '~'):
                    pass
            elif more == 'O':
                self.waiting(window)
            return text, False
        if key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
            return text[:-1], False
        if isinstance(key, str) and key.isprintable():
            return text + key, False
        return text, False

    def ask_line(self, question):
        """A line the person types on the bottom line, as a conversation's are, or
        None if they press escape."""
        height, width = self.stdscr.getmaxyx()
        self.put(height - 1, 0, f' {question} '.ljust(width), self.colour('foot'))
        self.stdscr.refresh()
        curses.curs_set(1)
        text, x0, came = '', min(len(question) + 2, width - 10), []
        try:
            while True:
                self.put(height - 1, x0, text[-(width - x0 - 2):].ljust(width - x0 - 1),
                         self.colour('foot'))
                self.stdscr.move(height - 1, min(x0 + len(text), width - 2))
                text, done = self.typed(came.pop() if came else self.stdscr.get_wch(), text,
                                        came)
                if done or done is None:
                    return text if done else None
        finally:
            curses.curs_set(0)

    def dialog(self, h, w):
        """A box of h rows and w columns, at most, in the middle of the screen, over
        it, for a question that waits for its answer: (box, its top row, its left
        column), on the screen."""
        height, width = self.stdscr.getmaxyx()
        h, w = max(3, min(height - 2, h)), max(10, min(width - 2, w))
        y0, x0 = max(0, (height - h) // 2), max(0, (width - w) // 2)
        oy, ox = self.stdscr.getbegyx()
        box = curses.newwin(h, w, oy + y0, ox + x0)
        box.keypad(True)
        return box, y0, x0

    def ask(self, question, secret=False):
        """A line the person types, in a box in the middle of the screen, or None if
        they press escape. A secret is shown as dots."""
        w = min(self.stdscr.getmaxyx()[1] - 2, DIALOG_WIDTH)
        heading = wrap([question], w - 4)
        box, _, _ = self.dialog(len(heading) + 5, w)
        h, w = box.getmaxyx()
        heading, field, room = heading[:max(1, h - 5)], min(len(heading), h - 5) + 2, w - 6
        curses.curs_set(1)
        text, came = '', []
        try:
            while True:
                shown = ('•' * len(text) if secret else text)[-room:]
                box.erase()
                box.border()
                with contextlib.suppress(curses.error):
                    for i, line in enumerate(heading):
                        box.addnstr(1 + i, 2, line, w - 4, curses.A_BOLD)
                    box.addnstr(field, 2, f' {shown.ljust(room)} ', w - 4, curses.A_REVERSE)
                    box.addnstr(h - 1, 2, ' enter answers, escape cancels ', w - 4)
                    box.move(field, 3 + len(shown))
                box.refresh()
                try:
                    key = came.pop() if came else box.get_wch()
                except curses.error:
                    continue
                text, done = self.typed(key, text, came, box)
                if done or done is None:
                    return text if done else None
        finally:
            curses.curs_set(0)
            del box
            self.stdscr.touchwin()
            self.stdscr.refresh()

    def choose(self, title, options, chosen=0, body=(), keys=None, hint=None):
        """The number of the option the person picks, with the arrows and enter or
        with the mouse, from a box in the middle of the screen, below title and body,
        if given: lines that say what the choice is about; None for escape. keys,
        {key: option}, are keys that choose an option, for enter to answer with."""
        height, width = self.stdscr.getmaxyx()
        w = min(width - 2, max(min(len(title) + 6, DIALOG_WIDTH),
                               *(len(o) + 6 for o in options), DIALOG_WIDTH if body else 0))
        heading = wrap([title], w - 4)
        text = (wrap(list(body), w - 4) if body else []) + ['']
        text = text[:max(0, height - len(heading) - len(options) - 4)]
        box, y0, x0 = self.dialog(len(heading) + len(text) + len(options) + 2, w)
        h, w = box.getmaxyx()
        # Where the options begin, below the title and the text.
        first = 1 + len(heading) + len(text)
        shown = options[:max(1, h - 1 - first)]
        try:
            while True:
                box.erase()
                box.border()
                with contextlib.suppress(curses.error):
                    for i, line in enumerate(heading):
                        box.addnstr(1 + i, 2, line, w - 4, curses.A_BOLD)
                    for i, line in enumerate(text):
                        box.addnstr(1 + len(heading) + i, 2, line, w - 4)
                    for i, option in enumerate(shown):
                        box.addnstr(first + i, 2, f' {option} '.ljust(w - 4), w - 4,
                                    curses.A_REVERSE if i == chosen else curses.A_NORMAL)
                    box.addnstr(h - 1, 2, hint or ' ↑↓ or the mouse, enter chooses, escape '
                                                  'cancels ', w - 4)
                box.refresh()
                key = self.key(box)
                if key in (keys or {}):
                    chosen = keys[key]
                    continue
                if key in (27, ord('q')):
                    return None
                if key in (10, 13, curses.KEY_ENTER):
                    return chosen
                if key == curses.KEY_MOUSE:
                    what, y = self.mouse()
                    if what in ('click', 'double') and \
                            y0 + first <= y < y0 + first + len(shown):
                        return y - y0 - first
                    key = {'up': curses.KEY_UP, 'down': curses.KEY_DOWN}.get(what)
                if key == curses.KEY_UP:
                    chosen = (chosen - 1) % len(shown)
                elif key == curses.KEY_DOWN:
                    chosen = (chosen + 1) % len(shown)
        finally:
            del box
            self.stdscr.touchwin()
            # What was under it, at once: another box may come over the screen next.
            self.stdscr.refresh()

    def pick_analyst(self, title):
        """An analyst's name, from a menu of the ones to choose from, the one asked
        now first chosen; None if the person cancels."""
        review = self.model.review
        found = review.described()
        width = max(len(name) for name, _, _ in found)
        now = review.analyst.name if review.analyst else None
        options = [f'{name:<{width}}  {program or "talks here, in the review"}' +
                   ('  (now)' if made == now else '') for name, made, program in found]
        options.append('another: type an agent, with its model, as --analyst takes')
        current = next((i for i, (_, made, _) in enumerate(found) if made == now), 0)
        picked = self.choose(title, options, current)
        if picked is None:
            return None
        if picked == len(found):
            name = self.ask('The agent:')
            if not name or not name.strip():
                return None
            self.flash = ' '.join(self.model.command(f'analyst {name.strip()}'))
            return name.strip()
        name, made, _ = found[picked]
        if made != now:
            self.flash = ' '.join(self.model.command(f'analyst {name}'))
        return name

    def confirm(self, question):
        """Yes or no, from a box in the middle of the screen: no, unless the person
        says yes."""
        picked = self.choose(question, ['Yes', 'No'], 1, keys=YES_NO,
                             hint=' y or n, or ↑↓, then enter; escape is no ')
        # Keys typed ahead are not taken for commands.
        curses.flushinp()
        return picked == 0

    def asking(self, question):
        """confirm, for something that costs: saying so if it is not asked."""
        if self.confirm(question):
            return True
        self.flash = 'Not asked: nothing was spent.'
        return False

    def select(self, job_id):
        """Choose the group that job_id is in now."""
        for i, (_, rs) in enumerate(self.model.groups()):
            if any(r.job.id == job_id for r in rs):
                self.model.selected = i
                return

    def wait(self, title, line, render, hint, stops=True):
        """Do a command that asks an agent, saying how long it has taken and
        rendering its steps as they come: render(busy line, steps). Its output.
        Unless not stops, x or escape stops the agent, once the person says so."""
        review = self.model.review
        found, live = [], io.StringIO()
        if stops:
            review.go()
            hint += '; x stops it'
        worker = threading.Thread(target=lambda: found.append(self.model.command(line, live)),
                                  daemon=True)
        started = time.time()
        worker.start()
        try:
            tick = 0
            while worker.is_alive():
                # Each time: a question asked meanwhile waits for its answer.
                self.stdscr.timeout(250)
                seconds = int(time.time() - started)
                render(f'{SPINNER[tick % 4]} {title}: {seconds // 60}:{seconds % 60:02} - '
                       f'{hint}', live.getvalue().rstrip('\n').split('\n'))
                tick += 1
                if self.key() in (27, ord('x')) and stops and worker.is_alive() and \
                        self.confirm(f'Stop {title[0].lower()}{title[1:]}? {STOPPING}'):
                    review.stop()
        finally:
            self.stdscr.timeout(-1)
            curses.flushinp()
            if stops:
                review.go()
        return found[0] if found else ['No output.']

    def first(self, number):
        return self.model.groups()[number - 1][1][0].job.id

    def waiting_count(self):
        """How many changes wait for the person's approval."""
        args = getattr(getattr(self.model, 'review', None), 'args', None)
        cache = getattr(args, 'cache', None)
        return approve.count_waiting(cache) if cache else 0

    def approvals(self, paths=None):
        """Ask the person about each change that waits for approval, of paths if
        given: to keep it, which checks it again, in the background, and pushes it;
        to see all of it; to reject it; or to leave it for later."""
        review = self.model.review
        todo = [(path, held) for path, held in review.waiting()
                if paths is None or path in paths]
        if not todo:
            self.flash = 'No change waits for approval.'
            return
        keep, rejected = [], 0
        for n, (path, held) in enumerate(todo, 1):
            self.offered.add(path)
            title = (f'A change to the tree waits for your approval' +
                     (f' ({n} of {len(todo)})' if len(todo) > 1 else ''))
            while True:
                self.draw()
                picked = self.choose(title, list(APPROVING), body=review.held_lines(path, held))
                if picked != 1:
                    break
                self.pager(f'The change for {held.get("job")}', review.change_lines(path, held))
            if picked == 0:
                keep.append(path)
            elif picked == 2:
                approve.reject(path, review.args.cache)
                rejected += 1
        said = [f'{rejected} rejected' if rejected else '',
                f'{len(todo) - len(keep) - rejected} left for later: A asks again'
                if len(todo) - len(keep) - rejected else '']
        self.flash = '; '.join(s for s in said if s) or None
        if keep:
            numbers = [str(i) for i, (path, _) in enumerate(review.waiting(), 1)
                       if path in keep]
            self.background(f'Keeping {len(keep)} change{"" if len(keep) == 1 else "s"}',
                            'approve ' + ' '.join(numbers), kind='approve')

    # W: done with the run.

    def wrap_up(self):
        """W: done with the run. Each update owed to the people who need to know about
        it, in turn, each asked about first, and skipped if there is nothing to do:
        the groups still to look into; the changes that wait for approval; a tracker
        issue for each new failure; a comment on each PR found to cause one; a comment
        on each known issue seen again; the run's section of the RADOS wiki; the PRs
        approved; and the run approved on its QA tracker. A step that needs a key the
        person has not given says how to make one and takes it, or is done by hand."""
        review = self.model.review
        self.wrapping = {'said': [], 'keys': {}}
        groups = self.model.groups()
        look = [n for n, (_, rs) in enumerate(groups, 1)
                if not rs[0].problem and not rs[0].sure and not review.blamed_pr(rs[0])]
        new = [n for n, (_, rs) in enumerate(groups, 1)
               if not rs[0].problem and not review.blamed_pr(rs[0]) and not any(
                   w['posted'] for w in review.ways(rs) if w['way'] in ('issue', 'tracker'))]
        caused = [n for n, (_, rs) in enumerate(groups, 1) if review.blamed_pr(rs[0]) and
                  not any(w['posted'] for w in review.ways(rs) if w['way'] == 'pr')]
        try:
            review.wiki_link()
            qa = True
        except ValueError:
            qa = False
        done = review.posted_for_run()
        steps = [s for s, todo in (
            (lambda k, n: self._wrap_look(k, n, look), look),
            (lambda k, n: self._wrap_waiting(k, n), review.waiting()),
            (lambda k, n: self._wrap_groups(k, n, new, 'tracker'), new),
            (lambda k, n: self._wrap_groups(k, n, caused, 'github'), caused),
            (self._wrap_sightings, [f for f in review.sightings(news=True) if not f[4]]),
            (self._wrap_wiki, qa),
            (self._wrap_approvals, qa and review.under_test()),
            (self._wrap_qa, qa and 'qa' not in done)) if todo]
        if not steps:
            self.flash = 'Nothing is left to do for the run: every update is posted.'
            return
        for k, step in enumerate(steps, 1):
            if step(k, len(steps)) == 'stop':
                self.wrapping['said'].append('Stopped there: W goes on from what is left.')
                break
        self.pager('Done with the run: what was done', self.wrapping['said'] + [
            '', 'What posts in the background posts one after another; the line above the '
                'keys says how far it has got.'])

    def _step(self, k, n, title, body, options, posts=False):
        """Ask about step k of n: the option picked, 'skip', or 'stop'. A step whose
        first options post where anyone can read them (posts) is skipped by enter
        alone, as i's box cancels: posting is chosen."""
        options = list(options) + ['Skip this step', 'Stop here: W goes on from what is left']
        self.draw()
        picked = self.choose(f'Done with the run, step {k} of {n}: {title}', options,
                             len(options) - 2 if posts else 0, body=body)
        if picked is None or picked == len(options) - 1:
            return 'stop'
        return 'skip' if picked == len(options) - 2 else options[picked]

    def _said(self, line):
        self.wrapping['said'].append(line)

    def _wrap_look(self, k, n, look):
        groups = self.model.groups()
        body = [f'{g}: {report.headline(groups[g - 1][1][0])}' for g in look]
        got = self._step(k, n, f'{len(look)} group{"" if len(look) == 1 else "s"} still to '
                               'look into', body, ['Go on: they go in the wiki under "Still to '
                                                   'look into"'])
        if got == 'stop':
            return got
        self._said(f'Still to look into: group{"" if len(look) == 1 else "s"} '
                   f'{", ".join(map(str, look))}.')

    def _wrap_waiting(self, k, n):
        waiting = self.model.review.waiting()
        got = self._step(k, n, f'{len(waiting)} change{"" if len(waiting) == 1 else "s"} to '
                               'the records and the tree wait for your approval',
                         [str(held.get('summary') or held.get('job')) for _, held in waiting],
                         ['Ask about each now'])
        if got in ('stop', 'skip'):
            self._said('The changes that wait for approval: left; A asks about them.')
            return got
        self.approvals()
        self._said('The changes that wait for approval: asked about.')

    def _wrap_groups(self, k, n, numbers, where):
        """A tracker issue for each new failure (where is tracker), or a comment on the
        PR that caused each (github): through each group's update box, as i shows it."""
        review, groups = self.model.review, self.model.groups()
        what = ('new failure' if where == 'tracker' else 'failure a PR under test caused')
        if self.wrapping['keys'].get(where) == 'skip':
            self._said(f'The {what}s: left; i updates each.')
            return None
        body = [f'{g}: {report.headline(groups[g - 1][1][0])}' for g in numbers]
        got = self._step(k, n, f'{len(numbers)} {what}{"" if len(numbers) == 1 else "s"}: '
                               + ('a tracker issue for each' if where == 'tracker' else
                                  'a comment on each PR, for its developer'), body,
                         ['Go through them, one at a time'])
        if got in ('stop', 'skip'):
            self._said(f'The {what}s: left; i updates each.')
            return got
        how = self.keys('tracker' if where == 'tracker' else 'github')
        if how == 'skip':
            self._said(f'The {what}s: left; i updates each.')
            return None
        for g in numbers:
            if how == 'key':
                self.idle()
                self.update_records(g, title=f'Group {g} of the {what}s: update the records')
            else:
                self.by_hand_group(g, where)
        self._said(f'The {what}s: gone through, groups {", ".join(map(str, numbers))}.')

    def _wrap_sightings(self, k, n):
        todo = [f for f in self.model.review.sightings(news=True) if not f[4]]
        if self.wrapping['keys'].get('tracker') == 'skip':
            self._said('The known issues seen again: left; I comments on them.')
            return None
        got = self._step(k, n, f'{len(todo)} known tracker issue'
                               f'{"" if len(todo) == 1 else "s"} the run saw again: a comment '
                               'on each, with its jobs',
                         [f'#{issue}: {text.count("/a/")} job'
                          f'{"" if text.count("/a/") == 1 else "s"}'
                          for issue, _, _, text, _ in todo], ['Go on: they are shown first'])
        if got in ('stop', 'skip'):
            self._said('The known issues seen again: left; I comments on them.')
            return got
        how = self.keys('tracker')
        if how == 'key':
            self.post_sightings()
            self._said('The known issues seen again: commented on, as you chose (I).')
        elif how == 'hand':
            lines = [line for issue, _, _, text, _ in todo for line in
                     [f'{TRACKER_ISSUES}{issue}', *text.split('\n'), '']]
            self.by_hand('The comments for the known issues', '\n'.join(lines), None, [
                'Paste each issue\'s lines as a comment on it: its link is above them.'])
            self._said('The known issues seen again: copied, to comment on by hand.')

    def _wrap_wiki(self, k, n):
        """The run's section of the RADOS wiki: copied, for the person to paste in."""
        review = self.model.review
        section = '\n'.join(self.model.command('wiki'))
        page = review.wiki_page_name()
        got = self._step(k, n, f'the run\'s section of the RADOS wiki, page {page}, to paste '
                               'in', section.split('\n'), ['Copy it, to paste in'])
        if got in ('stop', 'skip'):
            self._said('The wiki: left; w shows the run\'s section, and y copies it.')
            return got
        copied = copy(section)
        self.pager('The run\'s section of the RADOS wiki', [
            (f'Copied, with {copied}.' if copied else f'Copy it: it is in {COPIED} too.'),
            f'Paste it at {TRACKER_WIKI}{page} (Edit), at the top of the runs, under '
            '"Summaries are ordered latest --> oldest.", or in place of the run\'s section, '
            'if it has one: the approvals that follow link to it.', '', *section.split('\n')])
        self._said('The wiki: the run\'s section copied, to paste in.')

    def _wrap_approvals(self, k, n):
        review = self.model.review
        text, rows = review.approval()
        done = review.approved()
        todo = [number for number, _, ok in rows if ok and number not in done]
        if not todo:
            return None
        how = self.keys('github')
        if how == 'key':
            self.post_approvals()
            self._said('The PRs: approved, as you chose (P).')
        elif how == 'hand':
            self.by_hand('The approval for the PRs', text, None, [
                'Post it as a comment on each of these PRs:',
                *[f'  https://github.com/ceph/ceph/pull/{number}' for number in todo]])
            self._said('The PRs: the approval copied, to post by hand.')
        else:
            self._said('The PRs: left; P approves them.')

    def _wrap_qa(self, k, n):
        review = self.model.review
        qa, author, text, caused = review.qa_approval()
        who = author[1] if author else 'its author'
        body = [f'{TRACKER_ISSUES}{qa}', f'The comment: {text}', 'Its status: QA Approved',
                f'Assigned back to: {who}'] + ([
                    '', 'An analysis found PRs under test caused failures: '
                        f'{", ".join("#" + c for c in caused)}.'] if caused else [])
        how = self.keys('tracker')
        if how == 'skip':
            self._said('The QA tracker: left; qa approve, at the prompt, approves it.')
            return None
        if how == 'hand':
            self.by_hand('The approval for the QA tracker', text, f'{TRACKER_ISSUES}{qa}', [
                f'Post it as a comment, set its status to QA Approved, and assign it to {who}.'])
            self._said('The QA tracker: the approval copied, to post by hand.')
            return None
        got = self._step(k, n, f'approve the run on its QA tracker, issue {qa}', body,
                         [f'Approve it: comment, set QA Approved, assign it to {who}',
                          'Comment only'], posts=True)
        if got in ('stop', 'skip'):
            self._said('The QA tracker: left.')
            return got
        self.background('Approving the run on its QA tracker',
                        'qa comment' if got == 'Comment only' else 'qa approve', kind='post')
        self._said('The QA tracker: ' + ('commented on.' if got == 'Comment only' else
                                         f'approved, and assigned to {who}.'))

    def idle(self):
        """Wait, drawing, for what works in the background to finish, as a step that
        changes the records needs: any box its end shows is shown."""
        while self.bg:
            if self.bg['thread'].is_alive():
                self.draw(busy=f'Waiting for {self.bg["title"].lower()} to finish...')
                time.sleep(0.3)
            else:
                self.watch()

    def keys(self, where):
        """How the steps that post to where (tracker or github) are done: 'key', with
        the person's key; 'hand', by hand; or 'skip'. Without a key, asked once: to
        make one and give it, or to do them by hand, or to skip them."""
        known = self.wrapping['keys']
        if where in known:
            return known[where]
        from . import setup as setup_page
        from .sources import _token
        review = self.model.review
        if where == 'tracker':
            if review.tracker_key():
                known[where] = 'key'
                return 'key'
            title = 'Posting to tracker.ceph.com needs your API key, and none is given'
            options = ['Make one now: how, and its page opened; then paste it here']
        else:
            self.draw(busy='Asking GitHub who you are...')
            if setup_page._gh_user() or _token():
                known[where] = 'key'
                return 'key'
            title = 'Posting to GitHub needs gh, signed in, or a token, and neither is here'
            options = (['Sign gh in now (gh auth login)'] if shutil.which('gh') else []) + \
                ['Make a token now: how, and its page opened; then paste it here']
        options += ['Do these steps by hand: the text copied, and its page opened',
                    'Skip the steps that post there']
        while True:
            self.draw()
            picked = self.choose(title, options)
            label = options[picked] if picked is not None else options[-1]
            if label.startswith('Sign gh in'):
                self.outside(['gh', 'auth', 'login'], said='Signing gh in to GitHub, with gh '
                             'auth login: answer its questions.', pause=True)
                if setup_page._gh_user():
                    known[where] = 'key'
                    return 'key'
                self.flash = 'gh is not signed in.'
            elif label.startswith('Make'):
                if self.make_key('tracker_key' if where == 'tracker' else 'github_key'):
                    known[where] = 'key'
                    return 'key'
            else:
                known[where] = 'hand' if label.startswith('Do these') else 'skip'
                return known[where]

    def make_key(self, name):
        """Show how to make the key name (tracker_key or github_key), open its page,
        and take it: checked, and kept as the setup page keeps it. True if it was."""
        from . import setup as setup_page
        from .sources import github_user, tracker_user, use_github_key, use_tracker_key
        self.pager('How to make one', setup_page.HOWTO[name])
        url = setup_page.PAGES[name]
        opened = setup_page.open_page(url)
        key = self.ask(f'{"Opened" if opened else "Open"} {url}' +
                       ('' if opened else ' in your browser') + '. Paste the key here (it is '
                       'shown as dots), or nothing to go back:', secret=True)
        if not key:
            return False
        self.draw(busy='Checking it...')
        try:
            if name == 'tracker_key':
                who = tracker_user(key.strip())
            else:
                who, scopes = github_user(key.strip())
                if scopes is not None and not {'public_repo', 'repo'} & set(scopes):
                    self.flash = 'That token is read-only: it cannot comment on PRs.'
                    return False
        except (OSError, KeyError, ValueError) as e:
            self.flash = f'It was not taken: {e}'
            return False
        page = setup_page.Setup()
        path = page.keep_key(name, key)
        page.set(name, path)
        page.save()
        settings = getattr(self.model.review.args, 'settings', None)
        if isinstance(settings, dict):
            settings[name] = path
        (use_tracker_key if name == 'tracker_key' else use_github_key)(key.strip())
        self.flash = f'It is {who}\'s: kept in {path}, for every review.'
        return True

    def by_hand(self, title, text, url, how):
        """A step done by hand: text copied, url opened, if given, and how to finish
        it, with the text, shown."""
        from . import setup as setup_page
        copied = copy(text)
        opened = setup_page.open_page(url) if url else False
        self.pager(title, [
            (f'Copied, with {copied}.' if copied else f'Copy it: it is in {COPIED} too.'),
            *([f'{"Opened" if opened else "Open"} {url}' + ('' if opened else
                                                            ' in your browser') + '.']
              if url else []), *how, '', *text.split('\n')])

    def by_hand_group(self, number, where):
        """A tracker issue for group number's new failure, or a comment on the PR that
        caused it, by hand: its draft copied, its page opened; then, for an issue, its
        number, to keep a record for it."""
        review = self.model.review
        rs = self.model.groups()[number - 1][1]
        if where == 'tracker':
            subject, text = review.draft(rs, 'issue')
            self.by_hand(f'A tracker issue for group {number}', f'{subject}\n\n{text}',
                         f'{TRACKER}/projects/{review.tracker_project()}/issues/new',
                         ['The subject is its first line, then the description.'])
            self.idle()
            self.track_by_hand(number)
        else:
            pr = review.blamed_pr(rs[0])
            self.by_hand(f'A comment on PR #{pr}', review.draft(rs, 'pr', pr)[1],
                         f'https://github.com/ceph/ceph/pull/{pr}', [])

    def post_sightings(self):
        """I: a comment on each tracker issue the run saw again, with its jobs, all
        shown first, to post or leave."""
        review = self.model.review
        self.draw(busy='Asking the tracker which issues were resolved since the run...')
        found = review.sightings()
        news = {f[0] for f in review.sightings(news=True)}
        before = [f[0] for f in found if f[0] not in news and not f[4]]
        todo = [f for f in found if not f[4] and f[0] in news]
        if not todo:
            self.flash = ('Every tracker issue the run saw again has its comment already.'
                          if found else 'No group is placed in a tracker issue, apart from '
                                        'any a PR under test caused.')
            return
        if not review.tracker_key():
            self.flash = ('Commenting on the tracker needs your API key for it: o, then '
                          'Tracker key, takes one, and says how to make it.')
            return
        body = [f'#{issue}: group{"" if len(numbers) == 1 else "s"} '
                f'{", ".join(map(str, numbers))}, {text.count("/a/")} '
                f'job{"" if text.count("/a/") == 1 else "s"}'
                for issue, numbers, _, text, _ in todo]
        if len(todo) + len(before) < len(found):
            body.append(f'({len(found) - len(todo) - len(before)} posted before, not again)')
        if before:
            body.append(f'(left out: {", ".join("#" + i for i in before)}, resolved after the '
                        'run began, so it saw them before their fix: not news)')
        whole = [line for issue, numbers, _, text, _ in todo
                 for line in [f'Tracker issue {issue} (group'
                              f'{"" if len(numbers) == 1 else "s"} '
                              f'{", ".join(map(str, numbers))}):', '', *text.split('\n'), '']]
        while True:
            self.draw()
            picked = self.choose(
                f'Comment on the {len(todo)} tracker issue{"" if len(todo) == 1 else "s"} '
                'the run saw again, with its jobs? Anyone can read them.',
                ['Post them', 'Show each comment, as it will be posted', 'Not now'], body=body)
            if picked != 1:
                break
            self.pager('The comments for the tracker issues', whole)
        if picked == 0:
            self.background(f'Commenting on {len(todo)} tracker issue'
                            f'{"" if len(todo) == 1 else "s"}', 'sightings post', kind='post')

    def post_approvals(self):
        """P: "Rados approved" on each PR the run tests that it approves, the PRs and
        why shown first, to post or leave."""
        review = self.model.review
        try:
            text, rows = review.approval()
        except ValueError as e:
            self.flash = f'No approval: {e}.'
            return
        if not rows:
            self.flash = 'No PRs under test are known: p says where they are looked for.'
            return
        done = review.approved()
        todo = [number for number, _, ok in rows if ok and number not in done]
        body = [f'The comment: {text}', ''] + [
            f'#{number}  {"posted before" if number in done else "approve" if ok else "leave out"}'
            f': {why}' for number, why, ok in rows]
        if not todo:
            self.pager('The PRs under test', body + ['', 'None is left to approve.'])
            return
        picked = self.choose(
            f'Post the approval on the {len(todo)} PR{"" if len(todo) == 1 else "s"} marked '
            'approve? Anyone can read it; paste the wiki section (w) in first.',
            [f'Post it on the {len(todo)} PR{"" if len(todo) == 1 else "s"}', 'Not now'],
            body=body)
        if picked == 0:
            self.background(f'Approving {len(todo)} PR{"" if len(todo) == 1 else "s"}',
                            'approvals post', kind='post')

    def not_kept_box(self, output):
        """For each change that approving did not keep: why, in a box, and what to do
        about it: have the analyst make it again for the library as it is now, see
        every check, reject it, or leave it for later."""
        review = self.model.review
        for job, lines in not_kept_by_job(output):
            path = next((p for p, h in review.waiting() if h.get('job') == job), None)
            if not path:
                continue
            if 'kept changing' in lines[0]:
                options = [(TRY_AGAIN, 'again')]
            elif 'may not be needed any more' in lines[0]:
                options = [(REJECT_NEEDLESS, 'reject'), (REVISE, 'revise')]
            else:
                options = [(REVISE, 'revise'), ('Reject it', 'reject')]
            options[1:1] = [(SHOW_CHECKS, 'show')]
            options.append((LATER, 'later'))
            while True:
                self.draw()
                picked = self.choose(f'Not kept: the change for {job}', [o for o, _ in options],
                                     body=lines)
                act = 'later' if picked is None else options[picked][1]
                if act != 'show':
                    break
                self.pager(f'The checks of the change for {job}, done again',
                           [*lines, '', 'What the analyst found, and every check:', '', *output])
            if act == 'revise':
                # Its revision is asked about as a new change is.
                self.offered.discard(path)
                self.background(f'Revising the change for {job}', f'revise {job}',
                                kind='analyst', job=job.rpartition('/')[2])
            elif act == 'reject':
                approve.reject(path, review.args.cache)
                self.flash = (f'{job}: rejected; it is set aside in '
                              f'{approve.rejected(review.args.cache)}')
            elif act == 'again':
                number = next(n for n, (p, _) in enumerate(review.waiting(), 1) if p == path)
                self.background('Keeping 1 change', f'approve {number}', kind='approve')

    def offer_waiting(self):
        """As the review opens: offer to ask about the changes that wait for
        approval, such as those a review before made."""
        new = [path for path, _ in self.model.review.waiting() if path not in self.offered]
        if not new:
            return
        self.offered.update(new)
        n = len(new)
        self.draw()
        if self.confirm(f'{n} change{"" if n == 1 else "s"} to the tree, which the analyst '
                        f'made before, wait{"s" if n == 1 else ""} for your approval. Look at '
                        f'{"it" if n == 1 else "them"} now?'):
            self.approvals(new)
        else:
            self.flash = 'A asks about them when you choose.'

    def offer_changes(self):
        """Ask about the changes that wait for approval that the person was not asked
        about yet, as a lesson's is once it is done; True if there were some."""
        new = [path for path, _ in self.model.review.waiting() if path not in self.offered]
        if new:
            self.approvals(new)
        return bool(new)

    def work(self, title, line):
        """Ask the analyst, in the background, as the judge is asked: its answer
        comes into the lower pane, with the group's."""
        self.background(title, line, kind='analyst')

    def background(self, title, line=None, kind='judge', load=None, job=None):
        """Do a command that asks an agent on its own thread, while the screen stays
        in use: the line above the keys says how far it has got, the lower pane shows
        what the agent is doing while the group it works on is selected, and each
        answer once it is given. kind is whose work it is: the judge's, the
        analyst's, or, with load(say) in place of a command, the triage's; job is
        the first job of the group it is for, if not the group chosen."""
        if self.bg and self.bg['thread'].is_alive():
            # One thing at a time in the background: this waits for what is there, and
            # its group, if it is for one, is marked as waiting.
            # A command for a job, by its id, as digest is, keeps its line.
            named = bool(job and job in (line or '').split())
            number = self.number_in(line) if kind in ('judge', 'analyst') and not named else None
            self.queued.append({'title': title, 'line': line, 'kind': kind, 'load': load,
                                'job': job if named else self.first(number) if number else None,
                                **({'named': True} if named else {})})
            self.flash = f'{title}: it waits ({WAITING}) for {self.doing()}, and starts once ' \
                         'that is done.'
            return
        number = self.chosen()
        out = io.StringIO()

        def work():
            try:
                if load:
                    load(lambda said: print(said, file=out))
                else:
                    self.model.review.in_background(line, out)
            except Exception:
                out.write(traceback.format_exc())
            finally:
                self.model.review.go()

        self.model.review.go()

        self.bg = {'title': title, 'line': line, 'out': out, 'started': time.time(),
                   'done': 0, 'thread': threading.Thread(target=work, daemon=True),
                   'job': job or (self.first(number) if number else None), 'kind': kind}
        self.bg['thread'].start()

    def doing(self):
        """What is done in the background now, for a sentence."""
        now = self.bg['title']
        return now[0].lower() + now[1:]

    @staticmethod
    def number_in(line):
        """The group a command is for, by its number, as in learn 5; None if none."""
        words = (line or '').split()
        return int(words[1]) if len(words) > 1 and words[1].isdigit() else None

    def number_of(self, job_id):
        """The number of the group job_id is in now; None if it is in none."""
        return next((n for n, (_, rs) in enumerate(self.model.groups(), 1)
                     if any(r.job.id == job_id for r in rs)), None)

    def begin(self, task):
        """Begin what waited for the background: for a group, under its number now,
        as the groups may have been numbered anew while it waited."""
        title, line = task['title'], task['line']
        if task['job'] and not task.get('named'):
            number = self.number_of(task['job'])
            if not number:
                self.flash = f'{title}: not begun, as its job {task["job"]} is in no group now.'
                return
            words = line.split(' ', 2)
            line = ' '.join([words[0], str(number), *words[2:]])
            title = re.sub(r'\bgroup \d+', f'group {number}', title)
        self.background(title, line, task['kind'], task['load'], job=task['job'])

    def unqueued(self):
        """What stopping the background does not begin of what waits for it, for the
        question: all that asks an agent."""
        titles = [t['title'] for t in self.queued if t['kind'] not in ('approve', 'post')]
        return (f' What waits for it is not begun either: '
                f'{", ".join(t[0].lower() + t[1:] for t in titles)}.' if titles else '')

    def unqueue(self):
        """Not begin what waits for the background and asks an agent: changes the
        person kept are still kept."""
        self.queued = [t for t in self.queued if t['kind'] in ('approve', 'post')]

    def waits(self, number):
        """What waits for the background to be done, of group number's: a line for the
        lower pane; None if nothing does."""
        if not (self.bg and number):
            return None
        job_id = self.first(number)
        task = next((t for t in self.queued if t['job'] == job_id), None)
        if task:
            return (f'{WAITING} {task["title"]} waits for {self.doing()}, and starts once that '
                    'is done; x stops both.')
        if number in self.waiting_for_ai():
            who = 'the judge' if self.bg['kind'] == 'judge' else 'the analyst'
            others = ', and watch\'s other runs' if getattr(self.model.review, 'turns', None) \
                else ''
            return (f'{WAITING} This group waits for a turn of {who}, which the groups before '
                    f'it{others} have now.')
        return None

    def working(self):
        """{job: what the agent has said about its group so far} for each group the
        agent in the background works on now: several, as judges at once are; {} if
        no agent works."""
        if not (self.bg and self.bg['thread'].is_alive()) or \
                self.bg['kind'] in ('triage', 'approve', 'post'):
            return {}
        at_once = getattr(self.model.review, 'at_work', None)
        if at_once is not None:
            return {job_id: list(said) for job_id, said in dict(at_once).items()}
        text = self.bg['out'].getvalue()
        # Of several groups one after the other, the one it says it has begun last.
        begun = list(re.finditer(r'^Group \d+ of \d+, job (\S+):$', text, re.M))
        job_id = begun[-1].group(1) if begun else self.bg['job']
        return {job_id: text[begun[-1].start() if begun else 0:].rstrip('\n').split('\n')} \
            if job_id else {}

    def queued_groups(self):
        """The numbers of the groups whose work waits for what the background does now."""
        jobs = {t['job'] for t in getattr(self, 'queued', ()) if t['job']}
        return {n for n in map(self.number_of, jobs) if n} if jobs else set()

    def waiting_for_ai(self):
        """The numbers of the groups that wait for a turn of the AI, or for the groups
        before them."""
        waiting = set(getattr(self.model.review, 'waiting_for_ai', ()) or ())
        return {n for n, (_, rs) in enumerate(self.model.groups(), 1)
                if any(r.job.id in waiting for r in rs)} if waiting else set()

    def busy(self):
        """The numbers of the groups the agent in the background works on."""
        working = self.working()
        return {n for n, (_, rs) in enumerate(self.model.groups(), 1)
                if any(r.job.id in working for r in rs)}

    def working_job(self, number, working=None):
        """The job of group number that the agent in the background works on, or
        None; of working, as working() gives it, if given."""
        groups = self.model.groups()
        if not number or number > len(groups):
            return None
        working = self.working() if working is None else working
        return next((r.job.id for r in groups[number - 1][1] if r.job.id in working), None)

    def at_work(self, number):
        """What the agent in the background has said about group number so far, while
        it works on it; None if it is not working on it."""
        working = self.working()
        job_id = self.working_job(number, working)
        return working[job_id] if job_id else None

    def working_on(self, number, writes=False):
        """Who is at work on group number, and on what, for the lower pane's title:
        what it writes, as it writes it, if it writes."""
        review = self.model.review
        agent = review.judge if self.bg['kind'] == 'judge' else review.analyst
        who = 'The judge' if self.bg['kind'] == 'judge' else 'The analyst'
        job_id = self.working_job(number)
        streams = any(getattr(a, 'streams', False) for a in getattr(agent, 'judges', [agent]))
        return (f' {who} ({readable(agent.name) if agent else "none"}) on group {number}, job '
                f'{job_id}: ' +
                ('what it writes, as it writes it ' if writes else
                 'what it is thinking, and what it reads ' if streams or who != 'The judge'
                 else 'it gives its answer all at once, when it is done '))

    def written(self, number):
        """What the judge has written about group number so far, as it is, as lines,
        while it works on it; None if it has written nothing."""
        groups = self.model.groups()
        writing = getattr(self.model.review, 'writing', None)
        if not (writing and number and number <= len(groups)):
            return None
        text = next((''.join(list(writing.get(r.job.id) or ())) for r in groups[number - 1][1]
                     if r.job.id in writing), '')
        return text.rstrip('\n').split('\n') if text.strip() else None

    def progress(self):
        """(the groups the judge has begun, of how many) in the background, as it
        says, or, of groups judged at once, those it has done; (0, 0) for one group.
        Of the triage: (the jobs triaged, of how many)."""
        if self.bg['kind'] == 'triage':
            session = self.model.review.session
            return len(session.results), len(session.jobs)
        found = re.findall(r'^Group (\d+) of (\d+)', self.bg['out'].getvalue(), re.M)
        if not found:
            return 0, 0
        at_once = getattr(self.model.review, 'at_work', None)
        begun, total = int(found[-1][0]), int(found[-1][1])
        return (begun - len(at_once) if at_once is not None else begun), total

    def watch(self):
        """While the judge works in the background: show each answer as it comes,
        and what it did once it is done."""
        done, _ = self.progress()
        working = frozenset(self.working())
        if (done, working) != (self.bg['done'], self.bg.get('working')):
            self.bg['done'], self.bg['working'] = done, working
            self.model.details.clear()
        if self.bg['thread'].is_alive():
            return
        bg, self.bg = self.bg, None
        self.model.details.clear()
        self.finished(bg)
        queued = getattr(self, 'queued', None)
        if queued and not self.bg:
            self.begin(queued.pop(0))

    def finished(self, bg):
        """What the background work bg did, once it is done: what it found, or
        what to do next."""
        output = bg['out'].getvalue().rstrip('\n').split('\n')
        if any(line.startswith(('error:', 'Traceback')) for line in output):
            self.pager(bg['title'], output)
            return
        if bg['kind'] == 'triage' and not self.model.review.session.triaged:
            self.pager(bg['title'], output)
            return
        skipped = getattr(self.model.review.session, 'skipped', [])
        if bg['kind'] == 'triage' and skipped:
            self.flash = (f'Triage stopped: {len(skipped)} jobs not triaged '
                          f'({", ".join(skipped)}); starting the review again triages them.')
            return
        if any(line.startswith('Stopped:') for line in output):
            self.flash = f'{bg["title"]}: stopped. {STOPPED}'
            return
        if any(line.startswith('error:') for line in output):
            self.pager(bg['title'], output)
            return
        if bg['kind'] == 'triage':
            # Then the judge, as the settings say: the run is triaged, judged, and then
            # learned from.
            self.suggest()
            return
        if bg['kind'] == 'post':
            self.flash = next((line for line in reversed(output) if line.startswith('Posted ')),
                              f'{bg["title"]}: done.')
            if any(' did not take it' in line for line in output):
                self.pager(bg['title'], output)
            return
        if bg['kind'] == 'approve':
            self.flash = next((line for line in reversed(output) if line.startswith('Kept ')),
                              f'{bg["title"]}: done.')
            self.not_kept_box(output)
            return
        if (bg['line'] or '').startswith('revise '):
            # What came of it: a revision waits, or why none does.
            said = next((line for line in reversed(output) if ': revised' in line or
                         line.startswith('Not revised')), None)
            if said and ('does not pass' in said or said.startswith('Not revised')):
                self.pager(bg['title'], output)
            self.flash = said or f'{bg["title"]}: done.'
            if 'waits for your approval' in (said or ''):
                self.offer_changes()
            return
        self.flash = f'{bg["title"]}: done. Its answers are in the lower pane, with each group.'
        if bg['kind'] == 'analyst' and (bg['line'] or '').split()[:1] in (['doubt'], ['digest']) \
                and bg.get('job') and self.model.review.response_lines(bg['job']):
            # The analyst's response, whatever it says, for the person who doubted.
            self.respond_box(bg['job'])
            return
        if bg['kind'] == 'analyst' and self.offer_changes():
            return
        if bg['line'] == 'judge all':
            self.suggest_learning()

    def suggest_learning(self):
        """Offer the analyst the groups it could learn from."""
        review = self.model.review
        todo = review.to_learn()
        if not (todo and review.analyst):
            return
        numbers = {id(rs[0]): n for n, (_, rs) in enumerate(self.model.groups(), 1)}
        which = ', '.join(str(numbers.get(id(rs[0]), '?')) for rs, _ in todo)
        groups = f'{len(todo)} group{"" if len(todo) == 1 else "s"}'
        self.draw()
        if self.asking(f'Learn from {groups} ({which}) with {readable(review.analyst.name)}? '
                       f'{self.most()}'):
            self.work(f'The analyst on {groups}', 'learn all')
        else:
            self.flash = (f'The analyst could learn from {groups} ({which}): l on one, or u '
                          'offers them all again.')

    def draw_chat(self, number, job_id, busy=None, live=None):
        """The discussion of a job: what the agents said about it before, then the
        conversation, the newest at the bottom, and the steps of a reply coming."""
        review = self.model.review
        self.clear()
        height, width = self.stdscr.getmaxyx()
        agent = readable(review.analyst.name) if review.analyst else 'the analyst'
        self.put(0, 0, f' Discussing group {number} (job {job_id}) with {agent} '.ljust(width),
                 self.colour('head') | curses.A_BOLD)
        lines = []
        for answer in review.state.answers.get(job_id, []):
            if answer['kind'] != 'discussion':
                lines += [f'From {answer["kind"]} ({answer["agent"]}), {answer["when"]}:',
                          *answer['lines'], '']
        if lines:
            lines.append('─' * 40)
        for talk in review.state.discussions.get(job_id, []):
            for turn in talk['turns']:
                lines += ['', f'{turn["who"]}:',
                          *[f'  {line}' for line in turn['text'].split('\n')]]
        if live:
            lines += ['', f'{agent} is working:', *live]
        shown = wrap(lines, width - 2)[-(height - 3):]
        for i, line in enumerate(shown):
            self.put(1 + i, 1, line, curses.A_BOLD if line.endswith(':') and
                     not line.startswith(' ') else curses.A_NORMAL)
        cost = f' {self.model.status()} '
        self.put(height - 2, max(0, width - len(cost) - 1), cost, self.colour('cost'))
        if busy:
            self.put(height - 1, 0, f' {busy} '.ljust(width), self.colour('foot'))
        self.refresh()

    def outside(self, argv, said=None, pause=False):
        """Run argv on this terminal, as the person's editor, or a program that asks
        questions, such as gh auth login, until it is done: said, if given, is printed
        first, and with pause, the person presses enter to come back. An error, if it
        did not start, or None."""
        failed = None
        curses.def_prog_mode()
        terminal(MOUSE_OFF)
        curses.endwin()
        try:
            if said:
                print(said, flush=True)
            subprocess.run(argv)
        except OSError as e:
            failed = f'error: {argv[0]} did not start: {e}'
        finally:
            if pause:
                try:
                    input('\nPress enter to go back: ')
                except EOFError:
                    pass
            curses.reset_prog_mode()
            if self.mouse_on:
                terminal(MOUSE_ON)
            curses.flushinp()
            self.stdscr.clear()
        return failed

    def edited(self, text):
        """text, as the person leaves it in their editor ($VISUAL, $EDITOR or vim);
        None if the editor did not start."""
        editor = os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vim'
        with tempfile.NamedTemporaryFile('w', suffix='.md', prefix='qa_triage-',
                                         delete=False) as f:
            f.write(text)
        try:
            failed = self.outside([*shlex.split(editor), f.name])
            if failed:
                self.pager('Editing it', [failed])
                return None
            with open(f.name) as kept:
                return kept.read()
        finally:
            os.unlink(f.name)

    def update_records(self, number, title=None):
        """Tell the people who need to know about group number's failure, as the
        person chooses from a box, the recommended way first (see Review.ways): a
        comment on the PR that caused it, a comment on the tracker issue it is, or a
        new issue; or keep a record for an issue filed by hand. What is posted is
        shown first, whole, and posted only once the person says so: anyone can
        read it."""
        review = self.model.review
        rs = self.model.groups()[number - 1][1]
        ways = review.ways(rs)
        options = [w['says'] + (' (recommended)' if w['recommended'] else '') +
                   (f' (posted {w["posted"]})' if w['posted'] else '') for w in ways]
        options.append('Keep a record for a tracker issue filed by hand, for the tree to '
                       'place jobs like these in it')
        # The run's other groups that are tracker issues too: all at once, as I does.
        issues = len(review.sightings(news=True)) if any(w['way'] == 'jobs' for w in ways) \
            else 0
        if issues > 1:
            options.append(f'Comment on all {issues} tracker issues the run saw again, each '
                           'with only its jobs, as /a/ paths (I)')
        picked = self.choose(title or f'Update the records about group {number}', options,
                             next((i for i, w in enumerate(ways) if w['recommended']), 0),
                             body=[report.headline(rs[0])])
        if picked is None:
            return
        if picked == len(ways):
            self.track_by_hand(number)
            return
        if picked == len(ways) + 1:
            self.post_sightings()
            return
        way, n = ways[picked]['way'], ways[picked]['number']
        if way == 'issue' and self.bg:
            # Its record changes the results, as the agent's answers do.
            self.flash = f'{self.bg["title"]} is still working: i files it once it is done.'
            return
        if way == 'pr' and not n:
            prs = list(review.under_test().items())
            got = self.choose('Comment on which PR under test?',
                              [f'#{pr}: {"; ".join(how)}' for pr, how in prs])
            if got is None:
                return
            n = prs[got][0]
        elif way == 'tracker' and not n:
            n = (self.ask('The number of the tracker issue:') or '').strip().lstrip('#')
            if not n.isdigit():
                self.flash = 'Nothing was posted: no tracker issue was named.'
                return
        where = {'pr': f'PR #{n}, on GitHub', 'tracker': f'tracker issue {n}',
                 'jobs': f'tracker issue {n}',
                 'issue': f'a new issue on the tracker, in {review.tracker_project()}'}[way]
        try:
            subject, text = review.draft(rs, way, n)
        except ValueError as e:
            self.pager('Update records', [f'error: {e}'])
            return
        while True:
            body = ([f'Subject: {subject}', ''] if subject else []) + text.split('\n')
            picked = self.choose(f'Post this to {where}? Anyone can read it.',
                                 ['Post it', 'Edit it first', 'Show it all', 'Cancel'], 3,
                                 body=body)
            if picked == 0:
                break
            if picked == 1:
                changed = self.edited('\n'.join(body))
                if changed is not None:
                    subject, text = parse_draft(changed, subject is not None)
            elif picked == 2:
                self.pager(f'To post to {where}', body)
            else:
                self.flash = 'Nothing was posted.'
                return
        kind = 'product'
        if way == 'issue':
            got = self.choose('What needs fixing?', ['product', 'test', 'infra'])
            if got is None:
                self.flash = 'Nothing was posted.'
                return
            kind = ('product', 'test', 'infra')[got]
        job_id = self.first(number)
        self.draw(busy=f'Posting it to {where}...')
        output = self.model.call(review.post, rs, way, n, text, subject, kind)
        self.select(job_id)
        if way == 'issue' or any(line.startswith('error:') for line in output):
            self.pager(f'Posting to {where}', output)
        else:
            self.flash = output[-1] if output else 'Posted.'

    def track_by_hand(self, number):
        """Keep a record for a tracker issue the person filed by hand for group
        number's failure, if it passes the checks."""
        if self.bg:
            self.flash = f'{self.bg["title"]} is still working: a record can be kept once it is.'
            return
        issue = self.ask('The number of the tracker issue filed for it:')
        if not issue:
            return
        got = self.choose('What needs fixing?', ['product', 'test', 'infra'])
        if got is None:
            return
        job_id = self.first(number)
        output = self.model.command(f'track {number} {issue} {("product", "test", "infra")[got]}')
        self.select(job_id)
        self.pager(f'Tracking group {number} as {issue}', output)

    def options(self):
        """The analyst for this review, or the setup page, from a box."""
        review = self.model.review
        analyst = readable(review.analyst.name) if review.analyst else 'none'
        picked = self.choose('Options', [
            f'The analyst for learn, doubt and talk in this review (now {analyst})',
            'The settings: the setup page, for the judge and everything else'])
        if picked == 0:
            self.pick_analyst('The analyst, for learn, doubt and talk:')
        elif picked == 1:
            self.configure()

    def talk(self, number):
        """Talk with the analyst about a group's first job in the analyst's own
        program, on this terminal, until the person leaves it; then check what it
        proposed."""
        review, job_id = self.model.review, self.first(number)
        name, program = review.analyst.name, review.analyst.program
        ready = review.program(job_id)
        if not ready:
            output = self.wait(f'Getting {name} ready on group {number}', f'ready {job_id}',
                               lambda busy, live: self.draw(busy=busy, live=live),
                               'it reads the evidence and says what it makes of it')
            ready = review.program(job_id)
            if not ready:
                self.pager('Getting it ready', output)
                return
        argv, workdir = ready
        found = review.approving()
        after = ('you are back in the review, and qa_triage checks its proposal against every '
                 'job triaged before, keeping it only if it passes' +
                 (': then it waits for your approval (qa_triage pending).' if found['approve'] else
                  ', and sharing it at once.' if found['library'] else '.'))
        since = self.hand_over(argv, workdir, program,
                               ['', f'qa_triage: {program} on group {number} (job {job_id}), in '
                                    f'{workdir}.', ''] + approve.guidance(program, after))
        if since is None:
            return
        output = self.wait(f'Checking what {name} proposed on group {number}',
                           f'back {job_id} {since}',
                           lambda busy, live: self.draw(busy=busy, live=live),
                           'checking a proposal against the earlier runs takes a minute or two')
        self.select(job_id)
        if any(': proposed in' in line or line.startswith('error:') for line in output):
            # What the analyst proposed, and what the checks made of it, in full.
            self.pager('What it proposed', output)
        else:
            self.flash = output[-1] if output else 'Back from talking.'

    def hand_over(self, argv, workdir, program, said):
        """Leave the terminal to the analyst's own program, run in workdir, once the
        person has read said and starts it; back once they leave it. When it started,
        or None if it did not, as the screen then says."""
        since, failed, went = time.time(), None, True
        curses.def_prog_mode()
        terminal(MOUSE_OFF)
        curses.endwin()
        try:
            print('\n'.join(said), flush=True)
            try:
                went = not input(f'\nPress enter to start {program}, or q and enter to go back: '
                                 ).strip().lower().startswith('q')
            except EOFError:
                went = False
            since = time.time()
            if went:
                subprocess.run(argv, cwd=workdir,
                               env=getattr(self.model.review.analyst, 'env', lambda: None)())
        except OSError as e:
            failed = f'error: {program} did not start: {e}'
        finally:
            curses.reset_prog_mode()
            if self.mouse_on:
                terminal(MOUSE_ON)
            curses.flushinp()
            self.stdscr.clear()
        if failed:
            self.pager('Talking', [failed])
            return None
        if not went:
            self.flash = f'{program} not started.'
            return None
        return since

    def respond_box(self, job_id):
        """The analyst's response to the person's doubt about job_id's group, in a box,
        whether it agrees with them or not: to accept it, to discuss it with the
        analyst, after which it gives its final answer, in the background, and the box
        shows that once it is done; to see all of it; to dismiss it; or to leave it."""
        review = self.model.review
        while True:
            found = review.response_lines(job_id)
            if not found:
                return
            lines, can = found
            latest, got = review.response_of(job_id)
            # Its change is asked about here, not again as a lesson's.
            self.offered.update(p for p in (got.get('held'), got.get('kept')) if p)
            name = readable(latest.get('agent') or 'the analyst')
            analyst = review.analyst
            program = getattr(analyst, 'program', None)
            # Only the analyst that responded, in its own session, can discuss it.
            talks = bool(analyst and analyst.name == latest.get('agent') and
                         latest.get('session') and getattr(analyst, 'resumes', False))
            number = self.number_of(job_id)
            choices = ([('accept', 'Accept it: keep its answer, and any change it made, checked '
                                   'again and pushed to the shared library')]
                       if 'accept' in can else []) + \
                ([('discuss', f'Discuss it with {name}' + (f' in {program}' if program else
                                                            ' here') +
                   ': when you leave, it gives its final answer')] if talks else []) + \
                [('show', 'Show all it found, and its change')] + \
                ([('dismiss', 'Dismiss it: keep nothing it made')] if 'dismiss' in can else []) + \
                [('later', 'Leave it for now: d on the group shows it again' if can else
                  'Close')]
            self.draw()
            picked = self.choose(f'{name}\'s response to your doubt about group {number}',
                                 [text for _, text in choices], body=lines)
            kind = choices[picked][0] if picked is not None else 'later'
            if kind == 'show':
                self.pager(f'{name}\'s response, whole', review.response_whole(job_id))
                continue
            if kind == 'accept':
                self.background(f'Accepting {name}\'s response on group {number}',
                                f'accept {job_id}', kind='approve', job=job_id)
            elif kind == 'dismiss':
                output = self.model.command(f'dismiss {job_id}')
                self.flash = output[-1] if output else None
            elif kind == 'discuss':
                self.discuss_response(job_id)
            elif can:
                self.flash = 'd on the group shows its response again.'
            return

    def discuss_response(self, job_id):
        """Discuss the analyst's response to the person's doubt about job_id's group:
        in its own program, if it has one, going on from the doubt's session, or here;
        then ask it, in the background, for its final answer."""
        review = self.model.review
        name, program = readable(review.analyst.name), review.analyst.program
        number = self.number_of(job_id)
        output = self.wait(f'Getting {name} ready to discuss its response on group {number}',
                           f'ready {job_id} response',
                           lambda busy, live: self.draw(busy=busy, live=live),
                           'it says in a few lines what it concluded')
        if any(line.startswith(('error:', 'Traceback')) for line in output):
            self.pager('Getting it ready', output)
            return
        if program:
            ready = review.program(job_id)
            if not ready:
                self.pager('Getting it ready', output)
                return
            argv, workdir = ready
            since = self.hand_over(argv, workdir, program, [
                '', f'qa_triage: {program}, to discuss its response to your doubt about group '
                    f'{number} (job {job_id}), in {workdir}.', '',
                f'It goes on from its response, with all the evidence it had. Ask it what you '
                'like, and tell',
                'it where you think it is wrong; it is told not to start a new analysis '
                'unless you ask.',
                f'When you leave {program}, {name} is asked once more for its final answer, '
                'with any change',
                'the discussion calls for; it is checked, and you then accept it or not.'])
            if since is None:
                self.flash = (self.flash or '') + ' d on the group shows its response again.'
                return
        else:
            self.discuss(number, final=True)
        self.background(f'{name}\'s final answer on group {number}', f'digest {job_id}',
                        kind='analyst', job=job_id)

    def discuss(self, number, final=False):
        """Talk with the analyst about a group's first job here, until the person
        leaves: for an analyst with no program of its own. final, when they discuss
        its response to their doubt, after which it gives its final answer."""
        job_id = self.first(number)
        while True:
            self.draw_chat(number, job_id)
            message = self.ask_line(
                'You (enter sends; an empty line or escape leaves, and it then gives its final '
                'answer):' if final else
                'You (enter sends; /propose asks for a change to the records or the tree; an '
                'empty line or escape leaves):')
            if not message or not message.strip():
                break
            if message.strip().startswith('/propose') and not final:
                note = message.strip()[len('/propose'):].strip()
                line, hint = f'propose {job_id} {note}'.rstrip(), \
                    'a proposal is checked before it is kept; it can take a few minutes'
            else:
                line, hint = f'discuss {job_id} {message}', 'a reply usually takes under two minutes'
            output = self.wait(f'The analyst on group {number}', line,
                               lambda busy, live: self.draw_chat(number, job_id, busy, live),
                               hint)
            if any(line.startswith('error:') for line in output):
                self.pager('The discussion', output)
        self.select(job_id)
        self.flash = 'The discussion is kept, and in the lower pane; enter shows all of it.'

    def suggest(self):
        """Once the tree and the records have triaged the run: ask about the changes
        that wait for approval, then offer the judge for the groups they could not
        decide."""
        self.offer_waiting()
        review = self.model.review
        todo = len(review.unjudged())
        if not todo:
            return
        groups = f'{todo} group{"" if todo == 1 else "s"}'
        if not review.judge:
            self.flash = (f'The tree and the records could not decide {groups}, and there is '
                          'no judge: --judge or a config file sets one.')
            return
        self.draw()
        if self.settings.get('judge_on_open') or self.asking(
                f'{groups} not judged yet. Ask {readable(review.judge.name)} about '
                f'{"it" if todo == 1 else "them"} in the background? It costs.'):
            self.background(f'The judge on {groups}', 'judge all')
        else:
            self.flash = 'u asks the judge about all of them, and j about one, when you choose.'

    def again(self, number, kind, command, what):
        """Show what an agent answered about a group before, for nothing; or, if it
        was not asked, ask it, which costs."""
        if self.model.review.state.latest(self.first(number), kind):
            self.model.command(f'{command} {number}')
            self.flash = (f'{what} answered before: its answer is in the lower pane. '
                          f'{command[0].upper()} asks again, which costs.')
        elif kind == 'judge':
            # A judge's call costs little: asked for, it is made.
            self.background(f'The judge on group {number}', f'judge {number}')
        elif self.asking(f'Ask {what.lower()} about group {number}? {self.most()}'):
            self.work(f'{what} on group {number}', f'{command} {number}')

    def configure(self):
        """The setup page, over the review: what is saved there, the review takes up
        at once where it can."""
        # The setup page is built on this module's Screen.
        from . import setup
        page = setup.SetupScreen(self.stdscr, setup.Setup())
        try:
            page.run()
        finally:
            curses.curs_set(0)
        changed = page.setup.since()
        self.flash = self.model.review.take(changed)
        self.set_mouse(bool(self.settings.get('mouse', True)) if 'mouse' in changed
                       else self.mouse_on)

    def most(self):
        """What one lesson of the analyst's may cost at most, for the question that
        asks before it spends: the learn_budget setting, in the analyst's unit."""
        review = self.model.review
        budget = getattr(review.args, 'learn_budget', None)
        unit = review.units.get(review.analyst.name, 'of its own unit') if review.analyst \
            else 'of its own unit'
        return (f'Each lesson costs up to {budget:g} {unit}.' if budget else
                f'There is no limit on what a lesson costs, in {unit}: the learn_budget '
                'setting sets one.')

    # The keys.

    def run(self, load=None):
        """Until the person quits; with load(say), which triages the run into the
        review, in the background first."""
        self.start(load)
        while True:
            self.draw()
            # While the judge works in the background, the screen keeps up with it.
            self.stdscr.timeout(500 if self.bg else -1)
            key = self.key()
            self.stdscr.timeout(-1)
            if self.handle(key) == 'quit':
                return

    def start(self, load=None):
        """As the screen opens: triage the run into the review with load(say), in the
        background, if given; else offer what to do next."""
        if load:
            self.background('Triaging the run', kind='triage', load=load)
        else:
            self.suggest()

    def handle(self, key):
        """Do what key says: 'quit' if the person quits, else None. Background work
        that is done is taken up first."""
        if self.bg:
            self.watch()
        if key == -1:
            return None
        self.flash = None
        selected = self.kept
        if self.selecting(key) or key == 27 and selected:
            return None
        if key == ord('x'):
            if not self.bg:
                self.flash = 'No agent is at work: x stops one while it works.'
            elif self.bg['kind'] == 'triage':
                session = self.model.review.session
                if session.stop.is_set():
                    self.flash = 'The triage is stopping.'
                elif self.confirm('Stop the triage? The jobs triaged so far stay; the '
                                  'rest, such as one whose logs take long to read, are '
                                  'left, and triaged when the review starts again.' +
                                  self.unqueued()):
                    session.stop.set()
                    self.unqueue()
            elif self.bg.get('stopping'):
                self.flash = f'{self.bg["title"]} is stopping: it stops at its next step.'
            elif self.confirm(f'Stop {self.doing()}? {STOPPING}{self.unqueued()}'):
                self.model.review.stop()
                self.unqueue()
                # The status line says so until it has stopped.
                self.bg['stopping'] = True
            return None
        if self.bg and key in BUSY:
            done, total = self.progress()
            action = dict(FREE_KEYS + AI_KEYS).get(chr(key), chr(key))
            self.flash = (f'{self.bg["title"]} is still working' +
                          (f' ({done} of {total})' if total else '') +
                          f': {action} ({chr(key)}) can start once it is done.')
            return None
        count = len([n for n, _ in self.model.rows() if n])
        height = self.stdscr.getmaxyx()[0]
        step = max(1, (height - 4) // 2 - 1)
        moves = {curses.KEY_DOWN: 1, curses.KEY_UP: -1, curses.KEY_NPAGE: step,
                 curses.KEY_PPAGE: -step, curses.KEY_HOME: -count, curses.KEY_END: count}
        if key == 9:
            self.focus = 'pane' if self.focus == 'list' else 'list'
            return None
        if key == 27:
            self.focus = 'list'
            return None
        if key in (curses.KEY_SR, curses.KEY_SF):
            self.scroll_pane(-1 if key == curses.KEY_SR else 1)
            return None
        if key in moves and self.focus == 'pane':
            page = max(1, self.pane_room - 1)
            self.scroll_pane({curses.KEY_DOWN: 1, curses.KEY_UP: -1, curses.KEY_NPAGE: page,
                              curses.KEY_PPAGE: -page, curses.KEY_HOME: -1 << 30,
                              curses.KEY_END: 1 << 30}[key])
            return None
        if key in moves:
            self.model.selected = max(0, min(count - 1, self.model.selected + moves[key]))
            return None
        if key == curses.KEY_MOUSE:
            if self.resize():
                return None
            what, y = self.mouse()
            in_pane = self.pane_at[0] <= y < self.pane_at[1]
            if what in ('up', 'down') and in_pane:
                self.scroll_pane(-WHEEL if what == 'up' else WHEEL)
            elif what in ('up', 'down') and y in self.rows_at:
                self.model.selected = max(0, min(count - 1, self.model.selected +
                                                 (-1 if what == 'up' else 1)))
            elif what in ('click', 'double') and self.rows_at.get(y):
                numbers = [n for n, _ in self.model.rows() if n]
                self.model.selected, self.focus = numbers.index(self.rows_at[y]), 'list'
                if what == 'double':
                    number = self.rows_at[y]
                    self.pager(f'Group {number}', self.model.detail(number))
            elif what in ('click', 'double') and in_pane:
                self.focus = 'pane'
                if what == 'double' and self.chosen():
                    self.pager(f'Group {self.chosen()}', self.model.detail(self.chosen()))
            return None
        if key == curses.KEY_RESIZE or key == 12:
            self.stdscr.clear()
            return None
        n = self.chosen()
        if key == ord('q'):
            # The report is written as it is now; an agent in the background stops.
            if self.bg:
                self.model.review.stop()
            return 'quit'
        elif key == ord('?'):
            self.pager('Help', HELP.split('\n'))
        elif key in (10, 13, curses.KEY_ENTER) and n:
            self.pager(f'Group {n}', self.model.detail(n))
        elif key == ord('i') and n:
            self.update_records(n)
        elif key == ord('W'):
            self.wrap_up()
        elif key == ord('I'):
            self.post_sightings()
        elif key == ord('P'):
            self.post_approvals()
        elif key == ord('y') and n:
            how = copy('\n'.join(self.model.detail(n)))
            self.flash = (f'Copied the lower pane, group {n}, to the clipboard, with {how}.'
                          if how else f'Could not copy to the clipboard here: it is in {COPIED}.')
        elif key == ord('m'):
            self.set_mouse(not self.mouse_on)
            self.flash = ('The mouse is on: the wheel scrolls, a click chooses, a drag '
                          'selects and copies; m turns it off.' if self.mouse_on else
                          'The mouse is off: the terminal selects text as usual; m turns '
                          'it on again.')
        elif key == ord('w'):
            self.pager('The run\'s section for the RADOS wiki', self.model.command('wiki'))
        elif key == ord('p'):
            self.draw(busy='Asking GitHub for the titles of the PRs under test...')
            self.pager('The PRs under test', self.model.command('prs'))
        elif key == ord('c'):
            self.pager('Draft comment for the QA tracker', self.model.command('comment'))
        elif key == ord('r'):
            output = self.wait('Triaging again', 'retriage',
                               lambda busy, live: self.draw(busy=busy, live=live),
                               'with the records and the tree as they are now; no AI',
                               stops=False)
            self.pager('Triaged again', output)
        elif key == ord('f') and n:
            glob = self.ask('Files matching (empty for the top and the daemons\' logs):')
            if glob is not None:
                self.pager(f'Files of group {n}', self.model.command(f'files {n} {glob}'))
        elif key == ord('j') and n:
            self.again(n, 'judge', 'judge', 'The judge')
        elif key == ord('l') and n:
            self.again(n, 'lesson', 'learn', 'The analyst')
        elif key == ord('u'):
            todo = len(self.model.review.unjudged())
            groups = f'{todo} group{"" if todo == 1 else "s"}'
            if not todo:
                # All judged: what the analyst could learn from them.
                self.suggest_learning()
                self.flash = self.flash or 'Every group has been judged.'
            elif any(t['line'] == 'judge all' for t in
                     ([self.bg] if self.bg else []) + self.queued):
                self.flash = 'The judge is asked about every group not judged yet already.'
            else:
                self.background(f'The judge on {groups}', 'judge all')
        elif key == ord('J') and n:
            self.background(f'The judge on group {n}', f'judge {n} again')
        elif key == ord('L') and n:
            if self.asking(f'Ask the analyst to learn from group {n} again? {self.most()}'):
                self.work(f'The analyst on group {n}', f'learn {n} again')
        elif key == ord('t') and n:
            if self.pick_analyst(f'Talk about group {n} with (what you say costs):') is None:
                self.flash = 'Not asked: nothing was spent.'
            elif self.model.review.analyst.program:
                self.talk(n)
            else:
                self.discuss(n)
        elif key == ord('A'):
            self.approvals()
        elif key == ord('o'):
            self.options()
        elif key == ord('d') and n and (self.model.review.response_lines(self.first(n)) or
                                        (None, None))[1]:
            # The response to the doubt before waits for the person.
            self.respond_box(self.first(n))
        elif key == ord('d') and n:
            why = self.ask(f'Why you doubt the result of group {n}:')
            if not why or not why.strip():
                self.flash = 'No doubt: nothing was asked.'
                return None
            says = self.ask('What the job is, as you say: a tracker number, which the '
                            'analyst must then place it in; new; or empty, for the '
                            'analyst to find:')
            if says is None:
                self.flash = 'No doubt: nothing was asked.'
                return None
            if self.asking(f'Ask the analyst about group {n}? {self.most()}'):
                quoted = why.replace("'", "'\"'\"'")
                self.work(f'The analyst on a doubt about group {n}',
                          f'doubt {n} ' + (f'--says {says} ' if says.strip() else '') +
                          f"'{quoted}'")


# watch's screen: the pane down its left, and what is beside it.

# The columns of the pane, unless the person dragged its edge; the fewest it keeps,
# and the fewest it leaves beside it.
PANE, PANE_LEAST, BESIDE_LEAST = 34, 18, 50
# The keys of watch's summary, and what the pane adds to a run's.
SUMMARY_KEYS = (('↑↓', 'select'), ('enter', 'details'), ('→', 'its run'), ('←', 'the runs'),
                ('F', 'find runs'), ('D', 'dates'), ('y', 'copy'), ('m', 'mouse'),
                ('?', 'help'), ('q', 'quit'))
WATCH_HELP = """watch

Down the left, a pane: the summary of the runs at the top, each run below it, newest
first, and at the bottom, F, to find the runs again: the suite's runs of the base
branch, and of the QA batches for it, between the dates, by default the last week's
(D changes them). Each run
says how far its triage, with the tree and the records and no AI, has got, then how
many of its jobs failed or died.

  ←, →      the pane, or what is beside it; in the pane, ↑↓ choose, enter or → goes
            to the one chosen, x leaves a run out of the watch
  [ ]       the run before, or after, the one shown
  F         find the runs again, between the dates: runs that fell out of them
            leave the pane, and new ones come into it and are triaged. Started
            with --prs, the runs found are those of the PRs, of any suite (see
            qa_triage watch --help), and the pane says under each how it is known
            to test them
  D         the dates of the runs to find: from a date, or a number of days back,
            until a date, or today; then they are found again
  q         quit

The summary lists the failures of every run together, the commonest first: those
of the most runs, then of the most jobs, each a known problem, one seen again after
its fix, a new one the tree explains (which needs a tracker filed), or one for a
person to look into, and whether the base branch's runs have it too. enter shows a
failure's runs, jobs and explanation; → goes to it, in the newest run that has it,
where every key of the review works on it: j and l ask the judge and the analyst.

A run is its own review, with all its keys: ? there lists them. The pane shows the
PRs the run chosen tests, from its QA tracker and its branch, and p in the run lists
them with their titles. The runs share the AI: so many of the judge's turns at once,
judges_at_once, and of the analyst's, lessons_at_once, for all of them together; a
group that waits for a turn is marked …, where one at work turns, and a run whose
agent works says AI.

The mouse: a click in the pane chooses, and the wheel moves through it. Drag the
pane's right edge to make it wider or narrower, and the line under a list to give
the list more rows or fewer; the review keeps both."""


class SummaryModel:
    """What watch's summary shows: the failures of every run together, the commonest
    first, and the details of each."""

    def __init__(self, watching):
        self.watching = watching
        self.review = None
        self.selected = 0
        self.details = {}

    def failures(self):
        """Those of the tree's and the records' that are not the test infrastructure's
        first, then those that are."""
        results = [r for review in self.watching.reviews.values()
                   for r in list(review.results)]
        found = watch.combine(results)
        return [f for f in found if not f.infra] + [f for f in found if f.infra]

    def detail(self, f):
        """A failure's runs, jobs and explanation, and its first job as the tree and the
        records found it."""
        key = (f.key, len(f.results))
        if key not in self.details:
            r, base = f.first, self.watching.base
            kind = watch.KINDS[f.kind]
            lines = [f'{f.label}: {f.title}' if f.problem else f'{f.label}: {report.what(r)}',
                     '', f'{kind[0].upper()}{kind[1:]}; {watch.where(f, base)}.']
            if f.problem:
                lines.append(f'Its tracker: {r.problem.tracker}')
            lines += ['', f'{len(f.results)} job{"" if len(f.results) == 1 else "s"} in '
                          f'{len(f.runs)} run{"" if len(f.runs) == 1 else "s"}:']
            for run in f.runs:
                ids = ', '.join(o.job.id for o in f.results if o.job.run == run)
                lines.append(f'  {watch.short_name(run)}: {ids}   ({run})')
            screen = self.watching.screens.get(r.job.run)
            lines += ['', f'Its job {r.job.path}, as the tree and the records found it:', '']
            lines += screen.model.command(f'show {r.job.id}') if screen else \
                report.decisions(r)
            self.details[key] = lines
        return self.details[key]

    def status(self):
        return ''


class Summary(Screen):
    """watch's summary: the failures of every run together, the commonest first, above
    the details of the one chosen."""

    def __init__(self, stdscr, model, watching):
        super().__init__(stdscr, model)
        self.watching, self.listed, self.held_key = watching, [], None

    def cache(self):
        return self.watching.cache

    def waiting_count(self):
        return approve.count_waiting(self.watching.cache) if self.watching.cache else 0

    def chosen_failure(self):
        return self.listed[self.model.selected] if self.listed else None

    def key_at(self, n):
        return self.listed[n].key if 0 <= n < len(self.listed) else None

    def draw(self, busy=None, live=None):
        self.clear()
        height, width = self.stdscr.getmaxyx()
        watching = self.watching
        self.listed = self.model.failures()
        if self.held_key:
            # The failure the person chose stays chosen as the runs add to the list.
            keys = [f.key for f in self.listed]
            if self.held_key in keys:
                self.model.selected = keys.index(self.held_key)
        self.model.selected = max(0, min(self.model.selected, len(self.listed) - 1))
        chosen = self.chosen_failure()
        runs = watching.runs
        left = [run for run in runs if not watching.triaged(run)]
        self.put(0, 0, f' qa_triage watch  The commonest failures of {len(runs)} run'
                       f'{"" if len(runs) == 1 else "s"} '.ljust(width),
                 self.colour('head') | curses.A_BOLD)
        jobs = sum(len(f.results) for f in self.listed)
        self.put(1, 0, f' {jobs} failed or dead jobs, {len(self.listed)} failures, the '
                       'commonest first' +
                       (f'; {len(left)} run{"" if len(left) == 1 else "s"} still being '
                        'triaged' if left else '') + '.')
        foot = self.footer(width, SUMMARY_KEYS, ())
        status = height - len(foot) - 1
        if not hasattr(self, 'list_rows'):
            self.list_rows = layout(self.cache()).get('list_rows')
        list_height = max(LIST_ROWS, min(self.list_rows or (status - 3) // 2, status - 6))
        at = self.model.selected
        if at < self.top:
            self.top = at
        elif at >= self.top + list_height:
            self.top = at - list_height + 1
        self.top = max(0, min(self.top, len(self.listed) - list_height))
        self.rows_at = {}
        for i, f in enumerate(self.listed[self.top:self.top + list_height]):
            n, y = self.top + i, 2 + i
            self.rows_at[y] = n + 1
            runs, jobs = len(f.runs), len(f.results)
            text = (f'{n + 1:>3} {f.label + (" (infra)" if f.infra else ""):<17} '
                    f'{runs:>2} run{"s" if runs != 1 else " "} '
                    f'{jobs:>4} job{"s" if jobs != 1 else " "}  {f.title}')
            if n == self.model.selected:
                self.put(y, 0, f'>{text}'.ljust(width), curses.A_REVERSE)
            else:
                self.put(y, 0, f' {text}', self.colour('sure' if f.kind == 'known' else
                                                       'unsure'))
        if not self.listed:
            self.put(3, 2, 'No failures yet: the runs are being triaged.' if left else
                           f'No failures: F finds the runs of {watch.said(watching.state)} '
                           'again.')
        y = self.divider_at = 2 + list_height
        room = max(1, status - y - 1)
        self.pane_at, self.pane_room = (y + 1, y + 1 + room), room
        self.put(y, 0, '─' * width, self.colour('section'))
        if chosen:
            lines = wrap(self.model.detail(chosen), width - 2)
            self.pane_top = max(0, min(self.pane_top, len(lines) - room))
            for i, line in enumerate(lines[self.pane_top:self.pane_top + room]):
                self.put(y + 1 + i, 1, line)
            if len(lines) > room:
                self.put(y, 2, f' lines {self.pane_top + 1}-{min(self.pane_top + room, len(lines))}'
                               f' of {len(lines)}: shift-↑↓ or the wheel scrolls it ',
                         self.colour('section') | curses.A_BOLD)
        said = busy or self.flash or (
            ' → goes to the failure chosen, in its newest run, where the review\'s keys '
            'work on it' if chosen else '')
        waiting = self.waiting_count()
        if waiting and not busy and not self.flash:
            said += (f'   [A] {waiting} change{"" if waiting == 1 else "s"} to the tree '
                     f'wait{"s" if waiting == 1 else ""} for your approval, in a run')
        self.put(status, 0, said, curses.A_BOLD if busy or self.flash else curses.A_NORMAL)
        for i, (pieces, fill) in enumerate(foot):
            self.put_pieces(status + 1 + i, pieces, fill)
        self.refresh()

    def handle(self, key):
        """Do what key says: 'quit', ('go', run, job id) to go to a failure in its
        newest run, or None."""
        if key == -1:
            return None
        self.flash = None
        selected = self.kept
        if self.selecting(key) or key == 27 and selected:
            return None
        count = len(self.listed)
        step = max(1, (self.stdscr.getmaxyx()[0] - 4) // 2 - 1)
        moves = {curses.KEY_DOWN: 1, curses.KEY_UP: -1, curses.KEY_NPAGE: step,
                 curses.KEY_PPAGE: -step, curses.KEY_HOME: -count, curses.KEY_END: count}
        chosen = self.chosen_failure()
        if key in moves:
            self.model.selected = max(0, min(count - 1, self.model.selected + moves[key]))
            self.pane_top, self.held_key = 0, self.key_at(self.model.selected)
        elif key in (curses.KEY_SR, curses.KEY_SF):
            self.scroll_pane(-1 if key == curses.KEY_SR else 1)
        elif key == curses.KEY_MOUSE:
            if self.resize():
                return None
            what, y = self.mouse()
            if what in ('up', 'down') and self.pane_at[0] <= y < self.pane_at[1]:
                self.scroll_pane(-WHEEL if what == 'up' else WHEEL)
            elif what in ('up', 'down') and y in self.rows_at:
                self.model.selected = max(0, min(count - 1, self.model.selected +
                                                 (-1 if what == 'up' else 1)))
                self.held_key = self.key_at(self.model.selected)
            elif what in ('click', 'double') and y in self.rows_at:
                self.model.selected, self.pane_top = self.rows_at[y] - 1, 0
                self.held_key = self.key_at(self.model.selected)
                if what == 'double':
                    self.pager('A failure of the runs', self.model.detail(self.listed[
                        self.model.selected]))
        elif key in (10, 13, curses.KEY_ENTER) and chosen:
            self.pager('A failure of the runs', self.model.detail(chosen))
        elif key in (curses.KEY_RIGHT, ord('g')) and chosen:
            run = chosen.runs[0]
            return 'go', run, next(r.job.id for r in chosen.results if r.job.run == run)
        elif key == ord('y') and chosen:
            how = copy('\n'.join(self.model.detail(chosen)))
            self.flash = (f'Copied the failure\'s details, with {how}.' if how else
                          f'Could not copy to the clipboard here: it is in {COPIED}.')
        elif key == ord('m'):
            self.set_mouse(not self.mouse_on)
        elif key == ord('?'):
            self.pager('Help', WATCH_HELP.split('\n'))
        elif key in (curses.KEY_RESIZE, 12):
            self.stdscr.clear()
        elif key == ord('q'):
            return 'quit'
        return None


class Watching:
    """watch's screen: down the left, a pane of the summary of the runs, each run, and
    a way to find the last week's runs again; beside it, the one chosen there: the
    summary of the runs' failures together, or a run's own review, with all its keys.

    review_of(run) gives (a run's Review, load(say), which triages the run into it);
    find() gives (the last week's runs, [(a run left out, why)]); state is the runs
    watched, as watch.kept gives it."""

    def __init__(self, stdscr, state, review_of, find, cache, base):
        self.stdscr, self.state, self.review_of, self.find_runs = stdscr, state, review_of, find
        self.cache, self.base = cache, base
        self.runs = watch.watched(state)
        self.reviews, self.screens = {}, {}
        # 0 is the summary, then each run, then the way to find runs; and whether
        # the keys are the pane's or what is beside it.
        self.chosen, self.focus = 0, 'main'
        self.width = layout(cache).get('pane') or PANE
        self.dragging, self.finding, self.tick, self.entry_at = False, None, 0, {}
        # So many runs are triaged at once; the rest wait their turn.
        self.turns = threading.Semaphore(3)
        # The changes that wait for approval that the person was asked about, by any
        # run's screen.
        self.offered = set()
        self.main = self.summary = self.placed = None

    def place(self):
        """The window beside the pane: made again as the terminal or the pane changes
        size."""
        height, width = self.stdscr.getmaxyx()
        self.width = max(PANE_LEAST, min(self.width, width - BESIDE_LEAST))
        if (height, width, self.width) == self.placed:
            return
        self.placed = (height, width, self.width)
        self.main = self.stdscr.derwin(height, max(1, width - self.width), 0, self.width)
        self.main.keypad(True)
        for screen in [self.summary, *self.screens.values()]:
            if screen:
                screen.stdscr = self.main
        self.stdscr.clear()

    def triaged(self, run):
        review = self.reviews.get(run)
        return bool(review and review.session.triaged)

    def add(self, run):
        """Watch run: its review, triaged in the background, in its turn."""
        review, load = self.review_of(run)

        def in_turn(say):
            if not self.turns.acquire(blocking=False):
                say('Waiting for the triage of other runs to finish...')
                self.turns.acquire()
            try:
                load(say)
            finally:
                self.turns.release()

        review.followed = (self.state.get('tests') or {}).get(run) or {}
        screen = Screen(self.main, Model(review))
        screen.offered = self.offered
        self.reviews[run], self.screens[run] = review, screen
        screen.start(in_turn)

    def drop(self, run):
        """Watch run no more: its triage stops, if it is at work."""
        review = self.reviews.pop(run, None)
        self.screens.pop(run, None)
        if review:
            review.session.stop.set()
            review.stop()

    def find(self):
        """Find the last week's runs again, in the background."""
        if self.finding:
            return
        result = {}

        def work():
            try:
                result['runs'], result['left'] = self.find_runs()
            except Exception as e:  # noqa: BLE001 - said on the screen
                result['error'] = f'{type(e).__name__}: {e}'

        self.finding = {'thread': threading.Thread(target=work, daemon=True),
                        'result': result}
        self.finding['thread'].start()

    def found(self):
        """Take up the runs found, once they are: those that fell out of the week leave
        the pane, and new ones come into it."""
        if not self.finding or self.finding['thread'].is_alive():
            return
        result, self.finding = self.finding['result'], None
        if 'error' in result:
            self.current().flash = f'Could not find the last week\'s runs: {result["error"]}'
            return
        self.state.update(found=result['runs'],
                          found_at=datetime.datetime.now().isoformat(timespec='seconds'))
        watch.keep(self.cache, self.state)
        shown = self.current_run()
        runs = watch.watched(self.state)
        for run in [r for r in self.runs if r not in runs]:
            self.drop(run)
        for run in [r for r in runs if r not in self.reviews]:
            self.add(run)
        for run, review in self.reviews.items():
            review.followed = (self.state.get('tests') or {}).get(run) or {}
        self.runs = runs
        self.chosen = runs.index(shown) + 1 if shown in runs else 0
        left = result['left']
        why = ('not begun, or older than those taken' if self.state.get('prs') else
               'a part of the suite or not begun')
        self.current().flash = (f'{len(result["runs"])} runs of {watch.said(self.state)}' +
                                (f'; {len(left)} left out, {why}' if left else '') + '.')

    def current_run(self):
        return self.runs[self.chosen - 1] if 1 <= self.chosen <= len(self.runs) else None

    def current(self):
        run = self.current_run()
        return self.screens[run] if run else self.summary

    def colour(self, name):
        return self.summary.colour(name)

    def put(self, y, x, text, attr=curses.A_NORMAL):
        room = self.width - 1 - x
        if room > 0:
            with contextlib.suppress(curses.error):
                self.stdscr.addnstr(y, x, text.ljust(room), room, attr)

    def entry(self, run):
        """A run, for the pane: its name in a few words, and how far its triage has
        got, or how many of its jobs failed or died, and whether an agent works on it
        or waits for a turn of the AI."""
        review, screen = self.reviews.get(run), self.screens.get(run)
        session = review.session if review else None
        bg = screen.bg if screen and screen.bg and screen.bg['thread'].is_alive() else None
        spin = SPINNER[self.tick % 4]
        if not session:
            said = ''
        elif bg and bg['kind'] != 'triage':
            waiting = review.waiting_for_ai
            icon = WAITING if waiting and not set(screen.working()) - waiting else spin
            said = f'{icon} AI {len(session.results)} failed'
        elif session.triaged:
            said = f'{len(session.results)} failed'
        elif session.jobs:
            said = f'{spin} {len(session.results)}/{len(session.jobs)}'
        else:
            said = f'{spin} ...'
        name = watch.short_name(run)
        room = self.width - 4 - len(said)
        return f'{name[:room]:<{room}} {said}'

    def under_test(self, run):
        """Lines for the pane, under the run chosen: the PRs it tests, of those
        followed, with how it is known to, if the runs are the PRs'; else those its
        QA tracker and its branch name."""
        followed = (self.state.get('tests') or {}).get(run)
        if followed:
            lines = wrap(['tests ' + ' '.join(f'#{n}' for n in sorted(followed, key=int))],
                         self.width - 5)
            return lines[:3] + (['... p in the run says how'] if len(lines) > 3 else
                                ['p in the run says how'])
        review = self.reviews.get(run)
        if not (review and review.session.triage):
            return ['finding the PRs it tests...']
        prs = review.under_test()
        if not prs:
            return [f'no PRs under test: a run of {watch.branch_of(run)}'
                    if watch.branch_of(run) == self.base else 'no PRs under test found']
        tracker = review.qa_tracker()
        lines = wrap([(f'QA tracker {tracker}, ' if tracker else '') +
                      f'{len(prs)} PR{"" if len(prs) == 1 else "s"}: ' +
                      ' '.join(f'#{n}' for n in prs)], self.width - 5)
        return lines[:3] + (['... p in the run lists them'] if len(lines) > 3 else
                            ['p in the run lists their titles'])

    def draw_pane(self):
        height = self.stdscr.getmaxyx()[0]
        self.tick += 1
        for y in range(height):
            self.put(y, 0, '')
        self.put(0, 0, ' qa_triage watch', self.colour('head') | curses.A_BOLD)
        self.entry_at = {}

        def entry(y, n, text):
            self.entry_at[y] = n
            attr = curses.A_REVERSE if n == self.chosen and self.focus == 'pane' else \
                curses.A_BOLD if n == self.chosen else curses.A_NORMAL
            self.put(y, 0, ('>' if n == self.chosen else ' ') + text, attr)

        entry(1, 0, 'Summary of the runs')
        self.put(2, 0, '─' * self.width, self.colour('section'))
        # The runs, the one chosen with the PRs it tests under it: as many as fit
        # above what is at the bottom, around the one chosen.
        rows = []
        for n, run in enumerate(self.runs, 1):
            rows.append((n, self.entry(run)))
            if n == self.chosen:
                rows += [(None, f'   {line}') for line in self.under_test(run)]
        room = max(1, height - 11)
        at = next((i for i, (n, _) in enumerate(rows) if n == self.chosen), 0)
        top = getattr(self, 'pane_top', 0)
        top = max(min(top, at), at + sum(1 for n, _ in rows[at + 1:] if n is None) - room + 1)
        top = self.pane_top = max(0, min(top, len(rows) - room))
        y = 3
        if top:
            self.put(y - 1, 2, f' {top} more above ', self.colour('section'))
        for n, text in rows[top:top + room]:
            if n is None:
                self.put(y, 0, text, self.colour('section'))
            else:
                entry(y, n, text)
            y += 1
        if top + room < len(rows):
            self.put(y, 2, f' {len(rows) - top - room} more below ', self.colour('section'))
        if not self.runs:
            self.put(y, 1, 'No runs yet.' if not self.finding else 'Finding the runs...')
        y = max(y + 1, height - 8)
        self.put(y, 0, '─' * self.width, self.colour('section'))
        found = self.state.get('found_at')
        entry(y + 1, len(self.runs) + 1,
              f'F {SPINNER[self.tick % 4]} finding the runs' if self.finding else
              'F find the runs again' if found else 'F find the runs')
        self.put(y + 2, 0, f'   of {watch.said(self.state)}')
        entry(y + 3, 'dates', 'D other dates')
        if found and not self.finding:
            self.put(y + 4, 0, f'   found {found.replace("T", " ")[5:16]}')
        self.put(height - 2, 0, ' ← → pane or run, [ ] runs', self.colour('section'))
        self.put(height - 1, 0, ' drag this edge to resize', self.colour('section'))
        for y in range(height):
            with contextlib.suppress(curses.error):
                self.stdscr.addstr(y, self.width - 1, '┃' if self.dragging else '│',
                                   self.colour('section'))
        self.stdscr.noutrefresh()

    def pane_mouse(self, screen):
        """Take the mouse's report if it is the pane's, or drags its edge: True if it
        was. Beside the pane, the report's column is the screen's, from its left."""
        e = screen.event
        if not e:
            return False
        if self.dragging:
            screen.event = None
            self.width = max(PANE_LEAST, self.width + e.x + 1)
            if not e.pressed:
                self.dragging = False
                layout(self.cache, pane=self.width)
            return True
        if e.x >= 0:
            return False
        screen.event = None
        if e.pressed and not e.motion and e.x >= -2:
            self.dragging = True
        elif e.wheel:
            self.chosen = max(0, min(len(self.runs), self.chosen + e.wheel))
        elif not e.pressed and not e.motion and self.entry_at.get(e.y) == 'dates':
            self.dates()
        elif not e.pressed and not e.motion and e.y in self.entry_at:
            self.chosen, self.focus = self.entry_at[e.y], 'pane'
            if self.chosen == len(self.runs) + 1:
                self.find()
        return True

    def pane_key(self, key):
        """Take key if it is the pane's: True if it was."""
        last = len(self.runs) + 1
        moves = {curses.KEY_DOWN: 1, curses.KEY_UP: -1, curses.KEY_HOME: -last,
                 curses.KEY_END: last, curses.KEY_NPAGE: 5, curses.KEY_PPAGE: -5}
        if key in moves:
            self.chosen = max(0, min(last, self.chosen + moves[key]))
        elif key in (curses.KEY_RIGHT, 10, 13, curses.KEY_ENTER, 9):
            if self.chosen == last:
                self.find()
            else:
                self.focus = 'main'
        elif key in (ord('x'), curses.KEY_DC) and self.current_run():
            run = self.current_run()
            if self.summary.confirm(f'Leave {watch.short_name(run)} out of the watch?'):
                for part in ('given', 'found'):
                    self.state[part] = [r for r in self.state[part] if r != run]
                watch.keep(self.cache, self.state)
                self.drop(run)
                self.runs.remove(run)
                self.chosen = min(self.chosen, len(self.runs))
        elif key == ord('?'):
            self.current().pager('Help', WATCH_HELP.split('\n'))
        else:
            return False
        return True

    def dates(self):
        """Ask for the dates of the runs to find, then find them."""
        screen = self.current()
        since = screen.ask(f'Runs from: a date, as 10-29, or a number of days back (now '
                           f'{watch.said(self.state)}; empty keeps it):')
        if since is None:
            return
        until = screen.ask('Until: a date, or empty for today:')
        if until is None:
            return
        since, changed = since.strip(), {}
        try:
            if since.isdigit():
                changed.update(since=None, days=int(since))
            elif since:
                changed['since'] = str(watch.day(since))
            changed['until'] = str(watch.day(until)) if until.strip() else None
            watch.span(dict(self.state, **changed))
        except ValueError as e:
            screen.flash = f'{e}: the dates are as they were.'
            return
        self.state.update(changed)
        watch.keep(self.cache, self.state)
        self.find()

    def go(self, run, job_id):
        """Show run, with the group job_id is in chosen."""
        if run in self.runs:
            self.chosen, self.focus = self.runs.index(run) + 1, 'main'
            self.screens[run].select(job_id)

    def run(self):
        curses.curs_set(0)
        self.place()
        self.summary = Summary(self.main, SummaryModel(self), self)
        for run in self.runs:
            self.add(run)
        if not self.runs:
            self.find()
        while True:
            self.place()
            self.found()
            screen = self.current()
            self.draw_pane()
            screen.draw()
            # The pane keeps up with every run's triage.
            self.main.timeout(500)
            key = screen.key()
            self.main.timeout(-1)
            if key == curses.KEY_MOUSE and self.pane_mouse(screen):
                screen.handle(-1)
                continue
            if key == ord('F'):
                self.find()
                continue
            if key == ord('D'):
                self.dates()
                continue
            if self.focus == 'pane':
                if key == ord('q'):
                    return
                if self.pane_key(key):
                    screen.handle(-1)
                    continue
            elif key == curses.KEY_LEFT:
                self.focus = 'pane'
                continue
            elif key in (ord('['), ord(']')):
                self.chosen = max(0, min(len(self.runs), self.chosen + (1 if key == ord(']')
                                                                       else -1)))
                continue
            got = screen.handle(key)
            if got == 'quit':
                return
            if isinstance(got, tuple) and got[0] == 'go':
                self.go(got[1], got[2])


def watch_runs(state, review_of, find, cache, base):
    """Show watch's screen until the person quits (see Watching); what was written,
    for the terminal afterwards."""
    os.environ.setdefault('ESCDELAY', '25')
    watching = {}

    def show(stdscr):
        watching['screen'] = Watching(stdscr, state, review_of, find, cache, base)
        watching['screen'].run()

    printed = []
    try:
        with contextlib.redirect_stderr(Lines(printed.append)):
            curses.wrapper(show)
    finally:
        terminal(MOUSE_OFF)
        for line in printed:
            print(line, file=sys.stderr)
    screen = watching.get('screen')
    if not screen:
        return ''
    lines = []
    for run, review in screen.reviews.items():
        if review.session.triaged and not review.saved:
            review.write()
        lines.append(f'{run}: ' + (f'{review.session.out}/report.md' if review.session.triaged
                                   else 'not triaged: the watch stopped first'))
        review.session.stop.set()
    return '\n'.join(lines)

class Lines(io.TextIOBase):
    """A stream that gives each line written to it to say."""

    def __init__(self, say):
        self.say, self.part = say, ''

    def write(self, text):
        *lines, self.part = (self.part + text).split('\n')
        for line in lines:
            self.say(line)
        return len(text)


def run(review, load):
    """Show the review on the whole screen until the person quits: at once, while
    load(say) triages the run into it, its jobs coming into the list as they are
    decided. What the agents cost, for the terminal afterwards."""
    # Escape answers at once, not after a second.
    os.environ.setdefault('ESCDELAY', '25')
    model = Model(review)

    def show(stdscr):
        Screen(stdscr, model).run(load)

    # What is printed while the screen is up would mark it: it is said afterwards.
    printed = []
    try:
        with contextlib.redirect_stderr(Lines(printed.append)):
            curses.wrapper(show)
    finally:
        terminal(MOUSE_OFF)
        for line in printed:
            print(line, file=sys.stderr)
    if not review.session.triaged:
        return 'Stopped before the triage finished: nothing was written.'
    if not review.saved:
        review.write()
    return f'Report: {model.review.session.out}/report.md\n{model.status()}'
