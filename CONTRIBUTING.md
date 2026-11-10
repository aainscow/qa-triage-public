# Adding and changing problem records

Anyone can add a record: a person, or an AI agent working for one. Every record is
checked the same way, against real jobs, before it is merged.

## A record

One YAML file per known problem in `library/problems/`, named `<id>-<short-name>.yaml`:

```yaml
id: "80986"                       # the tracker number
tracker: https://tracker.ceph.com/issues/80986
title: "LibRadosIoECPP test failure: Perf counter 'objecter.op_reply' has incorrect value after operate()"
kind: test                        # what needs fixing: product, test or infra
fixes:                            # optional
  - pr: https://github.com/ceph/ceph/pull/72234
    branch: main                  # also matches QA branches ending in -main
    fixed_in: v21.3.0-4449-g05a7656731   # the tracker's "Fixed In"; leave out while unmerged
tree: workunit/gtest              # optional: where in the tree (tree/) it hangs
facts:                            # optional, needs tree: regexes over the tree's facts
  test: '^LibRadosIoECPPParamCombination/LibRadosIoECPP\.'
match:                            # regexes over the job's pulpito data; all must match
  reason: 'workunit test rados/test\.sh'     # the failure reason
  # description: 'rados/singleton/.*all/test-crash'   # the job description
confirm:                          # each must find a line; checked in this order
  - file: teuthology.log          # path in the job's archive; * matches within a path part
    pattern: "Perf counter 'objecter\\.op_reply' has incorrect value"
deep:                             # optional; only with --deep (big daemon logs)
  - file: remote/*/log/ceph-osd.*.log.gz
    pattern: 'unstable write on shard, bouncing to primary'
exclude:                          # optional; any line found rules the record out
  - file: teuthology.log
    pattern: 'some line that means it is a different problem'
notes: |
  The cause, in a few sentences: what goes wrong, and how the logs show it.
examples:                         # jobs known to have this problem: run/job
  - skanta-2026-10-12_23:32:34-rados-wip-bharath20-testing-20261012.132505-main-distro-default-trial/631086
```

Patterns are Python regexes, searched one line at a time. Files ending in `.gz` are
decompressed.

People do not always file a tracker issue. A problem nobody filed one for can have a
record of the pull request that fixed it, or that caused it: its id is `PR` and the
PR's number, and it gives the PR's URL as `pr` instead of `tracker`:

```yaml
id: "PR68305"
pr: https://github.com/ceph/ceph/pull/68305
title: "ganesha.nfsd: Failed to PR_SET_IO_FLUSHER with EPERM"
```

The rest is as above. People see records as TR 80986 and PR 68305, and name either
way where the tool asks for a record, such as `qa_triage settle <run>/<job> PR68305`.

A record with `tree` applies only to the jobs whose path in the tree is that one or
under it, and whose facts its `facts` regexes find; `match` and `confirm` are then
optional. A record without `tree` applies to the jobs its match keys fit, wherever
the tree put them. The report shows each job's path and facts, and a draft record
for a new failure already has them.

## What makes a good record

- **Where the tree finds the cause, the record hangs there.** A crash's assert and
  the function that called it, or a standalone test and its check, are facts the
  tree finds every time; a record that hangs on them needs no regex over the logs.
- **The confirm checks show the cause, not the symptom.** "timeout expired in
  wait_for_all_osds_up" has many causes. 59079's record also needs the admin socket
  timeout that shows the deadlock. If the cause is only visible in a big daemon log,
  put a cheap check of `teuthology.log` in `confirm` and the daemon log check in `deep`.
- **The match keys are loose and the confirm checks are strict.** The match keys only
  decide which records to check, so a record should not miss jobs because a host or a
  number differs.
- **The notes explain the cause.** Judges read them to decide whether a job they
  are looking at has the same cause, and people read them to decide whether a match
  is right.
- **Every record has examples,** and `verify` must pass on all of them.

## Checking a record

```
qa_triage lint
qa_triage verify 80986
```

`verify` runs each example job through the decision graph with only that record,
and shows which match key or check failed. Job archives are deleted after a
while; when an example's archive is gone, replace it with a newer one.

## From a triage run

For each group of jobs the judge called new, `qa_triage run` writes a draft record to
`triage/<run>/drafts/records/`, with a match regex made from the failure reason and
any checks the judge suggested. To turn one into a record:

1. Find the cause in the logs, and find or file its tracker.
2. Fill in the TODOs, and replace the suggested checks if they show the symptom
   rather than the cause.
3. Move the file into `library/problems/` and run `verify`.

An AI agent can do steps 1 and 2 when asked. A person should read the checks before
the record is merged.

## From a doubted result

