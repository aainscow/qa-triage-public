import contextlib
import gzip
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

import yaml

from fakes import RUN, FakeFiles, job

from qa_triage import sources
from qa_triage.checkout import (CEPH_CI, added, build_prs, check_out, fetch_all, named_prs,
                                naming, naming_each, under_test, under_test_text, versions)
from qa_triage.sources import (ArchiveError, JobFiles, SshJobFiles, check_archive, https_base,
                               job_files, parse_archive)

# Runs here the command that ssh would run on the host ($1).
FAKE_SSH = ['sh', '-c', 'exec sh -c "$2"', 'ssh']
LOG = ['2026-10-22T20:43:15.676 INFO: starting', 'progress 1%\rprogress 100%',
       "FAILED: assert backfillfull < 0.9", 'done']


class TestSshArchive(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='qa_triage-archive-')
        log = os.path.join(self.top, RUN, '1', 'remote', 'h1', 'log')
        os.makedirs(log)
        with open(os.path.join(self.top, RUN, '1', 'teuthology.log'), 'w', newline='') as f:
            f.write('\n'.join(LOG) + '\n')
        for i in range(3):
            with gzip.open(os.path.join(log, f'ceph-osd.{i}.log.gz'), 'wt') as f:
                f.write(''.join(f'osd.{i} line {n}\n' for n in range(1, 1001)))
        # A job that dies can leave a .gz file cut short.
        data = gzip.compress(''.join(f'mon line {n}\n' for n in range(1, 50001)).encode())
        with open(os.path.join(log, 'ceph-mon.a.log.gz'), 'wb') as f:
            f.write(data[:len(data) // 2])
        self.files = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH)

    def test_glob(self):
        osds = self.files.glob('remote/*/log/ceph-osd.*.log.gz')
        self.assertEqual([rel for rel, _ in osds],
                         [f'remote/h1/log/ceph-osd.{i}.log.gz' for i in range(3)])
        self.assertEqual(self.files.glob('*'),
                         [('teuthology.log', len('\n'.join(LOG)) + 1)])
        # The archive gzips older runs' logs: teuthology.log is then teuthology.log.gz,
        # which is read as it was.
        top = os.path.join(self.top, RUN, '1')
        with open(os.path.join(top, 'teuthology.log'), 'rb') as f, \
                gzip.open(os.path.join(top, 'teuthology.log.gz'), 'wb') as out:
            out.write(f.read())
        both = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH)
        self.assertEqual([rel for rel, _ in both.glob('teuthology.log')], ['teuthology.log'])
        os.remove(os.path.join(top, 'teuthology.log'))
        older = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH)
        [(rel, _)] = older.glob('teuthology.log')
        self.assertEqual(rel, 'teuthology.log.gz')
        self.assertEqual(list(older.lines(rel)), LOG)
        self.assertEqual([h.line for h in older.search('teuthology.log',
                                                       re.compile('backfillfull'))], [3])
        self.assertEqual(older.glob('remote/*/log/ceph-osd.0.log'),
                         older.glob('remote/*/log/ceph-osd.0.log.gz'))
        # A file named as it was is read as it is now, whatever reads it.
        self.assertEqual(older.scan('teuthology.log', {'f': ('FAILED: (.*)', ['FAILED'])})['f'][0],
                         1)
        self.assertEqual([h.text for h in older.tail('teuthology.log', 1)], ['done'])
        self.assertEqual(older.text('teuthology.log', 10), LOG[0][:10])
        self.assertEqual(list(older.lines('teuthology.log')), LOG)

    def test_search(self):
        hits = self.files.search('remote/*/log/ceph-osd.*.log.gz', re.compile(r'line 7\d\d$'),
                                 max_hits=2)
        self.assertEqual([(os.path.basename(h.file), h.line, h.text) for h in hits],
                         [(f'ceph-osd.{i}.log.gz', n, f'osd.{i} line {n}')
                          for i in range(3) for n in (700, 701)])
        # Lines end only at \n, as for downloaded files.
        [hit] = self.files.search('teuthology.log', re.compile(r'assert backfillfull < 0\.9'))
        self.assertEqual((hit.line, hit.text), (3, LOG[2]))
        self.assertEqual(self.files.search('none*', re.compile('x')), [])

    def test_reading(self):
        self.assertEqual(list(self.files.lines('teuthology.log')), LOG)
        self.assertEqual(len(list(self.files.lines('remote/h1/log/ceph-osd.2.log.gz'))), 1000)
        cut = list(self.files.lines('remote/h1/log/ceph-mon.a.log.gz'))
        self.assertTrue(0 < len(cut) < 50000)
        self.assertEqual(cut[0], 'mon line 1')
        [hit] = self.files.search('remote/h1/log/ceph-mon.a.log.gz', re.compile(r'line 3$'))
        self.assertEqual(hit.line, 3)
        self.assertEqual(self.files.text('teuthology.log', 10), LOG[0][:10])
        self.assertEqual(self.files.text('summary.yaml'), '')
        with self.assertRaisesRegex(ArchiveError, 'outside the job directory'):
            self.files.text('../../x')

    def test_scan(self):
        patterns = {'line7': (r'line (7\d\d)$', ['line 7']), 'none': ('nothing', ['nothing'])}
        # On the host, gzip and grep read the file first, then the regexes the lines found.
        marks = self.files.scan('remote/h1/log/ceph-osd.1.log.gz', patterns, first=2, last=3)
        count, hits = marks['line7']
        self.assertEqual(count, 100)
        self.assertEqual([h.line for h in hits], [700, 701, 797, 798, 799])
        self.assertEqual(hits[0].text, 'osd.1 line 700')
        self.assertEqual(marks['none'], (0, []))
        self.assertEqual(self.files.scan('teuthology.log', patterns)['line7'], (0, []))
        [last] = self.files.tail('teuthology.log', 1)
        self.assertEqual((last.line, last.text), (4, 'done'))
        self.assertEqual([h.text for h in self.files.between('teuthology.log', 2, 3)], LOG[1:3])
        # A search with literals: grep and gzip first, then the regex.
        hits = self.files.search('remote/*/log/ceph-osd.2.log.gz', re.compile(r'line 99\d$'),
                                 max_hits=2, literals=['line 99'])
        self.assertEqual([(h.line, h.text) for h in hits],
                         [(990, 'osd.2 line 990'), (991, 'osd.2 line 991')])
        # The same, read here.
        fake = FakeFiles({'teuthology.log': LOG})
        self.assertEqual(fake.scan('teuthology.log', {'f': ('FAILED: (.*)', ['FAILED'])})['f'],
                         (1, [fake.search('teuthology.log', re.compile('FAILED'))[0]]))

    def test_missing_job(self):
        files = SshJobFiles(job('2'), 'host', self.top, ssh=FAKE_SSH)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(files.glob('teuthology.log'), [])
        self.assertEqual(err.getvalue(), f'warning: host has no {self.top}/{RUN}/2\n')

    def test_answers_are_kept(self):
        kept = os.path.join(self.top, 'cache', RUN, '1', 'archive.json')
        files = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH, kept=kept)
        osds = files.glob('remote/*/log/ceph-osd.*.log.gz')
        hits = files.search('remote/*/log/ceph-osd.*.log.gz', re.compile(r'line 7\d\d$'))
        marks = files.scan('teuthology.log', {'f': ('FAILED: (.*)', ['FAILED'])})
        self.assertEqual((files.asked, files.reused), (3, 0))
        # Another run asks the same questions: the host is not asked again.
        unreachable = ['sh', '-c', 'echo "ssh: no route" >&2; exit 255', 'ssh']
        again = SshJobFiles(job('1'), 'host', self.top, ssh=unreachable, kept=kept)
        self.assertEqual(again.glob('remote/*/log/ceph-osd.*.log.gz'), osds)
        self.assertEqual(again.search('remote/*/log/ceph-osd.*.log.gz',
                                      re.compile(r'line 7\d\d$')), hits)
        self.assertEqual(again.scan('teuthology.log', {'f': ('FAILED: (.*)', ['FAILED'])}),
                         marks)
        self.assertEqual((again.asked, again.reused), (0, 3))
        # A new question is.
        with self.assertRaisesRegex(ArchiveError, 'cannot reach host'):
            again.search('teuthology.log', re.compile('done'))
        # A job the host does not have yet is asked about again.
        kept = os.path.join(self.top, 'cache', RUN, '2', 'archive.json')
        with contextlib.redirect_stderr(io.StringIO()):
            SshJobFiles(job('2'), 'host', self.top, ssh=FAKE_SSH, kept=kept).glob('*')
        self.assertFalse(os.path.exists(kept))

    def test_a_log_gzipped_after_its_files_were_listed(self):
        kept = os.path.join(self.top, 'cache', RUN, '1', 'archive.json')
        files = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH, kept=kept)
        self.assertEqual([rel for rel, _ in files.glob('teuthology.log')], ['teuthology.log'])
        # Then the archive gzips it, as it does older runs' logs. The list of the job's
        # files, kept from before, names a file that is gone: they are listed again.
        top = os.path.join(self.top, RUN, '1')
        with open(os.path.join(top, 'teuthology.log'), 'rb') as f, \
                gzip.open(os.path.join(top, 'teuthology.log.gz'), 'wb') as out:
            out.write(f.read())
        os.remove(os.path.join(top, 'teuthology.log'))
        later = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH, kept=kept)
        [(rel, _)] = later.glob('teuthology.log')
        self.assertEqual(later.scan(rel, {'f': ('FAILED: (.*)', ['FAILED'])})['f'][0], 1)
        self.assertEqual([h.file for h in later.search(rel, re.compile('backfillfull'))],
                         ['teuthology.log.gz'])
        self.assertEqual([rel for rel, _ in SshJobFiles(job('1'), 'host', self.top,
                                                        ssh=FAKE_SSH, kept=kept).glob('*')],
                         ['teuthology.log.gz'])

    def test_logs_the_archive_pruned_after_its_files_were_listed(self):
        kept = os.path.join(self.top, 'cache', RUN, '1', 'archive.json')
        files = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH, kept=kept)
        self.assertEqual(len(files.glob('remote/*/log/ceph-osd.*')), 3)
        # Then the archive removes the daemons' logs, as it does older runs'.
        shutil.rmtree(os.path.join(self.top, RUN, '1', 'remote'))
        later = SshJobFiles(job('1'), 'host', self.top, ssh=FAKE_SSH, kept=kept)
        self.assertEqual(later.search('remote/*/log/ceph-osd.*', re.compile('line 5')), [])
        # The search says which were gone, and the files are listed again.
        self.assertEqual(later.last_gone, [f'remote/h1/log/ceph-osd.{i}.log.gz'
                                           for i in range(3)])
        self.assertEqual(later.glob('remote/*/log/ceph-osd.*'), [])

    def test_unreachable_host(self):
        files = SshJobFiles(job('1'), 'host', self.top,
                            ssh=['sh', '-c', 'echo "ssh: connect to host: Operation timed '
                                             'out" >&2; exit 255', 'ssh'])
        with self.assertRaisesRegex(ArchiveError, r'cannot reach host over ssh \(ssh: '
                                                  r'connect to host: Operation timed out\): '
                                                  r'connect to the sepia VPN.*--https'):
            files.glob('*')
        with self.assertRaisesRegex(ArchiveError, 'cannot reach host'):
            list(files.lines('teuthology.log'))

    def test_settings(self):
        self.assertEqual(parse_archive('trial200:/a'), ('trial200', '/a'))
        for wrong in ('trial200', 'trial200:a', ':/a', '-oProxyCommand=x:/a'):
            with self.assertRaises(ValueError):
                parse_archive(wrong)
        self.assertIsInstance(job_files(job('1'), 'https', self.top), JobFiles)
        # Another server's copy of the runs, over https, as the person names it.
        self.assertEqual(https_base('https'), 'https://qa-proxy.ceph.com/teuthology')
        self.assertEqual(https_base('https://example.com/runs/'), 'https://example.com/runs')
        self.assertIsNone(https_base('trial200:/a'))
        files = job_files(job('1'), 'https://example.com/runs/', self.top)
        self.assertEqual(files.base, f'https://example.com/runs/{quote(RUN)}/1/')
        check_archive('https://example.com/runs')
        with self.assertRaisesRegex(ValueError, 'https URL'):
            check_archive('ftp://example.com/runs')
        files = job_files(job('1'), 'trial200:/a', self.top)
        self.assertEqual((files.host, files.root), ('trial200', f'/a/{RUN}/1'))
        # Not in the cache, whose path may be too long for a socket's.
        self.assertIn(f'ControlPath=/tmp/qa_triage-{os.getuid()}/{os.getpid()}-%C', files.ssh)
        self.assertEqual(oct(os.stat(f'/tmp/qa_triage-{os.getuid()}').st_mode & 0o777), '0o700')
        self.assertEqual(files.kept, f'{self.top}/{RUN}/1/archive.json')


