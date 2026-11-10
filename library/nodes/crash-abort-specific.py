import re

from qa_triage.tree.core import done, node, unknown
from qa_triage.tree.common import at

# This job, and every job the file was written for, with the path each takes.
EXAMPLES = {
    'skanta-2026-09-20_00:25:34-rados-wip-bharath25-testing-20260918.001212-main-distro-default-trial/570014': 'crash/abort/specific/cls-2pc-queue-concurrent-op-order',
    'skanta-2026-06-02_04:58:02-rados-wip-bharath3-testing-2026-06-02-0837-distro-default-trial/253356': 'crash/abort/specific/remote-reservation-rejected-too-full',
    'jayaprakash-2026-06-03_09:44:39-rados-wip-jaya-bs-faster-alloc-recovery-distro-default-trial/258807': 'crash/abort/specific/manifest-recovery-out-of-order-op',
}


@node('crash/abort/specific', 'What caused the abort?')
def crash_abort_specific(ctx):
    if ctx.facts.get('source') == 'osd/PrimaryLogPG.cc' and 'out of order op' in ctx.facts.get('message', ''):
        bad_order = ctx.search(
            'teuthology.log',
            r'bad op order, already applied (\d+) > this (\d+)',
            'bad op order',
        )
        if bad_order:
            runs = ctx.search('teuthology.log', r'\[ RUN\s+\] (\S+)', '[ RUN', max_hits=200)
            before = [h for h in runs if h.line < bad_order[0].line]
            test = re.search(r'\[ RUN\s+\] (\S+)', before[-1].text).group(1) if before else ''
            if test.startswith('TestCls2PCQueue.'):
                applied, this_tid = re.search(
                    r'bad op order, already applied (\d+) > this (\d+)', bad_order[0].text
                ).groups()
                return done(
                    'cls-2pc-queue-concurrent-op-order',
                    f'{test} ran concurrent client threads without op ordering, and osd_debug_op_order aborted '
                    f'when op {this_tid} arrived after {applied}{at(bad_order[0])}',
                    bad_order[0],
                    key=f'{test}: bad op order from concurrent threads with osd_debug_op_order',
                    test=test,
                )
            # Is the bad op order on a replicated pool pg (pg[N.X(, no EC shard suffix s\d+)?
            order_m = re.search(
                r'pg\[(\d+\.[0-9a-f]+)\(.*\] bad op order, already applied (\d+) > this (\d+)',
                bad_order[0].text,
            )
            if order_m:
                pg = order_m.group(1)
                applied, this_tid = order_m.group(2), order_m.group(3)
                abort = ctx.search(
                    'teuthology.log',
                    r'PrimaryLogPG\.cc: \d+: ceph_abort_msg\("out of order op"\)',
                    'ceph_abort_msg("out of order op")',
                )
                daemon = ctx.facts.get('daemon', 'osd')
                return done(
                    'manifest-recovery-out-of-order-op',
                    f'{daemon} aborted on ceph_abort_msg("out of order op") in '
                    f'PrimaryLogPG::execute_ctx{at(bad_order[0])}: on replicated pg {pg}, '
                    f'op {this_tid} arrived after {applied} was already applied, reordered '
                    'during recovery of a manifest object',
                    bad_order[0],
                    *([abort[0]] if abort else []),
                    key='PrimaryLogPG.cc ceph_abort_msg("out of order op") on replicated pg '
                        'during manifest recovery',
                    source='osd/PrimaryLogPG.cc',
                    message='ceph_abort_msg("out of order op")',
                    pg=pg,
                )
    if (ctx.facts.get('source') == 'osd/PeeringState.cc' and
            'we got a bad state machine event' in ctx.facts.get('message', '')):
        rejected = ctx.search(
            'remote/*/log/ceph-osd.*.log.gz',
            r'RemoteReservationRejectedTooFull',
            'RemoteReservationRejectedTooFull',
        )
        if rejected:
            return done(
                'remote-reservation-rejected-too-full',
                f'{ctx.facts.get("daemon", "osd")} crashed: a late RemoteReservationRejectedTooFull '
                f'arrived in WaitLocalBackfillReserved state, hitting the bad state machine event '
                f'abort{at(rejected[0])}',
                rejected[0],
                key='PeeringState::Crashed: RemoteReservationRejectedTooFull in WaitLocalBackfillReserved',
            )
    return unknown('none of the abort causes this node knows')
# learned: 2026-10-30 bob-analyst, from jayaprakash-2026-06-03_09:44:39-rados-wip-jaya-bs-faster-alloc-recovery-distro-default-trial/258807, as the QA reviewers and the tracker, via qa_triage (bob-analyst) said
