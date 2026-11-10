from qa_triage.tree.common import assert_line
from qa_triage.tree.core import done, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122186': 'crash/assert/specific',
    'skanta-2026-04-09_03:57:47-rados-wip-bharath16-testing-2026-04-08-1405-distro-default-trial/141122': 'crash/assert/specific',
}


@node('crash/assert', 'Is the assert one that tells problems apart by itself?')
def crash_assert(ctx):
    return done('specific', f'"{ctx.facts["message"]}" in {ctx.facts["source"]} names the '
                            'check that failed', assert_line(ctx))
