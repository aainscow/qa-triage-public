"""Settings: built-in defaults, then config files, then the environment, then
--set and the other command-line options, each overriding the ones before.

Without --config (or $QA_TRIAGE_CONFIG), the config files are
$XDG_CONFIG_HOME/qa_triage/config.yaml (by default ~/.config/qa_triage/config.yaml)
and then ./qa_triage.yaml, if they exist. Mappings, such as agents, are merged;
anything else is replaced. Paths in a config file are relative to that file.
See examples/ for config files.
"""

import os
import re
import subprocess

import yaml

from .sources import ARCHIVE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The library in this repository, and the branch everyone shares it on.
LIBRARY, BRANCH = 'library', 'main'
DEFAULTS = {
    'problems': os.path.join(ROOT, LIBRARY, 'problems'),
    'nodes': os.path.join(ROOT, LIBRARY, 'nodes'),
    'labels': os.path.join(ROOT, LIBRARY, 'labels'),
    'archive': ARCHIVE,
    'cache': os.path.join('~', '.cache', 'qa_triage'),
    # The ceph clone you run it in, or else the one qa_triage's checkout is in, as
    # ceph's src/script/qa_triage/qa_triage.sh puts it, unless a config names
    # another or none.
    'git': None,
    'worktrees': os.path.join('~', '.cache', 'qa_triage', 'worktrees'),
    'deep': False,
    # The most jobs of a run triaged at once; the archive is asked by at most eight.
    'at_once': 8,
    'corpus': None,
    'judge': 'none',
    'analyst': None,
    'learn_max': 5,
    # How many lessons ask their analysts at once: a number, or all. Each change is
    # still checked and kept one at a time, against the library as the others left it.
    'lessons_at_once': 1,
    # How many groups of jobs ask the judge at once: a number, or all.
    'judges_at_once': 4,
    # The most one lesson's analyst may cost, in its own unit; None for no limit.
    'learn_budget': None,
    # Where issues are filed (the tracker's project identifier), and a file with the
    # key to file them with, if $REDMINE_API_KEY is not set (default: ~/.redmine_key).
    # The shared library of what qa_triage learned: a directory, or a git URL, cloned
    # into the cache. Its problems/, nodes/ and labels/ are used, unless those
    # settings name others. By default, unless they do, the library/ of the
    # repository qa_triage was cloned from, on its main branch; none for no library.
    'db': None,
    # Before a change to the records, the tree or the settled jobs is kept: ask the
    # person (ask), keep every one that passes the checks (always), or keep each
    # for a person to approve later (hold). With a db, a change is pushed to it
    # once it is kept.
    'approve': 'ask',
    # Before a change edits what the library has, not only adds to it: as approve
    # says, but never always unless this says so; by default, ask (hold, where no
    # one can be asked).
    'approve_edits': None,
    # Answered runs held out of learning, kept as qa_triage answers keeps them: no
    # lesson is taught from them, the memory does not remember them, and a change
    # that places fewer of their jobs rightly is not kept. How the library does on
    # them is how it does on runs it has not seen.
    'holdout': None,
    'tracker_project': 'rados',
    'tracker_key': None,
    # A file with a GitHub token, for when gh is not signed in and $GITHUB_TOKEN is
    # not set: to read pull requests as you, and to comment on them when you ask.
    'github_key': None,
    'agents': {},
    # The most lines of the logs a result shows for a line the judge quotes, of those
    # that have it.
    'cited_lines': 10,
    # What qa_triage open, and v in the review, open a file of a job's archive at a
    # line with: a command with {file} and {line} in it, such as
    # lnav -c ':goto {line}' {file}; by default $VISUAL, or $EDITOR, or vim, given
    # +{line} {file}. It runs on the archive's host, over ssh, where the files are
    # (archive), or here, on a copy (here).
    'editor': None,
    'edit_on': 'archive',
    # The review's full screen: whether it starts with the mouse on, for the wheel to
    # scroll, a click to choose and a drag to select and copy (m turns it off, for the
    # terminal's own selection); and whether it asks the judge about every group not
    # judged yet as soon as it opens, in the background, without asking first.
    'mouse': True,
    'judge_on_open': False,
}
PATHS = ('problems', 'nodes', 'labels', 'cache', 'git', 'worktrees', 'tracker_key', 'github_key',
         'holdout')


def user_file(environ=os.environ):
    """Your own config file: $XDG_CONFIG_HOME/qa_triage/config.yaml."""
    home = environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(home, 'qa_triage', 'config.yaml')


def files(explicit=None, environ=os.environ):
    """The config files to read, in order."""
    explicit = explicit or ([environ['QA_TRIAGE_CONFIG']]
                            if environ.get('QA_TRIAGE_CONFIG') else [])
    if explicit:
        missing = [path for path in explicit if not os.path.exists(path)]
        if missing:
            raise ValueError(f'no config file {", ".join(missing)}')
        return explicit
    return [path for path in (user_file(environ), 'qa_triage.yaml') if os.path.exists(path)]


def _merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            value = _merge(out[key], value)
        out[key] = value
    return out


