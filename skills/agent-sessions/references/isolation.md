# Choosing isolation

Every child gets its own tmux session, its own log and its own result file.
That is always on. The decision is whether it also gets its own git worktree
(`--worktree`).

## The rule

Ask one question: could anything else write to this repo while the child is
working?

- No, and the child only reads: no worktree. Audits, reviews, questions,
  summaries. A worktree costs a checkout and buys nothing.
- No, and the child writes: usually no worktree. A single writer in a repo is
  the normal case and behaves exactly like you doing the work yourself.
- Yes: `--worktree`, always. Two agents editing one working tree produces
  corrupted state, and it fails confusingly: half-applied edits, a git index
  that disagrees with the files, tests that fail for reasons unrelated to the
  change. This is the most expensive failure mode in parallel agent work, and
  it is entirely preventable.
- The change is speculative or risky: `--worktree`. Discarding a branch is
  free. Untangling a half-finished refactor from a tree you also have work in
  is not.

## What --worktree actually does

`spawn` creates branch `agent-session/<name>` at the current HEAD of the repo
containing `<dir>`, checks it out as a worktree under the child's state
directory, and starts the child in the same relative subdirectory you named
(so `spawn ... ~/code/app/packages/api --worktree` runs in
`<state>/worktree/packages/api`). The parent's working tree is untouched.

The worktree is HEAD only. Staged and unstaged changes in the parent tree are
not in it. If the child must see your current changes, commit them first, or
run the child without `--worktree` when nothing else is writing. A child asked
to "run the tests against my changes" in a worktree will test the last commit
and report green for code it never saw; say which option you took.

Untracked and ignored files are not in it either. `node_modules`, `.venv`,
`.env`, build outputs and generated fixtures all live only in the parent tree,
so a worktree child told to "run the tests" starts from a bare checkout and
either fails or tests something else. Put the remedy in the prompt: how to
install dependencies (`npm ci`, `uv sync`, `make deps`), and, only when the
child genuinely needs them, the absolute path of the file to copy (`cp
/path/to/app/.env .`). The worktree is a full checkout under the state
directory (`~/.local/state/agent-sessions` by default), which matters for large
repositories.

A left-over branch from an earlier child blocks a new child with the same
name. The error says so and names the fix: `agent-session reap <name>` if the
old state still exists, otherwise `git -C <root> branch -D agent-session/<name>`.
`spawn --worktree --replace` reaps the old child first; if its branch has
commits that are not merged, the branch is kept and spawn stops with the
`git branch -D` command, so no commit is ever deleted implicitly.

## Collecting work from a worktree child

The child's commits are on `agent-session/<name>`. From the main repo:

```bash
git log agent-session/<name> --oneline     # what it did
git diff main...agent-session/<name>       # the whole change
git cherry-pick <sha>                      # take one commit
git merge agent-session/<name>             # take all of it
```

Tell children to commit, with a message that says what they did. A child that
finishes with dirty state makes its work awkward to collect; uncommitted
changes sit in the worktree directory (`agent-session result <name>` prints
the path).

## What reap does to a worktree

`agent-session reap` removes the worktree and then runs `git branch -d` on the
branch. That means:

- a worktree with uncommitted changes is refused: reap prints
  `skipped <name>: worktree has uncommitted changes at <path>` and leaves
  everything in place. Commit, or `reap --force` to discard those changes;
- commits survive on the branch. The branch is deleted only when it is merged
  into the current branch; otherwise reap prints `kept branch ...` with the
  `git branch -D` command to delete it deliberately;
- `--force` discards uncommitted changes and never deletes commits. It does
  not upgrade `branch -d` to `-D`.

## Non-git directories

`--worktree` requires a git repo and fails loudly otherwise, rather than
silently running unisolated. For a non-git directory, either accept the shared
directory or copy it yourself before spawning.

## What isolation does not protect

The child runs with permissions bypassed. A worktree isolates the repository,
not the machine: the child can still write anywhere on disk it has permission
to, reach the network, and run any command. Isolation is a concurrency tool,
not a containment boundary. `--readonly` narrows the engine's own tools (no
shell, no edits) on a best-effort basis; it is for reviews, not for running
untrusted prompts. Scope the prompt accordingly.
