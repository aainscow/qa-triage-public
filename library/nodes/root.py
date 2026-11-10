"""The root: what went wrong first? A daemon that crashed before the job failed
explains the failure, whatever teuthology then reported, so crashes come
before everything but the test infrastructure."""

import re

from qa_triage.tree.common import (INTENDED_CRASHES, at, daemon, failure, first_crash, logged,
                                   short, task_at)
from qa_triage.tree.core import go, node, unknown

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374619': 'workunit',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374634': 'crash',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374644': 'infra',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374651': 'test',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374705': 'hung',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374853': 'timeout',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122160': 'test',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122161': 'infra',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122162': 'cluster-log',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122164': 'crash',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122165': 'workunit',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122168': 'valgrind',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122169': 'command',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122172': 'hung',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127713': 'valgrind',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127714': 'command',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127723': 'cluster-log',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127742': 'timeout',
    'skanta-2026-06-04_15:14:33-rados-wip-bharath7-testing-2026-06-04-1630-distro-default-trial/265651': 'other',
    'skanta-2026-06-14_23:24:50-rados-wip-bharath11-testing-2026-06-12-0509-distro-default-trial/293728': 'other',
}


# Tasks that set up the test machines, before any of ceph runs.
INFRA_TASKS = re.compile(r'internal\.\w+|kernel|ansible\.\w+|pcp|selinux|clock|console_log|'
                         r'install(\.\w+)?|nvme_loop|pexec')


# Failures of the test infrastructure, whichever task they were in.
INFRA_REASONS = re.compile(
    r'Failed to fetch package version|VersionNotFoundError|_ansible_|AnsibleFailed|'
    r"'sudo (yum|dnf|apt-get|apt) |cephadm --image \S+ pull'|Error reimaging|"
    r'SSH connection to \S+ was lost|Could not reconnect|ConnectionLostError|'
    r'Failed to power|machine \S+ is locked')


WORKUNIT = re.compile(r'Command failed \(workunit test (\S+)\)')


TIMEOUT = re.compile(r'timeout expired|before timeout|reached maximum tries|timed out|'
                     r'[Tt]imeout \d+s? expired')


@node('', 'What went wrong first?')
def root(ctx):
    if not ctx.marks:
        return unknown('the job has no teuthology.log')
    job, reason = ctx.job, ctx.job.reason
    fail = failure(ctx)
    task = task_at(ctx, fail.line) if fail else ''
    in_task = f' in the {task} task' if task else ''

    if INFRA_REASONS.search(reason) or (task and INFRA_TASKS.fullmatch(task)):
        return go('infra', f'the job failed{in_task}, setting up the test machines or '
                           f'installing ceph: {short(reason)}', fail, task=task)

    crash = first_crash(ctx)
    if crash and (not fail or crash.line < fail.line):
        who = daemon(crash)[0]
        if INTENDED_CRASHES.search(job.description + ' ' + reason):
            ctx.facts['intended_crash'] = who
        else:
            return go('crash', f'{who} crashed{at(crash)}, before the job failed', crash,
                      fail, task=task)
    if reason.startswith('Command crashed:'):
        return go('crash', f'a command was killed by a signal: {short(reason)}', fail,
                  task=task)
    if ctx.count('coredump') and not crash:
        dumped = ctx.first('coredump')
        return go('crash', 'a process dumped core, and no daemon logged a crash: '
                           'a client or a test program crashed', dumped, fail, task=task)

    if reason.startswith('valgrind error'):
        return go('valgrind', f'valgrind found an error: {short(reason)}',
                  ctx.first('valgrind') or fail, task=task)
    if reason.endswith('in cluster log'):
        return go('cluster-log', f'the cluster log has an error or warning that the job '
                                 f'does not ignore: {short(reason)}',
                  logged(ctx) or fail, task=task)
    if job.status == 'dead' or ctx.count('killed') or 'max job timeout' in reason:
        # Without the signal that killed it, the log's last line shows where it stopped.
        last = ctx.last('killed') or fail or next(iter(ctx.tail(count=1)), None)
        return go('hung', f'the job did not finish, and was killed{at(ctx.last("killed"))}'
                          f': {short(reason) or job.status}', last, task=task)
    found = WORKUNIT.search(reason)
    if found:
        return go('workunit', f'the workunit {found.group(1)} failed', fail,
                  workunit=found.group(1), task=task)
    if reason.startswith('Test failure:') or ctx.count('unittest'):
        return go('test', f'a test failed{in_task}: {short(reason)}', fail, task=task)
    if TIMEOUT.search(reason) or 'WatchDog barked' in reason:
        return go('timeout', f'something the job waited for did not happen{in_task}: '
                             f'{short(reason)}', fail, task=task)
    if reason.startswith('Command failed'):
        return go('command', f'a command failed{in_task}: {short(reason)}', fail, task=task)
    return unknown(f'the failure reason is not one the tree knows: '
                   f'{short(reason) or "(none)"}', fail, branch='other')
