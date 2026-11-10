"""A workunit failed: which of its tests, and on which check?"""

import re

from qa_triage.tree.common import before, command_name, failure, short
from qa_triage.tree.core import done, judge, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374619': 'workunit/unexplained',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374749': 'workunit/set-e',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122165': 'workunit/unexplained',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122185': 'workunit/standalone',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122190': 'workunit/set-e',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127731': 'workunit/standalone',
    'skanta-2026-09-18_02:38:43-rados-wip-bharath24-testing-20260918.001059-main-distro-default-trial/565929': 'workunit/gtest',
    'skanta-2026-09-18_23:45:18-rados-wip-bharath3-testing-20260918.154843-main-distro-default-trial/567985': 'workunit/gtest',
}


# xtrace of a qa/standalone script: script:line: function:  command. The first
# TEST_ function that returns 1 is the one that failed, at its check.
TEST_RETURN = r'/([\w.-]+\.sh):(\d+): (TEST_\w+):\s+return 1\s*$'


# xtrace of a script run with set -x: + command
XTRACE = re.compile(r'tasks\.workunit\.[\w.-]+\.stderr:\++ (.+)')


ERROR = re.compile(r'\b(Error|ERROR|error|FAILED|[Ff]ailed|E[A-Z]{3,}\b|Traceback|'
                   r'[Aa]ssert)')


@node('workunit', 'Which test of the workunit failed, and on what?')
def workunit(ctx):
    script, fail = ctx.facts['workunit'], failure(ctx)
    found = ctx.search('teuthology.log', TEST_RETURN, ':  return 1', max_hits=1)
    if found and (not fail or found[0].line < fail.line):
        hit = found[0]
        source, line, test = re.search(TEST_RETURN, hit.text).groups()
        # The line before the return is the check that failed, or what it printed.
        checked = [h for h in ctx.between(hit.line - 2, hit.line - 1)
                   if 'tasks.workunit.' in h.text]
        return done('standalone', f'{test} failed: its check at {source}:{line} returned 1',
                    *checked, hit, key=f'{script} {test}', test=test,
                    check=f'{source}:{line}', script=script)
    gtests = before(ctx.hits('gtest'), fail.line) if fail else ctx.hits('gtest')
    if gtests:
        name = ctx.match('gtest', gtests[0]).group(1)
        return done('gtest', f'the gtest {name} failed', gtests[0], key=f'{script} {name}',
                    test=name, script=script)
    said = [h for h in ctx.between(max(1, fail.line - 80), fail.line)
            if 'tasks.workunit.' in h.text] if fail else []
    # A script run with set -ex: the last command it traced is the one that failed.
    traced = [i for i, h in enumerate(said) if XTRACE.search(h.text)]
    if traced:
        ran = said[traced[-1]]
        command = XTRACE.search(ran.text).group(1)
        if re.match(r'exit \d+$', command) and len(traced) > 1:
            # The script chose to fail: the check before the exit is what failed,
            # with the commands that gave it its values.
            checked = [XTRACE.search(said[i].text).group(1) for i in traced[-4:-1]]
            return done('set-e', f'it exited after the check "{short(checked[-1])}", '
                                 f'given by {"; ".join(short(c, 60) for c in checked[:-1])}',
                        *[said[i] for i in traced[-4:]],
                        key=f'{script}: {"; ".join(command_name(c) for c in checked)}',
                        script=script, command=command_name(checked[-1]))
        # The last command that printed an error: a trap that cleans up on exit
        # runs, and is traced, after the command that failed.
        for k in reversed(range(len(traced))):
            end = traced[k + 1] if k + 1 < len(traced) else len(said)
            errors = [h for h in said[traced[k] + 1:end] if ERROR.search(h.text)]
            if errors:
                ran = said[traced[k]]
                command = XTRACE.search(ran.text).group(1)
                error = errors[0].text.split('std', 1)[-1].split(':', 1)[-1].strip()
                return done('set-e', f'the last command that printed an error: '
                                     f'"{short(command)}": {short(error)}', ran, errors[0],
                            key=f'{script}: {command_name(command)}: {error}',
                            script=script, command=command_name(command),
                            error=short(error))
    return judge(f'Why did {script} fail?', 'the log shows no TEST_ function, gtest or '
                 'traced command that failed; the last lines the workunit wrote are shown',
                 *(said[-6:] or [fail]), branch='unexplained', key=script, script=script)
