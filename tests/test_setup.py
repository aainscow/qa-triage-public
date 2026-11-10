import io
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

from qa_triage import config, setup
from qa_triage.sources import ARCHIVE


class TestSetup(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='qa_triage-setup-'))
        self.env = {'XDG_CONFIG_HOME': os.path.join(self.dir, 'config')}
        # Not the library of wherever these tests were cloned from: nothing is fetched.
        patcher = mock.patch.object(config, 'own_library', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.setup = setup.Setup(self.env, cwd=self.dir)

    def test_what_it_shows(self):
        self.assertEqual(self.setup.path, os.path.join(self.dir, 'config', 'qa_triage',
                                                       'config.yaml'))
        rows = {name: (title, shown) for name, title, shown in self.setup.rows()}
        self.assertEqual(rows['judge'], ('Judge', f'{config.DEFAULTS["judge"]} (default)'))
        self.assertEqual(rows['archive'], ('Job files', f'{ARCHIVE} (default)'))
        self.assertIn('sepia VPN', self.setup.about('archive'))
        self.assertEqual(rows['db'], ('Library', 'the records that come with qa_triage (default)'))
        self.setup.set('db', self.dir)
        self.assertEqual(self.setup.check('db'), (False, 'it has no problems/ directory'))
        os.makedirs(os.path.join(self.dir, 'problems'))
        self.assertEqual(self.setup.check('db'), (True, 'a library'))
        # Each kind of agent installed here, as it is, and any other, typed.
        with mock.patch.object(setup, 'installed', return_value=[('claude', 'Claude Code')]):
            judges = dict(self.setup.choices('judge'))
        self.assertEqual(judges['claude'], 'Claude Code, with its default model')
        self.assertEqual(judges['none'], 'none: no AI, the tree and the records alone')
        self.assertIn(setup.OTHER, judges)
        self.assertIsNone(self.setup.choices('learn_max'))

    def test_changing_and_saving(self):
        s = self.setup
        s.set('judge', 'claude:haiku')
        s.set('analyst', 'claude:opus')
        s.set('learn_max', '3')
        s.set('archive', 'https')
        s.set('lessons_at_once', 'all')
        s.set('lessons_at_once', '3')
        for name, wrong in (('learn_max', 'many'), ('archive', 'trial200'),
                            ('judge', 'nonsense:x'), ('lessons_at_once', '0')):
            with self.assertRaises(ValueError):
                s.set(name, wrong)
        # A key you paste goes in a file only you can read; the config names it.
        path = s.keep_key('tracker_key', 'secret-key\n')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        s.set('tracker_key', path)
        self.assertTrue(s.changed)
        s.save()
        with open(s.path) as f:
            text = f.read()
        self.assertNotIn('secret-key', text)
        self.assertEqual(yaml.safe_load(text), {'judge': 'claude:haiku', 'analyst': 'claude:opus',
                                                'learn_max': 3, 'archive': 'https',
                                                'lessons_at_once': 3, 'tracker_key': path})
        # Back to the default; and qa_triage reads what was saved.
        s.set('learn_max', '')
        self.assertEqual(s.shown('learn_max'), '5 (default)')
        settings, read = config.load(environ=self.env)
        self.assertEqual((read, settings['judge'], settings['learn_max']),
                         ([s.path], 'claude:haiku', 3))

    def test_checks(self):
        s = self.setup
        s.set('judge', 'cmd:no-such-command-here')
        self.assertEqual(s.check('judge'),
                         (False, 'cmd: there is no no-such-command-here command here'))
        s.set('judge', 'none')
        self.assertEqual(s.check('analyst'), (True, 'no AI'))
        # The tracker key: whose the tracker says it is, asking nothing else.
        self.assertEqual(s.check('tracker_key')[0], True)
        path = s.keep_key('tracker_key', 'k1')
        s.set('tracker_key', path)
        with mock.patch.object(setup, 'tracker_user', return_value='A Person') as user:
            self.assertEqual(s.check('tracker_key'),
                             (True, 'the tracker takes it: it is A Person\'s'))
        user.assert_called_once_with('k1')
        s.set('archive', 'https')

        def answers(code):
            return mock.patch.object(setup, '_ssh', lambda host, command:
                                     subprocess.CompletedProcess([], code, '', ''))
        with answers(255):
            self.assertEqual(s.check('archive'), (True, 'downloaded from qa-proxy'))
        # https when the archive's host answers, as earlier setups chose by themselves.
        with answers(0):
            self.assertEqual(s.check('archive'),
                             (False, 'downloaded from qa-proxy, though trial200 answers over '
                                     'ssh, where they are searched in place: choose it'))
        s.set('archive', 'https://example.com/runs')
        with self.assertRaises(ValueError):
            s.set('archive', 'example.com/runs')
        # The ceph clone: the one you run it in.
        clone = os.path.join(self.dir, 'ceph')
        os.makedirs(os.path.join(clone, 'src'))
        open(os.path.join(clone, 'src', 'ceph_osd.cc'), 'w').close()
        subprocess.run(['git', 'init', '-q', clone], check=True)
        here = setup.Setup(self.env, cwd=os.path.join(clone, 'src'))
        self.assertEqual(here.check('git'), (True, f'here: {clone}'))
        here.set('git', self.dir)
        self.assertEqual(here.check('git'), (False, 'not a git clone'))

    def test_bobs_key(self):
        s = self.setup
        s.set('judge', 'bob')
        self.assertIn(('key:bob', 'Key for bob', 'none'), s.rows())
        self.assertIn('BOB_API_KEY', s.about('key:bob'))
        path = s.keep_key('key:bob', 'b1')
        self.assertTrue(path.endswith('/bob_key'))
        s.set('key:bob', path)
        self.assertEqual(s.data['agents'], {'bob': {'provider': 'bob', 'key_file': path}})
        # Bob is run with it, unless the environment has a key.
        agent = s.agent_named('bob')
        with mock.patch.dict(os.environ):
            os.environ.pop('BOB_API_KEY', None)
            self.assertEqual(agent.env()['BOB_API_KEY'], 'b1')
            os.environ['BOB_API_KEY'] = 'b2'
            self.assertIsNone(agent.env())


class TestKeys(unittest.TestCase):
    """The tracker key and the GitHub token: taken, checked, and how to make them."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='qa_triage-setup-'))
        self.env = {'XDG_CONFIG_HOME': os.path.join(self.dir, 'config')}
        patcher = mock.patch.object(config, 'own_library', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.setup = setup.Setup(self.env, cwd=self.dir)

    def test_how_to_make_them(self):
        s = self.setup
        rows = {name: shown for name, _, shown in s.rows()}
        self.assertEqual(rows['github_key'], 'none: gh, if it is signed in (default)')
        with mock.patch.object(setup.shutil, 'which', return_value=None):
            values = [value for value, _ in s.choices('github_key')]
        self.assertEqual(values, ['key', 'file', setup.HOW[0], setup.OPEN[0], ''])
        with mock.patch.object(setup.shutil, 'which', return_value='/usr/bin/gh'):
            self.assertEqual(s.choices('github_key')[0], setup.GH)
        self.assertIn(setup.HOW, s.choices('tracker_key'))
        # The steps, and the smallest key that does, with the pages that make them.
        tracker = '\n'.join(setup.HOWTO['tracker_key'])
        self.assertIn('https://tracker.ceph.com/my/account', tracker)
        self.assertIn('one API key', tracker)
        github = '\n'.join(setup.HOWTO['github_key'])
        self.assertIn('gh auth login', github)
        self.assertIn('Public repositories; no permissions', github)
        self.assertIn('public_repo scope alone', github)
        self.assertIn('scopes=public_repo', setup.PAGES['github_key'])

    def test_a_github_token(self):
        s = self.setup
        path = s.keep_key('github_key', 'ghp_x')
        self.assertTrue(path.endswith('/github_key'))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        s.set('github_key', path)
        with mock.patch.object(setup, '_gh_user', return_value=None):
            for scopes, said in ((['public_repo'], '; it can comment on PRs'),
                                 ([], '; read-only: it cannot comment on PRs'),
                                 (None, '; a fine-grained token: it can comment on PRs if it '
                                        'may write ceph/ceph\'s pull requests')):
                with mock.patch.object(setup, 'github_user',
                                       return_value=('someone', scopes)) as user:
                    self.assertEqual(s.check('github_key'),
                                     (True, f'GitHub takes it: it is someone\'s{said}'))
                user.assert_called_once_with('ghp_x')
        # gh, signed in, is used first.
        with mock.patch.object(setup, '_gh_user', return_value='someone'):
            self.assertEqual(s.check('github_key'),
                             (True, 'gh is signed in, as someone, and is used first; the '
                                    'token if gh fails'))
        s.save()
        settings, _ = config.load(environ=self.env)
        self.assertEqual(settings['github_key'], path)

    def test_where_the_github_token_is_found(self):
        from qa_triage import sources
        path = os.path.join(self.dir, 'token')
        with open(path, 'w') as f:
            f.write('{"github_token": "t1"}\n')
        self.assertEqual(sources.github_token(path, {}), 't1')
        self.assertEqual(sources.github_token(path, {'GITHUB_TOKEN': 't2'}), 't2')
        self.assertIsNone(sources.github_token(None, {}))
        # It is sent to GitHub alone, and only where gh is not signed in.
        sent = []

        class Answer(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout=None):
            sent.append((req.full_url, req.get_header('Authorization')))
            return Answer(b'{"login": "someone"}')

        sources.use_github_key('t1')
        self.addCleanup(sources.use_github_key, None)
        with mock.patch.object(sources.shutil, 'which', return_value=None), \
                mock.patch.dict(os.environ), \
                mock.patch.object(sources.urllib.request, 'urlopen', urlopen):
            os.environ.pop('GITHUB_TOKEN', None)
            self.assertEqual(sources.github_login(), 'someone')
        self.assertEqual(sent, [('https://api.github.com/user', 'Bearer t1')])


class TestAutomatically(unittest.TestCase):
    """a, and setup --auto: what can be set up from what this machine has."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='qa_triage-setup-'))
        self.env = {'XDG_CONFIG_HOME': os.path.join(self.dir, 'config')}
        # Not the library of wherever these tests were cloned from: nothing is fetched.
        patcher = mock.patch.object(config, 'own_library', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.setup = setup.Setup(self.env, cwd=self.dir)
        self.ran = []
        # Not this machine's gh.
        patcher = mock.patch.object(setup, '_gh_user', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def host(self, answer, returncode=0):
        def ssh(host, command):
            self.ran.append((host, command))
            return subprocess.CompletedProcess([], returncode, answer, '')
        return mock.patch.object(setup, '_ssh', ssh)

    def detect(self, answer, returncode=0, agents=(), clones=()):
        with self.host(answer, returncode), \
                mock.patch.object(setup, 'installed', return_value=list(agents)), \
                mock.patch.object(setup, 'CLONES', tuple(clones)):
            return self.setup.auto(), self.setup.checks

    def test_with_the_archive_host(self):
        changed, found = self.detect('archive\n/usr/bin/lnav\n/usr/bin/vim\n',
                                     agents=[('claude', 'Claude Code'),
                                             ('claude:opus', 'Claude Code with opus')])
        # The defaults fit: nothing to change.
        self.assertEqual(changed, [])
        self.assertEqual(self.ran[0], ('trial200', 'test -d /a && echo archive; '
                                                   'command -v lnav vim less; true'))
        self.assertEqual(found['archive'], (True, 'trial200 answers, and has /a'))
        self.assertEqual(found['editor'], (True, 'on trial200: its $VISUAL, $EDITOR or vim; '
                                                 'it has lnav, vim'))
        # No agent is chosen: that is the person's choice.
        self.assertEqual(found['judge'], (None, 'Claude Code installed here; none chosen: AI '
                                                'costs, so choose one, if any, with enter'))
        self.assertNotIn('judge', self.setup.data)

    def test_without_the_archive_host(self):
        clone = os.path.join(self.dir, 'git', 'ceph')
        os.makedirs(os.path.join(clone, 'src'))
        open(os.path.join(clone, 'src', 'ceph_osd.cc'), 'w').close()
        subprocess.run(['git', 'init', '-q', clone], check=True)
        changed, found = self.detect('', returncode=255, clones=[clone])
        # It never chooses https, which is much slower: that is for the person to.
        self.assertEqual(changed, ['Ceph clone'])
        self.assertEqual(self.setup.data, {'git': clone})
        self.assertEqual(found['archive'], (False, 'trial200 does not answer over ssh: connect '
                                                   'to the sepia VPN, or choose https'))
        self.assertEqual(found['git'], (True, f'found {clone}'))
        self.assertTrue(found['editor'][1].startswith('here: vim'))
        # An archive the person chose is left as it is.
        s = setup.Setup(self.env, cwd=self.dir)
        s.set('archive', 'otherhost:/archive')
        with self.host('', 255), mock.patch.object(setup, 'installed', return_value=[]), \
                mock.patch.object(setup, 'CLONES', ()):
            self.assertEqual(s.auto(), [])
        self.assertEqual(s.checks['archive'], (False, 'otherhost does not answer over ssh: '
                                                      'connect to the sepia VPN, or choose '
                                                      'https'))

    def test_with_no_page(self):
        said = []
        with self.host('archive\n/usr/bin/vim\n'), \
                mock.patch.object(setup, 'installed', return_value=[]), \
                mock.patch.object(setup, 'CLONES', ()):
            s = setup.automatically(self.env, said.append)
        self.assertTrue(os.path.exists(s.path))
        self.assertIn('Job files: trial200:/a (default) (trial200 answers, and has /a)', said)
        self.assertTrue(said[-1].startswith('Set nothing; saved to'))

    def test_the_new_settings(self):
        s = self.setup
        s.set('cited_lines', '4')
        s.set('edit_on', 'here')
        s.set('editor', "lnav -c ':goto {line}' {file}")
        for name, wrong in (('cited_lines', '0'), ('cited_lines', 'all'), ('edit_on', 'there')):
            with self.assertRaises(ValueError):
                s.set(name, wrong)
        self.assertEqual(s.data, {'cited_lines': 4, 'edit_on': 'here',
                                  'editor': "lnav -c ':goto {line}' {file}"})
        with mock.patch.object(setup.shutil, 'which', return_value='/usr/bin/lnav'):
            self.assertEqual(s.check('editor'), (True, 'here: /usr/bin/lnav'))
        s.set('edit_on', 'archive')
        self.assertNotIn('edit_on', s.data)
        with self.host('/usr/bin/lnav\n'):
            self.assertEqual(s.check('editor'), (True, 'on trial200: /usr/bin/lnav'))
        self.assertEqual(self.ran[-1], ('trial200', 'command -v lnav'))
        s.save()
        settings, _ = config.load(environ=self.env)
        self.assertEqual((settings['cited_lines'], settings['edit_on']), (4, 'archive'))

    def test_the_screens_settings(self):
        s = self.setup
        rows = {name: shown for name, _, shown in s.rows()}
        self.assertEqual((rows['approve'], rows['mouse'], rows['judge_on_open']),
                         ('ask (default)', 'on (default)', 'off (default)'))
        self.assertEqual([value for value, _ in s.choices('mouse')], ['', False])
        s.set('mouse', False)
        s.set('judge_on_open', True)
        s.set('approve', 'hold')
        self.assertEqual((s.shown('mouse'), s.shown('judge_on_open')), ('off', 'on'))
        s.save()
        settings, _ = config.load(environ=self.env)
        self.assertEqual((settings['mouse'], settings['judge_on_open'], settings['approve']),
                         (False, True, 'hold'))

    def test_what_was_saved_since(self):
        # What a review that opened the page takes up: what was saved, not what was
        # only changed, with its paths as config.load gives them.
        s = self.setup
        s.set('editor', 'vim -R +{line} {file}')
        s.save()
        page = setup.Setup(self.env, cwd=self.dir)
        page.set('editor', '')
        page.set('tracker_key', 'tracker_key')
        page.set('learn_max', '2')
        page.save()
        page.set('cited_lines', '3')
        self.assertEqual(page.since(), {
            'editor': None, 'learn_max': 2,
            'tracker_key': os.path.join(page.directory, 'tracker_key')})


if __name__ == '__main__':
    unittest.main()
