# The library

What qa_triage has learned about Ceph teuthology failures, shared by everyone who
uses it: `problems/` (a record for each known problem, named after its tracker
issue, or the pull request that fixed it), `nodes/` (the tree of questions, one
Python file for each node, from the root's, `root.py`, down), `labels/` (jobs whose answer someone settled,
which every change is checked against), `paths/` (where the tree took each settled
job and each example, and with which code of the tree, so that a change is checked
against the jobs it could reach without walking them all), `memory/` (what people answered for each kind of
failure) and `scripts/` (scripts people wrote to pull debug data out of a job's
logs).

qa_triage, in the directory above, uses this library by default: see
[The shared library](../docs/design.md#the-shared-library).