def _check(settings, where):
    unknown = set(settings) - set(DEFAULTS)
    if unknown:
        raise ValueError(f'{where}: unknown settings {", ".join(sorted(unknown))}; the '
                         f'settings are {", ".join(DEFAULTS)}')
    for key in ('approve', 'approve_edits'):
        if settings.get(key) not in (None, 'ask', 'always', 'hold'):
            raise ValueError(f'{where}: {key} is ask, always or hold, not '
                             f'{settings.get(key)!r}')
    at_once = settings.get('at_once', 1)
    if isinstance(at_once, bool) or not isinstance(at_once, int) or at_once < 1:
        raise ValueError(f'{where}: at_once is how many jobs are triaged at once, a number '
                         f'from 1, not {at_once!r}')
    for key, what in (('lessons_at_once', 'lessons ask their analysts'),
                      ('judges_at_once', 'groups of jobs ask the judge')):
        value = settings.get(key, 1)
        if value != 'all' and (isinstance(value, bool) or not isinstance(value, int) or
                               value < 1):
            raise ValueError(f'{where}: {key} is how many {what} at once, a number from 1, '
                             f'or all, not {value!r}')
    cited = settings.get('cited_lines', 1)
    if isinstance(cited, bool) or not isinstance(cited, int) or cited < 1:
        raise ValueError(f'{where}: cited_lines is how many lines of the logs a result shows '
                         f'for each line the judge quotes, a number from 1, not {cited!r}')
    if settings.get('edit_on', 'archive') not in ('archive', 'here'):
        raise ValueError(f'{where}: edit_on is archive (on its host, over ssh) or here, not '
                         f'{settings.get("edit_on")!r}')
    agents = settings.get('agents') or {}
    if not isinstance(agents, dict) or not all(isinstance(a, dict) for a in agents.values()):
        raise ValueError(f'{where}: agents must map each name to its settings')


def enclosing_repo(path):
    """The top of the git work tree that path is in, or None."""
    try:
        p = subprocess.run(['git', '-C', path, 'rev-parse', '--show-toplevel'],
                           capture_output=True, text=True)
    except OSError:
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def ceph_clone(path):
    """The top of the ceph clone that path is in, or None: not another repository."""
    top = enclosing_repo(path)
    return top if top and os.path.exists(os.path.join(top, 'src', 'ceph_osd.cc')) else None


def own_library():
    """The library of the repository qa_triage was cloned from, as a db setting:
    library/ on the main branch of its origin; None if it was not cloned from one."""
    if not os.path.isdir(os.path.join(ROOT, LIBRARY)):
        return None
    try:
        p = subprocess.run(['git', '-C', ROOT, 'remote', 'get-url', 'origin'],
                           capture_output=True, text=True)
    except OSError:
        return None
    url = p.stdout.strip()
    if p.returncode or not re.match(r'^(\w+://|git@)', url):
        return None
    return f'{url}#{BRANCH}:{LIBRARY}'


def resolve(settings, directory):
    """settings with its paths made absolute, relative to directory."""
    out = dict(settings)
    for key in PATHS:
        if out.get(key) and out[key] != 'none':
            out[key] = os.path.join(directory, os.path.expanduser(out[key]))
    if out.get('db') and out['db'] != 'none' and not re.match(r'^(\w+://|git@)',
                                                             str(out['db'])):
        out['db'] = os.path.join(directory, os.path.expanduser(out['db']))
    if out.get('corpus'):
        corpus = out['corpus'] if isinstance(out['corpus'], list) else [out['corpus']]
        out['corpus'] = [os.path.join(directory, os.path.expanduser(c)) for c in corpus]
    return out


def parse_set(text):
    """{key: value} from "a.b=value"; the value is read as YAML."""
    key, sep, value = text.partition('=')
    if not sep or not key.strip():
        raise ValueError(f'--set {text}: give key=value')
    out = yaml.safe_load(value) if value.strip() else None
    for part in reversed(key.strip().split('.')):
        out = {part: out}
    return out


def load(explicit=None, sets=(), environ=os.environ):
    """(settings, the config files read)."""
    settings, read = dict(DEFAULTS), files(explicit, environ)
    for path in read:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f'{path}: a config file is a mapping of settings')
        _check(data, path)
        settings = _merge(settings, resolve(data, os.path.dirname(os.path.abspath(path))))
    if environ.get('QA_TRIAGE_CACHE'):
        settings['cache'] = environ['QA_TRIAGE_CACHE']
    for text in sets:
        extra = parse_set(text)
        _check(extra, f'--set {text}')
        settings = _merge(settings, resolve(extra, os.getcwd()))
    settings['cache'] = os.path.expanduser(settings['cache'])
    settings['worktrees'] = os.path.expanduser(settings['worktrees'])
    if settings['git'] is None:
        settings['git'] = ceph_clone(os.getcwd()) or ceph_clone(os.path.dirname(ROOT))
    elif settings['git'] == 'none':
        settings['git'] = None
    if settings['db'] == 'none':
        settings['db'] = None
    elif settings['db'] is None and all(settings[part] == DEFAULTS[part]
                                        for part in ('problems', 'nodes', 'labels')):
        settings['db'] = own_library()
    return settings, read