class TestHttpsArchive(unittest.TestCase):
    """The job files downloaded over https: each once, then read from the cache, a
    big one kept gzipped; but a big one gzipped already is streamed each time."""

    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='qa_triage-https-')
        self.addCleanup(shutil.rmtree, self.top)
        self.log = [f'line {n}' for n in range(1, 3001)]
        osd = gzip.compress(b'osd line\n' * 10)
        big = gzip.compress(''.join(f'osd.1 line {n}\n' for n in range(5000)).encode())
        self.assertGreater(len(big), 1000)
        self.served = {'teuthology.log': ('\n'.join(self.log) + '\n').encode(),
                       'remote/h1/log/ceph-osd.0.log.gz': osd,
                       'remote/h1/log/ceph-osd.1.log.gz': big}
        self.downloaded = []
        self.files = JobFiles(job('1'), self.top, 'https://example.com/runs')
        prefix = self.files.base

        def listing(url):
            rel = url[len(prefix):]
            names = sorted({name[len(rel):].split('/')[0] + ('/' if '/' in name[len(rel):]
                                                              else '')
                            for name in self.served if name.startswith(rel)})
            return '\n'.join(f'<a href="{name}">{name}</a>   01-Nov-2026 10:00   ' +
                              ('-' if name.endswith('/') else
                               str(len(self.served[rel + name]))) for name in names)

        def opened(url):
            self.downloaded.append(url[len(prefix):])
            return io.BytesIO(self.served[url[len(prefix):]])
        for name, fake in (('_get_text', listing), ('_open', opened)):
            patcher = mock.patch.object(sources, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_file_is_downloaded_once(self):
        # teuthology.log is bigger than the cache keeps as it is: it is kept gzipped.
        with mock.patch.object(sources, 'CACHE_LIMIT', 1000):
            [hit] = self.files.search('teuthology.log', re.compile(r'line 2999$'))
            self.assertEqual(hit.line, 2999)
            self.assertEqual(list(self.files.lines('teuthology.log')), self.log)
            self.assertEqual(self.files.text('teuthology.log', 6), 'line 1')
            self.assertEqual(len(self.files.search('remote/*/log/*.gz', re.compile('line'),
                                                   max_hits=20)), 30)
            # A daemon's log, gzipped already and big: streamed, for each search.
            self.files.search('remote/*/log/ceph-osd.1.log.gz', re.compile('line 4999$'))
            self.assertEqual(len(list(self.files.lines('remote/h1/log/ceph-osd.1.log.gz'))),
                             5000)
        kept = os.path.join(self.top, RUN, '1', 'teuthology.log' + sources.KEPT_GZIPPED)
        with gzip.open(kept, 'rt') as f:
            self.assertEqual(f.read().split('\n')[:-1], self.log)
        self.assertEqual(self.downloaded, ['teuthology.log', 'remote/h1/log/ceph-osd.0.log.gz',
                                           *['remote/h1/log/ceph-osd.1.log.gz'] * 3])
        self.assertFalse(os.path.exists(os.path.join(self.top, RUN, '1', 'remote', 'h1', 'log',
                                                     'ceph-osd.1.log.gz')))
        # Another process, as after the review was quit and opened again, downloads
        # nothing it has.
        again = JobFiles(job('1'), self.top, 'https://example.com/runs')
        self.assertEqual(len(list(again.lines('teuthology.log'))), 3000)
        self.assertEqual(self.files.real('teuthology.log'), 'teuthology.log')
        self.assertEqual(len(self.downloaded), 5)


ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
           GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


def git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                          text=True, env=ENV).stdout.strip()


