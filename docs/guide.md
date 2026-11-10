# Using qa_triage

Every command, setting and key of the tool. [The README](../README.md) is the short version;
[How qa_triage works](design.md) says what the tool does with a job, and why.

## Getting it

Start in a ceph clone. Until ceph/ceph#73070 is merged, take its script from the
pull request first; this leaves your branch as it is, with the script's files
untracked (remove them before you pull a ceph that has them):

```
$ cd ceph
$ git fetch https://github.com/ceph/ceph.git pull/73070/head
$ git restore --source=FETCH_HEAD --worktree -- src/script/qa_triage
```

Then give it a run's pulpito URL:

```
$ src/script/qa_triage/qa_triage.sh https://pulpito.ceph.com/<run>/
```

The first time, it clones this repository into `src/script/qa_triage/qa-triage/`,
which git ignores, and makes its own Python environment; each time after, it brings
that clone up to date before it runs. This repository is private: ask its owner to
add you as a collaborator first. It clones over ssh, with your GitHub key. Where
that fails, as on a host whose ssh key for GitHub is a deploy key, which no other
repository takes, it clones over https with [gh](https://cli.github.com)'s sign-in, if
gh is signed in (`gh auth login`), for its own git commands only;
`QA_TRIAGE_REPO=https://github.com/aainscow/qa-triage.git` clones over https from the
start. It uses the ceph clone it is in for the source checkouts, the fix check and
telling which PRs under test changed what a job ran: run directly from a clone of
this repository instead, it says that the last is off, unless the git setting names a
ceph clone. A link to the script on your PATH, such as
`ln -s $PWD/src/script/qa_triage/qa_triage.sh ~/bin/qa_triage`, runs it from
anywhere as `qa_triage`, as the rest of this guide writes it.

It takes a branch as well, such as a QA batch's `wip-...-testing-...-main`, for its
run on pulpito (it asks which, if there are several); or a tracker issue's URL: a QA
batch's, for the run of the branch its "QA Runs" field names (or, if that has no runs
yet, of the branch before it), or one that links to a run.

It needs Python 3.9 or later, and git. A clone of this repository anywhere else
works as well as the ceph clone's script, through its own `qa_triage.sh`. The first time, it makes its own Python
environment, with PyYAML, in `~/.cache/qa_triage/venv` (`$QA_TRIAGE_CACHE/venv`),
and then runs the code of its checkout; `QA_TRIAGE_PYTHON` names the Python to make
it with. `python3 -m qa_triage` here,
with PyYAML installed, works too, and `pip install -e .` installs it as a
`qa_triage` command.

