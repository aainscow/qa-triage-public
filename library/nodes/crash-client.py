import re

from qa_triage.tree.common import command_name, failure, short
from qa_triage.tree.core import done, judge, node, unknown

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122164': 'crash/client/unnamed',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127709': 'crash/client/unnamed',
    'skanta-2026-09-30_04:36:41-rados-wip-bharath14-testing-20260930.003013-main-distro-default-trial/593684': 'crash/client/ceph_test_rados',
    'skanta-2026-09-30_21:49:09-rados-wip-bharath8-testing-20260930.133801-main-distro-default-trial/597588': 'crash/client/ceph_test_rados',
    'skanta-2026-10-03_10:12:06-rados-wip-bharath19-testing-20261002.123021-main-distro-default-trial/604392': 'crash/client/unexplained',
}


# A program, in a command teuthology ran.
PROGRAM = re.compile(r'\b(ceph_test_\w+|ceph_\w+|ceph-\w+|rados|rbd|ceph)\b')


@node('crash/client', 'Which program crashed, and what did it say last?')
def crash_client(ctx):
    reason, fail = ctx.job.reason, failure(ctx)
    program = PROGRAM.search(command_name(reason.split(':', 1)[-1]))
    if not program or not fail:
        return unknown('the failure reason does not name the program', fail,
                       branch='unnamed')
    name = program.group(1)
    said = [h for h in ctx.between(max(1, fail.line - 200), fail.line)
            if re.search(r'\.std(err|out):.*\b(error|Error|ERROR|failed|FAILED|abort|'
                         r'assert|Assertion)', h.text) and 'orchestra.run' not in h.text]
    if not said:
        return judge(f'Why did {name} fail?', f'{name} failed, and said nothing that looks '
                     'like an error in the lines before', fail, branch='unexplained', key=name,
                     program=name)
    last = said[-1]
    message = re.sub(r'^.*?\.std(err|out):', '', last.text)
    return done(name, f'{name} failed; the last error it printed: {short(message)}',
                last, key=f'{name}: {message}', program=name, error=short(message))