`qa_triage doubt` (see [Questioning a result](docs/guide.md#questioning-a-result)) has an
agent change the records when a result was wrong, and checks the change against every
record's examples and every settled job. A person still reviews the change before it is
merged, like any other.

A settled job is kept in `library/labels/<run>.yaml`, with the colons of the run's name
changed to underscores:

```yaml
run: skanta-2026-10-12_23:32:34-rados-wip-bharath20-testing-20261012.132505-main-distro-default-trial
jobs:
  "631199":
    problem: "81136"     # the record it must match; null: no record may match it
    by: aainscow         # who doubted the result
    why: the reviewer says 81136
    summary: the analyst's one-line summary
    status: fail         # the job's pulpito data, so that it can be checked offline
    description: rados/singleton/{...}
    reason: Command failed (workunit test mon/mon-stretch-mode-5-mons-8-osds.sh) ...
```

`verify` checks the settled jobs as well as the examples. Unlike an example, a
settled job can say that no record may match it, which keeps a record from
creeping back onto a job with a different cause.

## Adding a node to the tree

The tree asks the questions a reviewer asks. Its nodes are the files of
[library/nodes/](library/nodes/), one for each, from the root's (`root.py`) down;
[qa_triage/tree/](qa_triage/tree/) is the engine that walks them. When the report says
a job is unmatched, its explanation shows where the tree stopped: at a node that could
not answer (`unknown`), at one that asked the judge (`judge`), or at a branch that
leads to no node yet. That is where a node can be added, or a node taught a new
answer; and where a node answered wrongly, it is the node to mend. A node is a
function registered at a path:

```python
@node('crash/abort/bluestore-error',
      'Which error did BlueStore not handle, in which operation, from where?')
def bluestore_error(ctx):
    found = ctx.search(f'remote/*/log/ceph-{ctx.facts["daemon"]}.log.gz',
                       r'_txc_add_transaction error \((-?\d+)\) ...',
                       '_txc_add_transaction ')       # a literal every line it finds has
    if not found:
        return judge('Which error did BlueStore not handle?', 'the log does not say',
                     branch='unexplained')
    return done(f'error-{code}', f'BlueStore did not handle error {code} ...', *found,
                key=f'error {code} in {op} from {caller}', error=code, op=op, caller=caller)
```

- A node reads `ctx.job` (pulpito's data), `ctx.facts` (what the nodes before it
  found), the landmarks (`ctx.first('signal')`, `ctx.hits('task')`; see marks.py),
  and the job's files (`ctx.search`, `ctx.between`, `ctx.tail`). A landmark that
  several nodes need belongs in marks.py, so that the one pass over teuthology.log
  finds it; give it the plain strings that every line it finds has, so that grep can
  find them fast.
- It answers with `go`, `done`, `judge` or `unknown` (see core.py), a sentence that
  says what it found, and the lines that show it: the explanation is for people.
  Every answer but `unknown` cites the lines it was decided by, such as the assert
  or the error, not only the symptom: the report shows them under the answer, as
  the evidence a person checks the result against (`Finding.uncited` finds answers
  that cite none).
- `key` says what tells this failure apart from others on the same path; jobs with
  the same path and keys are judged as one. Facts are what records match.
- Say `done` only when the answer explains the failure, and never on a guess: a
  conclusive explanation with no record makes a job new without any judge. If the
  logs show the answer but deciding it needs judgement, say `judge`, with the
  question.
- Give `judge` and `unknown` a branch, so that the tree can grow there: a node added
  at that path later, by a person or an analyst, is asked next.
- Put it in its own file of `library/nodes/`, named for its path, such as
  `crash-abort-bluestore-error.py`, with `EXAMPLES`: real jobs it decides, each
  with the path it takes, which every later change must keep. A node file may
  import only `re` and the engine (`qa_triage.tree.core`, `common`, `marks`); a
  helper several nodes share goes in `common.py`.
- Add a test to tests/test_tree.py with the lines of a real job's log that the
  node reads, and the node's file to `TREE` in tests/fakes.py, which loads the
  nodes the tests are written for.
- Run the tree over earlier runs before and after (`qa_triage run <run> --judge
  none`), and compare: no job a record placed may move, and every newly conclusive
  explanation should be right.

## Reviewing a node an analyst changed

An analyst (see
[What the analyst changes in the tree](docs/design.md#what-the-analyst-changes-in-the-tree))
may add a node file, or change any of them, the root's too. The tool checks the change
against every example and every job triaged before, and runs the tests on it, before it
keeps it; `qa_triage verify` checks every example again. Review a node file as you
would any code:

- Does it answer `done` only on the lines that show the cause, and `unknown` for
  every other job that reaches it?
- Is its sentence what a reviewer would write, and its key what tells this failure
  apart?
- Where it mends a node, does the lesson's `analysis.md` show the node was clearly
  wrong for the doubted job, and did the change move only jobs that failed the same
  way?

Changing a node file by hand is fine: run the tests and `qa_triage verify`
afterwards.
