---
name: agent-sessions
description: >
  Run Claude and Codex as isolated child agent sessions in tmux, so one agent
  can delegate work to others and collect the results. Use this skill whenever
  work would benefit from a second agent: running several independent jobs in
  parallel, getting a second opinion or an adversarial review, delegating a
  long-running task so the parent stays responsive, asking Codex to check
  Claude's work (or the reverse), or holding a back-and-forth with another
  agent. Trigger on "spawn an agent", "run these in parallel", "get codex to
  look at this", "second opinion", "have another agent review", "delegate
  this", "run it in the background", "child session", "subagent", and on any
  request to fan work out across multiple agents. Also use it to check on,
  steer, or clean up children already running.
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 1.0.0
  license: MIT
  requires: tmux, claude CLI, codex CLI, git (only for --worktree)
---

# Agent Sessions

Delegate work to other agents, each in its own tmux session, without losing
track of what they are doing.

The naive version of this is `claude --dangerously-skip-permissions "..."` as a
bare subprocess. That works right up until you want two of them at once, or you
need to know whether one finished, or one corrupts a working tree the other is
editing, or a runaway spawns more of itself. This skill is the same idea with
those failure modes engineered out.

Use `scripts/agent-session`. Add it to PATH or call it by absolute path.

## When delegation is worth it

Spawning a child costs real tokens and real wall-clock. It pays off when:

- **Work is genuinely parallel.** Three independent audits of three
  subsystems finish in the time of the slowest, not the sum.
- **You want an independent judgement.** A child that has never seen your
  reasoning is a far better reviewer than you reviewing yourself. Cross-engine
  is stronger still: Codex has not read your context window.
- **The task is long and you want to stay responsive.** Detach, keep working,
  collect later.
- **The work is risky to your own tree.** A child in its own git worktree can
  fail without touching what you have.

It is not worth it for something you could do in two tool calls. A child pays
a fixed startup cost and cannot see your context, so anything you delegate has
to be described from scratch.

## Pick a model tier deliberately

**Default to `--tier cheap`.** The single biggest waste in multi-agent work is
running a frontier model on mechanical tasks. A child that greps files, applies
a well-specified edit, or runs a test suite does not think better with a bigger
model — it just costs more and finishes later.

| Tier | Claude | Use for |
|---|---|---|
| `cheap` (default) | haiku | Mechanical, well-specified work: search sweeps, format conversions, running tests, applying a described edit, summarising a file. |
| `standard` | sonnet | Ordinary implementation and review where judgement matters but the problem is not deep. |
| `deep` | opus | Genuinely hard reasoning: subtle debugging, architecture, adversarial review where being wrong is expensive. |

Codex uses its configured default unless you pass `--model`.

Escalate when a cheap child fails or returns something shallow — that is
evidence, and it costs less than starting at the top every time.

## One-shot children (the common case)

Detached by default, so you can run several and collect them later.

```bash
agent-session spawn claude audit-auth ~/code/app \
  "Read src/auth/. List every place a token is validated. Output a bullet list of file:line and what it checks. Do not change files." \
  --tier cheap

agent-session list                 # what is running
agent-session wait audit-auth 600  # block until done, prints the result
agent-session result audit-auth    # structured result: status, exit code, tail
agent-session log audit-auth 200   # full transcript if you need it
```

The result file is the handoff. It carries status, exit code and the tail of
the transcript, so you never have to parse scrollback to find out what
happened. Read the full log only when the result is not enough.

**Write the prompt as if to a stranger**, because that is what a child is. It
has none of your context: no file paths, no prior decisions, no idea what
"the refactor" refers to. State the goal, the constraints, where to look, and
what output you want back.

## Steerable children (a real conversation)

When you need back-and-forth, spawn with `--steerable` and use `send`. Each
turn resumes the same conversation, so the child remembers.

```bash
agent-session spawn claude design ~/code/app \
  "You are reviewing a caching design. Read src/cache/. Reply READY when done." \
  --tier standard --steerable

agent-session send design "What breaks if two writers race on the same key?"
agent-session send design "Now write that up as a patch to src/cache/lock.ts"
agent-session stop design          # ends after the current turn
```

