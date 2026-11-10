import re

from qa_triage.tree.common import failure, logged, short
from qa_triage.tree.core import done, judge, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122162': 'cluster-log/message',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122167': 'cluster-log/health',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127723': 'cluster-log/health',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127725': 'cluster-log/message',
}


CLUSTER_LOG = re.compile(r'"\S+ (\S+) \(\S+\) \d+ : cluster \[(\w+)\] (.*)" in cluster log')


HEALTH = re.compile(r'Health check failed: (.*) \((\w+)\)')


@node('cluster-log', 'What did the cluster log report, and which daemon?')
def cluster_log(ctx):
    reason = ctx.job.reason
    found = CLUSTER_LOG.search(reason)
    line = logged(ctx) or failure(ctx)
    if not found:
        return judge('What did the cluster log report?', 'the failure reason is not in the '
                     'usual form', line, branch='other', key=reason)
    who, level, message = found.groups()
    health = HEALTH.search(message)
    if health:
        return done('health', f'{who} logged [{level}] health check {health.group(2)}: '
                              f'{health.group(1)}', line,
                    key=f'{level} {health.group(2)}', level=level, check=health.group(2),
                    message=message, reporter=who)
    return done('message', f'{who} logged [{level}] {short(message)}', line,
                key=f'{level} {who.split(".")[0]} {message}', level=level, message=message,
                reporter=who)
