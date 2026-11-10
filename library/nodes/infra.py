import re

from qa_triage.tree.common import failure, short
from qa_triage.tree.core import done, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374644': 'infra/other',
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374652': 'infra/build',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122161': 'infra/build',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122175': 'infra/other',
    'skanta-2026-04-13_14:09:46-rados-wip-bharath11-testing-2026-04-13-1517-distro-default-trial/148479': 'infra/packages',
    'skanta-2026-05-12_11:16:42-rados-wip-bharath18-testing-2026-05-12-0450-distro-default-trial/208761': 'infra/container-image',
    'skanta-2026-06-04_09:53:35-rados-wip-bharath5-testing-2026-06-02-1032-distro-default-trial/263741': 'infra/packages',
    'skanta-2026-07-12_15:01:05-rados-wip-bharath4-testing-2026-07-09-1342-distro-default-trial/358474': 'infra/ssh',
    'skanta-2026-10-03_03:26:16-rados-wip-bharath19-testing-20261002.123021-main-distro-default-trial/603334': 'infra/ansible',
}


INFRA = (
    ('build', r'Failed to fetch package version|VersionNotFoundError',
     'the build was not on shaman for this distro: it had not finished, or it failed'),
    ('container-image', r"cephadm --image \S+ pull'",
     'cephadm could not pull the container image of the build'),
    ('packages', r"'sudo (yum|dnf|apt-get|apt) ", 'installing packages failed'),
    ('ansible', r'_ansible_|AnsibleFailed', 'ansible failed to set up a test machine'),
    ('ssh', r'SSH connection to \S+ was lost|Could not reconnect|ConnectionLostError',
     'teuthology lost its connection to a test machine'),
    ('machines', r'Error reimaging|Failed to power|machine \S+ is locked',
     'a test machine could not be reimaged, powered or locked'),
)


@node('infra', 'Which part of the test infrastructure failed?')
def infra(ctx):
    reason, fail = ctx.job.reason, failure(ctx)
    for branch, pattern, says in INFRA:
        if re.search(pattern, reason):
            return done(branch, says, fail, key=branch)
    task = ctx.facts.get('task') or 'a setup'
    return done('other', f'the {task} task failed: {short(reason)}', fail,
                key=f'{task}: {reason}')
