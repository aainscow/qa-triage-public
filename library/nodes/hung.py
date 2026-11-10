"""Jobs that waited: hung until teuthology killed them, or timed out waiting
for the cluster. What they waited for says what failed, but rarely why."""

import re

from qa_triage.tree.common import at, before, command_name
from qa_triage.tree.core import done, judge, node, unknown

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374705': 'hung/test',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374729': 'hung/command',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122172': 'hung/command',
    'skanta-2026-06-04_15:14:33-rados-wip-bharath7-testing-2026-06-04-1630-distro-default-trial/265491': 'hung/test',
}


def command_text(ctx, ran):
    """The command teuthology logged last in ran, its command lines. A script
    given to bash -c is logged one line at a time: its lines are joined, without
    the ones that only set up the shell."""
    lines = [ran[-1]]
    for hit in reversed(ran[:-1]):
        if hit.line != lines[0].line - 1:
            break
        lines.insert(0, hit)
    if len(lines) == 1:
        return command_name(ctx.match('command', lines[0]).group(2))
    script = [ctx.match('command', h).group(2).strip() for h in lines[1:]]
    script = [s for s in script if s.strip("'") and not re.match(r'set -\w+$|\w+=\S*$', s)]
    return '; '.join(script[-2:])


@node('hung', 'What was the job doing when it was killed?')
def hung(ctx):
    killed = ctx.last('killed')
    end = killed.line if killed else float('inf')
    started = before(ctx.hits('task'), end)
    task = ctx.match('task', started[-1]).group(1) if started else ''
    waiting = before(ctx.hits('waiting_test'), end)
    if waiting:
        test, waited = ctx.match('waiting_test', waiting[-1]).groups()
        return done('test', f'the {task} task was still waiting for the test {test}, after '
                            f'{waited} seconds', waiting[-1], key=f'waiting for {test}',
                    test=test, hung_task=task)
    ran = before(ctx.hits('command'), end)
    if not ran:
        return unknown('the job ran no command before it was killed', killed,
                       branch='no-command')
    host = ctx.match('command', ran[-1]).group(1)
    name = command_text(ctx, ran)
    return judge(f'Why did the {task} task hang?', f'the last command it ran was "{name}" on '
                 f'{host}{at(ran[-1])}', ran[-1], killed, branch='command',
                 key=f'{task}: {name}', hung_task=task, command=name)
