# Choosing isolation

Every child gets its own tmux session, its own log and its own result file.
That is always on. The decision is whether it also gets its own **git
worktree** (`--worktree`).

## The rule

Ask one question: *could anything else write to this repo while the child is
working?*

- **No, and the child only reads** → no worktree. Audits, reviews, questions,
  summaries. A worktree costs a checkout and buys nothing.
- **No, and the child writes** → usually no worktree. A single writer in a repo
  is the normal case and behaves exactly like you doing the work yourself.
- **Yes** → `--worktree`, always. Two agents editing one working tree produces
  corrupted state, and it fails *confusingly*: half-applied edits, a git index
  that disagrees with the files, tests that fail for reasons unrelated to the
  change. This is the single most expensive failure mode in parallel agent
  work, and it is entirely preventable.
- **The change is speculative or risky** → `--worktree`. Discarding a branch is
  free. Untangling a half-finished refactor from a tree you also have work in
  is not.

## What --worktree actually does

Creates `agent-session/<name>` as a branch, checks it out into a worktree under
the session's state directory, and runs the child there. The parent repo's
working tree is untouched.

`agent-session reap` removes the worktree via `git worktree remove --force`.
Reaping a child whose work you want to keep loses it, so merge or cherry-pick
the branch first.

## Collecting work from a worktree child

The child's commits are on `agent-session/<name>`. From the main repo:

```bash
git log agent-session/<name> --oneline     # what it did
git diff main...agent-session/<name>       # the whole change
git cherry-pick <sha>                      # take one commit
git merge agent-session/<name>             # take all of it
```

If the child left uncommitted changes, they are in the worktree directory
(`agent-session list` prints the path). Tell children to commit — a child that
finishes with dirty state makes its work awkward to collect, and the commit
message is useful evidence of what it thought it was doing.

## Non-git directories

`--worktree` requires a git repo and fails loudly otherwise, rather than
silently running unisolated. For a non-git directory, either accept the shared
directory or copy it yourself before spawning.

## What isolation does not protect

The child runs with permissions fully bypassed. A worktree isolates the
*repository*, not the machine: the child can still write anywhere on disk it
has permission to, reach the network, and run any command. Isolation is a
concurrency tool, not a containment boundary. Scope the prompt accordingly.
