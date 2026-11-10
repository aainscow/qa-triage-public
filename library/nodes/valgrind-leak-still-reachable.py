from qa_triage.tree.core import done, node, unknown

EXAMPLES = {
    'skanta-2026-10-12_23:32:34-rados-wip-bharath20-testing-20261012.132505-main-distro-default-trial/631151': 'valgrind/Leak_StillReachable/openssl-evp-cipher-fetch',
    'skanta-2026-09-24_03:23:39-rados-wip-bharath10-testing-20260924.010810-main-distro-default-trial/577552': 'valgrind/Leak_StillReachable/rocksdb-compact-thread-bgitem',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122168': 'valgrind/Leak_StillReachable/rocksdb-error-handler',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122189': 'valgrind/Leak_StillReachable/rocksdb-error-handler',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127713': 'valgrind/Leak_StillReachable/rocksdb-error-handler',
    'skanta-2026-03-31_12:00:11-rados-wip-bharath12-testing-2026-03-30-1431-distro-default-trial/127735': 'valgrind/Leak_StillReachable/rocksdb-error-handler',
    'yuriw-2026-08-10_19:17:25-fs-wip-yuri10-testing-20260810.174752-squid-distro-default-trial/453132': 'valgrind/Leak_StillReachable/glibc-dlopen-static-init',
    'yuriw-2026-08-10_19:17:25-fs-wip-yuri10-testing-20260810.174752-squid-distro-default-trial/453238': 'valgrind/Leak_StillReachable/glibc-dlopen-static-init',
}


@node('valgrind/Leak_StillReachable', 'What leaked via CRYPTO_malloc?')
def valgrind_leak_still_reachable(ctx):
    evp = ctx.search('remote/*/log/valgrind/mon.*.log.gz',
                     r'<fn>EVP_CIPHER_fetch</fn>',
                     '<fn>EVP_CIPHER_fetch</fn>')
    if evp:
        return done('openssl-evp-cipher-fetch',
                    'OpenSSL EVP_CIPHER_fetch called from CryptoAES256KRB5 during monitor '
                    'AuthRegistry init (Crypto.cc) left memory still-reachable at exit; '
                    'needs a valgrind suppression in qa/valgrind.supp',
                    evp[0],
                    key='valgrind Leak_StillReachable EVP_CIPHER_fetch CRYPTO_malloc')
    compact = ctx.search('remote/*/log/valgrind/mon.*.log.gz',
                         r'<fn>RocksDBStore::compact_thread_entry\(\)</fn>',
                         '<fn>RocksDBStore::compact_thread_entry()</fn>')
    if compact:
        return done('rocksdb-compact-thread-bgitem',
                    'rocksdb ThreadPoolImpl BGItem deque allocated by RocksDBStore::CompactThread '
                    'during CompactRange is not freed at monitor exit; '
                    'needs a valgrind suppression in qa/valgrind.supp',
                    compact[0],
                    key='valgrind Leak_StillReachable rocksdb CompactThread BGItem')
    err = (ctx.search('remote/*/log/valgrind/*.log.gz',
                      r'error_handler\.cc',
                      'error_handler.cc') or
           ctx.search('teuthology.log',
                      r'_GLOBAL__sub_I_error_handler',
                      '_GLOBAL__sub_I_error_handler'))
    if err:
        return done('rocksdb-error-handler',
                    'RocksDB static initialization in error_handler.cc left memory '
                    'still-reachable at exit; needs a valgrind suppression in qa/valgrind.supp',
                    err[0],
                    key='valgrind Leak_StillReachable rocksdb error_handler')
    dlopen = ctx.search('remote/*/log/valgrind/mon.*.log.gz',
                        r'<fn>_sub_I_65535_0\.0</fn>',
                        '<fn>_sub_I_65535_0.0</fn>')
    if dlopen:
        return done('glibc-dlopen-static-init',
                    "libceph-common's static initializer (_sub_I_65535_0.0) calls dlopen while "
                    "the monitor starts, and glibc's dynamic linker allocates memory there that "
                    "it never frees, so valgrind reports it as still reachable at exit; "
                    "needs a valgrind suppression in qa/valgrind.supp",
                    dlopen[0],
                    key='valgrind Leak_StillReachable libceph-common dlopen static init')
    return unknown('leak from a source this node does not know')
# learned: 2026-10-30 bob-analyst, from yuriw-2026-08-10_19:17:25-fs-wip-yuri10-testing-20260810.174752-squid-distro-default-trial/453132, as the QA reviewers and the tracker, via qa_triage (bob-analyst) said
