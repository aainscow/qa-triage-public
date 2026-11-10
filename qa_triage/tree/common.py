"""What several nodes need to know about a job: the helpers every node may use."""

import re

from .core import REASON, when
from .marks import DAEMON

# Tests that crash daemons on purpose: a crash in one is not a failure by itself.
INTENDED_CRASHES = re.compile(r'all/test-crash\b|rados/test_crash\.sh')
LIMIT = 200


def short(text, limit=LIMIT):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit] + '...'


def failure(ctx):
    """The line where teuthology saw the job fail: the first that gives the
    failure reason, or else the first task failure; None if there is neither."""
    return ctx.first('reason') or ctx.first('task_failed')


# The cluster log's file, of the ceph task and of cephadm.
CLUSTER_LOGS = ('remote/*/log/ceph.log*', 'remote/*/log/*/ceph.log*')


def logged(ctx):
    """The line of the cluster log that the job's failure reason quotes: where the
    check of the cluster log found it, in teuthology.log, or else in the cluster
    log itself; None if neither has it."""
    if 'logged' not in ctx.found:
        found = ctx.first('cluster_line')
        quoted = re.match(r'"([^\n]+?)" in cluster log', ctx.job.reason.strip())
        words = quoted.group(1)[:REASON].split() if quoted and not found else []
        for log in CLUSTER_LOGS if words else ():
            hits = ctx.search(log, r'\s+'.join(map(re.escape, words)), words[0], max_hits=1)
            if hits:
                found = hits[0]
                break
        ctx.found['logged'] = found
    return ctx.found['logged']


def task_at(ctx, line):
    """The task that was running at line: the last one started before it."""
    started = [h for h in ctx.hits('task') if h.line < line]
    found = ctx.match('task', started[-1]) if started else None
    return found.group(1) if found else ''


def first_crash(ctx):
    """The first line that shows a daemon crash: a failed assert or a fatal signal."""
    found = [h for name in ('failed_assert', 'signal') for h in ctx.hits(name)]
    return min(found, key=lambda h: h.line, default=None)


def daemon(hit):
    """(daemon, host) whose output hit is, or ('', '')."""
    found = re.search(DAEMON, hit.text) if hit else None
    return found.groups() if found else ('', '')


def daemon_hits(ctx, name, who):
    """The lines of the landmark name that the daemon who logged."""
    return [h for h in ctx.hits(name) if daemon(h)[0] == who]


# Frames of the crash handler and the C library, which say nothing about where
# a daemon crashed.
HANDLER_FRAMES = re.compile(r'BackTrace|handle_fatal_signal|__restore_rt|^/lib|libc\.so|'
                            r'pthread_kill|^raise|^abort|gsignal|__ceph_abort|'
                            r'__ceph_assert_fail|ceph::__ceph_assert|__GI_|^\s*$')
TOP_FRAMES = 3


def frames(ctx, who, after_line):
    """The first frames of who's backtrace after after_line, without the crash
    handler's."""
    return list(frame_lines(ctx, who, after_line))


def frame_lines(ctx, who, after_line):
    """{function: its line} of the first frames of who's backtrace after
    after_line, without the crash handler's."""
    found = {}
    for hit in daemon_hits(ctx, 'frame', who):
        if hit.line < after_line:
            continue
        function = ctx.match('frame', hit).group(4)
        if function in found:
            break
        if not HANDLER_FRAMES.search(function):
            found[function] = hit
        if len(found) == TOP_FRAMES:
            break
    return found


def assert_line(ctx):
    """The line of the assert or abort that the crash node found."""
    found = daemon_hits(ctx, 'failed_assert', ctx.facts.get('daemon'))
    return found[0] if found else None


# ObjectStore::Transaction's op codes (src/os/Transaction.h), which are on disk
# and do not change.
OPS = {0: 'OP_NOP', 7: 'OP_CREATE', 8: 'OP_COLL_MOVE', 9: 'OP_TOUCH', 10: 'OP_WRITE',
       11: 'OP_ZERO', 12: 'OP_TRUNCATE', 13: 'OP_REMOVE', 14: 'OP_SETATTR',
       15: 'OP_SETATTRS', 16: 'OP_RMATTR', 17: 'OP_CLONE', 18: 'OP_CLONERANGE',
       20: 'OP_MKCOLL', 21: 'OP_RMCOLL', 22: 'OP_COLL_ADD', 23: 'OP_COLL_REMOVE',
       24: 'OP_COLL_SETATTR', 25: 'OP_COLL_RMATTR', 26: 'OP_COLL_SETATTRS', 28: 'OP_RMATTRS',
       29: 'OP_COLL_RENAME', 31: 'OP_OMAP_CLEAR', 32: 'OP_OMAP_SETKEYS',
       33: 'OP_OMAP_RMKEYS', 34: 'OP_OMAP_SETHEADER', 35: 'OP_SPLIT_COLLECTION',
       37: 'OP_OMAP_RMKEYRANGE', 38: 'OP_COLL_MOVE_RENAME', 39: 'OP_SETALLOCHINT',
       40: 'OP_COLL_HINT', 41: 'OP_TRY_RENAME', 42: 'OP_COLL_SET_BITS',
       43: 'OP_MERGE_COLLECTION', 44: 'OP_TOUCH_TEMP'}

# The parts of a command line that every command teuthology runs has.
NOISE = re.compile(r"(sudo |adjust-ulimits |ceph-coverage \S+ |timeout \d+ |daemon-helper \w+ |"
                   r"CEPH_CLIENT_ID=\d+ |TESTDIR=\S+ |--cluster ceph |bash -c |/bin/sh -c |"
                   r"/home/ubuntu/cephtest/)")


def command_name(command):
    """The command without the noise around it, and only its first words."""
    words = re.sub(r'[\\\'"]', '', NOISE.sub('', command))
    # Temporary names, which differ every time.
    words = re.sub(r'\btmp\.[\w.]+', 'tmp.*', words).split()
    return ' '.join(w for w in words[:8] if not w.startswith('--format'))


def at(hit):
    """When hit was logged, for a sentence."""
    time = when(hit)
    return f' at {time:%H:%M:%S}' if time else ''


def before(hits, line):
    return [h for h in hits if h.line < line]


def after(hits, line):
    return [h for h in hits if h.line > line]
