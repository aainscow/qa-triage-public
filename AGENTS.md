# AGENTS.md

For an AI agent asked to use qa_triage for a person, or to work on it. The
[README](README.md) is the person's short version, [docs/guide.md](docs/guide.md) has
every command and setting, and [docs/design.md](docs/design.md) says how the tool works.

## What it is

qa_triage triages the failed and dead jobs of a Ceph teuthology QA run. For each job a
tree of questions in Python (`library/nodes/`) explains the failure from its logs, and
problem records (`library/problems/`, one YAML file per tracker issue or PR) say which
known problem it is. Neither uses AI. Jobs that failed the same way are grouped, and an
optional AI judge is asked about one job per group that the tree and the records could
not decide; an optional AI analyst changes the records and the tree, under the tool's
checks, so that later runs decide such jobs by themselves.

## Running it

From a ceph clone that has `src/script/qa_triage/`, run `src/script/qa_triage/qa_triage.sh`;
from a clone of this repository, `./qa_triage.sh`. Either makes its own Python
environment the first time. `qa_triage` below means either.

- **There is no full screen for you.** Without a terminal, `qa_triage <run>` and
  `qa_triage review <run>` read commands from stdin, as `--plain` does, and stop at
  its end, so pipe in the commands you want:

  ```
  printf 'summary\nshow 2\ngrep 2 "FAILED assert" remote/*/log/ceph-osd.*.log.gz\n' |
      qa_triage review <run> --plain
  ```

  `help` lists the commands, and `show N`, `files N`, `grep N REGEX [glob]` and `prs`
  (the PRs the run tests) are free.
- **Triage in one go:** `qa_triage run <run> -q` prints a summary and writes the report.
- **Several runs:** `qa_triage watch --plain [<run> ...]` triages each, with no AI, and
  prints the failures they have in common, the commonest first; with no runs, it takes
  the `rados` runs of main and of the QA batches for main between `--since` and
  `--until` (by default the last week); with `--prs mine` (or PR numbers), the runs of
  the person's PRs, of any suite, official or their own. Without `--plain`, at a
  terminal, it opens a screen.
  Name the run by its pulpito URL, its name, its branch, or a tracker issue that points
  at it.
- **No AI unless the person asked for it.** Each call of the judge or the analyst costs
  the person money or quota. Pass `--set judge=none`, unless they named a judge, and
  run nothing that asks the analyst (`doubt`, `teach`, `outcomes --learn`, `learn` and
  `judge` in a review, or `run --analyst`) without their say.
- **Settings from the command line:** every command takes `--config <file>` and
  `--set key=value` before its name; `qa_triage config` shows the settings in force.
- **The job files** are read over ssh on the archive's host (`trial200:/a` by default),
  which needs the sepia VPN. If the tool says the host does not answer, tell the
  person to connect the VPN; `--https` downloads the files instead, which is much
  slower. The tool only reads the archive.

## Reading the results

Each run's output is in `~/.cache/qa_triage/runs/<run>/` (or `--out`):

- `results.json`: a list, one object per job, with `run`, `job`, `status`,
  `description`, `reason` (pulpito's failure reason), `verdict`, `problem` (the
  record's id: a tracker number, or `PR` and a number), `sure`, `path` (how it was
  decided), `tree` (the tree's explanation: its path, facts and steps, each with the log
  lines it read), `notes`, `judge` (the judge's answer, if asked), `suspects` and
  `blamed` (PRs under test that may have caused it), and `baseline` (the same failure
  on the base branch).
- `report.md`: the same, for a person, with each cited log line.
- `drafts/`: the QA tracker comment, the RADOS wiki section, and a comment for each
  known problem's tracker. Hand these to the person; do not post them.

The verdicts: `duplicate` (a record's log checks placed it), `probable-duplicate` (the
judge placed it, and every line it quoted was found), `seen-after-fix` (a known
problem whose fix is already in the build), `ambiguous` (several records matched),
`new` (no known problem matched). A judge's `new` means it knew no record that fits,
not that no tracker issue exists.

Every answer cites `file:line` in the job's archive. Check a citation before you rely on
it: `qa_triage files <run>/<job> [glob]` lists the archive, and `grep N` in a review
searches it.

## What you must not do

- **Post nothing.** Do not file or update tracker issues, or comment on PRs (`file`,
  `track` and `post` in a review), or push to the library (`share`), unless the
  person told you to, for that issue, PR or change. `update N` and `draft N WAY` show
  what would be posted, for nothing: hand that to the person.
- **Approve nothing for the person.** The changes the analyst makes wait for a
  person's approval (`qa_triage pending` lists them). Do not run `approve`, nor set
  `approve` or `approve_edits` to `always`, unless the person told you to.
- **Settle nothing as a person.** `qa_triage settle` says what a job is, as a person,
  and every later change is checked against it. If you settle a job for someone, add
  `--unconfirmed` and `--by "<your name> for <them>"`, so that a person confirms it.
- **Print no key.** The tool reads the tracker key and the agents' keys from files
  itself; never print them or copy them into a command line.

## Questioning a result

If a result looks wrong, say why to the person, with the log lines that show it. If
they agree to the cost, `qa_triage doubt <run>/<job> --says <id>|new --why "..." --by
"<your name>"` asks the analyst to find the cause and correct the records or the tree;
its change is checked against every job triaged before, and then waits for approval.

## Working on the tool

- `qa_triage/` is the tool; `qa_triage/tree/` is the tree's engine. `library/` is the
  data everyone shares: `problems/`, `nodes/` (the whole tree, one file per node, from
  `root.py` down), `labels/` (settled jobs), `paths/`, `memory/` and `scripts/`.
- Tests: `python3 -m unittest discover -s tests -t tests` (about a minute, offline).
  Lint the library: `python3 . --archive https --set db=none lint`. Both run in CI and
  must pass.
- A node file may import only `re` and `qa_triage.tree.core`, `common` and `marks`,
  must list the jobs it was written for in `EXAMPLES`, and is sandboxed when loaded;
  see [CONTRIBUTING.md](CONTRIBUTING.md) for records and nodes.
- `qa_triage verify` checks every record and node against its example jobs.
- Write as the code around you does: plain sentences in docstrings and comments, and
  the same names for the same things. The docs name no AI agent or vendor as the
  one to use.
