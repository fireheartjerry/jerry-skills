# Reliability, budgets and scheduling

How a child is watched, classified when it fails, retried, capped and resumed.
Option syntax is in `references/commands.md`; the record fields are in
`references/schema.md`.

## Reliability: watchdog, failure classes, retries

Watchdog: while a turn runs, no stdout line from the engine for `--stall`
seconds writes a `stall` event and stops the engine with the ladder below; the
turn is `error`, `failure_class stall`, reason `no engine output for Ss`. A child
running a silent long command (a 30-minute build with no output) needs `--stall
0` or a larger value; claude emits the `tool_use` and then nothing until the tool
returns, codex emits `item.started` first.

Ladder: the stall watchdog, `--timeout` (per turn), `--deadline` (whole child)
and the codex/gemini `--max-turns` tool-call limit send SIGINT to the engine's
process group (claude ends the turn and saves the session), then SIGTERM after
15 s, then SIGKILL after another 15 s. `kill` sends SIGTERM, then SIGKILL after
`--grace`. A turn is over when the engine's terminal event arrived (claude
`result`, codex `turn.completed`, gemini `result`) and nothing since says more
is coming. claude -p can report a result and keep going: with background tasks
running it reports a result, waits for them (`task` events), starts a new
segment (`system init`) and reports another result; on `--resume` it can lead
with an empty placeholder result (`num_turns 0`, empty text), which is
ignored. So for claude the turn ends at a result with no background task
pending and no new segment after it; the answer, cost and usage come from that
last result (claude's totals are cumulative for the process). Until the turn
is over, `--timeout`, `--deadline` and the stall watchdog stay armed, so a
background task that runs silently longer than `--stall` stops the turn as a
stall. Once it is over, an engine still alive `AGENT_SESSIONS_POST_RESULT_WAIT`
seconds later gets SIGTERM, then SIGKILL after 5 s; the status comes from the
parsed result and `warnings` gets `engine lingered after its result and was
terminated`. An engine that exits on its own after a result while a task is
still pending keeps its status and gets a warning that the answer may be
incomplete.

Failure classes, first match wins:

1. Runner decisions: kill or a signal to the runner -> `killed`; `--timeout` or
   `--deadline` -> `timeout`; the watchdog -> `stall`; a runner budget stop ->
   `budget`.
2. claude result subtype: `error_max_budget_usd` and `error_max_turns` ->
   `budget`; `error_max_structured_output_retries` -> `schema`. gemini exit 53
   -> `budget`, 42 and 55 -> `engine_error`.
3. Error text (claude `api_retry` errors and `result` text, codex `turn.failed`
   and `error` messages, the stderr tail), matched against provider error
   shapes rather than bare words: an HTTP status next to `http`, `status`,
   `error` or `code` (`API Error: 429`), `Too Many Requests`, `rate limit`,
   `usage limit`, `insufficient_quota`, `quota exceeded` (not `Disk quota
   exceeded`), `exceeded your current quota`, `RESOURCE_EXHAUSTED` ->
   `rate_limit`; `529` or `overloaded` -> `rate_limit` too, but without the
   engine-wide pause (an overloaded provider says nothing about this account);
   a 401 or 403 status, `Unauthorized`, `Not logged in`, `Invalid API key`,
   `Please run /login`, `authentication failed|error|required`, missing,
   invalid or expired credentials, an expired token -> `auth`. A line number
   such as `line 403` or a crash that merely mentions credentials is not
   matched. The patterns are `RATE_LIMIT_RE`, `OVERLOADED_RE` and `AUTH_RE` in
   `scripts/agent_sessions/engines.py`.
4. Any other codex `turn.failed`/`error` or claude/gemini `is_error` ->
   `engine_error`.
5. Non-zero exit, or exit 0 without a terminal event -> `crash` (reason
   `no terminal event (...)` in the second case).
6. Structured output invalid after the fixup -> `schema`; verify failed after
   its fixups -> `verify`; lost runner -> `lost`; failed upstream -> `dependency`.

The reason names the evidence (`engine exited 1: Not logged in`).

Retries: with `--retries N`, a turn that failed with `rate_limit`, `stall`,
`crash`, or `engine_error` without an answer runs again, at most N more times.
`auth`, `budget`, `schema`, `verify`, `timeout` and `killed` are never retried.
A retried first turn starts a fresh conversation (claude a new `--session-id`,
recorded as `meta.conversation_id`; codex a new `exec`; gemini a new session).
The new conversation knows nothing of the failed attempt, which may have left
partial edits in the workdir (a worktree isolates the parent, not the retry).
So when the failed attempt made tool calls and a write-capable child's git
workdir shows new dirty paths or new commits since the turn began, the retried
prompt starts with `Note from the runner: an earlier attempt at this task
stopped (<class>) ...` naming them; outside git it says files may have
changed. Read-only children get no note. A later turn of a steerable child resumes the same conversation and keeps its
message id. Backoff: `rate_limit` waits the reset the engine named, else 60 s x
attempt (at most 900 s); other classes wait 5 s x attempt. A wait that would
cross `--deadline` is not taken: the turn fails with its original class and the
reason says so. Failed attempts stay as `turns/N.attempt<k>.*`, each retry is a
`retry` event, and a `rate_limit` failure (other than an overload) also pauses the engine (see
[Scheduling](#scheduling-caps-queue-and-pauses)).

### Failure classes at a glance

| Class | Typical source | Retried by `--retries` | `resume` allowed |
|---|---|---|---|
| `rate_limit` | HTTP 429, "usage limit", "quota exceeded", claude `api_retry` with `rate_limit`; also 529 / `overloaded` (retried, no pause) | yes, after the parsed reset or a backoff | yes |
| `auth` | 401, 403, "Not logged in", "Invalid API key" | never | no: log in first |
| `timeout` | `--timeout` (per turn) or `--deadline` (whole child) | never | yes |
| `stall` | no engine output for `--stall` seconds | yes | yes |
| `crash` | non-zero exit, or exit 0 without a terminal event | yes | yes |
| `budget` | claude `error_max_budget_usd` or `error_max_turns`, the runner's post-turn check, gemini exit 53, an exhausted ledger | never | no: raise the limit by respawning |
| `schema` | output still invalid after the one fixup turn, claude `error_max_structured_output_retries` | never | no |
| `verify` | `--verify` still failing after its fixups | never | no |
| `engine_error` | codex `turn.failed` or `error`, claude or gemini `is_error`, gemini exit 42 or 55 | only when no answer came back | yes |
| `killed` | `kill`, `send --interrupt`, a signal to the runner (tmux dying sends SIGHUP) | never | yes |
| `lost` | the backend is gone and no result was written (reboot, killed tmux server) | n/a | yes, or `recover` |
| `dependency` | an `--after` upstream finished non-ok | n/a | no: fix the upstream |

## When a child looks stuck

1. `agent-session status <name>`. `activity_s` is time since the last engine
   event of any kind and `stall_in_s` is how long until the watchdog acts. A
   large `activity_s` means silent, not hung: a child running a 15-minute test
   suite emits nothing until the command returns.
2. `agent-session events <name> --since 0` or `log <name>`. If the last event is
   a `tool_start` or `cmd` that can plausibly take that long (tests, a build, an
   install), it is working; check again later. `peek <name>` shows the live
   pane (tmux) or the runner output (process backend).
3. If the log is quiet after an `assistant` line, or shows the same step
   repeating, the prompt is the likely cause: a task written with your context
   assumed.
4. Let the watchdog act, or `kill <name>`: it terminates the engine's process
   group, escalates to SIGKILL, kills any children that child spawned, and
   prints `terminated` or `force-killed`.
5. After a `stall` or `kill`, `resume <name>` once. If it stalls again,
   respawn with `--replace` and a clearer prompt, a larger `--stall` or a higher
   tier. Never start a second writer in the same directory before `kill` has
   returned.

Do not run `tmux kill-session` by hand: the runner then records `killed` from
SIGHUP, but children the child spawned keep running.

## Budgets and tree ledgers

| Limit | claude | codex, gemini |
|---|---|---|
| `--budget-usd X` | passed as `--max-budget-usd X` (stops subagents too) and checked by the runner after each turn | checked after each turn from the estimated cost; refused at spawn without `AGENT_SESSIONS_<ENGINE>_PRICE` |
| `--max-tokens N` | checked after each turn | checked after each turn |
| `--max-turns N` | passed as `--max-turns N` (agentic turns) | approximated as N tool calls: the ladder runs when the parsed tool-call count exceeds N |
| `--deadline` | wall clock across turns, retries, verify and fixups | same |

A breach ends the turn as `error`, `failure_class budget`, keeps the answer and
writes a `budget` event; a steerable child stops after it. Usage arrives at turn
end for codex and gemini, so one turn can overshoot a USD or token limit by its
own size. Without a price, a USD budget on codex or gemini exits 2: `codex
reports tokens, not cost; set AGENT_SESSIONS_CODEX_PRICE=input=..,cached=..,output=..
(USD per 1M tokens) or use --max-tokens`.

`--tree-budget-usd X` or `--tree-max-tokens N` creates `ledgers/<id>.json` and
sets `AGENT_SESSIONS_LEDGER=<id>` in the child, so every child it spawns, and
theirs, joins the ledger. The runner adds each turn's cost and tokens to the
ledger under the lock. A claude turn gets `--max-budget-usd` lowered to what the
ledger has left; a codex or gemini turn that leaves the ledger exhausted ends
as `budget`. A spawn under an exhausted ledger exits 2 with `tree budget
exhausted: $X of $Y spent by a, b`. `budget` shows the ledgers.

## Concurrency cap

A child holds a slot while it has state, no result, and either a live tmux
session, a live runner process, or a reservation younger than 60 s. Finished
and lost children do not count; unrelated tmux sessions never count. `spawn`
holds `fcntl.flock` on `<state>/.lock` from the count until tmux has started
the runner (through `git worktree add`, however long that takes), so
concurrent spawns cannot both take the last slot, and `wait`, `result` and
`reap` take the same lock before declaring a child lost or deleting it, so
they cannot act on a child that is still being set up.

## Scheduling: caps, queue and pauses

Caps: `AGENT_SESSIONS_MAX` (default 3) for all engines, plus
`AGENT_SESSIONS_MAX_CLAUDE` (unset: the global cap), `AGENT_SESSIONS_MAX_CODEX`
(2; codex starts returning 429 past two or three concurrent streams) and
`AGENT_SESSIONS_MAX_GEMINI` (2), counted under the lock. A refused spawn names
the cap that blocked it.

Pauses: a turn that fails with `rate_limit` (not an overload) writes `pauses/<engine>.json`
(`until_epoch`, `reason`, `by`, `written_iso`). The pause lasts until the reset
the engine named (claude `retry_delay_ms`; `try again in N seconds|minutes|hours`
or `try again at 3:05 PM` in the error text), else 300 s for a plain 429 and
3600 s for usage-limit or quota wording. While it is active `spawn <engine>`
exits 2 with `<engine> is paused until HH:MM after a usage limit; pass --queue
to wait, pick another engine, or run: agent-session unpause <engine>`.

Queue: `spawn --queue` creates a blocked child with `meta.queue` set and
`progress.phase` `queued`; no engine, worktree or env file exists yet. `kick`
runs under the lock at the end of every runner (after result.json, before the
linger) and at the start of `spawn`, every `wait` poll, `status`, `list` and
`unpause`. It drops expired pauses, then walks queued children oldest first and
starts each one whose engine is not paused, whose caps have room and whose
ledger has budget; starting creates the worktree, writes `env.json` and launches
the backend. A queued child whose ledger is exhausted is finalized as `error`,
`failure_class budget`. There is no daemon: a queued child on a machine where no
agent-session command runs again stays queued. `kill` on a queued child records
`killed` without starting it.

## Resume and recover

`resume <name>` continues a child in the same engine conversation (claude
`--resume <id>`, codex `exec resume <thread id>`, gemini `--resume <id>`) with a
continuation message that tells the child to re-read what it changed. It is
allowed after `lost`, `killed`, `timeout`, `stall`, `crash`, `rate_limit` and
`engine_error`, and refused (exit 2, result untouched) for `ok`, `auth`,
`budget`, `schema`, `verify` and `dependency`, or when the session id or the
workdir is gone. The replaced result moves to `attempts/<k>.result.json`.
Caps, pauses and ledgers apply: a blocked resume is queued.

`recover` is the first command after a reboot. It records every unfinished
child of yours whose backend is gone as `lost`, then resumes each one that has
a session id, printing `resumed`, `queued` or `skipped (no session id;
respawn)` per child. `recover --dry-run` changes nothing. Engines may reload
only part of the earlier context on resume; check the resumed child's answer
against its diff rather than assuming it remembered everything.

## What is approximate

- Codex and gemini report usage only at the end of a turn, so a USD or token
  budget can be overshot by one turn's spend; the result records the
  overshoot.
- `--max-turns` for codex and gemini counts tool calls, not model turns.
- Claude's `total_cost_usd` is the CLI's own estimate, not billing. Codex and
  gemini costs are estimates from the price you configure.
- The stall watchdog sees engine output, not progress: a silent long command
  looks the same as a hang.
- Rate-limit reset times come from the error text when the engine gives one;
  otherwise the pause is 300 s for a 429 and 3600 s for a usage limit.
- Read-only denial warnings exist for claude only; codex and gemini give no
  equivalent signal.
- Queued children advance only when some agent-session command runs; there is
  no daemon.
- The parent's own spend is never in a tree ledger.
