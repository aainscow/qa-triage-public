"""A daemon or a client crashed: which, and how? An assert says where; a generic
one, such as BlueStore's "unexpected error", needs more from the daemon's log
before it tells one problem from another."""

import re

from qa_triage.tree.common import (at, daemon, daemon_hits, failure, first_crash, frame_lines,
                                   frames)
from qa_triage.tree.core import go, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374634': 'crash/abort',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122164': 'crash/client',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122186': 'crash/assert',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127709': 'crash/client',
    'skanta-2026-04-09_03:57:47-rados-wip-bharath16-testing-2026-04-08-1405-distro-default-trial/141122': 'crash/assert',
    'skanta-2026-06-02_04:58:02-rados-wip-bharath3-testing-2026-06-02-0837-distro-default-trial/253356': 'crash/abort',
}


@node('crash', 'Which process crashed, and how?')
def crash(ctx):
    first = first_crash(ctx)
    if not first:
        return go('client', 'no daemon logged a crash, so it was a client or a test '
                            'program', ctx.first('coredump') or failure(ctx))
    who, host = daemon(first)
    kind = who.split('.')[0]
    asserts = daemon_hits(ctx, 'failed_assert', who)
    where = daemon_hits(ctx, 'in_function', who)
    function = ctx.match('in_function', where[0]).group(4) if where else ''
    facts = {'daemon': who, 'daemon_type': kind, 'host': host}
    if asserts:
        hit = asserts[0]
        source, line, message = ctx.match('failed_assert', hit).groups()[2:]
        source = re.sub(r'^.*?/src/', '', source)
        branch = 'assert' if message.startswith('FAILED') else 'abort'
        in_function = f' in {function}' if function else ''
        # With the top of its backtrace, which says where it was called from.
        return go(branch, f'{who} on {host}{at(hit)}: {message} at {source}:{line}'
                          f'{in_function}', where[0] if where else None, hit,
                  *frame_lines(ctx, who, hit.line).values(),
                  key=f'{kind} {source} {message}{in_function}', source=source,
                  line=line, message=message, function=function, **facts)
    signals = daemon_hits(ctx, 'signal', who)
    signal = ctx.match('signal', signals[0]).group(3) if signals else 'a signal'
    top = frames(ctx, who, first.line)
    return go('signal', f'{who} on {host} got {signal}{at(first)}' +
                        (f', in {" < ".join(top)}' if top else ''), first,
              key=f'{kind} {signal} in {" < ".join(top) or "?"}', signal=signal,
              frames=' < '.join(top), **facts)
