from qa_triage.tree.core import done, node, unknown
from qa_triage.tree.common import at

# This job, and every job the file was written for, with the path each takes.
EXAMPLES = {
    'skanta-2026-04-13_14:09:46-rados-wip-bharath11-testing-2026-04-13-1517-distro-default-trial/148640':
        'command/timed-out/thrasher-min-size-pgs-not-active',
}


@node('command/timed-out', 'Why did the command time out?')
def command_timed_out(ctx):
    failed = ctx.search('teuthology.log',
                        r'daemon_watchdog:OSDThrasher failed',
                        'OSDThrasher failed')
    waited = ctx.search('teuthology.log',
                        r"'check for active or peered' reached maximum tries",
                        'check for active or peered')
    if failed and waited:
        return done('thrasher-min-size-pgs-not-active',
                    f'the OSDThrasher failed{at(failed[0])}: PGs did not become active or peered '
                    f'after killing OSDs{at(waited[0])}, so the watchdog killed all daemons and '
                    'subsequent ceph CLI calls timed out',
                    failed[0], waited[0],
                    key='OSDThrasher test_pool_min_size: PGs not active or peered',
                    thrasher_failure='test_pool_min_size')
    return unknown('none of the command timeouts this node knows')
# learned: 2026-10-30 bob-analyst, from skanta-2026-04-13_14:09:46-rados-wip-bharath11-testing-2026-04-13-1517-distro-default-trial/148640, as the QA reviewers and the tracker, via qa_triage (bob-analyst) said
