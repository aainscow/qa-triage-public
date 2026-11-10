import re

from qa_triage.tree.common import at, before, failure, short
from qa_triage.tree.core import done, judge, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374853': 'timeout/slow-ops',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127742': 'timeout/unexplained',
    'skanta-2026-04-09_03:57:47-rados-wip-bharath16-testing-2026-04-08-1405-distro-default-trial/141099': 'timeout/slow-ops',
    'skanta-2026-04-10_13:01:53-rados-wip-bharath4-testing-2026-04-10-1224-distro-default-trial/143048': 'timeout/unexplained',
    'skanta-2026-07-01_13:33:05-rados-wip-bharath1-testing-2026-07-01-0424-distro-default-trial/331681': 'timeout/admin-socket',
    'skanta-2026-07-26_00:40:46-rados-wip-bharath7-testing-20260724.101654-main-distro-default-trial/400500': 'timeout/admin-socket',
}


WAITS = (
    (r'timeout expired in (\w+)', None),
    (r'(\w+): failed before timeout expired', None),
    (r'expired waiting for healthy cluster', 'wait_until_healthy'),
    (r'snap trimming before timeout', 'snap_trimming'),
    (r'reached maximum tries', 'MaxWhileTries'),
)


@node('timeout', 'What did the job wait for, and what stopped it?')
def timeout(ctx):
    reason, fail = ctx.job.reason, failure(ctx)
    wait = ''
    for pattern, name in WAITS:
        found = re.search(pattern, reason)
        if found:
            wait = name or found.group(1)
            break
    wait = wait or short(reason, 60)
    line = fail.line if fail else float('inf')
    hung = before(ctx.hits('asok_timeout'), line)
    if hung:
        return done('admin-socket', f'{wait} timed out, and before it an admin socket '
                                    f'command timed out{at(hung[0])}: a daemon stopped '
                                    'answering', hung[0], fail,
                    key=f'{wait}: admin socket timed out', wait=wait)
    slow = before(ctx.hits('slow_ops'), line)
    if slow:
        return judge(f'What blocked the ops, so that {wait} timed out?',
                     f'{wait} timed out, and there were slow ops from{at(slow[0])}', slow[0],
                     fail, branch='slow-ops', key=f'{wait}: slow ops', wait=wait)
    return judge(f'Why did {wait} not finish?', f'{wait} timed out, and the log shows no '
                 'admin socket hang and no slow ops before it', fail, branch='unexplained',
                 key=wait, wait=wait)
