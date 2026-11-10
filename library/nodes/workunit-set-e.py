import re

from qa_triage.tree.core import done, node, unknown
from qa_triage.tree.common import at

# This job, and every job the file was written for, with the path each takes.
EXAMPLES = {
    'jayaprakash-2026-04-20_13:52:18-rados-wip-jaya-bs-testing-20-04-2026-distro-default-trial/164518': 'workunit/set-e/test-repos-add-repo-metadata-404',
    'skanta-2026-07-12_15:01:05-rados-wip-bharath4-testing-2026-07-09-1342-distro-default-trial/358483': 'workunit/set-e/test-repos-add-repo-gpg-404',
    'skanta-2026-05-07_14:11:34-rados-wip-bharath15-testing-2026-05-07-0507-distro-default-trial/197877': 'workunit/set-e/rbd-lock-mode-exclusive-transient',
    'yuriw-2026-07-28_17:11:12-rados-wip-yuri3-testing-20260728.135610-main-distro-default-trial/406311': 'workunit/set-e/test-repos-add-repo-shaman-http-error',
    'lflores-2026-01-26_23:21:06-rados-wip-yuri12-testing-2026-01-22-2045-distro-default-trial/19083': 'workunit/set-e/fusermount-missing',
    'skanta-2026-11-03_06:27:24-rados-wip-bharath11-testing-20261028.083425-main-distro-default-trial/707131': 'workunit/set-e/cephadm-release-mismatch',
}


