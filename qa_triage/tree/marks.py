"""The landmarks: the lines of teuthology.log that the nodes read, found for all
of them in one pass. Each is a regex, and the plain strings that any line it
finds must have."""

from .core import landmarks

# A daemon's output, which the ceph task copies into teuthology.log, or which
# cephadm's journalctl does: groups 1 and 2 are the daemon and its host.
DAEMON = r'(?:tasks\.ceph\.|journalctl@ceph\.)([a-z]+\.\w+)\.([\w-]+)\.std(?:err|out):'

landmarks(
    task=(r'INFO:teuthology\.run_tasks:Running task (\S+)\.\.\.', 'Running task '),
    unwind=(r'DEBUG:teuthology\.run_tasks:Unwinding manager (\S+)', 'Unwinding manager '),
    task_failed=(r'ERROR:teuthology\.run_tasks:(?:Saw exception from tasks\.|'
                 r'Manager failed: (\S+))', 'ERROR:teuthology.run_tasks:'),
    # The last line of a Python traceback.
    exception=(r'^(?:[\w.]+\.)?(\w+(?:Error|Exception|Exit|Tries|Timeout|Failure)): (.+)',
               'Error: ', 'Exception: ', 'Exit: ', 'Tries: ', 'Timeout: ', 'Failure: '),
    signal=(DAEMON + r'.*\*\*\* Caught signal \(([^)]+)\)', 'Caught signal ('),
    failed_assert=(DAEMON + r'.*?/src/(\S+): (\d+): (FAILED ceph_assert\(.*\)|'
                   r'ceph_abort_msg\(.*\))', 'FAILED ceph_assert(', 'ceph_abort_msg('),
    in_function=(DAEMON + r".*?/src/(\S+): In function '(.+?)' thread", "In function '"),
    frame=(DAEMON + r'\s*(\d+): \((.+?)\+0x[0-9a-f]+\) \[0x', ') [0x'),
    coredump=(r'Found coredumps on (?:\w+@)?([\w.-]+)', 'Found coredumps on '),
    watchdog=(r'The WatchDog barked due to (.+)', 'WatchDog barked'),
    cluster_log=(r'"(.+)" in cluster log', '" in cluster log'),
    health=(r'cluster \[(ERR|WRN)\] Health check failed: (.+) \((\w+)\)',
            'Health check failed: '),
    valgrind=(r'valgrind error: (.+)|saw valgrind issue (.+)', 'valgrind error: ',
              'saw valgrind issue'),
    asok_timeout=(r'admin_socket: exception:.*timed out', 'admin_socket: exception:'),
    slow_ops=(r'(\d+) slow ops, oldest one blocked for (\d+) sec',
              ' slow ops, oldest one blocked for '),
    # teuthology was killed: a dead job that hit the max job timeout.
    killed=(r'teuthology\.exit:Got signal (\d+)', 'teuthology.exit:Got signal'),
    command=(r'DEBUG:teuthology\.orchestra\.run\.([\w-]+):> (.+)',
             'DEBUG:teuthology.orchestra.run.'),
    waiting_test=(r'Waiting for test (\S+) \(PID \d+\)\.\.\. waited (\d+) seconds',
                  'Waiting for test '),
    unittest=(r'(FAIL|ERROR): (test_\w+) \(([\w.]+)\)', 'FAIL: test_', 'ERROR: test_'),
    gtest=(r'\[  FAILED  \] (\S+)', '[  FAILED  ]'),
    timed_out=(r'timeout expired in (\w+)|failed before timeout expired|'
               r'reached maximum tries \((\d+)\) after waiting for ([\d.]+) seconds',
               'timeout expired', 'reached maximum tries'),
)
