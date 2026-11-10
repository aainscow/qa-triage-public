import re

from qa_triage.tree.core import done, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122168': 'valgrind/Leak_StillReachable',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127713': 'valgrind/Leak_StillReachable',
    'skanta-2026-07-19_00:55:49-rados-wip-bharath5-testing-2026-07-19-0500-distro-default-trial/376388': 'valgrind/InvalidRead',
    'skanta-2026-08-13_04:40:13-rados-wip-bharath8-testing-20260813.003211-main-distro-default-trial/464335': 'valgrind/InvalidRead',
}


@node('valgrind', 'Which daemon did valgrind find an error in, and what error?')
def valgrind(ctx):
    reason = ctx.job.reason
    found = re.match(r'valgrind error: (\w+) ?(.*)', reason)
    kind, frames = (found.group(1), found.group(2)) if found else ('', '')
    where = [ctx.match('valgrind', h) for h in ctx.hits('valgrind')]
    daemons = sorted({d for m in where if m for d in re.findall(r'valgrind/([a-z]+\.\w+)\.log',
                                                                  m.group(0))})
    return done(kind or 'error', f'valgrind reported {kind or "an error"}' +
                (f' in {frames}' if frames else '') +
                (f', in the log of {", ".join(daemons)}' if daemons else ''),
                ctx.first('valgrind'), key=reason, kind=kind, frames=frames,
                daemons=' '.join(daemons))