The review screen opens at once and shows the tree and the records triaging the
run's failed and dead jobs, which costs nothing. Then it offers to ask the judge
about the groups of jobs they could not decide, and asks the analyst only when you
press a key (see
[Working through a run](#working-through-a-run)). Its output, and
every answer an agent gave, is kept in `~/.cache/qa_triage/runs/<run>`. The first
time, a setup page helps you choose the agents and the other settings, and keeps
your keys (see [Setup](#setup)); with no agent chosen, the tree and the records
work alone. The analyst is the judge's agent, reading the evidence files, unless
you choose another. If an agent cannot run here, as when its program is not
installed, it says so and stops.

## Working through a run

`qa_triage <run URL>`, or `qa_triage review <run>`, opens a full screen at once,
with the run's groups of jobs, as the summary gives them, above what the tree and
the records found for the one selected; the analyst and the judge, and what they
have cost so far, are on the line above the keys. The tree and the records triage
the run in the background, with no AI: each job comes into the list as soon as it
is decided, and the line above the keys says how many are done. Then the judge, if
`judge_on_open` is set, and then the analyst, as below: each group an agent works
on turns in the list, and the lower pane says which agent it is, on which group and
job, and what it is thinking: of the judge, what it writes, as it writes it, for an
agent that streams its output, as Bob Shell does. Arrows choose a group, enter shows
everything found for it, tab moves to the lower pane to scroll it, and `?` lists the
keys. The lower pane says first what the group failed on, in a line (its tracker
issue or PR, or a new issue), then why, as the analyst or the judge tells it, step
by step (or that `j` asks the judge for that): the analyst's account, once its answer
passes the checks, even while its change waits for approval, which it says, and when
the review starts again; then, below a line, where its logs
are, what the agents said, and what the tree and the records found. The mouse
starts on (the `mouse` setting), as in Claude Code's full screen: the wheel
scrolls the list, the lower pane or a pager, whichever it is over, a click chooses
a group, and a drag selects text, which is copied to the clipboard as you let go
(with `pbcopy`, `wl-copy` or `xclip`, or else by asking the terminal, OSC 52). The
text stays highlighted: shift and an arrow move its far end, and copy it again, and
a click anywhere, escape or another key leaves it. `m` turns the mouse off, for the
terminal's own selection. Dragging the line under the list gives the list more rows
or fewer, and the review keeps it so. `y` copies the lower pane, or a
whole pager, such as the wiki section. What is being done is said on the line above
the keys, which stay where they are.
Once the run is triaged, the screen offers to ask the judge about every group (`u`
does so later, `j` for one group); with `judge_on_open: true` it asks it at once,
without the question.
The judge works in the background, on several groups at once (`judges_at_once`), so the
screen stays in use: each group it works on turns in the list, the line above the keys
says how many it has done, and its answers come
into the lower pane as it gives them, each starting with a plain explanation of the
failure for the person reviewing the run. It decides the groups the tree and the
records could not, and checks and explains the ones they did, without changing
their result; `!` marks a group whose result it disagrees with. Then the screen
recommends the groups the analyst could learn from: those that needed the judge,
those it disagrees about, and new ones whose cause nobody has looked for; it offers
to ask the analyst about them. The analyst works in the background too, so the list
and the other groups can be looked through meanwhile: the group an agent works on
turns, in the list, the lower pane shows what it is doing while that group is
selected, and its answer once it is given. The keys that ask an
agent (`j`udge, `u`, `l`earn, `d`oubt and `t`alk) are on the bottom line, after
"Costs AI:", apart from the free ones. The judge's are done when pressed, as a
judge's call costs little; the analyst's ask first, saying the most a lesson may
cost (the `learn_budget` setting), and the screen says how long the agent has been
working. One asked for while another works in the background (`t`alk aside, which is
refused) waits for it: its group is marked `…`, its lower pane says what it waits
for, and the line above the keys says what comes next. `x` stops an agent at work,
and what waits for it, or the checks of its answer (escape too, while
the screen waits for the analyst): nothing it was doing is kept, and what it had
cost by then is spent; the status line says it is stopping until it has. While
the run is being triaged as the review opens, `x` stops the triage: the jobs
triaged by then stay in the review and its report, which names those left out, and
the next review triages those again, and the rest from what the cache kept: the
archive's answers over ssh, the files over https (but for big daemon logs). A
question waits for its answer in a box in the middle of the screen, as the approvals
do; a paste of several lines is one answer, and y or n, then enter, answers yes or no. `--plain` gives a prompt to type
the same commands at instead. They are, with a group's number N:

- `summary`: the groups again, and the costs.
- `show N`: what the tree and the records decided about group N's first job, and
  why: each question the tree asked and the lines it read, each record tried and
  whether its checks found their lines, and the verdict. N can also be a job id.
- `files N [glob]` (the `f` key) and `grep N REGEX [glob]`: the files of the job's
  archive, and the lines a regex finds in them (teuthology.log unless a glob says
  otherwise).
- `comment`: the draft comment for the QA tracker; `retriage`: triage again after
  editing a record.
- `prs` (the `p` key): the PRs the run tests: those its QA tracker lists, and those
  git shows merged into its branch, in the ceph clone, each with its title and state
  from GitHub, and the groups whose jobs ran a file it changes. The line under the
  screen's title says how many there are.
- `wiki` (the `w` key): the run's section for the RADOS team's wiki of reviewed
  runs, [MAIN](https://tracker.ceph.com/projects/rados/wiki/MAIN), in its Textile,
  to paste there: the run's QA tracker, its new problems, its known ones, one line
  each with their jobs, and the jobs still to look into.
- `update N` (the `i` key, "update records"): the ways to tell the people who need
  to know about group N's failure, the recommended one first: a comment on the PR
  under test that an analyst found caused it, explaining the failure to its
  developer, step by step, with the log lines that show it; a comment on the
  tracker issue it is, that it was seen again, and why; or, for a new failure, a new
  tracker issue. `draft N pr|tracker|issue [NUMBER]` shows what one says, and `post
  N pr|tracker|issue [NUMBER] [product|test|infra]` posts it; NUMBER, the PR's or the
  issue's, is by default the one `update` names. The `i` key asks from a box, and
  shows what it would post, whole, to post, edit in your editor ($VISUAL or $EDITOR)
  or leave; a way posted already says when, and is not recommended again. Anyone can
  read the tracker and GitHub, so what is posted names no agent, and has no costs or
  local paths.
  A new issue (`issue N` drafts it, `file N [product|test|infra]` files it) goes to
  tracker.ceph.com, in the project the `tracker_project` setting names (default
  `rados`), and a record is kept for it as `track` does. The tracker needs its API
  key: `$REDMINE_API_KEY`, or the file the `tracker_key` setting names, which holds
  the key or is a JSON object with it as `tracker.ceph.com_token`, or
  `~/.redmine_key`. A PR is commented on with `gh`, signed in (`gh auth login`), or
  a GitHub token. The setup page takes both keys, and says how to make them.
  `track N TRACKER` (also in the `i` key's box) keeps a record for an issue filed
  some other way.
- `sightings` (the `I` key): once you are happy with the tracker issues the groups
  are placed in, a comment for each issue the run saw again, apart from any that a
  PR under test was found to cause. The comment lists the issue's jobs as `/a/<run>/<job>`
  paths, and says if the failure was seen again after its fix. `sightings post`
  posts those not posted yet. The `I` key shows them all first, to post or leave. A
  comment posted by `i` or `I` is not posted again.
- `qa [approve|comment]`: approves the run on its QA tracker, as reviewers do:
  a comment, "Rados approved", with a link to the run's wiki section; its status set
  to QA Approved; and the issue assigned back to its author, who built the batch.
  `qa comment` posts the comment alone.
- The `W` key, "done": once you have been through the groups, it takes you through
  the rest in turn, each step asked about first and skipped if there is nothing to
  do. The steps are: the groups still to look into; the changes that wait for
  approval; a tracker issue for each new failure, and a comment on each PR an
  analysis found caused one (each through the `i` key's box); a comment on each
  known issue seen again (`I`); the run's wiki section, copied for you to paste in
  (the tool does not edit the wiki); "Rados approved" on the PRs (`P`); and the run
  approved on its QA tracker. A step that posts asks first. Where it needs a key that is not given, it
  asks once: make one (it shows how, opens the page, takes the key, checks it, and
  keeps it as the setup page does), sign `gh` in, do the steps by hand (each text
  copied, and its page opened), or skip them. At the end it says what was done.
- `approvals` (the `P` key): "Rados approved", with a link to the run's section of
  the RADOS wiki (`.../wiki/MAIN#httpstrackercephcomissues<QA tracker>`, as people
  write it), for each PR the run tests. It covers the PRs merged into the build the
  run tested, if the ceph clone shows them, else those its QA tracker lists. A PR an
  analysis found caused a failure is left out, as is one the tracker lists that is
  not in the build. `approvals post` posts it on each not posted yet; paste the wiki
  section (`wiki`, the `w` key) in first, since the link goes to it. A QA tracker
  lists its PRs in its description, its comments, or both; the tool takes the
  latest list, whichever holds it.

These ask an agent, and cost:

- `judge N`: the judge's verdict on group N.
- `learn N`: a lesson from group N, as a run with an analyst gives: the analyst
  places it, teaches the tree, or, for a new failure, looks for its cause.
- `doubt N [--says ID|new] [-i] WHY` (the `d` key): the analyst's answer to a doubt
  about the job, with `-i` to talk with it before anything is kept. Its response,
  whether it agrees with you or not, waits for you: on the screen, a box shows it as
  soon as it is done (and `d` on the group shows it again); `response N` shows it
  here. `accept N` takes it: its change, if it made one, is checked again and kept,
  and if it answered otherwise than you, the job is settled as it says, by you.
  `dismiss N` drops it. `talk N response` (in the box, "Discuss it") discusses it
  with the analyst in its own program, going on from the doubt's session, with all
  the evidence, told to discuss rather than start a new analysis; when you leave,
  it is asked once more for its final answer, with any change the discussion called
  for (`digest N` does only that), which is checked, and waits for you the same way.
  For an analyst with no program of its own, the discussion is in the review.
- `talk N` (the `t` key): talk with the analyst about the job in the analyst's own
  interactive program, for an agent that has one, on the same terminal; leaving it
  goes back to the review. The analyst starts with the evidence and the same
  instructions a doubt gives it, so it knows how the records and the tree work and
  how to change them, and with what was said about the job before; getting it ready
  costs one turn of its, done once (`ready N` does only that). Ask it for its
  proposal, and it writes it to `proposal.json`; when you leave, the tool checks it
  as it checks a doubt's answer, asking the analyst nothing, and keeps it only if it
  passes (`back N` does only that). If it does not pass, the checks are at the end of
  the discussion's `analysis.md`: talk again and tell it. The program asks you before
  it writes a file or runs a command, as it always does.
- `discuss N MESSAGE` (the `t` key, for an analyst with no program of its own): a
  conversation with the analyst in plain text, in the review, which starts the same
  way. `propose N [NOTE]` (`/propose` in its chat) asks the analyst, in the same
  session, for the change it came to; the tool checks it, and asks the analyst
  again if a check fails; only a change that passes is kept.
- `analyst [NAME]` (the `o` key's box, a menu; `t` asks too): the analyst, or another one:
  one of the config's agents, each kind of agent installed here, or any agent with
  its model, as `--analyst` takes; it is used from then on, and a new discussion is
  started with it.
- `judge all` (the `u` key): the judge's verdict on every group it was not asked
  about, then what the analyst could learn from; `learn all` asks the analyst about
  each of those.

And `settings` (the `o` key's box too) opens the [setup page](#setup) over the review, to change
the settings in your config file. What you save there the review takes up at once:
the judge, the analyst and their keys, the limits on lessons, approval, the lines
shown, the editor, the tracker and the mouse. The job files, the ceph clone and the
library were used to triage the run, so they apply from the next review, as
`judge_on_open` does. Unlike `analyst`, it changes the analyst for every review to come.

In the list, a new problem the tree explains is one its questions explained
conclusively, with no record that nearly fits: matched as new, with no judge, and it
needs a tracker issue filed (`i` files one, or keeps a record for one filed by hand).
Among those for a person to look into, "unmatched" means the judge is needed, or was
not sure, or knew no problem it is: a judge has the records and a tracker search,
not every issue people place jobs in, so its "new" is a reason to look on the
tracker, not a verdict (of 9 RADOS wiki jobs Bob's judge called new, people had
placed all 9 in known issues).

The report, the drafts and results.json are written when the review starts, as each
change to its results is done, and when you quit; results.json makes the run one that later
changes to the records and the tree are checked against. Nothing paid for is thrown
away. What an agent says is kept, with how it got there,
its cost and its session, in `review.json` in the output directory as soon as it is
said, and shown first, newest first, with the group it is about. `judge N` and
`learn N` show the answer they gave before, for nothing, unless `again` is added
(`J` and `L` on the full screen). Starting the review again with the same output
directory starts from all of it: the answers, the discussions, what was spent, and
the judge's verdicts, taken back from those kept by their evidence.

## Watching several runs

`qa_triage watch` triages several runs with the tree and the records, with no AI, and
lists the failures they have in common, the commonest first: those seen in the most
runs, then those with the most jobs. A job a record, the judge's verdict kept from
before, or the memory places in a known problem is counted with that problem; the
others with the jobs the tree found failed the same way. Each failure says whether it
is a known problem, one seen again after its fix, a new one the tree explains (which
needs a tracker filed), or one for a person to look into, and whether the base
branch's runs have it too, so that a failure only QA batches have may be one of their
PRs'. The test infrastructure's failures are counted apart.

```
$ qa_triage watch <run> <run> ...        # runs, branches or tracker issues, as run takes
$ qa_triage watch                        # the runs watched last, or found between the dates
$ qa_triage watch --since 2026-10-20 --until 10-31
```

With no runs given, it watches the runs it watched last, or, the first time, finds the
`--suite`'s (`rados`) runs of the `--base` branch (`main`), and of the QA batches for
it, scheduled between `--since` and `--until` (dates, as 2026-10-29 or 10-29; by
default the `--days`, 7, to today), the newest first, at most `--most` (10): runs of the
whole suite, with at least `--least` (100) jobs, not a developer's few, and all of whose
jobs have begun. The batches are those whose QA tracker was updated since the first
day. The dates are kept with the runs, in `watch/runs.json` in the cache, until they
are changed. A branch or an issue with several runs gives the newest of the suite.

At a terminal, it opens a screen. Down its left, a pane: the summary of the runs, then
each run, newest first, saying how far its triage has got, then how many of its jobs
failed; at its bottom, `F` finds the runs again, between the dates, and `D` changes
them: from a date or a number of days back, until a date or today. Runs that fall
outside them leave the pane, and new ones come into it and are triaged, three at once.
`←` and `→` move between the pane and what is beside it; in the pane, `↑↓` choose,
enter goes to the one chosen, and `x` leaves a run out of the watch; `[` and `]` go to
the run before or after. Beside the pane, the summary lists the failures of every run
together, with the details of the one chosen below: its runs and jobs, and its first
job as the tree and the records found it. `→` on a failure goes to it in its newest
run, which is a whole review, as `qa_triage review` shows it, with all its keys: `j`
and `l` ask the judge and the analyst there. Under the run chosen, the pane shows the
PRs it tests, and `p` in the run lists them with their titles.

The runs share the AI: at most `judges_at_once` of the judge's calls, and
`lessons_at_once` of the analyst's lessons and doubts, for all of them together, so
that opening more runs asks no more of it at once. A group waiting for its turn, or
for the groups before it, is marked `…` in its run's list, where one the agent works
on turns, and the line above the keys says how many wait; in the pane, a run whose
agent works says `AI`, after `…` while all it has waits. Drag the pane's edge to make it wider or
narrower, and the line under any list to give it more rows or fewer: the sizes are
kept, in `layout.json` in the cache.

With no terminal, or `--plain`, it prints the commonest failures instead, `--top`
(10) of them; `watch.md`, in `watch/<when>/` in the cache (or `--out`), has them all,
each with its runs and jobs linked. Each run's own report is written as `run` writes
it, so `qa_triage review <run>` then starts from it.

### Watching the runs of your PRs

```
$ qa_triage watch --prs mine                 # the runs of the PRs you opened
$ qa_triage watch --prs 73070,73100          # the runs of these PRs
```

`--prs` is for that watch alone: `qa_triage watch` without it takes the suite's runs
again, whatever the watch before followed.

With `--prs`, the runs found between the dates are those that test the PRs, of any
suite and of any size, instead of the suite's: `mine` is the PRs you opened, open or
closed since the first day, as GitHub has them for the login `gh` (or
`$GITHUB_TOKEN`) is signed in as. A run tests a PR if:

- its QA batch's QA tracker lists the PR;
- its build is the PR's head, as it is;
- it is one of your own runs, or of a branch with the PR's branch name in it, and its
  build has the PR in it: merged into it, as ptl-tool, `git pull` or GitHub merge a PR;
  with the PR's commits, as GitHub links them; with a commit of the same author and
  title as one of the PR's, as one rebased has; or with a commit that names the PR, as
  `#73254`, `PR 73254`, `PRs 72643 + 72644`, a PR's URL, or the number with a commit
  id after it, as a squash of the PR does, if that commit changes some of the PR's
  files, so that a tracker issue's number, or a PR named for another reason, is not
  taken for one under test. A revert's PRs are not.

Your own runs are those whose user, the name before the date in the run's name, is
one of `--user`'s (by default your GitHub login and your user name here). Their builds
are fetched from ceph-ci into the ceph clone, by their commits alone, as `checkout`
does, and what each tests is kept in `watch/builds.json` in the cache, since a build
does not change; what GitHub said of each PR is kept in `watch/prs.json` until the PR
is updated. A run with jobs still queued waits, and the newest `--most` are taken. The
screen finds the runs of the PRs again with `F`, and keeps `--user` for the next time; under
the run chosen, the pane lists the PRs it tests, and `p` in the run says how each is
known to. Without the screen, the runs of each PR are printed before the failures.

## Triaging a run in one go

To triage a run in one go, with no screen:

```
$ qa_triage run https://pulpito.ceph.com/<run>/ --judge <agent>
21 jobs: 11 duplicate, 10 new
Report: ~/.cache/qa_triage/runs/<run>/report.md
Drafts: ~/.cache/qa_triage/runs/<run>/drafts/
```

Nothing is posted unless you say so: the output is a report and draft comments for a
person to check and post, and the review files a tracker issue only when you ask it
to. Nor is anything learned kept without your say: what the tree and the records
learn goes into the shared library only as [The shared library](design.md#the-shared-library)
says.

`run` prints, for each job, what the tree and the records decided and why: each
question the tree asked and its answer, where it stopped, each record the prefilter
chose and whether its checks found their lines, and the verdict with its reason.
`-q` prints only the summary.

- `--deep` also runs the records' expensive checks, which read big daemon logs.
- `--https` downloads the job files from qa-proxy instead of reading them over ssh
  where they are; `--archive` names another host that has the archive, as
  `host:/directory`, or another server's https URL of the runs. Either may come
  before the run's URL or after it.
- The fix check uses `git merge-base` in the ceph clone you run qa_triage in (else
  the one its checkout is in), or the one `--git` names, when the version numbers
  cannot decide.
- `--qa-tracker <number>` names the run's QA tracker, if looking it up by branch
  fails; `--qa-tracker none` skips it.
- The output of each run is in `runs/<run>` in `$QA_TRIAGE_CACHE` (default
  `~/.cache/qa_triage`), unless `--out` says otherwise; the archive's answers, and
  files downloaded with `--https`, are kept there too.

The judge is asked about four groups at once, in a run and in the review's `judge all`:
`--judges-at-once` (or `judges_at_once`: a number, or `all`) says how many, and 1 asks
about one after the other. A local model may answer one at a time whatever this says.

## Questioning a result

Anyone, a person or an agent, can say that a result is in doubt:

```
$ qa_triage doubt <run>/<job> --says 81136 --why "the reviewer says 81136"
```

The analyst, an AI agent (see [Agents and config](#agents-and-config)), finds out
what made the job fail, decides whether the result was right, and if it was wrong,
changes the records, or adds to the tree, to get it right. It gets the evidence a
judge gets, every record, the tree's code and the nodes it learned, and the list of
files in the job's archive; it can ask the tool to search
any of those files, such as the daemon logs, to copy small ones, and to search
tracker.ceph.com and read issues. It can also read the source the job ran (see
[The source a job ran](#the-source-a-job-ran)). `--says` is optional, and the
analyst can disagree with it. Your `--says` is met only by a change that places the
job in that issue: a record that lists your issue under `also`, as a duplicate, does
not agree with you. Of two open issues for the same cause, the analyst is told to
put the jobs in the older one, and list the newer under `also`. Without `--says`,
the job is settled as the analyst's answer, and its label says so, not that it is
yours. The drafts name a record's `also` issues after its own, as possibly the same
problem, for you to choose among. In a review, the analyst's response waits for you
to accept, dismiss or discuss: see `doubt` in [Working through a
run](#working-through-a-run).

The tool checks the answer, without any AI, before it keeps a change: see
[Checking a change](design.md#checking-a-change).

If the analyst's answer differs from the one `--says` expected, nothing is written,
and a person needs to decide. With `--interactive`, a person sees each answer, the
diff of the records and the results of the checks, and can tell the analyst what is
wrong before accepting it; the analyst continues the same session. `--dry-run`
writes nothing. The evidence, the analyst's answers and the checks' results are in
`runs/<run>/doubt-<job>/` in the cache.

## Learning from a run

With an analyst (`--analyst`, or `analyst` in a config file), a run learns from the groups
of jobs the tree and the records could not decide: see
[Learning, when asked](design.md#learning-when-asked).

`--learn-max` limits how many groups one run learns from (5 by default); the rest
are left for later runs. `--lessons-at-once` (or `lessons_at_once`: a number, or
`all`) lets that many lessons ask their analysts at once, in a run and in the review's
`learn all`, rather than one after the other. Each lesson still checks and keeps its
change in turn, against the library as the lessons before it left it; one whose
records or nodes another lesson changed meanwhile is told so, and makes its change
again on them as they now are. Each line a lesson says then begins with its job.
`--learn-budget` (or `learn_budget`) limits what one lesson
may cost, in the analyst's own unit: once it has cost that much, the analyst is asked
no more, and the lesson is left for a person. A lesson can run away when the answer
it is given conflicts with an answer settled before, such as two reviewers who
gave the same failure different trackers. Learning from one group costs about as much as a few dozen
judge calls, but only once: later runs decide those jobs for nothing.

## Approving what was learned

Every change the analyst makes to the records or the tree passes the checks, and then waits
for your say, unless the `approve` setting says otherwise. On the review's full screen, a
box asks about each change as soon as its lesson is done: what failed, as the analyst
found it; what the change does; and a commit of it, with the command to inspect its code
(`git -C <library> show <commit>`). Keep it, and it is checked again, against the library
as it is now, and pushed to the shared library for everyone; reject it, and it is set
aside. If others changed the library since the change was made, keeping it first makes
it again on top of what they did, keeping both sides' jobs and issues. If it then fails
a check, or both changed the same thing, a box says which, and offers to have the
analyst make it again for the library as it is now (`revise N` at the review's prompt;
costs AI): its revision waits for your say in place of the old. As the review opens, it offers to ask about the changes that already wait, as
from a review before; `A` asks about every change that waits, at any time, even while
an agent works, and the line above the keys says how many do. Outside the review, `qa_triage pending` lists them, with their commits. See
[The shared library](design.md#the-shared-library) for how a change is shared, and the
`approve` and `approve_edits` settings.

## Training on what people decided

`answers`, `score`, `remember`, `teach`, `settle`, `stale` and `outcomes` learn from what
people wrote about failures, on the RADOS wiki and the tracker: see
[Learning from what people decided](design.md#learning-from-what-people-decided).

## The source a job ran

qa_triage checks out the source a job ran from the ceph clone you run it in, or,
outside one, from the clone it lives in (the `git` setting names another): a git
worktree at the commit in the job's config (`sha1`), and another at the qa suite's
commit (`suite_sha1`) if that differs. A commit the clone does not have, such as one
on a wip branch in ceph-ci, is fetched by its id from the repository the config
names, without adding a branch or a tag. Each commit gets one worktree, which every
job that ran it shares, in `~/.cache/qa_triage/worktrees/<sha1>` (the `worktrees`
setting); remove one with `git worktree remove` when you no longer need it.

```
$ qa_triage checkout <run>/<job>
source: ~/.cache/qa_triage/worktrees/7d00fbae1783542d04759ef48371951d26d878f1
```

The analyst gets the worktrees as `source/` (and `suite/`) in its evidence, so that
it can read the code the logs point at, such as a qa task or the function that
asserted. The `git` setting names another clone, or `none` for no checkouts; the
fix check uses the same clone.

## Setup

`qa_triage setup` opens a page of the settings that matter most, each with what it is
for and what it is now, and qa_triage opens it by itself the first time it runs
with no config file; in a review, `o` opens it over the run. Enter changes a setting, from a menu where there are choices,
`c` checks them all, `a` sets up what it can automatically, and `s` keeps them in
your config file, `~/.config/qa_triage/config.yaml`.

`a` looks at what this machine has: whether the archive host answers over ssh, and
has the archive (if not, it says so: connect to the VPN, or choose https), the editors there or
here, a ceph clone, if you run qa_triage in none, and a tracker key. It sets what it
found, says what it found beside each setting, and leaves the rest to you. It
chooses no AI agent: that costs, so the judge stays none until you choose one, and
it says which agents are installed here. The page does this by itself the first
time, before a config file is saved; `qa_triage setup --auto` does it with no page,
and saves. The settings are:

- the judge and the analyst: each kind of agent installed here, any agent from your
  config, or any other you type, with its model; checked by whether it can run here;
- the API keys (see [Keys](#keys)): your tracker.ceph.com key, checked by asking
  the tracker whose key it is, and the key of an agent that needs one; a key you
  paste is kept in a file only you can read, which the config names, and never in
  the config itself;
- where the job files are read from, checked by whether the archive host answers;
- the ceph clone for checkouts and the fix check, and the limits on lessons;
- how many lines of the logs a result shows for each line the judge quotes, and the editor
  that `qa_triage open` opens a log at a line with, and where it runs:
  checked by whether it is there.
- whether a change to the records or the tree is asked about, kept, or held for
  `qa_triage pending` (`approve`); and, on the review's full screen, whether the
  mouse starts on, and whether the judge is asked about every group as it opens.

Any setting of [Config files](#config-files) can be set there by hand as well.

## Output

In `runs/<run>/` in the cache (`~/.cache/qa_triage`), or in `--out`:

- `report.md`: every job's verdict and whether it matched, the path it took through
  the graph, and why: each answer of the tree and each check of the records, with
  the lines of the logs it was decided by under it, whole, with what was found in
  bold, as `file:line` linked to the file in the archive; and the judge's
  explanation, with the lines of the logs that have what it quotes under it.
- `drafts/qa-tracker-comment.md`: a comment for the batch's QA tracker, listing the
  known problems with their jobs and the jobs that need investigating.
- `drafts/wiki.textile`: the run's section for the RADOS wiki of reviewed runs.
- `drafts/sighting-<id>.md`: one comment per known problem, listing the new
  occurrences (`/a/<run>/<job>`) for its tracker.
- `drafts/records/<job>.yaml`: draft problem records for the new failures, placed
  where the tree put them, with regexes made from the facts it found.
- `results.json`: the same verdicts, for other tools.
- `judge/<job>/`: the evidence and prompts each judge was given, and its raw output.
- `learn/<job>/`: the analyst's evidence, answers and `analysis.md`, for each group
  the run learned from.

## Agents and config

The judge and the analyst are AI agents you choose; neither is needed. What each does is in
[The judge and the analyst](design.md#the-judge-and-the-analyst).

### Providers

| Provider | What runs | Spec |
|---|---|---|
| `bob` | IBM Bob Shell 2.0.6 or later, `bob run` (headless) | `bob` |
| `claude` | Claude Code, `claude -p` | `claude`, `claude:haiku` |
| `goose` | [goose](https://github.com/block/goose), `goose run`, with any of its providers: Anthropic, OpenAI, Google, Ollama, OpenRouter and more | `goose:anthropic/claude-sonnet-4-5` |
| `ollama` | a model served by [Ollama](https://ollama.com), such as Gemma, Llama or Qwen | `ollama:gemma4:26b` |
| `openai` | any OpenAI-compatible chat API: OpenAI, vLLM, llama.cpp's server, LM Studio, LiteLLM, OpenRouter | `openai:gpt-5`, `openai:<model>@http://host:8000/v1` |
| `cmd` | any command that reads the prompt on stdin and prints the answer's JSON | `cmd:my-agent --json` |

See [The judge and the analyst](design.md#the-judge-and-the-analyst) for how each reads the
files.

### Config files

Settings are read from `~/.config/qa_triage/config.yaml` (yours) and then
`./qa_triage.yaml` (a project's), or only from the files given with `--config` (or
`$QA_TRIAGE_CONFIG`). Options override them, and `--set key=value` overrides any
setting, so a script or an agent can do everything from the command line. Paths are
relative to the file they are in.

```yaml
judge: [gemma, haiku]     # a free local model first, then a cheap hosted one
analyst: claude           # a strong model, run once per problem; leave it out for none
learn_max: 5
corpus: [~/.cache/qa_triage/runs/*/results.json]
agents:
  gemma:
    provider: ollama
    model: gemma4:26b
    context: 32768        # Ollama cuts prompts longer than its context
  haiku:
    provider: claude
    model: haiku
    budget_usd: 0.5
```

[examples/](../examples/) has a file for each provider, and
[examples/config.yaml](../examples/config.yaml) lists every setting.
`qa_triage config` shows the settings in force, the files they came from, and the
judge and analyst they give, and `--judge` and `--analyst` take an agent's name from
the config or a spec, such as `--judge ollama:gemma4:26b,claude:haiku`.

The report lists the cost each agent reported, in its own unit: Bob Shell's own
unit, dollars for Claude Code, and dollars for an `openai` agent given a `price`.

### Keys

qa_triage needs no key to triage a run: pulpito and qa-proxy are public, the archive
host is reached with your own ssh access, and the tracker and GitHub can be read
anonymously. Keys let it read more, as you, and each AI agent needs its own:

| Key | What it gives | Where qa_triage finds it |
|---|---|---|
| tracker.ceph.com API key (on your account page, "API access key") | the tracker read as you: every issue and search result your account may see, the RADOS wiki of reviewed runs (`answers`), and filing issues (`file`, `track`) | `$REDMINE_API_KEY`; else the file the `tracker_key` setting names, which holds the key, or a JSON object with it as `tracker.ceph.com_token`; else `~/.redmine_key` |
| GitHub | pull requests, read for what fixed a problem and the PRs a run tests (`outcomes`, lessons, doubts, `watch --prs`), and commented on when you ask (`i`); without a sign-in GitHub allows 60 requests an hour | `gh`, if it is installed and signed in (`gh auth login`); else `$GITHUB_TOKEN`; else the file the `github_key` setting names, which holds the token, or a JSON object with it as `github_token` |
| ssh access to the archive host | the job files, read where they are (the default, `trial200`, needs the Sepia VPN) | your ssh config and agent; `--archive https` needs none |
| push access to the library | `share`: pushing what was learned, and proposing it as a pull request | your git credentials for the `db` repository, and `gh` for the pull request |

The agents' keys, in alphabetical order; this is not a recommendation of one over
another:

| Agent | Key | Where it is read |
|---|---|---|
| Bob Shell (IBM Bob) | an IBM Bob API key, even when you are signed in, because qa_triage runs it headless | `$BOB_API_KEY`, or the file the agent's `key_file` setting names (the setup page keeps a pasted key in such a file) |
| Claude Code | a signed-in `claude`, or an Anthropic API key | Claude Code's own sign-in, or `$ANTHROPIC_API_KEY` |
| goose | the key of the provider it uses | goose's own configuration (`goose configure`) |
| Ollama | none: the model runs here, or on the host the agent's `url` names | |
| OpenAI-compatible API | the service's key, if it needs one | the variable the agent's `api_key_env` names (`OPENAI_API_KEY` by default); none for a local server |
| `cmd` | whatever the command needs | the command's own environment |

The setup page takes both: choose the Tracker key or GitHub token row. Each says how
to make the smallest key that does, and opens the page that makes it. The tracker has
one kind of key, with all your account's rights. For GitHub, `gh` signed in needs no
token. Otherwise, to read only, use a fine-grained token with access to public
repositories and no permissions; to comment on PRs too, a classic token with the
`public_repo` scope alone, or, if the ceph organization allows fine-grained tokens, one
for ceph/ceph with Pull requests: read and write. The page checks a key as soon as it
is given: whose it is, and, for GitHub, whether it can comment on PRs.

No config file holds a key, only the name of a file that does, which the setup page
makes readable by you alone. qa_triage reads the tracker key to send it to the
tracker, and an agent's `key_file` to put the key in that agent's environment; it
reads no other credential, and sends each key only to the service it is for.

## Commands

```
qa_triage watch [<run> ...] [--days N] [--top N]   # the commonest failures of runs
qa_triage watch --prs mine|<PR>,<PR> [--user <names>]   # ... of the runs of PRs
qa_triage run <run name or pulpito URL> [--judge ...] [--analyst ...]
                         [--jobs 631086,631204] [--known <file>] [--check [blind]]
qa_triage review <run> [--judge ...] [--analyst ...]   # work through it, full screen
qa_triage doubt <run>/<job> [--says <id>|new] [--why ...] [--interactive]
qa_triage settle <run>/<job> <id>|new --why ...  # say what a job is, as a person,
                                      # where the records, or the analyst's change kept
                                      # when it disputed the people, place it: scores
                                      # take it over the wiki's and the tracker's answers
qa_triage open <run>/<job> [<file>[:<line>]]   # open a job's log at a line a
                                      # result cites, in your editor: to check it
qa_triage files <run>/<job> [<glob>]  # the files of a job's archive
qa_triage checkout <run>/<job>        # check out the source the job ran
qa_triage verify [<problem id> ...]   # check records and nodes against
                                                # their examples, and the settled jobs
qa_triage lint                        # validate the records
qa_triage answers [--tracker]         # people's answers, from the RADOS wiki or the
                                      # tracker's issues, for run --known
qa_triage score --answers <dir>       # how often the runs triaged agree with them
qa_triage remember --answers <dir>    # the library's memory of what people answered
qa_triage paths                       # keep where the tree takes each job of the library
qa_triage teach --answers <dir>       # teach their commonest mistakes, one lesson each
qa_triage outcomes [--learn]          # learn from what people wrote on the trackers
qa_triage share                       # propose what was learned to the library
qa_triage pending                     # the changes that wait for your approval
qa_triage stale [--days N]            # the records that may have had their day
qa_triage setup [--auto]              # choose the settings and keys; --auto sets up
                                      # what it can from what this machine has
qa_triage config                      # show the settings in force
python3 -m unittest discover -s tests -t tests  # offline tests
```

Every command takes `--config <file>` and `--set <key>=<value>` before its name,
e.g. `qa_triage --config examples/claude.yaml run <run>`.