@node('workunit/set-e', 'Why did the command fail?')
def workunit_set_e(ctx):
    if ctx.facts.get('script', '').startswith('cephadm/test_cephadm.sh'):
        release_mismatch = ctx.search(
            'teuthology.log',
            r'Container release \S+ != cephadm release \S+',
            'Container release', 'cephadm release',
        )
        if release_mismatch:
            return done(
                'cephadm-release-mismatch',
                f'test_cephadm.sh failed{at(release_mismatch[0])}: cephadm bootstrap refused '
                'because the container image release does not match the cephadm binary release',
                release_mismatch[0],
                key='test_cephadm.sh: Container release != cephadm release',
                script=ctx.facts.get('script', ''),
            )
        io_flusher = ctx.search(
            'teuthology.log',
            r'ganesha\.nfsd.*FATAL :Failed to PR_SET_IO_FLUSHER with EPERM',
            'Failed to PR_SET_IO_FLUSHER with EPERM',
        )
        if io_flusher:
            return done(
                'ganesha-io-flusher-eperm',
                f'test_cephadm.sh deployed nfs.a but ganesha.nfsd exited immediately'
                f'{at(io_flusher[0])}: on Rocky 10 the container lacks CAP_SYS_ADMIN so '
                'prctl(PR_SET_IO_FLUSHER) returns EPERM, and ganesha treats it as fatal '
                'because allow_set_io_flusher_fail was not set in samples/nfs.json; '
                'the script hit set -e on the cephadm log dump of the crashed service',
                io_flusher[0],
                key='test_cephadm.sh: ganesha Failed to PR_SET_IO_FLUSHER with EPERM',
                script=ctx.facts.get('script', ''),
            )
        nfs_v3_tcp = ctx.search(
            'teuthology.log',
            r'ganesha\.nfsd.*__Register_program :DISP :MAJ :Cannot register NFS V3 on TCP',
            'Cannot register NFS V3 on TCP',
        )
        if nfs_v3_tcp:
            return done(
                'ganesha-nfsv3-tcp-register',
                f'test_cephadm.sh deployed nfs.a but ganesha.nfsd could not register '
                f'NFS V3 on TCP{at(nfs_v3_tcp[0])}: no rpcbind/portmapper was running on Rocky 10; '
                'the script hit set -e on the cephadm log dump of the crashed service',
                nfs_v3_tcp[0],
                key='test_cephadm.sh: ganesha Cannot register NFS V3 on TCP',
                script=ctx.facts.get('script', ''),
            )
        nfs_v3_udp = ctx.search(
            'teuthology.log',
            r'ganesha\.nfsd.*__Register_program :DISP :MAJ :Cannot register NFS V3 on UDP',
            'Cannot register NFS V3 on UDP',
        )
        if nfs_v3_udp:
            return done(
                'ganesha-nfsv3-udp-register',
                f'test_cephadm.sh deployed nfs.a but ganesha.nfsd could not register '
                f'NFS V3 on UDP{at(nfs_v3_udp[0])}: no portmapper (rpcbind) was running; '
                'the script hit set -e on the cephadm log dump of the crashed service',
                nfs_v3_udp[0],
                key='test_cephadm.sh: ganesha Cannot register NFS V3 on UDP',
                script=ctx.facts.get('script', ''),
            )
    if ctx.facts.get('script', '').startswith('cephadm/test_repos.sh'):
        shaman_error = ctx.search(
            'teuthology.log',
            r'HTTP Error \d+.*shaman\.ceph\.com',
            'shaman.ceph.com',
        )
        if shaman_error:
            return done(
                'test-repos-add-repo-shaman-http-error',
                f'test_repos.sh failed{at(shaman_error[0])}: cephadm add-repo could not '
                'contact shaman.ceph.com and received an HTTP error (transient infra problem)',
                shaman_error[0],
                key='test_repos.sh: shaman.ceph.com HTTP error',
                script=ctx.facts.get('script', ''),
            )
        gpg_404 = ctx.search(
            'teuthology.log',
            r'failed to fetch GPG repo key from https://download\.ceph\.com/keys/release\.gpg: HTTP Error 404: Not Found',
            'failed to fetch GPG repo key', 'release.gpg',
        )
        if gpg_404:
            return done(
                'test-repos-add-repo-gpg-404',
                f'test_repos.sh failed{at(gpg_404[0])}: cephadm add-repo failed because '
                'download.ceph.com/keys/release.gpg returned HTTP 404 (the file was missing from the server)',
                gpg_404[0],
                key='test_repos.sh: release.gpg 404 from download.ceph.com',
                script=ctx.facts.get('script', ''),
            )
        metadata_404 = ctx.search(
            'teuthology.log',
            r"unable to fetch repo metadata: <HTTPError 404: 'Not Found'>",
            'unable to fetch repo metadata',
        )
        failed = ctx.search(
            'teuthology.log',
            r'Error: failed to fetch repository metadata\. please check the provided parameters are correct',
            'failed to fetch repository metadata',
        )
        if metadata_404 and failed:
            return done(
                'test-repos-add-repo-metadata-404',
                f'test_repos.sh failed{at(metadata_404[0])}: cephadm add-repo could not fetch the '
                'repository metadata (repomd.xml) from download.ceph.com, which returned HTTP 404 '
                'because the requested release is not published for this distro',
                metadata_404[0], failed[0],
                key='test_repos.sh: add-repo repository metadata 404',
                script=ctx.facts.get('script', ''),
            )
    if ctx.facts.get('script', '').startswith('rbd/test_librbd_python.sh'):
        imported = ctx.search(
            'teuthology.log',
            r"ImportError: cannot import name 'RBD_LOCK_MODE_EXCLUSIVE_TRANSIENT' from 'rbd'",
            'RBD_LOCK_MODE_EXCLUSIVE_TRANSIENT',
        )
        if imported:
            return done(
                'rbd-lock-mode-exclusive-transient',
                f'test_librbd_python.sh failed{at(imported[0])}: pytest could not import test_rbd.py '
                "because 'rbd' does not define 'RBD_LOCK_MODE_EXCLUSIVE_TRANSIENT'",
                imported[0],
                key="test_librbd_python.sh: cannot import 'RBD_LOCK_MODE_EXCLUSIVE_TRANSIENT' from 'rbd'",
                script=ctx.facts.get('script', ''),
            )
    if ctx.facts.get('script', '').startswith('objectstore/test_fuse.sh'):
        missing = ctx.search(
            'teuthology.log',
            r'objectstore/test_fuse\.sh: line \d+: fusermount: command not found',
            'fusermount: command not found',
        )
        if missing:
            return done(
                'fusermount-missing',
                f'test_fuse.sh failed{at(missing[0])}: its final `fusermount -u` found no '
                'fusermount on the test node (the fuse package was not installed), so the '
                'script exited 127; the earlier rmdir error is one the script expects',
                missing[0],
                key='test_fuse.sh: fusermount: command not found',
                script=ctx.facts.get('script', ''),
            )
    return unknown('none of the failures this node knows')
# learned: 2026-10-30 bob-analyst, from skanta-2026-04-09_03:57:47-rados-wip-bharath16-testing-2026-04-08-1405-distro-default-trial/141123, as the QA reviewers and the tracker, via qa_triage (bob-analyst) said
# learned: 2026-11-05 Bob, from skanta-2026-11-03_06:27:24-rados-wip-bharath11-testing-20261028.083425-main-distro-default-trial/707131, as qa_triage (bob+files) said
