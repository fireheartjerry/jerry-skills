# Choosing isolation

Every child gets its own backend session (a tmux session, or a detached
process group with the process backend), its own log and its own result file.
That is always on. The decision is whether it also gets its own git worktree
(`--worktree`).

## The rule

Ask one question: could anything else write to this repo while the child is
working?

- No, and the child only reads: no worktree, and `--role review` or
  `--role research` so it cannot write. Audits, reviews, questions, summaries.
  A worktree costs a checkout and buys nothing.
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
containing `<dir>` (or at `--base REF`), checks it out as a worktree under the child's state
directory, and starts the child in the same relative subdirectory you named
(so `spawn ... ~/code/app/packages/api --worktree` runs in
`<state>/worktree/packages/api`). The parent's working tree is untouched.

`--base REF` starts the branch from another commit or branch. It is resolved
when the child starts, so a queued child can stack on an upstream's finished
work: `--after impl --base agent-session/impl`. `worktree.base_commit` in the
result records the resolved commit, and `diff` and `judge` compare against it.

The worktree is the base commit only. Staged and unstaged changes in the parent tree are
not in it. If the child must see your current changes, commit them first, or
run the child without `--worktree` when nothing else is writing. A child asked
to "run the tests against my changes" in a worktree will test the last commit
and report green for code it never saw; say which option you took.

Untracked and ignored files are not in it either. `node_modules`, `.venv`,
`.env`, build outputs and generated fixtures all live only in the parent tree,
so a worktree child told to "run the tests" starts from a bare checkout and
either fails or tests something else. Two remedies: `--copy .env,node_modules`
copies those paths from the repository root into the new worktree before the
child starts (files with their metadata, directories with symlinks kept; a
missing path is a warning, not an error), or the prompt says how to install
dependencies (`npm ci`, `uv sync`, `make deps`). Copy secrets only when the
child needs them: the copy lives under the state directory until `reap`. The worktree is a full checkout under the state
directory (`~/.local/state/agent-sessions` by default), which matters for large
repositories.

A left-over branch from an earlier child blocks a new child with the same
name. The error says so and names the fix: `agent-session reap <name>` if the
old state still exists, otherwise `git -C <root> branch -D agent-session/<name>`.
`spawn --worktree --replace` reaps the old child first, and refuses before
removing anything when the old worktree is dirty or its branch has commits
that are not merged; the error names `collect <name> --merge` and
`reap <name> --discard <tip>`, so no commit is ever deleted implicitly.

## Collecting work from a worktree child

The child's commits are on `agent-session/<name>`:

```bash
agent-session diff <name> --stat            # what it changed, uncommitted included
agent-session conflicts                     # shared files, parent commits since the spawn, simulated merges
agent-session collect <name> --merge        # or --squash, --cherry-pick <sha>, --branch
agent-session reap <name>                   # deletes the merged branch with git branch -d
```

`collect` refuses a dirty worktree, a branch with nothing beyond its base, and
(for `--merge` and `--squash`) a dirty parent tree; a merge that stops on
conflicts is aborted. Plain git works as well: `git log agent-session/<name>`,
`git merge`, `git cherry-pick`.

Tell children to commit, with a message that says what they did. A child that
finishes with dirty state makes its work awkward to collect; uncommitted
changes sit in the worktree directory (`agent-session result <name>` prints
the path).

## What reap does to a worktree

`agent-session reap` removes the worktree and then runs `git branch -d` on the
branch. That means:

- a worktree with uncommitted changes is refused: reap prints
  `skipped <name>: worktree has uncommitted changes at <path>` and leaves
  everything in place. Commit, or `reap --force`;
- `reap --force` first writes a snapshot of the whole worktree (tracked,
  modified and untracked non-ignored files) as a commit at
  `refs/agent-sessions/<name>/snapshot-<timestamp>`, prints the ref and the
  restore command, and only then removes the worktree;
- commits survive on the branch. The branch is deleted only when it is merged
  into the current branch; otherwise reap prints `kept branch ...`. `--force`
  never upgrades `branch -d` to `-D`;
- `reap <name> --discard <sha>` deletes an unmerged branch, after a snapshot,
  only when `<sha>` is that branch's tip, so giving up commits takes a
  deliberate, specific act. Any other sha exits 2 and names the tip.

Snapshots are plain git refs. `agent-session snapshots` lists them,
`git worktree add <dir> <ref>` or `git checkout <ref> -- <path>` restores one,
and `snapshots --prune --older-than 30d` deletes old ones. Age counts from
when the snapshot was taken (recorded in `snapshots.jsonl` and in the ref
name), not from the commit date, so a `--discard` snapshot of an old branch tip
is not pruned early.

## Non-git directories

`--worktree` requires a git repo and fails loudly otherwise, rather than
silently running unisolated. For a non-git directory, either accept the shared
directory or copy it yourself before spawning.

## What isolation does not protect

The process backend changes none of this: it replaces the tmux session with a
detached process group, and the worktree, snapshot and reap rules are the same.

An implementation child runs with permissions bypassed. A worktree isolates the repository,
not the machine: the child can still write anywhere on disk it has permission
to, reach the network, and run any command. Isolation is a concurrency tool,
not a containment boundary. `--role review` and `--role research` (or
`--readonly`) narrow the engine's own tools (no shell, no edits) on a
best-effort basis; they are for reviews, not for running untrusted prompts.
There is no container sandbox. Scope the prompt accordingly.
