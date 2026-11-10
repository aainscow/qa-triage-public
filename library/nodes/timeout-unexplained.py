import re

from qa_triage.tree.core import done, node, unknown
from qa_triage.tree.common import at

EXAMPLES = {
    'skanta-2026-09-09_16:03:40-rados-wip-bharath14-testing-20260909.090422-main-distro-default-trial/546238': 'timeout/unexplained/thrasher-wait-for-recovery-failed',
    'jenkins-build-2026-09-17_06:05:09-rados-main-distro-default-trial/563110': 'timeout/unexplained/thrasher-min-size-pgs-not-active',
    'skanta-2026-04-21_10:56:08-rados-wip-bharath2-testing-2026-04-21-1241-distro-default-trial/165894': 'timeout/unexplained/registry-pull-failed',
    'skanta-2026-05-07_14:11:34-rados-wip-bharath15-testing-2026-05-07-0507-distro-default-trial/197901': 'timeout/unexplained/thrasher-min-size-list-remove',
    'skanta-2026-04-21_10:56:08-rados-wip-bharath2-testing-2026-04-21-1241-distro-default-trial/166131': 'timeout/unexplained/osds-not-deployed',
    'jayaprakash-2026-04-29_15:21:37-rados-wip-jaya-bs-testing-tentacle-29-04-2026-distro-default-trial/185886': 'timeout/unexplained/host-config-key-missing',
    'jayaprakash-2026-06-19_10:43:50-rados-wip-jaya-bs-testing-18-06-2026-distro-default-trial/300531': 'timeout/unexplained/thrasher-wait-for-clean-pg-down',
    'skanta-2026-11-04_12:54:04-rados-wip-bharath9-testing-20261104.043109-main-distro-default-trial/712417': 'timeout/unexplained/mons-not-in-monmap',
}


