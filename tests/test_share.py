import os
import subprocess
import tempfile
import unittest

from qa_triage import share

ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
           GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


class TestShare(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='qa_triage-share-')
        self.env = os.environ.copy()
        os.environ.update({k: v for k, v in ENV.items() if k.startswith('GIT_')})
        # The shared library, as a bare repository, with a record in it.
        self.remote = os.path.join(self.root, 'qa-triage-db.git')
        seed = os.path.join(self.root, 'seed')
        os.makedirs(os.path.join(seed, 'problems'))
        with open(os.path.join(seed, 'problems', '100-a.yaml'), 'w') as f:
            f.write('id: "100"\n')
        git('init', '-q', '-b', 'main', seed)
        git('-C', seed, 'add', '-A')
        git('-C', seed, 'commit', '-q', '-m', 'seed')
        git('clone', '-q', '--bare', seed, self.remote)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_a_library_in_a_directory_of_a_branch(self):
        # As a pull request into a bigger repository holds it: the library is a
        # directory of one of its branches.
        big = os.path.join(self.root, 'big')
        os.makedirs(os.path.join(big, 'src', 'other'))
        os.makedirs(os.path.join(big, 'lib', 'db', 'problems'))
        for rel, text in (('src/other/code.c', 'int x;\n'),
                          ('lib/db/problems/100-a.yaml', 'id: "100"\n')):
            with open(os.path.join(big, rel), 'w') as f:
                f.write(text)
        git('init', '-q', '-b', 'main', big)
        git('-C', big, 'add', '-A')
        git('-C', big, 'commit', '-q', '-m', 'big')
        git('-C', big, 'branch', 'wip-db')
        bare = os.path.join(self.root, 'big.git')
        git('clone', '-q', '--bare', big, bare)
        git('-C', bare, 'config', 'uploadpack.allowFilter', 'true')
        db = f'file://{bare}#wip-db:lib/db'
        self.assertEqual(share.parse(db), (f'file://{bare}', 'wip-db', 'lib/db'))
        cache = os.path.join(self.root, 'cache')
        directory = share.update(db, cache, [].append)
        self.assertEqual(directory, os.path.join(cache, 'db', 'big-wip-db', 'lib', 'db'))
        self.assertTrue(os.path.exists(os.path.join(directory, 'problems', '100-a.yaml')))
        # Only the library's directory is there.
        self.assertFalse(os.path.exists(os.path.join(cache, 'db', 'big-wip-db', 'src')))
        self.assertTrue(share.nested(directory))
        # A record and a node, which is pushed to the branch too: those who
        # may push to it cannot make branches beside it.
        with open(os.path.join(directory, 'problems', '200-b.yaml'), 'w') as f:
            f.write('id: "200"\n')
        os.makedirs(os.path.join(directory, 'nodes'))
        with open(os.path.join(directory, 'nodes', 'n.py'), 'w') as f:
            f.write('EXAMPLES = {}\n')
        self.assertEqual(share.changes(directory), {'problems': ['problems/200-b.yaml'],
                                                    'nodes': ['nodes/n.py']})
        self.assertRegex(share.publish(directory, 'Learned 200'), r'^Pushed 1 records, 1 nodes '
                                                                  r'to ')
        self.assertEqual(git('-C', bare, 'show', 'wip-db:lib/db/problems/200-b.yaml'), 'id: "200"')
        self.assertEqual(git('-C', bare, 'rev-parse', 'main'), git('-C', big, 'rev-parse', 'main'))

    def test_taking_and_sharing(self):
        cache = os.path.join(self.root, 'cache')
        said = []
        url = f'file://{self.remote}'
        directory = share.update(url, cache, said.append)
        self.assertEqual(directory, os.path.join(cache, 'db', 'qa-triage-db'))
        self.assertTrue(os.path.exists(os.path.join(directory, 'problems', '100-a.yaml')))
        self.assertEqual(share.update(url, cache, said.append), directory)
        # Something learned: a new record and a node.
        with open(os.path.join(directory, 'problems', '200-b.yaml'), 'w') as f:
            f.write('id: "200"\n')
        os.makedirs(os.path.join(directory, 'nodes'))
        with open(os.path.join(directory, 'nodes', 'crash.py'), 'w') as f:
            f.write('EXAMPLES = {}\n')
        self.assertEqual(share.summary(share.changes(directory)), '1 records, 1 nodes')
        # Not updated over it.
        share.update(url, cache, said.append)
        self.assertIn('has changes not shared yet', said[-1])
        done = share.share(directory, gh=False)
        self.assertRegex(done, r'Pushed 1 records, 1 nodes as learned/\S+ to ')
        branch = done.split(' as ')[1].split(' ')[0]
        self.assertIn(f'refs/heads/{branch}', git('ls-remote', self.remote))
        # Still used here, while it is reviewed; and nothing more to share.
        self.assertTrue(os.path.exists(os.path.join(directory, 'problems', '200-b.yaml')))
        self.assertEqual(share.share(directory), 'Nothing to share: the library has no changes.')
        # Once merged, updating takes it, and keeps nothing twice.
        merge = os.path.join(self.root, 'merge')
        git('clone', '-q', self.remote, merge)
        git('-C', merge, 'merge', '-q', '--squash', f'origin/{branch}')
        git('-C', merge, 'commit', '-q', '-m', 'Merge what was learned')
        git('-C', merge, 'push', '-q', 'origin', 'main')
        share.update(url, cache, said.append)
        self.assertEqual(git('-C', directory, 'status', '--porcelain'), '')
        self.assertEqual(git('-C', directory, 'rev-parse', 'HEAD'),
                         git('-C', merge, 'rev-parse', 'HEAD'))

    def test_sync_and_publish(self):
        mine, theirs = os.path.join(self.root, 'mine'), os.path.join(self.root, 'theirs')
        for clone in (mine, theirs):
            git('clone', '-q', self.remote, clone)
        self.assertEqual(share.root(os.path.join(mine, 'problems')), os.path.realpath(mine))
        self.assertFalse(share.sync(mine))
        # Someone else learns something, and publishes it at once.
        with open(os.path.join(theirs, 'problems', '200-b.yaml'), 'w') as f:
            f.write('id: "200"\n')
        self.assertRegex(share.publish(theirs, 'Learned 200'), r'^Pushed 1 records to ')
        # Here, a change in progress is set aside while the library is brought up to
        # date, and put back.
        with open(os.path.join(mine, 'problems', '300-c.yaml'), 'w') as f:
            f.write('id: "300"\n')
        self.assertTrue(share.sync(mine))
        self.assertTrue(os.path.exists(os.path.join(mine, 'problems', '200-b.yaml')))
        self.assertTrue(os.path.exists(os.path.join(mine, 'problems', '300-c.yaml')))
        # Theirs moves on again before this is published: it is rebased onto it.
        with open(os.path.join(theirs, 'problems', '400-d.yaml'), 'w') as f:
            f.write('id: "400"\n')
        share.publish(theirs, 'Learned 400')
        self.assertRegex(share.publish(mine, 'Learned 300'), r'^Pushed 1 records to ')
        self.assertEqual(git('-C', mine, 'log', '--format=%s', '-3').splitlines(),
                         ['Learned 300', 'Learned 400', 'Learned 200'])
        self.assertEqual(share.publish(mine, 'again'),
                         'Nothing to publish: the library has no changes.')

    def test_several_at_once_clone_it_once(self):
        # qa_triage started several times at once on an empty cache: each waits for
        # the one before, and finds its clone.
        import concurrent.futures
        cache = os.path.join(self.root, 'cache')
        db = f'file://{self.remote}'
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            got = list(pool.map(lambda _: share.update(db, cache, say=lambda *a: None),
                                range(4)))
        self.assertEqual(len(set(got)), 1)
        self.assertTrue(os.path.isdir(os.path.join(got[0], 'problems')))

    def test_a_change_says_what_else_went_with_it(self):
        # A change made here before, and not shared, goes with the next one, named in
        # its message; the tree-path files go unnamed.
        mine = os.path.join(self.root, 'mine')
        git('clone', '-q', self.remote, mine)
        for name in ('problems/200-b.yaml', 'problems/300-c.yaml', 'paths/run.yaml'):
            os.makedirs(os.path.dirname(os.path.join(mine, name)), exist_ok=True)
            with open(os.path.join(mine, name), 'w') as f:
                f.write('id: "x"\n')
        share.publish(mine, 'Learned 300', own=['problems/300-c.yaml'])
        self.assertEqual(git('-C', mine, 'log', '-1', '--format=%B').strip(),
                         'Learned 300\n\nAlso in this commit, made here before and not '
                         'shared until now:\n  problems/200-b.yaml')

    def test_a_checked_change_is_pushed_only_onto_what_it_was_checked_against(self):
        mine, theirs = os.path.join(self.root, 'mine'), os.path.join(self.root, 'theirs')
        for clone in (mine, theirs):
            git('clone', '-q', self.remote, clone)
        # Someone else pushes while a change here is checked against the library as
        # it was.
        with open(os.path.join(theirs, 'problems', '200-b.yaml'), 'w') as f:
            f.write('id: "200"\n')
        share.publish(theirs, 'Learned 200')
        with open(os.path.join(mine, 'problems', '100-a.yaml'), 'w') as f:
            f.write('id: "100"\ntitle: mine\n')
        with self.assertRaisesRegex(share.Moved, r'the library changed while this change was '
                                                 r'checked: \w+ t: Learned 200$'):
            share.publish(mine, 'Learned 100', checked=True)
        # It was not merged with theirs: it was taken back, to be checked again.
        self.assertEqual(git('-C', mine, 'status', '--porcelain'), '')
        self.assertEqual(git('-C', mine, 'log', '--format=%s').splitlines(), ['seed'])
        self.assertEqual(git('-C', self.remote, 'log', '--format=%s', 'main').splitlines(),
                         ['Learned 200', 'seed'])
        # Made again on theirs, it is pushed.
        self.assertTrue(share.sync(mine))
        with open(os.path.join(mine, 'problems', '100-a.yaml'), 'w') as f:
            f.write('id: "100"\ntitle: mine\n')
        self.assertRegex(share.publish(mine, 'Learned 100', checked=True), r'^Pushed ')
        self.assertEqual(git('-C', self.remote, 'log', '--format=%s', 'main').splitlines(),
                         ['Learned 100', 'Learned 200', 'seed'])

    def test_publishing_where_main_is_not_taken(self):
        # A remote that takes no push to main is proposed to instead.
        hook = os.path.join(self.remote, 'hooks', 'pre-receive')
        with open(hook, 'w') as f:
            f.write('#!/bin/sh\nwhile read old new ref; do\n'
                    '  [ "$ref" = refs/heads/main ] && echo "main is protected" && exit 1\n'
                    'done\nexit 0\n')
        os.chmod(hook, 0o755)
        mine = os.path.join(self.root, 'mine')
        git('clone', '-q', self.remote, mine)
        with open(os.path.join(mine, 'problems', '300-c.yaml'), 'w') as f:
            f.write('id: "300"\n')
        said = []
        done = share.publish(mine, 'Learned 300', said.append, gh=False)
        self.assertIn('did not take the push', said[0])
        self.assertRegex(done, r'Pushed 1 records as learned/\S+ to ')

    def test_code_is_proposed_for_review(self):
        mine = os.path.join(self.root, 'mine')
        git('clone', '-q', self.remote, mine)
        main = git('-C', mine, 'rev-parse', 'HEAD')
        os.makedirs(os.path.join(mine, 'nodes'))
        with open(os.path.join(mine, 'nodes', 'crash.py'), 'w') as f:
            f.write('EXAMPLES = {}\n')
        done = share.publish(mine, 'Learned a node', gh=False)
        self.assertRegex(done, r'^Pushed 1 nodes as learned/\S+ to .* once someone '
                               r'has reviewed and merged it\.$')
        branch = done.split(' as ')[1].split(' ')[0]
        # Proposed on a branch; main, here and there, is as it was, without it.
        self.assertIn(f'refs/heads/{branch}', git('ls-remote', self.remote))
        self.assertEqual(git('-C', mine, 'rev-parse', 'HEAD'), main)
        self.assertEqual(git('-C', self.remote, 'rev-parse', 'main'), main)
        self.assertFalse(os.path.exists(os.path.join(mine, 'nodes', 'crash.py')))

    def test_a_fork_for_who_may_not_push(self):
        gh = os.path.join(self.root, 'gh')
        with open(gh, 'w') as f:
            f.write('#!/bin/sh\ncase "$1 $2" in\n  "api user") echo lee ;;\n'
                    '  "repo fork") echo "$3" >> ' + os.path.join(self.root, 'forked') + ' ;;\n'
                    'esac\n')
        os.chmod(gh, 0o755)
        self.assertEqual(share._fork('git@github.com:aainscow/qa-triage-db.git', gh),
                         ('lee', 'git@github.com:lee/qa-triage-db.git'))
        self.assertEqual(share._fork('https://github.com/aainscow/qa-triage-db', gh),
                         ('lee', 'https://github.com/lee/qa-triage-db'))
        with open(os.path.join(self.root, 'forked')) as f:
            self.assertEqual(f.read().split(), ['aainscow/qa-triage-db'] * 2)
        # Not a fork of one's own, nor of what is not on GitHub.
        self.assertIsNone(share._fork('git@github.com:lee/qa-triage-db.git', gh))
        self.assertIsNone(share._fork(f'file://{self.remote}', gh))

    def test_a_library_with_no_remote(self):
        directory = os.path.join(self.root, 'mine')
        git('clone', '-q', self.remote, directory)
        git('-C', directory, 'remote', 'remove', 'origin')
        with open(os.path.join(directory, 'problems', '300-c.yaml'), 'w') as f:
            f.write('id: "300"\n')
        self.assertIn('which has no remote', share.share(directory))
        self.assertEqual(git('-C', directory, 'status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
