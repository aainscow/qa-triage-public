import os
import stat
import tempfile
import unittest

import yaml

from fakes import FakeFiles

from qa_triage import library

LOG = ['op 1 start', 'op 1 finish', 'op 2 start', 'noise', 'op 3 start']
PROGRAM = '''#!/bin/sh
# The ops that started and never finished, in the files given.
cat "$@" | awk '/start/ {s[$2] = 1} /finish/ {delete s[$2]}
                END {for (o in s) print "unfinished", o}' | sort
'''


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='qa_triage-library-')
        os.makedirs(os.path.join(self.root, 'problems'))
        script = os.path.join(self.root, 'scripts', 'hung-ops')
        os.makedirs(script)
        program = os.path.join(script, 'hung_ops')
        with open(program, 'w') as f:
            f.write(PROGRAM)
        os.chmod(program, os.stat(program).st_mode | stat.S_IEXEC)
        with open(os.path.join(script, 'script.yaml'), 'w') as f:
            yaml.safe_dump({'about': 'ops that started and never finished', 'run': 'hung_ops',
                            'files': 'remote/*/log/ceph-osd.*.log', 'pattern': 'start|finish'}, f)
        # A script with no program is no script.
        os.makedirs(os.path.join(self.root, 'scripts', 'broken'))
        with open(os.path.join(self.root, 'scripts', 'broken', 'script.yaml'), 'w') as f:
            yaml.safe_dump({'about': 'x', 'run': 'missing', 'files': '*'}, f)

    def test_scripts_and_running_them(self):
        scripts = library.scripts_of(os.path.join(self.root, 'problems'))
        self.assertEqual(list(scripts), ['hung-ops'])
        self.assertEqual(library.listing(scripts),
                         'hung-ops: ops that started and never finished. It reads '
                         'remote/*/log/ceph-osd.*.log, the lines matching /start|finish/\n')
        files = FakeFiles({'remote/h1/log/ceph-osd.0.log': LOG, 'teuthology.log': ['x']})
        workdir = tempfile.mkdtemp(prefix='qa_triage-library-work-')
        said = library.run(scripts['hung-ops'], files, workdir)
        self.assertIn('unfinished 2\nunfinished 3\n', said)
        # Only the lines the pattern finds were copied for it.
        with open(os.path.join(workdir, 'script-input', 'hung-ops',
                               'remote_h1_log_ceph-osd.0.log')) as f:
            self.assertNotIn('noise', f.read())
        self.assertIn('the job has no', library.run(
            library.Script('x', 'y', '/bin/cat', 'nothing*'), files, workdir))


if __name__ == '__main__':
    unittest.main()
