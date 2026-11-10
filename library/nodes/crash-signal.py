from qa_triage.tree.common import daemon_hits, frame_lines
from qa_triage.tree.core import done, judge, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-01-26_23:21:06-rados-wip-yuri12-testing-2026-01-22-2045-distro-default-trial/19099': 'crash/signal/backtrace',
    'skanta-2026-01-26_08:54:40-rados-wip-bharath4-testing-2026-01-26-1300-distro-default-trial/17788': 'crash/signal/backtrace',
}


@node('crash/signal', 'Do the frames say where it crashed?')
def crash_signal(ctx):
    who = ctx.facts['daemon']
    signal = next(iter(daemon_hits(ctx, 'signal', who)), None)
    if not ctx.facts.get('frames'):
        return judge('Where did it crash?', 'its backtrace is not in teuthology.log', signal,
                     branch='no-backtrace', key='no backtrace')
    lines = frame_lines(ctx, who, signal.line if signal else 0)
    return done('backtrace', f'it crashed in {ctx.facts["frames"]}', signal, *lines.values())
