import re

from qa_triage.tree.common import assert_line
from qa_triage.tree.core import done, go, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374634': 'crash/abort/bluestore-error',
    'skanta-2026-06-02_04:58:02-rados-wip-bharath3-testing-2026-06-02-0837-distro-default-trial/253356': 'crash/abort/specific',
    'skanta-2026-08-25_11:49:18-rados-wip-bharath3-testing-20260824.233415-main-distro-default-trial/513109': 'crash/abort/specific',
}


GENERIC_ABORTS = {
    # message (a regex) and source: the branch that gathers what tells them apart
    'bluestore-error': (r'unexpected error', r'os/bluestore/BlueStore\.cc'),
}


@node('crash/abort', 'Is the abort a generic one, which needs more evidence?')
def crash_abort(ctx):
    for branch, (message, source) in GENERIC_ABORTS.items():
        if re.search(message, ctx.facts['message']) and re.search(source, ctx.facts['source']):
            return go(branch, f'"{ctx.facts["message"]}" is aborted on for many errors: '
                              'what went wrong is in the lines before it', assert_line(ctx))
    return done('specific', f'"{ctx.facts["message"]}" in {ctx.facts["source"]} names '
                            'what went wrong', assert_line(ctx))
