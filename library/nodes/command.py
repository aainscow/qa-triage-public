import re

from qa_triage.tree.common import command_name, failure, short
from qa_triage.tree.core import done, judge, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122169': 'command/error',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127714': 'command/error',
    'skanta-2026-04-09_03:57:47-rados-wip-bharath16-testing-2026-04-08-1405-distro-default-trial/141085': 'command/unexplained',
    'skanta-2026-04-13_14:09:46-rados-wip-bharath11-testing-2026-04-13-1517-distro-default-trial/148640': 'command/timed-out',
    'skanta-2026-04-21_10:56:08-rados-wip-bharath2-testing-2026-04-21-1241-distro-default-trial/165899': 'command/unexplained',
    'skanta-2026-05-26_00:44:20-rados-wip-bharath17-testing-2026-05-25-1331-distro-default-trial/237418': 'command/timed-out',
    'skanta-2026-09-24_03:23:39-rados-wip-bharath10-testing-20260924.010810-main-distro-default-trial/577422': 'command/not-found',
}


COMMAND = re.compile(r'Command failed on (\S+) with status (\d+): (.*)')


@node('command', 'Which command failed, and what did it say?')
def command(ctx):
    fail = failure(ctx)
    found = COMMAND.search(ctx.job.reason)
    if not found or not fail:
        return judge('Which command failed?', 'the failure reason does not name it', fail,
                     branch='unnamed', key=ctx.job.reason)
    host, status, line = found.groups()
    name = command_name(line)
    facts = {'command': name, 'status': status, 'host': host}
    if status == '124':
        return done('timed-out', f'"{name}" did not finish before its timeout (status 124)',
                    fail, key=f'{name}: timed out', **facts)
    if name.startswith('grep ') and status == '1':
        return done('not-found', f'"{name}" found nothing', fail, key=f'{name}: not found',
                    **facts)
    # What anything on the host wrote to stderr after the command started.
    window = ctx.between(max(1, fail.line - 120), fail.line)
    start = f'orchestra.run.{host}:> '
    started = [i for i, h in enumerate(window) if start in h.text and
               line.strip('\'" ')[:40] in h.text] or \
        [i for i, h in enumerate(window) if start in h.text]
    said = [h for h in window[started[-1] + 1 if started else 0:]
            if f'.{host}.stderr:' in h.text and h.text.split('stderr:', 1)[1].strip()]
    errors = [h for h in said if re.search(r'\b(Error|ERROR|error|[Ff]ailed|E[A-Z]{3,}\b)',
                                           h.text)]
    if not errors:
        return judge(f'Why did "{name}" fail?', f'"{name}" failed with status {status}, and '
                     'none of what it wrote to stderr looks like an error', *said[-3:], fail,
                     branch='unexplained', key=f'{name}: {status}', **facts)
    message = errors[-1].text.split('stderr:', 1)[1].strip()
    return done('error', f'"{name}" failed with status {status}: {short(message)}',
                errors[-1], fail, key=f'{name}: {message}', error=short(message), **facts)
