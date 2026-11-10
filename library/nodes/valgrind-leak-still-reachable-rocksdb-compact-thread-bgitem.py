from qa_triage.tree.core import done, node, unknown

EXAMPLES = {
    'skanta-2026-10-12_16:15:49-rados-wip-bharath5-testing-20261012.095720-main-distro-default-trial/628620': 'valgrind/Leak_StillReachable/rocksdb-compact-thread-bgitem/rocksdb-queue-unsuppressed',
}


@node('valgrind/Leak_StillReachable/rocksdb-compact-thread-bgitem',
      'Why is the rocksdb thread pool queue reported now?')
def valgrind_leak_rocksdb_compact_thread_bgitem(ctx):
    queue = ctx.search('remote/*/log/valgrind/mon.*.log.gz',
                       r'<fn>rocksdb::ThreadPoolImpl::Impl::Submit\(',
                       '<fn>rocksdb::ThreadPoolImpl::Impl::Submit(')
    hidden = ctx.search('remote/*/log/valgrind/mon.*.log.gz',
                        r'<name>OpenSSL EVP_CIPHER_fetch reachable leak via CryptoAES256KRB5',
                        'CryptoAES256KRB5')
    if queue and hidden:
        return done('rocksdb-queue-unsuppressed',
                    "the monitor's rocksdb thread-pool queue (a ThreadPoolImpl BGItem deque "
                    'node queued by the compact thread) stays reachable at exit because '
                    'rocksdb 9.11 never destroys Env::Default(), and qa/valgrind.supp has no '
                    'rule for it; it was hidden behind the OpenSSL EVP_CIPHER_fetch leak, '
                    'which a suppression now covers, so teuthology reports this one',
                    queue[0], hidden[0],
                    key='valgrind Leak_StillReachable rocksdb thread pool queue unsuppressed')
    return unknown('the rocksdb leak is not shown to have been hidden behind the OpenSSL one')
# learned: 2026-11-05 Bob, from skanta-2026-10-12_16:15:49-rados-wip-bharath5-testing-20261012.095720-main-distro-default-trial/628620, as qa_triage (bob+files) said