def commit(repo, text):
    with open(os.path.join(repo, 'osd.cc'), 'w') as f:
        f.write(text)
    git(repo, 'add', 'osd.cc')
    git(repo, 'commit', '-q', '-m', text)
    return git(repo, 'rev-parse', 'HEAD')


def config(**fields):
    return FakeFiles({'orig.config.yaml': yaml.safe_dump(fields).splitlines()})


class TestCheckout(unittest.TestCase):
    def setUp(self):
        top = tempfile.mkdtemp(prefix='qa_triage-checkout-')
        self.ci, self.clone = os.path.join(top, 'ceph-ci'), os.path.join(top, 'ceph')
        os.makedirs(self.ci)
        git(self.ci, 'init', '-q')
        git(self.ci, 'config', 'uploadpack.allowReachableSHA1InWant', 'true')
        self.old = commit(self.ci, 'v1')
        git(top, 'clone', '-q', self.ci, self.clone)
        # The build under test is only in ceph-ci.
        self.new = commit(self.ci, 'v2')
        self.worktrees = os.path.join(top, 'worktrees')
        self.said = []

    def test_versions(self):
        url = f'file://{self.ci}'
        self.assertEqual(versions(config(sha1=self.new, repo=url, suite_sha1=self.old,
                                         suite_repo='/not/a/url')),
                         {'source': (self.new, url), 'suite': (self.old, CEPH_CI)})
        self.assertEqual(versions(config(sha1=self.new, suite_sha1=self.new)),
                         {'source': (self.new, CEPH_CI)})
        self.assertEqual(versions(config(sha1='main')), {})
        self.assertEqual(versions(FakeFiles({})), {})

    def test_check_out(self):
        url = f'file://{self.ci}'
        files = config(sha1=self.new, repo=url, suite_sha1=self.old, suite_repo=url)
        found = check_out(files, self.clone, self.worktrees, self.said.append)
        self.assertEqual(found, {'source': os.path.join(self.worktrees, self.new),
                                 'suite': os.path.join(self.worktrees, self.old)})
        with open(os.path.join(found['source'], 'osd.cc')) as f:
            self.assertEqual(f.read(), 'v2')
        self.assertEqual([line.split()[0] for line in self.said],
                         ['Fetching', 'Checking', 'Checking'])
        # The worktrees are made once, and shared.
        self.said.clear()
        self.assertEqual(check_out(files, self.clone, self.worktrees, self.said.append), found)
        self.assertEqual(self.said, [])

    def test_the_prs_under_test(self):
        def change(branch, start, path, text, message):
            git(self.ci, 'checkout', '-q', '-b', branch, start)
            os.makedirs(os.path.dirname(os.path.join(self.ci, path)), exist_ok=True)
            with open(os.path.join(self.ci, path), 'w') as f:
                f.write(text)
            git(self.ci, 'add', '-A')
            git(self.ci, 'commit', '-q', '-m', message)

        # The base: a PR merged on GitHub. Then the testing branch: a PR merged by
        # ptl-tool, and a commit made on it.
        base = git(self.ci, 'rev-parse', 'HEAD')
        change('fix', base, 'qa/standalone/misc.sh', 'length == 15\n', 'qa: fix the count')
        git(self.ci, 'checkout', '-q', '-b', 'testing', base)
        git(self.ci, 'merge', '-q', '--no-ff', 'fix', '-m', 'Merge pull request #1 from a/fix')
        self.assertEqual(under_test(self.ci), [])
        # A developer's own branch: its commits on the base are under test.
        change('dev', 'testing', 'src/osd/OSD.cc', 'mine\n', 'osd: a change of mine')
        self.assertEqual([(what, files) for what, _, _, files in under_test(self.ci, 'dev')],
                         [('commit "osd: a change of mine"', ['src/osd/OSD.cc'])])
        change('vampire', 'fix', 'qa/standalone/misc.sh', 'length == 14\n',
               'qa: update for vampire')
        git(self.ci, 'checkout', '-q', 'testing')
        git(self.ci, 'merge', '-q', '--no-ff', 'vampire', '-m',
            'Merge PR #72810 into wip-x-testing-20261019.181708-main')
        commit(self.ci, 'v3')
        changes = under_test(self.ci)
        self.assertEqual([(what, files) for what, _, _, files in changes],
                         [('commit "v3"', ['osd.cc']),
                          ('PR #72810', ['qa/standalone/misc.sh'])])
        self.assertEqual(under_test(self.ci, 'HEAD~1'), changes[1:])
        # The files of a change that name a test.
        _, pr, _, files = changes[1]
        self.assertEqual(naming(self.ci, pr, files + ['qa/gone.py'], ['length', 'nothing']),
                         ['qa/standalone/misc.sh'])
        self.assertEqual(naming(self.ci, pr, files, ['nothing']), [])
        # Which name is in which file, from one git grep; a name found inside a longer
        # one counts too.
        self.assertEqual(naming_each(self.ci, pr, files, ['length == 14', 'length', 'none']),
                         {'length == 14': {'qa/standalone/misc.sh'},
                          'length': {'qa/standalone/misc.sh'}})
        # Triage looks each name up once in each change, for all the jobs.
        from qa_triage import graph
        triage = graph.Triage([], git_dir=self.ci)
        with mock.patch.object(graph, 'naming_each', wraps=naming_each) as grep:
            for names in (['length', 'nothing'], ['length'], ['nothing', 'other']):
                found = triage.for_another_job()._naming(pr, files, names)
            self.assertEqual(found, [])
            self.assertEqual(triage._naming(pr, files, ['length']), ['qa/standalone/misc.sh'])
        self.assertEqual([c.args[3] for c in grep.call_args_list],
                         [['length', 'nothing'], ['other']])
        # A PR's branch merged with git pull.
        change('ec', 'fix', 'src/osd/ECBackend.cc', 'fix\n', 'osd: fix')
        git(self.ci, 'checkout', '-q', 'testing')
        git(self.ci, 'merge', '-q', '--no-ff', 'ec', '-m', "Merge branch 'wip-ec' of "
            'https://github.com/a/ceph into wip-x-testing-2026-06-27-0520')
        self.assertEqual(under_test(self.ci)[0][0], 'branch wip-ec of https://github.com/a/ceph')
        # The analyst gets the diffs of qa/; the judge, those of the files its job
        # mentions.
        text = under_test_text(self.ci, changes)
        self.assertTrue(text.startswith('commit "v3" ('))
        self.assertIn('PR #72810, its changes to qa/standalone/misc.sh:\n', text)
        self.assertIn('+length == 14', text)
        text = under_test_text(self.ci, changes, shown=lambda path: path == 'osd.cc')
        self.assertIn('commit "v3", its changes to osd.cc:\n', text)
        self.assertNotIn('+length == 14', text)

    def test_the_prs_a_build_tests(self):
        def change(path, message, branch=None):
            if branch:
                git(self.ci, 'checkout', '-q', '-b', branch)
            os.makedirs(os.path.dirname(os.path.join(self.ci, path)), exist_ok=True)
            with open(os.path.join(self.ci, path), 'a') as f:
                f.write(message + '\n')
            git(self.ci, 'add', '-A')
            git(self.ci, 'commit', '-q', '-m', message)
            return git(self.ci, 'rev-parse', 'HEAD')

        # The base branch, as GitHub merges a PR into it.
        trunk = git(self.ci, 'rev-parse', '--abbrev-ref', 'HEAD')
        change('src/mon/Monitor.cc', 'mon: a fix', 'fix')
        git(self.ci, 'checkout', '-q', trunk)
        git(self.ci, 'merge', '-q', '--no-ff', 'fix', '-m', 'Merge pull request #1 from a/fix')
        base = git(self.ci, 'rev-parse', 'HEAD')
        self.assertEqual(added(self.ci, base), [])
        # A developer's branch: a squash of two PRs, a commit that names a PR for
        # another reason, a tracker issue's URL, a revert, a merge of another PR's
        # branch, and ptl-tool's and git pull's merges.
        squash = change('src/osd/OSD.cc', 'squash: PRs 72643 e0632f6a69b + 72644 3a30da2cc88',
                        'dev')
        change('qa/standalone/mon-ping.sh', 'exp: drop mon-ping.sh, as #72742 does')
        change('src/osd/PG.cc', 'osd: fix it\n\nFixes: https://tracker.ceph.com/issues/72888')
        change('src/osd/OSD.cc', 'osd: revert #70377 "set projected_size"')
        change('src/osd/Migration.cc', 'osd: pool migration', 'migration')
        git(self.ci, 'checkout', '-q', 'dev')
        git(self.ci, 'merge', '-q', '--no-ff', 'migration', '-m',
            'exp: merge pool migration #69112 6bcf771e650 + 73254 onto main')
        change('qa/tasks/thrash.py', 'qa: thrash', 'pr')
        git(self.ci, 'checkout', '-q', 'dev')
        git(self.ci, 'merge', '-q', '--no-ff', 'pr', '-m', 'Merge PR #72810 into wip-x-testing')
        change('src/osd/EC.cc', 'osd: ec', 'ec')
        git(self.ci, 'checkout', '-q', 'dev')
        git(self.ci, 'merge', '-q', '--no-ff', 'ec', '-m',
            "Merge branch 'wip-ec' of https://github.com/a/ceph into wip-x-testing")
        commits = added(self.ci, 'dev')
        self.assertEqual(len(commits), 10)
        self.assertEqual(commits[-1][:3], (squash, [base], 't@t'))
        self.assertEqual(commits[-1][4], ['src/osd/OSD.cc'])
        # A merge's files are those it changes from its first parent.
        merge = next(c for c in commits if c[3].startswith('exp: merge'))
        self.assertEqual(merge[4], ['src/osd/Migration.cc'])
        files = {'72643': ['src/osd/OSD.cc'], '72644': ['src/osd/OSD.cc', 'qa/x'],
                 '72742': ['src/mon/MonClient.cc'], '72888': ['src/osd/PG.cc'],
                 '70377': ['src/osd/OSD.cc'], '69112': ['src/osd/Migration.cc'],
                 '73254': ['src/osd/Migration.cc', 'src/mon/Monitor.cc']}
        linked = {squash: ['72643']}
        found = build_prs(commits, files.get,
                          lambda asked: {c: n for c, n in linked.items() if c in asked},
                          lambda url, branch: '555' if branch == 'wip-ec' else None)
        self.assertEqual(found, {
            '555': ['merged into its branch'], '72810': ['merged into its branch'],
            '69112': ['a commit that changes its files names it'],
            '73254': ['a commit that changes its files names it'],
            '72643': ['a commit that changes its files names it', 'its commits are in the build'],
            '72644': ['a commit that changes its files names it']})
        # Fetched from ceph-ci, the build's commits are read in the clone.
        dev = git(self.ci, 'rev-parse', 'dev')
        self.assertEqual(fetch_all(self.clone, [self.new, dev], f'file://{self.ci}'), [])
        self.assertEqual(len(added(self.clone, dev)), 10)
        self.assertEqual(fetch_all(self.clone, ['f' * 40], f'file://{self.ci}'), ['f' * 40])

    def test_the_prs_a_commit_message_names(self):
        self.assertEqual(named_prs('squash: stretchy-C PRs 72643 e0632f6a69b + 72644 '
                                   '3a30da2cc88'), ['72643', '72644'])
        self.assertEqual(named_prs('exp: merge pool migration #69112 6bcf771e650 + 73254 + '
                                   '73154 + 72977 onto main c210cb8a700'),
                         ['69112', '73254', '73154', '72977'])
        self.assertEqual(named_prs('exp: 72644 69fe13ffd34 (a test sets the class)'), ['72644'])
        self.assertEqual(named_prs('squash: QA PR 72644 at 9a326375ee2 (24 commits)'),
                         ['72644'])
        self.assertEqual(named_prs('osd: x\n\nSee https://github.com/ceph/ceph/pull/71000 and '
                                   'https://tracker.ceph.com/issues/72888'), ['71000'])
        self.assertEqual(named_prs('osd: fix trackers 82289-82295 (#1 of 2)'), [])
        self.assertEqual(named_prs('Revert "osd: x (#70377)"'), [])

    def test_what_cannot_be_checked_out_is_left_out(self):
        files = config(sha1='f' * 40, repo=f'file://{self.ci}')
        self.assertEqual(check_out(files, self.clone, self.worktrees, self.said.append), {})
        self.assertIn('Could not check out ffffffffff', self.said[-1])
        self.assertEqual(check_out(files, None, self.worktrees, self.said.append), {})


if __name__ == '__main__':
    unittest.main()
