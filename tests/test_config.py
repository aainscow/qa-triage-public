import contextlib
import glob
import io
import os
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import yaml

from qa_triage import cli, config
from qa_triage.agents import CascadeJudge, make_agent, make_judge, parse_spec
from qa_triage.cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWN_LIBRARY = config.own_library


def write(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w') as f:
        yaml.safe_dump(data, f)


class TestConfig(unittest.TestCase):
    def setUp(self):
        # As the config finds it: on macOS, /var is /private/var.
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='qa_triage-config-'))
        self.home = os.path.join(self.dir, 'home')
        self.env = {'XDG_CONFIG_HOME': self.home}
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        # Not the library of wherever these tests were cloned from: nothing is fetched.
        patcher = mock.patch.object(config, 'own_library', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.chdir(self.cwd)

    def test_files_merge_in_order(self):
        write(os.path.join(self.home, 'qa_triage', 'config.yaml'),
              {'judge': 'haiku', 'learn_max': 2, 'problems': 'mine',
               'agents': {'gemma': {'provider': 'ollama', 'model': 'gemma4:26b'}}})
        write('qa_triage.yaml', {'learn_max': 3,
                                'agents': {'gemma': {'url': 'http://gpu:11434'}}})
        settings, read = config.load(environ=dict(self.env, QA_TRIAGE_CACHE='/c'),
                                     sets=['analyst=gemma', 'agents.gemma.context=8192'])
        self.assertEqual(read, [os.path.join(self.home, 'qa_triage', 'config.yaml'),
                                'qa_triage.yaml'])
        self.assertEqual((settings['judge'], settings['learn_max'], settings['analyst'],
                          settings['cache']), ('haiku', 3, 'gemma', '/c'))
        self.assertEqual(settings['agents']['gemma'],
                         {'provider': 'ollama', 'model': 'gemma4:26b',
                          'url': 'http://gpu:11434', 'context': 8192})
        # Paths are relative to the file they are in.
        self.assertEqual(settings['problems'], os.path.join(self.home, 'qa_triage', 'mine'))

    def test_archive_and_source(self):
        settings, _ = config.load(environ=self.env)
        self.assertEqual(settings['archive'], 'trial200:/a')
        # Outside a ceph clone, the one qa_triage's checkout is in, if it is in one.
        self.assertEqual(settings['git'], config.ceph_clone(os.path.dirname(ROOT)))
        # In one, that clone: where the person runs it.
        clone = os.path.join(self.dir, 'ceph')
        os.makedirs(os.path.join(clone, 'src'))
        open(os.path.join(clone, 'src', 'ceph_osd.cc'), 'w').close()
        subprocess.run(['git', 'init', '-q', clone], check=True)
        os.chdir(os.path.join(clone, 'src'))
        self.assertEqual(config.load(environ=self.env)[0]['git'], clone)
        # Not another repository.
        os.remove(os.path.join(clone, 'src', 'ceph_osd.cc'))
        self.assertEqual(config.load(environ=self.env)[0]['git'],
                         config.ceph_clone(os.path.dirname(ROOT)))
        os.chdir(self.dir)
        self.assertTrue(settings['worktrees'].endswith('/.cache/qa_triage/worktrees'))
        settings, _ = config.load(environ=self.env, sets=['git=none', 'archive=https'])
        self.assertEqual((settings['git'], settings['archive']), (None, 'https'))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['--archive', 'trial200', 'lint']), 2)
        self.assertIn('ssh host and the directory', err.getvalue())

    def test_checkout_in_a_ceph_clone(self):
        # Where ceph's src/script/qa_triage/qa_triage.sh puts it: its own repository,
        # in a directory of the ceph clone, whose clone it uses.
        clone = os.path.join(self.dir, 'ceph')
        checkout = os.path.join(clone, 'src', 'script', 'qa_triage', 'qa-triage')
        os.makedirs(os.path.join(checkout, 'library'))
        open(os.path.join(clone, 'src', 'ceph_osd.cc'), 'w').close()
        for repo in (clone, checkout):
            subprocess.run(['git', 'init', '-q', repo], check=True)
        with mock.patch.object(config, 'ROOT', checkout), \
                mock.patch.object(config, 'own_library', OWN_LIBRARY):
            self.assertEqual(config.load(environ=self.env)[0]['git'], clone)
            # Its library: library/ on the main branch of where it was cloned from.
            self.assertIsNone(config.own_library())
            subprocess.run(['git', '-C', checkout, 'remote', 'add', 'origin',
                            'git@github.com:someone/qa-triage.git'], check=True)
            self.assertEqual(config.own_library(),
                             'git@github.com:someone/qa-triage.git#main:library')
            self.assertEqual(config.load(environ=self.env)[0]['db'],
                             'git@github.com:someone/qa-triage.git#main:library')
            # Not where the settings name a library, or its parts, of their own.
            for setting in ('db=/mine', 'problems=/mine/problems'):
                settings = config.load(environ=self.env, sets=[setting])[0]
                self.assertEqual(settings['db'], '/mine' if setting.startswith('db') else None)
            self.assertIsNone(config.load(environ=self.env, sets=['db=none'])[0]['db'])

    def test_explicit_files_only(self):
        write('qa_triage.yaml', {'judge': 'haiku'})
        write('other.yaml', {'corpus': 'runs/*/results.json'})
        settings, read = config.load(['other.yaml'], environ=self.env)
        self.assertEqual((read, settings['judge']), (['other.yaml'], 'none'))
        self.assertEqual(settings['corpus'], [os.path.join(self.dir, 'runs/*/results.json')])
        with self.assertRaisesRegex(ValueError, 'no config file'):
            config.load(['missing.yaml'], environ=self.env)

    def test_mistakes_are_reported(self):
        write('qa_triage.yaml', {'jugde': 'haiku'})
        with self.assertRaisesRegex(ValueError, 'unknown settings jugde'):
            config.load(environ=self.env)
        write('empty.yaml', {})
        with self.assertRaisesRegex(ValueError, 'give key=value'):
            config.load(['empty.yaml'], sets=['judge'], environ=self.env)
        with self.assertRaisesRegex(ValueError, 'at_once is how many jobs'):
            config.load(['empty.yaml'], sets=['at_once=0'], environ=self.env)
        self.assertEqual(config.load(['empty.yaml'], sets=['at_once=3'],
                                     environ=self.env)[0]['at_once'], 3)
        with self.assertRaisesRegex(ValueError, 'lessons_at_once is how many lessons'):
            config.load(['empty.yaml'], sets=['lessons_at_once=some'], environ=self.env)
        for value, read in (('4', 4), ('all', 'all')):
            self.assertEqual(config.load(['empty.yaml'], sets=[f'lessons_at_once={value}'],
                                         environ=self.env)[0]['lessons_at_once'], read)
        with self.assertRaisesRegex(ValueError, 'ollama does not take modle'):
            make_agent('x', {'x': {'provider': 'ollama', 'modle': 'y'}})
        with self.assertRaisesRegex(ValueError, 'ollama needs a model'):
            make_agent('x', {'x': {'provider': 'ollama'}})

    def test_examples(self):
        for path in glob.glob(os.path.join(ROOT, 'examples', '*.yaml')):
            settings, _ = config.load([path], environ=self.env)
            # Every agent each one has, and its judge and analyst, if it chooses them.
            for name in settings['agents']:
                make_agent(name, settings['agents'], files=True)
            if settings['judge'] != config.DEFAULTS['judge']:
                self.assertTrue(make_judge(settings['judge'], settings['agents']), path)
            if settings['analyst']:
                make_agent(settings['analyst'], settings['agents'], files=True)

    def test_the_run_url_alone(self):
        commands = {'run', 'review', 'doubt'}
        url = ('https://pulpito.ceph.com/someone-2026-10-12_23:32:34-rados-main-distro-default-'
               'trial/')
        self.assertEqual(cli.with_command([url], commands), ['review', url])
        self.assertEqual(cli.with_command(['--config', 'x.yaml', '--set', 'judge=none', url,
                                           '--jobs', '1'], commands),
                         ['--config', 'x.yaml', '--set', 'judge=none', 'review', url, '--jobs',
                          '1'])
        # A review option with its value, before the URL.
        self.assertEqual(cli.with_command(['--analyst', 'cmd:x y', url], commands,
                                          {'--analyst'}), ['review', '--analyst', 'cmd:x y', url])
        for argv in (['run', url], ['--cache', '/c', 'doubt', url], ['-h'], []):
            self.assertEqual(cli.with_command(argv, commands), argv)
        # Its output is kept in the cache.
        args = SimpleNamespace(cache='/c')
        self.assertEqual(cli.runs_dir(args), '/c/runs')

    def test_where_the_job_files_are(self):
        """--archive and --https, before the run's URL or after it: the job files are
        downloaded over https only when the person says."""
        url = ('https://pulpito.ceph.com/someone-2026-10-12_23:32:34-rados-main-distro-default-'
               'trial/')
        conf = os.path.join(self.dir, 'none.yaml')
        with open(conf, 'w') as f:
            f.write('db: none\n')
        seen = []
        # Neither the library's records nor its nodes: they are the tests' own.
        with mock.patch.object(cli, 'cmd_review', lambda args, _: seen.append(args.archive)), \
                mock.patch.object(cli, 'cmd_run', lambda args, _: seen.append(args.archive)), \
                mock.patch.object(cli.db, 'load', return_value=[]), \
                mock.patch.object(cli.learned, 'load'), \
                mock.patch.object(cli, 'reach', return_value=(True, 'h answers')):
            for argv in ([url], [url, '--https'], ['--https', url], ['run', '--https', url],
                         ['--archive', 'h:/x', url], [url, '--archive', 'https://example.com/r'],
                         ['--archive', 'h:/x', 'run', url, '--https']):
                cli.main(['--config', conf, *argv])
        self.assertEqual(seen, ['trial200:/a', 'https', 'https', 'https', 'h:/x',
                                'https://example.com/r', 'https'])
        # A host that does not answer over ssh is said at once, with what to do.
        args = SimpleNamespace(archive='h:/x')
        with mock.patch.object(cli, 'reach', return_value=(False, 'h does not answer')):
            self.assertRegex(cli.unreachable(args), r'^h does not answer: connect to the sepia '
                                                    r'VPN.*--https')
        with mock.patch.object(cli, 'reach', return_value=(True, 'h answers')):
            self.assertIsNone(cli.unreachable(args))
        self.assertIsNone(cli.unreachable(SimpleNamespace(archive='https')))
        # Every command that reads job files says so before it starts, not job by job.
        with mock.patch.object(cli, 'reach', return_value=(False, 'h does not answer')), \
                mock.patch.object(cli.db, 'load', return_value=[]), \
                mock.patch.object(cli.learned, 'load'), \
                mock.patch.object(cli, 'cmd_verify') as verify, \
                mock.patch('sys.stderr', new=io.StringIO()) as said:
            self.assertEqual(cli.main(['--config', conf, '--archive', 'h:/x', 'verify']), 2)
        verify.assert_not_called()
        self.assertIn('error: h does not answer: connect to the sepia VPN', said.getvalue())

    def test_a_finished_runs_jobs_are_kept(self):
        """pulpito lists a big run slowly: a finished run's jobs are listed once, and
        kept; an unfinished one's are listed again each time."""
        from qa_triage.sources import Job
        listed = []
        pages = {'done': [Job('done', '1', 'pass', 'd', ''), Job('done', '2', 'fail', 'd', 'r')],
                 'busy': [Job('busy', '1', 'running', 'd', '')]}
        with mock.patch.object(cli, 'list_jobs', lambda run: listed.append(run) or pages[run]), \
                mock.patch.dict(cli._RUNS_JOBS, clear=True):
            for _ in range(2):
                self.assertEqual(cli.jobs_of('done', self.dir)['2'].reason, 'r')
                self.assertEqual(list(cli.jobs_of('busy', self.dir)), ['1'])
            cli._RUNS_JOBS.clear()
            self.assertEqual(cli.jobs_of('done', self.dir)['1'].status, 'pass')
        self.assertEqual(listed, ['done', 'busy', 'busy'])

    def test_saying_what_is_slow(self):
        """A step that takes long says what it waits for; one that does not, nothing."""
        said = []
        with cli.slowly('waiting for h', said.append, after=0.05):
            time.sleep(0.2)
        with cli.slowly('waiting for nothing', said.append, after=0.05):
            pass
        time.sleep(0.1)
        self.assertEqual(said, ['waiting for h'])

    def test_agents_that_cannot_run(self):
        self.assertIsNone(cli.unready(None, make_judge('cmd:cat')))
        with mock.patch.dict(os.environ):
            os.environ.pop('BOB_API_KEY', None)
            self.assertEqual(cli.unready(make_judge('cmd:cat'), make_judge('bob')),
                             'bob: BOB_API_KEY is not set, and Bob Shell needs it: export '
                             'BOB_API_KEY=<your key>, or give a file with it in qa_triage '
                             'setup')
        self.assertEqual(cli.unready(make_judge('cmd:no-such-command-here --x')),
                         'cmd: there is no no-such-command-here command here')
        self.assertIn('no-such-command-here',
                      make_judge('cmd:cat,cmd:no-such-command-here').missing())

    def test_command_line(self):
        write('qa_triage.yaml', {'judge': ['gemma', 'claude:haiku'],
                                'agents': {'gemma': {'provider': 'ollama', 'model': 'g'}}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['config']), 0)
            self.assertEqual(main(['config', '--analyst', 'claude:haiku']), 0)
            self.assertEqual(main(['--set', 'judge=nobody', 'config']), 2)
        # There is an analyst only if one is asked for.
        self.assertIn('# judge: gemma,claude:haiku\n# analyst: none', out.getvalue())
        self.assertIn('# analyst: claude:haiku+files', out.getvalue())
        self.assertIn('error: unknown agent "nobody"', out.getvalue())


