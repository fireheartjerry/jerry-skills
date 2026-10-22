# Choosing isolation

Every child gets its own backend session (a tmux session, or a detached
process group with the process backend), its own log and its own result file.
That is always on. The decision is whether it also gets its own git worktree
(`--worktree`).

## The rule

Ask one question: will anything else write to this repo while the child is
working? Count yourself: if you or the user will edit it meanwhile (you were
going to "do other work"), the answer is yes.

- No, and the child only reads: no worktree, and `--role review` or
  `--role research` so it cannot write. Audits, reviews, questions, summaries.
  A worktree costs a checkout and buys nothing.
- No, and the child writes, and you will wait for it without touching the
  repo: no worktree is needed. The child then works on the shared tree like
  you would; `judge` leaves out the paths that were already uncommitted when
  it started.
- Yes, or several children write in parallel: `--worktree`, always. Two agents
  editing one working tree produces
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

The worktree is the base commit only. Staged and unstaged changes in the
parent tree are not in it, and `spawn` warns when the parent tree has any.
If the child must see your current changes, commit them first, or pass
`--base "$(git stash create)"`: that starts the branch from a commit holding
your tracked changes without touching your tree or the stash list (untracked
files are not in it; `--copy` them), and the branch then has that stash
commit as an ancestor, so collect it with `--cherry-pick` or `--branch`. A
child asked to "run the tests against my changes" in a worktree will test the
last commit and report green for code it never saw; say which option you took.
Before a `--verify` on a worktree child, run the verify command once at the
base yourself: a failure there is not the child's.

Untracked and ignored files are not in it either. `node_modules`, `.venv`,
`.env`, build outputs and generated fixtures all live only in the parent tree,
so a worktree child told to "run the tests" starts from a bare checkout and
either fails or tests something else; `spawn` names the ignored entries the
worktree will lack (`absent from the worktree: .env, node_modules`). A verify
failure there is often a missing environment, not a wrong change: read
`verify.<k>.out` before spending a fixup turn. Two remedies: `--copy .env,node_modules`
copies those paths from the repository root into the new worktree before the
child starts (files with their metadata, directories with symlinks kept; a
missing path is a warning, not an error), or the prompt says how to install
dependencies (`npm ci`, `uv sync`, `make deps`). Copy secrets only when the
child needs them: the copy lives under the state directory until `reap`. The
worktree is a full checkout under the state
directory (`~/.local/state/agent-sessions` by default), which matters for large
repositories.

## Repositories with hooks, signing, submodules or LFS

A worktree child is told to commit, and four repository features commonly
break that in a fresh worktree. `spawn --worktree` warns about each one it
finds, with the remedy:

- Commit hooks (`.git/hooks/*`, `core.hooksPath`, `.husky`,
  `.pre-commit-config.yaml`, `lefthook.yml`). Hooks live in the common git
  directory, so the worktree runs the parent's hooks, but without the parent's
  `node_modules` or virtualenv. The commit fails, and the child either
  bypasses the hook with `--no-verify` or leaves the work uncommitted, which
  `collect` refuses. Install the dependencies in the prompt or `--copy` them,
  and say in the prompt: "if a commit hook fails, fix the cause or report it;
  never use --no-verify" (the implementation template in
  `references/prompts.md` says so). Allow `--no-verify` only when the user
  agreed.
- `commit.gpgsign=true`. Signing can hang or fail in a detached session with
  no agent or pinentry. Say whether the child may commit with `git -c
  commit.gpgsign=false commit`; unsigned commits may not be acceptable on the
  user's branch, so ask when it matters.
- Submodules (`.gitmodules`). `git worktree add` does not initialise them: put
  `git submodule update --init` in the prompt before any build or `--verify`.
- Git LFS (`filter=lfs` in `.gitattributes`). Files arrive as pointers unless
  git-lfs is installed and set up for the repository: put `git lfs pull` in the
  prompt, or expect tests that read those files to fail.

