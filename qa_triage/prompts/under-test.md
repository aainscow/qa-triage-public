The PRs under test. A run tests a batch of PRs, merged into its branch. When
under-test.txt lists them, decide whether one of them made this job fail before you
decide it is a known problem: is the test, task or code that failed, as the logs and
any backtrace show, in a file one of them changes, and does its diff explain the
failure? The tool's own guesses are in doubt.yaml's notes, as "<PR>, under test,
changes <file>, which this job ran": they say only that the PR touches a file the job
ran. A failure that the base branch's runs show too, as the notes may say, is less
likely to be a PR's.
- If one of them caused it, give it in "pr" as under-test.txt names it, such as
  "PR #12345", and set "problem" to null: the failure is new, the PR's, for its
  author to fix before it merges. Quote in "evidence" the line of its diff, from
  under-test.txt or source/, and the log line it explains. Write no record for it,
  which would claim other runs' jobs once the PR has changed or gone; a node that
  explains the failure is still welcome.
- If none did, set "pr" to null, and if the notes suspected one, say in the
  explanation why it is not at fault.