`send` waits for the reply by default and prints it, because you almost always
need the answer before deciding what to say next. Use `--no-wait` for a nudge
you do not need to hear back from.

This is driven headlessly, turn by turn — not a terminal UI being typed into.
That matters: it means every turn is logged, individually inspectable in
`turns/N.json`, and nothing depends on a TUI rendering correctly in a detached
pane.

## Cross-engine dialogue

Two engines arguing productively is one of the best uses of this skill, because
Codex genuinely has not seen your reasoning and will not politely agree with it.

**You arbitrate.** Spawn both as steerable children, relay between them, and
decide when it is over. Do not let them run unbounded — cap it at 3 or 4
exchanges. Two agents will happily converge on agreeing with each other, or
oscillate, long past the point of new information.

Full protocol in `references/dialogue.md`. Read it before running a debate.

## Isolation: choose per spawn

Default is to run in the directory you name. Add `--worktree` and the child
gets its own git worktree on its own branch instead.

Decide by what the child will do:

- **Read-only work** (audits, reviews, questions) — no worktree. It cannot
  collide with anything, and a worktree is just cost.
- **Writing, when it is the only writer** — no worktree is usually fine.
- **Writing, when anything else might touch the same repo** — use
  `--worktree`. Two agents editing one tree is how you get a corrupted working
  copy, and it fails confusingly rather than loudly.
- **Speculative or risky changes** — use `--worktree`. Throwing away a branch
  is free; untangling a half-applied refactor from your real tree is not.

`agent-session reap` removes finished state and any worktrees it created.

More detail in `references/isolation.md`.

## Limits, and why they exist

- **3 concurrent working children** (`AGENT_SESSIONS_MAX`). This box has 8
  cores and already runs a production browser stack. Finished-but-lingering
  children do not count against it.
- **Nesting stops at depth 2.** A child may spawn a grandchild; a grandchild
  may not spawn anything. Without a ceiling, one confused agent fans out
  exponentially and the first symptom is the box falling over.
- **Children linger ~15 minutes after finishing** (`AGENT_SESSIONS_LINGER`),
  so you can attach and read a failure, then clean themselves up.
- **Steerable children exit after an idle hour** (`AGENT_SESSIONS_IDLE`).

Children run with permissions fully bypassed — `--dangerously-skip-permissions`
for Claude, `--dangerously-bypass-approvals-and-sandbox` for Codex. That is
deliberate and is what makes unattended delegation work. It also means a child
can do anything you can: **be specific about scope in the prompt**, because
there is no second line of defence.

## Adopting orphans

State lives on disk, so a session that outlives the conversation that started
it is not lost. `agent-session list` shows children from any earlier session —
`orphaned` means the tmux session is gone but the state remains. You can read
their results and reap them exactly as if you had spawned them.

## Recovering from a stuck child

If a child produced nothing, work through it in this order — each rules out a
real failure mode seen in practice:

1. `agent-session list` — is it `running`, `orphaned`, or already finished?
2. `agent-session log <name>` — did the engine start at all? A missing binary
   or a bad model name shows up here.
3. `agent-session peek <name>` — live pane, for a child still running.
4. Check the prompt. The commonest cause of an empty result is a child that
   did not understand a task written with the parent's context assumed.
5. `agent-session kill <name>` then respawn with a clearer prompt, or a higher
   tier if the work was genuinely too hard for a cheap model.

## Command reference

| Command | Does |
|---|---|
| `spawn <engine> <name> <dir> <prompt> [opts]` | Start a child. `--tier`, `--model`, `--worktree`, `--steerable`, `--timeout` |
| `list` | All known children and their state |
| `wait <name> [s]` | Block until finished, print result |
| `result <name>` | Structured result |
| `log <name> [n]` | Transcript tail |
| `peek <name> [n]` | Live tmux pane |
| `send <name> <text>` | Next turn of a steerable child (`--no-wait`, `--timeout`) |
| `stop <name>` | End a steerable child after its current turn |
| `kill <name>` | Kill now |
| `reap [--force]` | Remove finished state and worktrees |
