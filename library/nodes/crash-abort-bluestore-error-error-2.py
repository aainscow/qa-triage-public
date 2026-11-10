import re

from qa_triage.tree.core import done, node, unknown
from qa_triage.tree.common import at

# This job, and every job the file was written for, with the path each takes.
EXAMPLES = {
    'skanta-2026-10-12_16:15:49-rados-wip-bharath5-testing-20261012.095720-main-distro-default-trial/628557':
        'crash/abort/bluestore-error/error-2/enoent-on-object-removed-as-divergent',
}

NOT_HANDLED = r'_txc_add_transaction error \((-?\d+)\) (.+?) not handled on operation (\d+)'


@node('crash/abort/bluestore-error/error-2',
      'Why did the shard not have the object it was sent a write for?')
def bluestore_error_2_cause(ctx):
    who = ctx.facts.get('daemon', '')
    obj = ctx.facts.get('object', '')
    if not who or not obj:
        return unknown('the tree did not name the daemon and the object')
    log = f'remote/*/log/ceph-{who}.log.gz'
    found = ctx.search(log, NOT_HANDLED, 'not handled on operation', max_hits=1)
    if not found:
        return unknown(f'{who}\'s log does not say which error BlueStore did not handle')
    error = found[0]
    # 1#3:45cfbf7e:::name:95# is shard 1 of the object PGLog names 3:45cfbf7e:::name:95.
    hobj = re.match(r'(?:[0-9a-f]+#)?(.+?)#[0-9a-f]*$', obj)
    if not hobj:
        return unknown(f'{obj} is not an object name this node reads', error)
    hobj = hobj.group(1)
    removed = [h for h in ctx.search(log, r'_merge_object_divergent_entries: hoid ' +
                                     re.escape(hobj) + r' cannot roll back, removing and '
                                     r'adding to missing',
                                     'cannot roll back, removing and adding to missing',
                                     max_hits=5)
               if h.file == error.file and h.line < error.line]
    if not removed:
        return unknown(f'{who} did not log removing {obj} as a divergent entry', error)
    op = ctx.facts.get('op', 'a write')
    name = re.sub(r'\(.*', '', ctx.facts.get('caller', ''))
    return done('enoent-on-object-removed-as-divergent',
                f'BlueStore on {who} got ENOENT{at(error)} in {op} of {obj}' +
                (f', a write sent by {name}' if name else '') +
                f': {who} had removed that object{at(removed[-1])}, as a divergent log '
                'entry it could not roll back, and added it to its missing set, but the '
                'primary still sent it the write as if the shard had the object',
                removed[-1], error,
                key=f'ENOENT in {op} of an object removed as divergent' +
                    (f', from {name}' if name else ''))
# learned: 2026-11-05 Bob, from skanta-2026-10-12_16:15:49-rados-wip-bharath5-testing-20261012.095720-main-distro-default-trial/628557, as qa_triage (bob+files) said
