# qa_triage

The original idea for this tool is Gordon's ([hutchig](https://github.com/hutchig)): a
decision tree that explains how each job failed, with records of the known problems
laid out at its leaves.

> This is a read-only snapshot of qa_triage, as it was on 2026-11-10, published for
> reading. The repository it is developed in, with the library its users share and
> add to, is private: the quick start below is for its collaborators.

Triage the failed jobs of a Ceph [teuthology](https://github.com/ceph/teuthology) QA
run: find the ones that are already-known problems, and hand the rest to a person
with the evidence gathered and a draft tracker comment ready.

## Quick start

You need to be a collaborator on this repository. The sepia VPN is the fastest way to
the job files, but not needed: without it, `--archive https` (or choosing https on the
setup page) reads them from qa-proxy, which is enough for most runs. Once, in a ceph
clone:

```
$ git fetch https://github.com/ceph/ceph.git pull/73070/head
$ git restore --source=FETCH_HEAD --worktree -- src/script/qa_triage
$ ln -s $PWD/src/script/qa_triage/qa_triage.sh ~/bin/qa_triage
```

Then:

1. `qa_triage https://pulpito.ceph.com/<run>/` opens the run on a full screen. The
   first time, the setup page comes first: its defaults, with no AI, are fine.
2. The run's failed and dead jobs come into the list as they are triaged, in groups
   that failed the same way. ↑ ↓ choose a group; enter shows what was found for it,
   with the log lines.
3. `c` shows the comment for the run's QA tracker, and `w` its section for the RADOS
   wiki; `y` copies it. `i` updates the tracker about a group, and shows you what it
   would post first. When you are done, `W` takes you through the rest, step by
   step, asking before each post: new tracker issues, comments on known ones and on
   the PRs that caused failures, the wiki section (copied, for you to paste in),
   "Rados approved" on the PRs, and the QA tracker. A step that needs a key you have not given says how to make one,
   or lets you do it by hand.
4. `qa_triage watch` shows the week's `rados` runs of main together, the commonest
   failures first; `qa_triage watch --prs mine` shows the runs of your PRs. `←` goes
   to the pane of runs.
5. `?` lists every key, and `q` quits. The keys marked "costs AI" ask an agent, if
   you choose one: `o` opens the setup page again.

The rest of this page and [the guide](docs/guide.md) cover the command line, the AI
agents, and what the tool learns.

Most failures in a QA run are problems someone has already seen and filed. The tool
explains each failure with a tree of questions, such as which daemon crashed, on what
assert, called from where, and looks the explanation up in its records of known
problems. Neither step uses AI, and every result cites the log lines it was decided
by. An AI judge, if you choose one, is asked only about the failures they could not
match, once per distinct failure. An AI analyst, if you ask for one, teaches the tree
and the records, so that the tool decides such failures by itself from then on.

It is not tied to any AI agent, or to any company's: the judge and the analyst are
whatever agents you choose, from the command line of a coding agent to a local model
(see [Agents and config](docs/guide.md#agents-and-config)), or none.

- **This page:** how to start, and the everyday commands.
- **[The user guide](docs/guide.md):** every command, setting and key.
- **[How qa_triage works](docs/design.md):** the tree, the records, the checks,
  how it learns and shares what it learns, and why.
- **[AGENTS.md](AGENTS.md):** for an AI agent that uses or works on the tool.
- **[CONTRIBUTING.md](CONTRIBUTING.md):** adding problem records and tree nodes.

## Getting started

You need Python 3.9 or later, git, and ssh access to a host that has the teuthology
archive (`trial200` by default), which needs the sepia VPN. This repository is private:
ask its owner to add you as a collaborator. A read-only snapshot of it is public, to
read: [aainscow/qa-triage-public](https://github.com/aainscow/qa-triage-public).

Until ceph/ceph#73070 is merged, the commands in the [quick start](#quick-start) take
its script from the pull request. They leave your ceph clone's branch as it is, with
the script's files untracked. Then give it a run:

```
$ qa_triage https://pulpito.ceph.com/<run>/
```

The first time, it clones this repository, makes its own Python environment, and
opens a setup page to choose the settings; each time after, it brings itself up to
date first. A run can be named by its pulpito URL, its name, its branch, or a
tracker issue that points at it.

## Reviewing a run

The review opens on a full screen at once. The tree and the records triage the run's
failed and dead jobs in the background, which costs nothing, and each group of jobs
that failed the same way comes into the list as it is decided. Once they are done,
the screen offers to ask the judge about the groups they could not decide.

| Key | What it does | Costs AI |
|---|---|---|
| ↑ ↓, enter | choose a group; everything found for it | |
| `w`, `c` | the run's section for the RADOS wiki; the QA tracker comment | |
| `p` | the PRs the run tests, with their titles, and the groups that ran their files | |
| `i` | update records: comment on the PR that caused it, comment on its tracker issue (only its jobs, as `/a/` paths, or with what was found), or file a new one, the recommended way first; or all the run's tracker issues at once, as `I` does; what is posted is shown first | |
| `W` | done: wrap the run up, step by step, each post asked about first; a missing key is made and given on the way, or the step done by hand | |
| `I` | comment on every tracker issue the run saw again, with its jobs as `/a/` paths, all shown first; an issue resolved after the run began is left out, as the run saw it before its fix | |
| `P` | post "Rados approved", linking the run's wiki section, on each PR the run tests that no failure was found to be caused by | |
| `j`, `u` | the judge on this group; on every group not judged yet | yes |
| `l` | the analyst learns from this group | yes |
| `d` | doubt this group's result: the analyst finds the cause, and you accept its response, or discuss it with it | yes |
| `t` | talk about this group with an analyst | yes |
| `A` | approve the changes the analyst made, which wait for you | |
| `o` | options: the analyst for this review, or the settings | |
| `x`, `?`, `q` | stop the agent at work; all the keys; quit | |

What the agents said, and what they cost, is kept: starting the review of the same
run again starts from all of it, and nothing paid for is paid for twice.

## Approving what the tool learns

When the analyst learns from a group, its change to the records or the tree is kept
only if it passes the tool's checks, against every job triaged before, and then only
with your say. A box asks about each change as soon as it is ready: what failed, as
the analyst found it; what the change does; and a commit to inspect its code in:

```
$ git -C <library> show <commit>
```

Keep it, and it is checked again and pushed to the shared library, for everyone's
runs. If others changed the library meanwhile, it is made again on top of what they
did, keeping both: the jobs either side added to a record, and what only one side
changed. If it then fails a check, or both changed the same thing, the screen says
which, and offers to have the analyst make it again for the library as it is now,
which costs AI. Reject it, and it is set aside. As the review opens, it offers to ask
about the changes that already wait; `A` asks about them at any time, and
`qa_triage pending` lists them.

## Watching several runs

To see the commonest failures of several runs at once, as whoever watches the QA runs
does, give them, or let it find the `rados` runs of main and of the QA batches for
main between two dates, by default the last week's:

```
$ qa_triage watch <run> <run> <run>            # these runs
$ qa_triage watch                              # the runs it watched last, or the week's
$ qa_triage watch --since 10-20 --until 10-31  # the runs of those days
$ qa_triage watch --prs mine                   # the runs of your PRs, of any suite
```

With `--prs`, the runs are those that test the PRs: the QA batches whose QA tracker
lists them, and your own runs, or anyone's of the PR's branch, whose build has them,
merged in, as their commits, or as commits that name them, as a squash of a PR does
(see [the guide](docs/guide.md#watching-the-runs-of-your-prs)).

It opens a screen with a pane down its left: the summary of the runs at the top, then
each run, newest first, with how far its triage has got. The summary lists the
failures of every run together, those seen in the most runs first, saying for each
whether it is a known problem, one seen again after its fix, or new, and whether main
has it too; `→` goes to one in its run, which is a whole review, with all its keys.
Under the run chosen, the pane shows the PRs it tests. The runs share the AI: the
judge and the analyst take turns, so many at once for all the runs together, and a
group waiting for a turn is marked `…`, where one at work turns.
`←` goes to the pane, `F` finds the runs again, and `D` changes the dates. Drag the
pane's edge, or the line under a list, to resize them. It triages with the tree and
the records alone, with no AI, until you ask an agent in a run. `--plain` prints the
list instead.

## Without the screen

```
$ qa_triage run <run> --judge <agent>       # triage a run in one go
21 jobs: 11 duplicate, 10 new
Report: ~/.cache/qa_triage/runs/<run>/report.md
Drafts: ~/.cache/qa_triage/runs/<run>/drafts/
$ qa_triage open <run>/<job> teuthology.log:1234   # a cited line, in your editor
$ qa_triage doubt <run>/<job> --says 81136 --why "the reviewer says 81136"
$ qa_triage settle <run>/<job> 81136 --why "the OSD log shows it"
$ qa_triage pending                          # the changes that wait for approval
$ qa_triage setup                            # choose the agents and keys again
```

The verdicts are `duplicate` (a record's checks placed it), `probable-duplicate` (the
judge placed it, with quotes that were found in the logs), `seen-after-fix` (a known
problem whose fix is in the build), `ambiguous` (several records matched) and `new`.
The output of each run, the report, the drafts and `results.json`, is in
`~/.cache/qa_triage/runs/<run>/`; see [Output](docs/guide.md#output).

Nothing is posted anywhere unless you say so: the tool writes a report and drafts for
a person to check and post, and files a tracker issue only when you ask it to.

## Choosing AI agents

None is needed: with no agent, the tree and the records work alone. The setup page
(`qa_triage setup`, or `o` in a review) lists the agents installed here, and keeps
their keys in files only you can read.

- The **judge** is asked often, about one job per group, so a quick, cheap one is
  best. Several can be given, each asked only when the one before is unsure.
- The **analyst** reads the logs itself and writes what every later run uses, so a
  strong one is worth what it costs. By default it is the judge's agent.

The providers are the command lines of Bob Shell, Claude Code and goose, a model
served by Ollama, any OpenAI-compatible API, or any command that reads a prompt and
prints an answer: see [Providers](docs/guide.md#providers) and
[Keys](docs/guide.md#keys).

## License

As Ceph is: the code, and the library, under the LGPL, version 2.1 or 3
([COPYING-LGPL2.1](COPYING-LGPL2.1), [COPYING-LGPL3](COPYING-LGPL3)), and the
documentation under CC-BY-SA-3.0. [COPYING](COPYING) says which is which.

The repository is private, and its snapshot is published, under the same license, at
[aainscow/qa-triage-public](https://github.com/aainscow/qa-triage-public): an archived, read-only copy of
it, without its history, brought up to date from time to time.