When the child still leaves the worktree dirty, the result carries a warning
naming the uncommitted paths, and the hooks or signing when the repository has
them: read the child's log for the hook output, fix the cause, and resume it
with a message to commit.

## Absolute paths and the parent tree

A worktree child works in its own directory, but a bypass-permissions child
obeys absolute paths: a prompt that says "edit /path/to/app/src/auth.ts" sends
it into your checkout, around the isolation. Name paths relative to the child's
directory, and say "never edit files by absolute path outside your working
directory" (the template does). At the end of the run the runner compares the
parent tree's dirty paths with those at the child's start and warns when they
changed (`the parent tree ... changed during the run: <paths>`): that is you,
another writer, or the child writing by absolute path. Check which before
collecting.

## Replacing and retrying

A left-over branch from an earlier child blocks a new child with the same
name. The error says so and names the fix: `agent-session reap <name>` if the
old state still exists, otherwise `git -C <root> branch -D agent-session/<name>`.
`spawn --worktree --replace` reaps the old child first, and refuses before
removing anything when the old worktree is dirty or its branch has commits
that are not merged, so no commit is ever deleted implicitly. To retry a
failed or stalled child, either give its work up (`reap <name> --discard
<tip>`, then spawn again), or keep its committed work and continue under a
new name: `spawn <engine> <new-name> <dir> <prompt> --worktree --base
agent-session/<name>` (uncommitted files in the old worktree are not carried;
commit them there first). `collect --merge` of a failed child would put
half-done work on the user's branch, which is why `collect` refuses it
without `--allow-failed`.

## Collecting work from a worktree child

The child's commits are on `agent-session/<name>`:

```bash
agent-session diff <name> --stat            # what it changed, uncommitted included
agent-session conflicts                     # shared files, parent commits since the spawn, simulated merges
agent-session collect <name> --merge        # or --squash, --cherry-pick <sha>, --branch
agent-session reap <name>                   # deletes the merged branch with git branch -d
```

`collect` prints the branch it merges into first, and refuses a child that
did not finish `ok` with verify `pass` (unless `--allow-failed`), a dirty
worktree, a branch with nothing beyond its base, and (for `--merge`,
`--squash` and `--cherry-pick`) a parent tree with uncommitted changes, which
are the user's to commit or stash, never yours: use `--branch` and let the
user merge. A merge that stops on conflicts is aborted
(`references/review.md`). Plain git works as well: `git log
agent-session/<name>`, `git merge`, `git cherry-pick`.

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

## Backends

The process backend changes none of this: it replaces the tmux session with a
detached process group, and the worktree, snapshot and reap rules are the same.

## What isolation does not protect

An implementation child runs with permissions bypassed. A worktree isolates
the repository, not the machine: the child can still write anywhere on disk it
has permission to, reach the network, and run any command. Isolation is a
concurrency tool, not a containment boundary. A worktree does limit what
reaches the user's branch (nothing does until `collect`), so the diff can be
reviewed first; it does nothing about commands, network access or writes
elsewhere.

MCP servers are off by default (`--mcp none`) for claude and for the codex
servers declared in codex's config, so such a child cannot act through the
user's connectors (mail, drives, databases, payments). Two exceptions: codex
servers that a plugin provides cannot be switched off per run, and gemini has
no per-run switch at all, so `--mcp none` is not enforced there. `spawn` names
both in a warning and on its `access :` line; read that line before spawning a
write child you will not watch. `--mcp inherit` loads every server for a write
child, unattended and with approvals off: pass it only when the task needs a
named connector and the user agreed.

`--role review` and `--role research` (or `--readonly`) narrow the engine's
own tools (no shell, no edits) on a best-effort basis; they are for reviews,
not for running untrusted prompts. A read-only child also gets only its own
engine's environment variables. There is no container sandbox. Scope the
prompt accordingly: name the paths it may change and forbid push, merge, pull
requests and messages (`references/prompts.md`).
