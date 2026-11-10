import re

from qa_triage.tree.common import OPS, assert_line, frames
from qa_triage.tree.core import done, judge, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374634': 'crash/abort/bluestore-error/unexplained',
}


@node('crash/abort/bluestore-error',
      'Which error did BlueStore not handle, in which operation, from where?')
def bluestore_error(ctx):
    who = ctx.facts['daemon']
    log = f'remote/*/log/ceph-{who}.log.gz'
    found = ctx.search(log, r'_txc_add_transaction (error \((-?\d+)\) .* not handled on '
                            r'operation (\d+)|op (\d+) got \S+ on (\S+))',
                       '_txc_add_transaction ', max_hits=4) or \
        ctx.search('teuthology.log', r'_txc_add_transaction (error|op \d+ got)',
                   '_txc_add_transaction ', max_hits=4)
    if not found:
        return judge('Which error did BlueStore not handle?', f'{who}\'s log does not say '
                     'which error it was', assert_line(ctx), branch='unexplained',
                     key='no error found')
    error = re.search(r'error \((-?\d+)\) (.*?) not handled on operation (\d+)',
                      ' '.join(h.text for h in found))
    obj = re.search(r'op \d+ got \S+ on (\S+)', ' '.join(h.text for h in found))
    top = frames(ctx, who, 0)
    caller = next((f for f in top if 'BlueStore' not in f and 'ObjectStore' not in f), '')
    if not error:
        return judge('Which error did BlueStore not handle?', 'the log names an operation '
                     'but not the error', *found, branch='unexplained', key='no error found')
    code, text, op = error.groups()
    op = OPS.get(int(op), f'op {op}')
    return done(f'error-{code.lstrip("-")}',
                f'BlueStore did not handle error {code} ({text}) in {op}' +
                (f' of {obj.group(1)}' if obj else '') +
                (f', from {caller}' if caller else ''), *found[:2],
                key=f'error {code} in {op}' + (f' from {caller}' if caller else ''),
                error=code, op=op, caller=caller, object=obj.group(1) if obj else '')