@node('timeout/unexplained', 'Why did the wait not finish?')
def timeout_unexplained(ctx):
    failed = ctx.search(
        'teuthology.log',
        r'AssertionError: wait_for_recovery: failed before timeout expired',
        'wait_for_recovery: failed before timeout expired',
    )
    barked = ctx.search(
        'teuthology.log',
        r'daemon_watchdog:OSDThrasher failed',
        'OSDThrasher failed',
    )
    if failed and barked:
        return done(
            'thrasher-wait-for-recovery-failed',
            f'the OSDThrasher gave up in wait_for_recovery{at(failed[0])} and the daemon '
            f'watchdog killed the daemons{at(barked[0])}, so the task waiting on the workload '
            'could only time out',
            failed[0],
            barked[0],
            key='wait_for_recovery failed, watchdog killed daemons, then a wait timed out',
            wait='wait_for_recovery',
        )
    frame = ctx.search('teuthology.log', r'in test_pool_min_size$', 'in test_pool_min_size')
    waited = ctx.search('teuthology.log',
                        r"MaxWhileTries: 'check for active or peered' reached maximum tries",
                        "'check for active or peered'")
    if frame and waited:
        window = ctx.between(max(1, waited[0].line - 400), waited[0].line)
        stuck = [h for h in window if re.search(r'PG \S+ is not active or peered', h.text)]
        pg = state = ''
        if stuck:
            pg = re.search(r'PG (\S+) is not active or peered', stuck[-1].text).group(1)
            stats = [h for h in window if h.line == stuck[-1].line + 1]
            found = re.search(r"'state': '([^']*)'" , stats[0].text) if stats else None
            state = found.group(1) if found else ''
        facts = {'thrasher_failure': 'test_pool_min_size'}
        if state:
            facts['pg_state'] = state
        what = f'PG {pg} stayed {state}' if pg and state else 'PGs stayed neither active nor peered'
        return done(
            'thrasher-min-size-pgs-not-active',
            f'the OSDThrasher failed in test_pool_min_size{at(waited[0])}: after it killed '
            f"OSDs of a PG's acting set, {what} through every check, so the daemon "
            'watchdog killed all daemons and the waiting task could only time out',
            frame[0],
            waited[0],
            stuck[-1] if stuck else None,
            barked[0] if barked else None,
            key='OSDThrasher test_pool_min_size: PGs not active or peered',
            **facts,
        )
    value_error = ctx.search(
        'teuthology.log',
        r'ValueError: list\.remove\(x\): x not in list',
        'list.remove(x): x not in list',
    )
    if frame and value_error:
        return done(
            'thrasher-min-size-list-remove',
            f"the OSDThrasher's test_pool_min_size tried to kill an OSD that was already "
            f'dead{at(value_error[0])}: live_osds.remove raised ValueError because the OSD '
            'was not in live_osds, so the daemon watchdog barked',
            frame[0],
            value_error[0],
            barked[0] if barked else None,
            key='OSDThrasher test_pool_min_size: kill_osd ValueError list.remove',
            thrasher_failure='test_pool_min_size',
        )
    placed = ctx.search(
        'teuthology.log',
        r'Failed while placing \S+ on \S+: .*(?:unexpected HTTP status: 50\d|TLS handshake timeout)',
        'Failed while placing',
    )
    health = ctx.search(
        'teuthology.log',
        r'Health check failed: Failed to place \d+ daemon\(s\) \(CEPHADM_DAEMON_PLACE_FAIL\)',
        'CEPHADM_DAEMON_PLACE_FAIL',
    )
    if placed and health:
        daemon = re.search(r'Failed while placing (\S+) on', placed[0].text).group(1)
        return done(
            'registry-pull-failed',
            f'cephadm could not place {daemon}{at(placed[0])} because the container registry '
            'refused its image pull (a 5xx or TLS timeout from quay.io), so the daemon '
            'never started and the wait for it could only time out',
            placed[0],
            health[0],
            key='cephadm: daemon placement failed, container registry error',
            error='registry-pull-failed',
        )
    waiting_osds = ctx.search(
        'teuthology.log',
        r'tasks\.cephadm:Waiting for \d+ OSDs to come up\.\.\.',
        'OSDs to come up',
    )
    frame_osds = ctx.search('teuthology.log', r'in ceph_osds', 'in ceph_osds')
    if waiting_osds and frame_osds:
        m = re.search(r'Waiting for (\d+) OSDs', waiting_osds[0].text)
        n = m.group(1) if m else '?'
        return done(
            'osds-not-deployed',
            f'the cephadm task waited for {n} OSDs to come up in ceph_osds{at(waiting_osds[0])} '
            f'but not all devices became OSDs in time, so the wait timed out{at(frame_osds[0])}',
            waiting_osds[0],
            frame_osds[0],
            key='cephadm ceph_osds: OSDs did not all deploy in time',
        )
    host_timeout = ctx.search(
        'teuthology.log',
        r'tasks\.cephadm:Hit timeout while adding host \S+: reached maximum tries',
        'Hit timeout while adding host',
    )
    host_config_key = ctx.search(
        'teuthology.log',
        r'Host got config-key entry: False',
        'Host got config-key entry: False',
    )
    if host_timeout and host_config_key:
        m = re.search(r'adding host (\S+):', host_timeout[0].text)
        host = m.group(1) if m else 'a host'
        return done(
            'host-config-key-missing',
            f'cephadm timed out waiting for {host} to get a config-key entry{at(host_timeout[0])}: '
            f'the host appeared in ceph orch host ls and in the inventory but its per-host '
            f'config-key entry was never written, so ceph_bootstrap raised MaxWhileTries',
            host_timeout[0],
            host_config_key[0],
            key='cephadm ceph_bootstrap: host config-key entry missing after orch host add',
        )
    clean_frame = ctx.search('teuthology.log', r'in test_map_discontinuity$',
                             'in test_map_discontinuity')
    clean_failed = ctx.search(
        'teuthology.log',
        r'AssertionError: wait_for_clean: failed before timeout expired',
        'wait_for_clean: failed before timeout expired',
    )
    down_pgs = ctx.search(
        'teuthology.log',
        r"'pgid': '[^']+'.*'state': 'down[^']*'",
        "'state': 'down",
    )
    if clean_frame and clean_failed and down_pgs:
        pg = re.search(r"'pgid': '([^']+)'", down_pgs[0].text).group(1)
        state = re.search(r"'state': '(down[^']*)'", down_pgs[0].text).group(1)
        return done(
            'thrasher-wait-for-clean-pg-down',
            f'the OSDThrasher gave up in wait_for_clean{at(clean_failed[0])}, in '
            f'test_map_discontinuity{at(clean_frame[0])}: after it killed an OSD, PG {pg} '
            f'stayed {state}{at(down_pgs[0])} until the timeout ran out, so the daemon '
            'watchdog barked',
            clean_frame[0],
            clean_failed[0],
            down_pgs[0],
            barked[0] if barked else None,
            key='OSDThrasher test_map_discontinuity: wait_for_clean, PG down',
            wait='wait_for_clean',
            thrasher_failure='test_map_discontinuity',
            pg_state=state,
        )
    waiting_mons = ctx.search(
        'teuthology.log',
        r'tasks\.cephadm:Waiting for \d+ mons in monmap\.\.\.',
        'mons in monmap...',
    )
    frame_mons = ctx.search('teuthology.log', r'in ceph_mons', 'in ceph_mons')
    if waiting_mons and frame_mons:
        m = re.search(r'Waiting for (\d+) mons', waiting_mons[0].text)
        n = m.group(1) if m else '?'
        return done(
            'mons-not-in-monmap',
            f'the cephadm task waited for {n} mons in monmap in ceph_mons{at(waiting_mons[0])} '
            f'but not all deployed mons joined the monmap in time, so the wait timed out{at(frame_mons[0])}',
            waiting_mons[0],
            frame_mons[0],
            key='cephadm ceph_mons: mons did not join monmap in time',
        )
    return unknown('none of the failures this node knows')
# learned: 2026-10-30 bob-analyst, from jayaprakash-2026-06-19_10:43:50-rados-wip-jaya-bs-testing-18-06-2026-distro-default-trial/300531, as the QA reviewers and the tracker, via qa_triage (bob-analyst) said
# learned: 2026-11-05 Bob, from skanta-2026-11-04_12:54:04-rados-wip-bharath9-testing-20261104.043109-main-distro-default-trial/712417, as qa_triage (bob+files) said