class TestSpecs(unittest.TestCase):
    def test_providers(self):
        self.assertEqual(parse_spec('goose:ollama/gemma4:26b+files'),
                         {'provider': 'goose', 'files': True, 'goose_provider': 'ollama',
                          'model': 'gemma4:26b'})
        self.assertEqual(parse_spec('openai:gpt-5@http://h:8000/v1')['url'], 'http://h:8000/v1')
        goose = make_agent('goose:anthropic/claude-sonnet-4-5', files=True)
        argv = goose.command('/w')
        self.assertEqual(argv[:4], ['goose', 'run', '--no-session', '--no-profile'])
        self.assertIn('-m qa_triage mcp /w', argv[argv.index('--with-extension') + 1])
        self.assertEqual(goose.name, 'goose:anthropic/claude-sonnet-4-5+files')
        self.assertNotIn('--with-extension', make_agent('goose').command('/w'))
        self.assertEqual(make_agent('ollama:gemma4', files=True).name, 'ollama:gemma4+files')
        chat = make_agent('c', {'c': {'provider': 'ollama', 'model': 'm', 'max_turns': 9}},
                          files=True)
        self.assertEqual((chat.name, chat.max_turns), ('c+files', 9))
        bob = make_agent('b', {'b': {'provider': 'bob', 'max_turns': 9}}, files=True)
        self.assertEqual((bob.name, bob.command('/w')[-1]), ('b+files', '9'))
        judge = make_judge('ollama:a,claude:haiku')
        self.assertIsInstance(judge, CascadeJudge)
        self.assertEqual(judge.name, 'ollama:a,claude:haiku')
        cmd = make_judge(['mine', 'claude'], {'mine': {'provider': 'cmd', 'command': 'a --x 1,2'}})
        self.assertEqual(cmd.judges[0].argv, ['a', '--x', '1,2'])


if __name__ == '__main__':
    unittest.main()
