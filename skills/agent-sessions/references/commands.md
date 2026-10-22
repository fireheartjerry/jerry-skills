# agent-session command reference

Every command, option, exit code, environment variable, file and engine
command line of `scripts/agent-session`. SKILL.md has the decision procedure;
the record formats are in `references/schema.md`, the failure handling in
`references/reliability.md`, verify, judge and best-of-N in
`references/review.md`, and plans and hooks in `references/pipelines.md`.

## Security note

What runs with permissions bypassed. A child with the default role
(`implementation`) runs its engine with approvals and sandbox off: claude
`--dangerously-skip-permissions`, codex `--dangerously-bypass-approvals-and-sandbox`,
gemini `--approval-mode yolo`. It can run any command, write anywhere the user
can, and reach the network. That is what makes unattended delegation work, and
it is the reason to scope every prompt.

What `--role review|research` (or `--readonly`) restricts. claude gets only
`Read,Grep,Glob` under `--permission-mode dontAsk` with no MCP servers, and can
read only inside `<dir>`; codex runs in its read-only sandbox; gemini runs in
`--approval-mode plan`. This narrows the engine's own tools. It is not a
containment boundary (details in [What --readonly does](#what---readonly-does-and-does-not-do)).

Shell commands. agent-session itself runs a shell only for the two commands
the user supplies: `--verify CMD` and `--on-finish CMD`, each as
`/bin/sh -c CMD` with the child's environment. Every other subprocess is an
argument vector (engines, `git`, `tmux`, `ps`).

Where prompts and keys travel. Prompts are written to `prompt.txt` in the
child's state directory (mode 0700) and fed to the engine on stdin; they never
appear in process arguments. API keys and provider settings reach the child
through `env.json` (mode 0600), which the runner loads and deletes before the
engine starts; they never appear on a tmux command line, and the tmux server is
started with a minimal environment. agent-session makes no network calls of its
own; only the engine CLIs talk to their providers.

Auditing the package. `scripts/agent-session` is a 30-line entry that loads
`scripts/agent_sessions/`. Read these modules to audit what runs:

| Module | What to check |
|---|---|
| `engines.py` | the exact argv per engine (`build_cmd`), permission flags, the prompts the runner sends (schema fixup, verify fixup, resume, judge) |
| `runner.py` | the engine `Popen`, the watchdog and signal ladder, `/bin/sh -c` for `--verify` and `--on-finish` |
| `backend.py` | how the runner is started (tmux session or detached process) and with which environment |
| `launch.py` | spawn validation, `env.json`, worktree creation, the scheduler (`kick`) |
| `gitops.py` | every git command: worktrees, diffs, snapshots, merges |
| `procs.py` | `ps` liveness checks, process-group kill, owner tokens, binary lookup |
| `state.py`, `common.py` | file layout, locking, atomic writes, option parsing |
| `commands.py`, `review.py`, `plans.py`, `cli.py` | the user-facing commands |
| `schema.py` | the JSON Schema validator and the built-in schemas |

## Grammar

```
agent-session spawn   <engine> <name> <dir> [<prompt>] [--prompt-file F | -]
                      [--tier cheap|standard|deep] [--model M] [--role implementation|review|research]
                      [--readonly | --write] [--worktree] [--base REF] [--copy P1,P2] [--steerable]
                      [--timeout S|Nm|Nh] [--stall S] [--deadline S|Nm|Nh] [--retries N]
                      [--budget-usd X] [--max-tokens N] [--max-turns N]
                      [--tree-budget-usd X] [--tree-max-tokens N]
                      [--schema FILE] [--verify CMD] [--verify-timeout S] [--verify-fixups N]
                      [--after a,b] [--queue] [--n N] [--engines e1,e2] [--on-finish CMD]
                      [--backend tmux|process] [--replace] [--json]
agent-session list    [--json] [--mine] [--running] [--group G]
agent-session status  <name> [--json]
agent-session wait    [--timeout S] [--any] [--json] [--max-chars N] <name|group>... | --mine
agent-session result  <name> [--json] [--max-chars N]
agent-session events  <name> [--follow] [--timeout S] [--since N] [--json]
agent-session send    <name> [<text>] [--file F | -] [--no-wait] [--interrupt] [--timeout S] [--json]
agent-session turn    <name> [N | last] [--message ID] [--timeout S] [--json]
agent-session log     <name> [N] [--turn K]
agent-session peek    <name> [N]
agent-session stop    <name>
agent-session kill    <name> [--grace S]
agent-session resume  <name> [--message TEXT] [--json]
agent-session recover [--all] [--dry-run] [--json]
agent-session judge   <name> [--engine E] [--tier T] [--brief F] [--timeout S] [--json]
agent-session select  <group> [--judge] [--json]
agent-session diff    <name> [--stat] [--base REF]
agent-session collect <name|group> --merge | --squash | --cherry-pick SHA | --branch [--json]
agent-session conflicts [<name|group>...] [--json]
agent-session reap    [<name|group>...] [--all] [--force] [--discard SHA] [--dry-run] [--json]
agent-session snapshots [--prune --older-than Nd] [--json]
agent-session budget  [--json]
agent-session unpause <engine>
agent-session plan    run <file.json> [--replace-failed] | status <name> [--json]
agent-session engines [--json]
agent-session schema  [result|judge|event]
agent-session doctor  [--json] [--engine claude|codex|gemini] [--backend tmux|process]
agent-session --help | -h | help | --version
agent-session _run    <state-dir>          # internal: the runner (tmux pane or detached process)
agent-session _hook   <state-dir>          # internal: --on-finish for a child finalized without its runner
```

`<engine>` is `claude`, `codex` or `gemini` (gemini is experimental: its flags and
event format come from the documentation and the fake engine in the tests, not from
the real CLI; `engines` marks it `verified: false`). `<name>` matches
`[A-Za-z0-9_-]{1,40}`; dots are refused because tmux parses them as a
window/pane separator. `<dir>` must exist. Backward-compatible forms still
accepted: `wait <name> <seconds>`, `log <name> <lines>`, `peek <name> <lines>`,
`send <name> word word ...` (joined by single spaces).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success; for `wait`, `result`, `turn`, `send`, `status`, `judge`: the child or turn finished with status `ok` |
| 1 | the child or turn finished non-ok (`error`, `timeout`, `killed`, `lost`); `spawn` could not start the backend or git; `conflicts` found overlaps or a simulated merge conflict; `collect` stopped on a merge conflict and aborted; `select` found no `ok` member; `judge` produced no valid verdict |
| 2 | usage error, invalid input, missing dependency, unknown name, refused spawn (cap or paused engine without `--queue`, depth, name in use, `--schema` with `--steerable`, unreadable schema, tree budget exhausted, USD budget on an engine with no cost and no price), refused `resume`, `collect` or `reap --discard` |
| 3 | still pending when the budget ran out (`wait`, `send`, `turn`, `result`, `status`, `judge`, `select`, `events --follow`, `plan status`) |

`doctor` exits 0 when every required check passes, 1 otherwise. `engines` exits
1 only when no engine is installed. Errors go to stderr as
`agent-session: <message>`, with a `  try: <command>` line when there is an
obvious remedy.

## Subcommands

### spawn

Validates everything before any side effect: engine, name, directory, exactly
one prompt source (positional text, `--prompt-file F`, or `-` for stdin;
non-empty), tier, role, durations, budgets, the schema file, `--after`
upstreams and templates, nesting depth (the larger of `AGENT_SESSION_DEPTH` and
the calling child's recorded depth), the engine binary (PATH, then a login
shell for nvm-style shims), `--worktree` preconditions, the backend, caps,
pauses and the tree ledger. Then, under a lock on the state root held until the
runner is up, it reserves the state directory, creates the worktree, writes
`env.json` and `meta.json` and starts the runner. A failure after the
reservation removes what it created and exits 1. Output names the state
directory and the next command (`wait` for a one-shot child, `turn <name> 1`
then `send` for a steerable one). `--json` prints meta.json instead.

Prompts are written to `prompt.txt` and fed to the engine on stdin, so `ps`
never shows them and a leading `-` cannot be read as a flag.

Model and role:

- `--tier cheap|standard|deep` (default `cheap`): claude `haiku/sonnet/opus`,
  gemini `flash/pro/pro`, codex unset (codex's configured model, with a notice)
  unless `AGENT_SESSIONS_CODEX_MODEL_<TIER>` is set. `--model M` overrides.
- `--role implementation|review|research` (default `implementation`, recorded
  in meta.json and result.json). `review` and `research` imply `--readonly`
  unless `--write` is given; `--readonly --write` is a usage error.
- `--readonly`: see [What --readonly does](#what---readonly-does-and-does-not-do).
- `--steerable`: the child stays alive between turns and takes messages from
  `send`; it ends after `stop` or `AGENT_SESSIONS_IDLE` seconds without a
  message.

Isolation (details in `references/isolation.md`):

- `--worktree`: run on branch `agent-session/<name>` in a worktree under the
  state directory. Needs a git repository; the branch must not exist.
- `--base REF` (needs `--worktree`): start the branch from REF instead of
  HEAD. Resolved when the child starts, so `--after a --base agent-session/a`
  stacks on a's finished branch; without `--after` an unknown REF is refused.
- `--copy P1,P2` (needs `--worktree`): copy these paths from the repository
  root into the new worktree (typically ignored files such as `.env`). Paths
  must stay inside the root and may not name `.git`; a failed copy is a
  warning.
- `--replace`: reap the old state of this name first. Refused before anything
  is removed when the old child is running, its worktree is dirty, or its branch
  has unmerged commits (the error names `collect <name> --merge` and
  `reap <name> --discard <tip>`). A finished child that still lingers is ended
  first and its runner waited for (up to 10 s), so nothing of the old run is
  written into the new state.
- `--backend tmux|process` (default `AGENT_SESSIONS_BACKEND`, else tmux when
  installed, else process): see [Backends](#backends-tmux-and-process).

Time and reliability (details in `references/reliability.md`):

- `--timeout S|Nm|Nh`: per turn, default 3600 s, minimum 5 s.
- `--stall S`: stop a turn after S seconds without engine output (default
  `AGENT_SESSIONS_STALL` or 900; `0` disables).
- `--deadline S|Nm|Nh`: wall clock for the whole child (turns, retries, verify,
  fixups).
- `--retries N`: retry `rate_limit`, `stall`, `crash` and answerless
  `engine_error` failures up to N times (default `AGENT_SESSIONS_RETRIES` or 0,
  at most 5).

Budgets (enforcement per engine in `references/reliability.md`):

- `--budget-usd X`, `--max-tokens N`, `--max-turns N`: per child. A USD budget
  on codex or gemini without `AGENT_SESSIONS_<ENGINE>_PRICE` exits 2.
- `--tree-budget-usd X`, `--tree-max-tokens N`: create a ledger shared by this
  child and everything it spawns. Refused when the caller already runs under a
  tree budget.

Output checks (details in `references/review.md` and `references/schema.md`):

- `--schema FILE`: structured output. FILE must parse as a JSON object;
  keywords the validator ignores are a warning. claude gets
  `--json-schema <text>`, codex `--output-schema <path>`, gemini a prompt
  prefix. The runner validates locally and sends one fixup turn on failure.
  Refused with `--steerable`. Codex hands the schema to OpenAI strict
  structured output, which rejects a schema unless every object sets
  `"additionalProperties": false` and lists all its properties in `required`
  (express an optional field as nullable, `"type": ["string", "null"]`); the
  child then fails with `engine_error` naming the problem.
- `--verify CMD`: run `/bin/sh -c CMD` in the workdir after the last turn.
  Refused for a read-only child.
- `--verify-timeout S` (default 600), `--verify-fixups N` (default 1 for a
  one-shot child, 0 for a steerable one, at most 5; needs `--verify`).

Scheduling and pipelines (details in `references/pipelines.md`):

- `--queue`: when a cap or an engine pause blocks the start, create the child
  queued (exit 0, `state : queued`) instead of refusing.
- `--after a,b`: start only after every upstream finished `ok`; implies
  queueing. Upstreams must exist and may not form a cycle with queued children.
  The prompt may use `{{answer:a}}`, `{{answer_file:a}}`, `{{branch:a}}`,
  `{{worktree:a}}`, `{{structured:a}}` and `{{name}}`.
- `--n N` with `--engines e1,e2` (best-of-N, N from 2 to 6): members
  `<name>-1 .. <name>-N`, engines assigned round-robin, every member in its own
  worktree, members beyond the caps queued. Needs a git repository; refused with
  `--steerable`. `--engines` needs `--n`.
- `--on-finish CMD` (default `AGENT_SESSIONS_ON_FINISH`): run `/bin/sh -c CMD`
  once after the result is written.

### list

One row per state directory: `NAME ENGINE STATE MODEL ACTIVITY OWNER DIR`.
STATE is the result status (`ok`, `error`, `timeout`, `killed`, `lost`) with
`/linger` while the tmux session is still attachable; `running` for a working
one-shot child; `turn N` or `waiting` for a steerable child; `lost` when tmux
and the runner are gone and no result exists. ACTIVITY is seconds since the
last engine event of any kind, rendered or not; a long quiet command (tests, a
build) keeps it growing until the command returns, so a large value means
"silent", not "hung". OWNER is `mine` or the short owner token. Works without
tmux (everything shows as `lost`, with a warning). `--json` prints meta plus
`state` and `activity_s`. Filters: `--mine` (owner is the caller), `--running`
(no result yet), `--group G` (members of `groups/G.json`; exit 2 when the group
does not exist). A `paused: <engine> until HH:MM (<reason>)` line precedes the
table while a pause marker is active, and `queued` appears as the STATE of a
child waiting for a slot, a pause to end or an upstream. `list` runs `kick`
first.

### status

`status <name> [--json]`: one child. Running: `state` (as `list`), `phase`
(`queued | starting | turn | waiting | verify | linger | finished`), `turn`, `attempt`,
`activity_s`, `events`, `tool_calls`, `last_event`, `usage_so_far`,
`cost_so_far`, `budget` (limits and remaining), `stall_in_s` (seconds until the
stall watchdog fires without engine output; null outside a turn or with
`--stall 0`), `queue` (`reason` `cap|pause|after`, `detail` naming the blocker,
`since`, `position` in the queue, oldest first; null unless queued), `verify`,
`pid` (engine) and `runner_pid`; exit 3. `status` runs `kick` first. Finished: the
result document without `answer` plus `state` and `phase: finished`; exit 0 or
1 by status. Human output prints the result header and where the answer is.

Liveness never depends on the caller's environment: each child's tmux socket
is in its meta.json, and the runner's pid and start time are in
progress.json, so a caller that lost `AGENT_SESSIONS_TMUX_SOCKET` between tool
calls still sees the child as running.

### wait

Polls every second until every named child is terminal or the budget
(`--timeout`, default 240 s) is spent. Unknown names exit 2 before any waiting.
A child whose tmux session and runner are both gone with no result is recorded
as `lost` on the spot (under the state lock, so a spawn still setting the child
up is never mistaken for a lost one). Output depends on whether everything
finished: when it did, the full `result` view for each child; while anything
is still pending, one line per child (`done : a ok` or `pending : b (running,
last activity 12s ago)`), so a polling loop never re-prints a long answer and
two finished answers cannot blow through a host's shell output cap. A pending
steerable child in the `waiting` state gets a note: `wait` only returns after
`stop` or the idle exit, and the replies are read with `turn <name> N`. The
240 s default exists because agent hosts cap a single shell call (Claude Code's
Bash tool defaults to 120 s and caps at 600 s): call `wait` with a tool timeout
at least 30 s longer than `--timeout` and loop while the exit code is 3.
`--json` prints result.json for one name or a list for several, and
`--max-chars` applies to the text view only.

`--any <name>...` returns as soon as one named child is terminal: the finished
children get the full result view, the rest `pending : ...` lines; exit 0 when
every finished child is `ok`, 1 otherwise, 3 when none finished within the
budget. `--json` prints `{"finished": [result...], "pending": [...]}`. Call it
again with the remaining names to collect the next one. `--mine` (no names)
waits for every unfinished child the caller owns; with none it prints a note
and exits 0.

### events

`events <name> [--follow] [--timeout S] [--since N] [--json]` prints
`<state>/<name>/events.jsonl`: by default `[HH:MM:SS] turn N kind: text`, with
`--json` the raw objects. `--since N` skips the first N lines. `--follow` keeps
printing until the child is terminal (exit by status) or `--timeout` (default
240) expires (exit 3). Kinds: `lifecycle`, `init`, `assistant`, `tool_start`,
`tool_end`, `cmd`, `edit`, `tool_result`, `api_retry`, `denied`, `error`,
`turn_start`, `turn_end`, `retry`, `stall`, `budget`, `verify`, `result`, `task`;
`schema event` prints the line schema.

### result

The finished child's status, exit code, turns, duration, cost, files and the
answer, truncated to `--max-chars` (default 20000) with a marker naming the
file that holds the rest; `--max-chars 0` prints only the length and the file.
A running child gives exit 3 and a `pending` line. `--json` prints result.json
verbatim.

### send

For `--steerable` children only. Refuses, each with exit 2: unknown name, a
one-shot child, a finished child, a dead tmux session, a child already asked
to `stop`, an empty message. Writes the message atomically into the inbox with
a 12-character id, prints `queued : <id>`, then waits (default 240 s) for the
turn that consumed that id and prints its `turn` view. Exit 0 or 1 follow the
turn's status; exit 3 means still pending (pick it up with
`turn <name> --message <id> --timeout S`); exit 1 with
`child ended before answering` means the session finished or died first.
`--no-wait` queues and returns. `--file F` or `-` reads the message from a file
or stdin. A message that begins with `--` and contains a space is taken as
text; a single `--word` token is treated as an option, so send that kind of
message with `--file F` or `-`.

`--interrupt`: when a turn is running, the runner stops it with the signal
ladder (SIGINT, then SIGTERM, then SIGKILL, `AGENT_SESSIONS_LADDER_S` apart;
claude saves its session on SIGINT), records that turn as `killed` with
`interrupted: true`, and runs the message as the next turn of the same
conversation; the output adds `interrupt : turn N is being stopped`. With no
turn running it is an ordinary send. The interrupted turn is listed in the
result's `interrupted_turns`, not in `failed_turns`, and does not decide the
session's status: a session whose later turns succeed ends `ok`. Its turn file
keeps `status killed` and the reason `interrupted by send --interrupt`.
Without `--interrupt` a message waits for the current turn (queue semantics).

### turn

Shows one turn record: `turn <name> 3`, `turn <name> last`, or
`turn <name> --message <id>`. With `--timeout S` it waits for the record to
appear, using the same liveness rules as `send`. `turn <name> 1 --timeout 240`
is how the opening reply of a steerable child is collected; `wait` does not
return until the session ends.

### log and peek

`log <name> [N]` tails N lines (default 80) of the timestamped transcript; no
tmux needed. `log <name> --turn K` renders turn K's raw engine events through
the same renderer. `peek <name> [N]` captures the last N lines (default 60) of
the live tmux pane and exits 2 when the session is gone; for a process-backend
child it prints the tail of `<name>/pane.log` (the runner's output), which
outlives the runner.

### stop

Writes the `stop` marker. The runner finishes the current turn and any
messages already queued, then exits with `ended: stopped`. Exit 2 for an
unknown, one-shot or finished child.

### kill

Writes the `kill` marker, sends SIGTERM to the engine's process group, waits
`--grace` seconds (default 10), sends SIGKILL if needed, then waits up to 5 s
for the runner to write a `killed` result. If the runner is gone it writes the
result itself; if the runner is still there but did not answer (hung or
stopped), it terminates the runner (TERM, then KILL after 3 s; for tmux it also
ends the session) and then writes the result. Prints what happened to the engine (`pid N terminated`,
`pid N force-killed after Gs`, `not running`) and to the runner. Then it does
the same to every unfinished child that child had spawned (their owner is the
child's token, and they run in their own tmux sessions, so killing the engine
alone would leave them running unsandboxed and holding cap slots); each is
printed as `child : <name> killed too`. On a child that has already finished it
ends the linger (if any) and exits 0. On a `lost` child whose engine process is
still alive it kills that orphan. It never touches a session whose exact name
differs. The engine's start time is compared under the C locale, so a kill from
a shell in another locale finds the same process.

### resume

`resume <name> [--message TEXT] [--json]` continues a finished child in the
same engine conversation: claude `--resume <conversation id>`, codex
`exec resume <thread id>`, gemini `--resume <session id>`. Allowed when the
child ended `lost`, `killed` or `timeout`, or with failure class `stall`,
`crash`, `rate_limit` or `engine_error`, and its session id is known and its
workdir still exists. Refused with exit 2 and the result untouched: `ok`
(`nothing to resume`), `auth`, `budget`, `schema`, `verify` and `dependency`
(each message says what to fix first), still running or queued, no session id
(`respawn`), workdir gone, tree budget exhausted.

On success result.json moves to `attempts/<k>.result.json`, `meta.resumes`
(and later `result.resumes`) gains `{"at_iso", "from_status", "message"}`, and
a new runner starts on the same state directory. The first new turn's prompt
is `--message`, or by default: "You were interrupted (<reason>) at <iso>.
Continue the task. Re-read any file you changed before editing it again, and
finish with your final report." A steerable child goes back to its waiting
loop; `--message` (if given) is its next turn. Caps, pauses and ledgers apply:
when blocked, the child is queued (`queued : <name> (cap: ...)`, exit 0) and
starts on a later `kick`. The final status of a resumed run is judged by its
new turns. Engines may not reload all earlier context on resume; the default
message tells the child to re-read what it changed. Output: `resumed : <name>
(session <id>)` and the `wait` command; `--json` prints
`{"name", "state": "resumed|queued|refused|skipped", "detail"}`.

### recover

`recover [--all] [--dry-run] [--json]`: the first command after a reboot. For
the caller's children (everyone's with `--all`) that are `lost`, or unfinished
with neither tmux session nor runner, it records `lost` and resumes each one
that has a session id. One line per child: `resumed : a (session ...)`,
`queued : b (cap: ...)`, `skipped : c (no session id; respawn)`. `--dry-run`
prints `would resume` / `skipped` and changes nothing. `--json` prints
`{"dry_run": bool, "children": [{"name", "state", "detail"}]}`. Always exit 0;
it runs `kick` first.

### judge

`judge <name> [--engine E] [--tier T] [--brief F] [--timeout S] [--json]`: an
independent read-only review of a finished child. Default engine: the other
one (claude to codex, codex to claude, gemini to claude); default tier
`standard`. It writes the brief (`--brief F`, else the child's prompt), the
answer and the diff into `<state>/<name>/judge/`, spawns `<name>-judge` with
`--role review` and a fixed schema, waits `--timeout` (default 240) and prints
`verdict : revise (codex standard, 3 findings, $0.01)` and the findings. Exit 3
while the judge runs (run `judge <name>` again; the running judge is reused).
The verdict is stored in `<state>/<name>/judge.json`. Protocol, schema and
prompt: `references/review.md`.

### select

`select <group> [--judge] [--json]`: rank the members of a best-of-N group and
record the winner in the group file. Every member must be finished (exit 3
otherwise). `--judge` first judges every member that finished `ok` with a
passing or absent verify, reusing stored verdicts. Prints an evidence table and
`winner : <member> (<branch>)`; exit 0 with a winner, 1 when no member finished
`ok`. Ranking key: `references/review.md`.

### diff

`diff <name> [--stat] [--base REF]`: for a worktree child, `git diff` of the
worktree against its base commit (or REF), uncommitted changes included, plus
untracked files. For a child without a worktree, the diff of its workdir with
`note : shared tree; changes may include other writers`. Exit 2 when there is no git repository.

### collect

`collect <name|group> --merge | --squash | --cherry-pick SHA | --branch [--json]`:
bring a worktree child's commits into the branch checked out in the repository
root. A group name collects its winner. Refused (exit 2) when the child is
running, has no worktree, its worktree is dirty, its branch has no commits
beyond the base, or (for `--merge` and `--squash`) the parent tree is dirty. A
merge, squash or cherry-pick that stops on conflicts is aborted and exits 1.
`--branch` only prints the branch, its commit count and tip. `--json` prints
`{mode, branch, base, head, merged_into, commit}`.

### conflicts

`conflicts [<name|group>...] [--json]`: for each worktree child in scope (named,
else every unreaped worktree child of the caller) the files changed since its
base commit plus dirty files. The member `(parent tree)` is what the parent
changed since that child's base: commits on the parent branch after the spawn
(`git diff <base_commit> HEAD` in the root) plus the root's dirty files.
Prints every pair that shares a file, `a <-> b : src/x.ts`. With git 2.38+ it
also merges each child's committed branch tip in memory (`git merge-tree
--write-tree`) against the parent's HEAD and against sibling tips and prints
`merge conflict (simulated): a <-> b : files`; uncommitted work is only in the
path forecast. Exit 0 when neither finds anything, 1 otherwise, 2 without
worktree children. Shared paths are a forecast; a clean simulation does not
mean the tests pass. `--json`: `{"members": {...}, "overlaps": [{"a", "b",
"files"}], "merge_conflicts": [{"a", "b", "files"}] or null, "merge_simulation":
"git merge-tree" or "unavailable (needs git 2.38+)"}`.

### reap

Scope: the names given (a group name means its members); otherwise every
finished child whose owner token matches yours; `--all` lifts the owner filter.
The children of every child in scope are added (they are owned by the child,
not by you). Lingering finished children are ended first and their runners
waited for (up to 10 s each, outside the lock). Per child, under the state
lock: a running child is skipped unless
`--force`, which runs `kill` first; a lingering tmux session is ended; a lost
child gets a `lost` result first; a worktree with uncommitted changes is
skipped unless `--force`, which first writes a snapshot
(`snapshot : refs/agent-sessions/<name>/snapshot-<ts> (restore: git -C <root> worktree add <dir> <ref>)`);
the worktree is removed and the branch deleted with `git branch -d`, or kept
and reported with plain git hints (`git merge`, `git branch -D`; the child's
state is gone, so `collect` no longer knows it) when it has unmerged commits.
After `collect --squash` or `--cherry-pick` the branch never merges, so
collect's `next` line suggests `reap <name> --discard <tip>`. `--force` never
deletes commits.
`--discard SHA` (one child) also deletes a branch with unmerged commits with
`-D`, after a snapshot, but only when SHA (7 or more characters) is that
branch's tip; any other SHA exits 2 and names the tip. Without names, reap also
removes abandoned reservations (a `reserved` marker older than 60 s and no
meta.json). `--dry-run` prints the plan, including `would keep branch ...` for
branches the real reap would keep. `--json` prints one record per child.
Summary: `reaped N, skipped M, kept-branches K`.

### snapshots

`snapshots [--prune --older-than Nd] [--json]`: list the
`refs/agent-sessions/*` snapshot refs, with age and commit, in every
repository a known child used (reap remembers the ones it snapshotted) and in
the current repository. Age is from when the snapshot was taken, never the
commit date. `--prune` deletes those older than N days (default 30) and
prints the count. A snapshot is a plain commit: `git log <ref>`,
`git worktree add <dir> <ref>` or `git checkout <ref> -- <path>` restore it.

### budget

`budget [--json]`: the tree ledgers the caller owns, plus the one named by
`AGENT_SESSIONS_LEDGER` when set: id, root child, limits, spent and remaining
USD and tokens, and per-child spend. `--json` prints `{"ledgers": [...]}` with
the ledger files' fields plus `remaining_usd` and `remaining_tokens`. The
parent's own spend is never in a ledger.

### unpause

`unpause <engine>`: deletes `pauses/<engine>.json`, runs `kick` and prints one
`started : <name>` line per queued child that started. Use it when you know the
limit has reset; a pause that has expired is ignored and removed anyway.

### plan

`plan run <file.json> [--replace-failed]` spawns a small DAG described in one
file; `plan status <name> [--json]` reports it. See
[Pipelines](pipelines.md#pipelines---after-templates-and-plans) for the file format and the
replay rules. `plan status` prints one row per node (`NODE CHILD STATE FAILURE
VERIFY COST`) and `total : $X; N ok, ...`; exit 0 when every node is `ok`, 3
while any is running or queued, 1 otherwise. `--json`:
`{"plan", "nodes": [{"node", "child", "state", "failure_class", "verify",
"cost_usd"}], "cost_usd", "states"}`. `wait <plan>` waits for every node.

### engines

`engines [--json]`: per engine `installed` (path or null), `version`,
`verified` (claude and codex true, gemini false), `tiers` (the effective
mapping after `AGENT_SESSIONS_<ENGINE>_MODEL_*`), `capabilities`
(`structured_output`: claude `--json-schema`, codex `--output-schema`;
`native_budget_usd`, `native_max_turns`, `resume`, `readonly`, `reports_cost`,
`reports_tokens`, and `price_configured` for engines that do not report cost),
`auth` (codex: `codex login status` exit code as `ok|missing`; claude and
gemini `unknown`, there is no free probe) and `missing_flags` (unverified
engines only: the adapter's flags that `<engine> --help` does not list, `[]`
when all are there; `doctor` reports the same check as `gemini-flags`). Spends
no tokens. Exit 1 only when no engine is installed.

### schema

`schema [result|judge|event]` prints the built-in JSON Schema (result.json,
judge.json, one events.jsonl line). The validator behind `--schema` supports
`type` (string or list; `integer` vs `number`; `null`), `properties`,
`required`, `additionalProperties` (boolean or schema), `enum`, `const`,
`items`, `minItems`, `maxItems`, `minimum`, `maximum`, `minLength`,
`maxLength`, `pattern`, `anyOf`, `oneOf`, `allOf`, `not` and `$ref` to
`#/definitions/...` or `#/$defs/...`. Other keywords are ignored and reported
once as a warning.

### doctor

Checks python >= 3.8, tmux present and >= 3.2, `<engine> --version` for
claude, codex and gemini (one working verified engine is enough: a missing
engine is a warning while another is present, FAIL when none is found or when
`--engine` asked for that one; a missing gemini is always a warning unless
`--engine gemini`), git (warn only), a writable state root with a working lock,
`ps -o lstart=`, and that a tmux session can be created and destroyed on the
configured socket. Backend: `backend: tmux` when tmux 3.2+ is usable;
`backend: process (tmux not found)` as `ok`, with the tmux check a `warn`, when
tmux is missing; `--backend tmux` (or `AGENT_SESSIONS_BACKEND=tmux`) without
tmux makes both FAIL. Never runs an engine with a prompt.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_SESSIONS_STATE` | `~/.local/state/agent-sessions` | state root |
| `AGENT_SESSIONS_MAX` | `3` | max children in `running`/`waiting` state; queued children do not count |
| `AGENT_SESSIONS_LINGER` | `900` | seconds a finished tmux session stays attachable |
| `AGENT_SESSIONS_IDLE` | `3600` | steerable idle exit, seconds since the last turn ended |
| `AGENT_SESSIONS_TMUX_SOCKET` | unset | if set, new children are started with `tmux -L <value>`; recorded in meta.json and used for that child from then on, whatever the caller's environment |
| `AGENT_SESSIONS_TMUX_CONF` | unset | if set, every tmux call passes `-f <value>`; the tests use `/dev/null` so a developer's `~/.tmux.conf` cannot affect them |
| `AGENT_SESSIONS_PASS_ENV` | unset | extra variables handed to children, comma-separated; a trailing `*` makes a prefix (`MYCO_*,FOO`) |
| `AGENT_SESSIONS_OWNER` | unset | explicit owner token |
| `AGENT_SESSIONS_CLAUDE_MODEL_CHEAP/STANDARD/DEEP` | `haiku`/`sonnet`/`opus` | tier mapping for claude |
| `AGENT_SESSIONS_CODEX_MODEL_CHEAP/STANDARD/DEEP` | unset | tier mapping for codex; unset means codex's default, with a notice (example: `AGENT_SESSIONS_CODEX_MODEL_CHEAP=gpt-5.1-codex-mini`, an illustration, not a recommendation of a specific model) |
| `AGENT_SESSIONS_GEMINI_MODEL_CHEAP/STANDARD/DEEP` | `flash`/`pro`/`pro` | tier mapping for gemini (documented aliases; set a full model id if an alias is refused) |
| `AGENT_SESSIONS_CODEX_PRICE`, `AGENT_SESSIONS_GEMINI_PRICE` | unset | `input=1.25,cached=0.125,output=10` in USD per million tokens; when set, `cost_usd` is estimated from the reported tokens with `cost_source: estimate`. Malformed values are refused at spawn |
| `AGENT_SESSIONS_STALL` | `900` | default `--stall`: seconds of engine silence during a turn before the watchdog stops it; `0` disables |
| `AGENT_SESSIONS_RETRIES` | `0` | default `--retries` (at most 5) |
| `AGENT_SESSIONS_POST_RESULT_WAIT` | `10` | seconds an engine may stay alive after its terminal event before it is terminated |
| `AGENT_SESSIONS_MAX_CLAUDE`, `_MAX_CODEX`, `_MAX_GEMINI` | unset (global cap), `2`, `2` | per-engine caps, never above `AGENT_SESSIONS_MAX` |
| `AGENT_SESSIONS_BACKEND` | tmux when installed, else process | default `--backend`; inherited by children, so their children use the same backend |
| `AGENT_SESSIONS_ON_FINISH` | unset | default `--on-finish` command |
| `AGENT_SESSIONS_LADDER_S` | `15` | seconds between the INT, TERM and KILL steps of the signal ladder (stall, timeout, deadline, `send --interrupt`) |
| `AGENT_SESSIONS_LEDGER` | unset | tree ledger id; set by the runner in a child created with or under a tree budget, so every spawn the child makes joins that ledger |
| `AGENT_SESSION_DEPTH` | `0` | set by the runner for children; 2 is the ceiling, also enforced from the parent's recorded depth |
| `AGENT_SESSION_NAME` | unset | set by the runner: the child's own name |
| `AGENT_SESSION_STARTED` | unset | set by the runner: the child's start time, part of its owner token |
| `AGENT_SESSION_STATUS`, `AGENT_SESSION_FAILURE_CLASS`, `AGENT_SESSION_RESULT` | unset | set for the `--on-finish` command only: the result's status, its failure class (empty when ok) and the path of result.json |

### Environment passthrough

The child engine sees the spawner's configuration, not the tmux server's. At
spawn, every variable named below is written to `<state>/<name>/env.json`
(mode 0600). Before the engine starts, the runner rebuilds its environment
from scratch: terminal basics from the backend (`USER`, `LOGNAME`, `TERM`,
`TMUX`, `TMUX_PANE`, `TMUX_TMPDIR`), the spawner's `PATH` and `HOME`, the
spawner's `AGENT_SESSIONS_*` variables recorded in meta.json, and the contents
of env.json, which it then deletes. Anything else in the tmux server's global
environment (a shared default server can hold whatever was exported when it
started) never reaches the engine, `--verify` or `--on-finish`. Nothing in this set ever appears on a command
line. Passed: `SHELL`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TERM`,
`CLOUD_ML_REGION`, the proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`,
`NO_PROXY`, `ALL_PROXY` and lowercase), the certificate variables
(`SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`,
`NODE_EXTRA_CA_CERTS`), and every variable starting with `AGENT_SESSIONS_`,
`CODEX_`, `CLAUDE_`, `ANTHROPIC_`, `OPENAI_`, `AWS_`, `GOOGLE_`, `CLOUDSDK_`,
`AZURE_` or `FAKE_ENGINE_`, plus whatever `AGENT_SESSIONS_PASS_ENV` adds. So
Bedrock, Vertex, Azure and proxied setups work without extra configuration.
`PATH`, `HOME` and the `AGENT_SESSION_*` / `AGENT_SESSIONS_*` variables go
through `tmux new-session -e`, since the runner needs them before it can read
the file and none is secret. The tmux client that may start the server is run
with a minimal environment (PATH, HOME, user, shell, locale, TMPDIR,
TMUX_TMPDIR), so a server agent-session starts never holds an API key. Set
`AGENT_SESSIONS_TMUX_SOCKET` to keep children off the user's default server.

## Owner token

Used only to scope the default `reap` and to label the OWNER column. In order:
`child:<name>:<start epoch>` when `AGENT_SESSION_NAME` is set (a child's
children are its own; the start time keeps a reused name from claiming an
earlier child's descendants); `env:<value>` from `AGENT_SESSIONS_OWNER`;
otherwise the first ancestor process (via `ps -o ppid=,lstart=,comm=`, run
under the C locale) whose command basename, or failing that the basename of its
argv[0] (`/proc/<pid>/cmdline`, else `ps -o args=`; Linux's comm is the
executable's own name, macOS's is argv[0]), is one of `claude`, `codex`,
`gemini`, `node`, `cursor-agent`, `opencode`, `t3`, as
`proc:<name>:<pid>:<start time>`; otherwise the controlling tty or the user id.
A misattribution costs at most an extra or missing `reap` entry; explicit
names and `--all` are always available.

## Backends: tmux and process

`tmux` (default when installed): each child is the session `as-<name>` on the
socket recorded in its meta.json; `peek` captures the pane, a human can
attach, and a finished session lingers for `AGENT_SESSIONS_LINGER` seconds.
`process` (`--backend process`, `AGENT_SESSIONS_BACKEND=process`, or the
fallback when tmux is missing): the runner is started directly as a detached
process group (`start_new_session`), stdin from /dev/null, stdout and stderr to
`<name>/pane.log`, cwd the workdir, a minimal environment (PATH, HOME, user,
shell, locale, TMPDIR plus the `AGENT_SESSION(S)_*` variables; secrets still
travel through env.json). It ignores SIGHUP (closing the parent's terminal does
not stop it), keeps the SIGTERM handling, and exits right after the result and
the hook (no linger). Liveness, `kill`, `list`, `status`, `wait`, lost
detection, `resume` and `recover` work the same for both, from the runner's
pid and start time in progress.json and the engine's in engine.pid. A process
child whose runner died without a result is recorded `lost` with the reason
`runner process gone without a result`.

## State layout

```
$AGENT_SESSIONS_STATE/
  .lock                     # flock target for spawn, lost-detection and reap
  engine-versions.json      # cache of `<engine> --version`, keyed by binary path and mtime
  pauses/<engine>.json      # active pause after a rate or usage limit: until_epoch, reason, by, written_iso
  ledgers/<id>.json         # tree budget: id, owner, root, limit_usd, limit_tokens, spent_usd, spent_tokens, children, created_iso
  groups/<group>.json       # best-of-N groups and plans (kind, members, winner, selection, plan_file, ledger, per-node hashes)
  snapshots.jsonl           # where reap wrote snapshot refs, so `snapshots` finds them after the child is gone
  <name>/
    meta.json               # written once at spawn (version 3; includes tmux_socket, role, stall_s, schema_file, engine_version, prompt_sha256)
    env.json                # passthrough environment, 0600; deleted by the runner at start
    prompt.txt              # the opening prompt
    schema.json             # copy of --schema FILE
    progress.json           # rewritten by the runner on every event; runner_pid, runner_lstart, usage_so_far, cost_so_far, attempt, stall_deadline_epoch
    engine.pid              # present only while an engine process runs: pid, pgid, lstart, turn
    log                     # timestamped transcript
    events.jsonl            # one JSON object per event, every turn: ts, iso, turn, attempt, type, text, data
    inbox/                  # steerable: pending messages, <time_ns>-<id>
    messages/<id>           # steerable: the turn number that consumed message <id>
    turns/N.json            # per-turn record
    turns/N.events.jsonl    # raw engine stdout (stream-json or --json events)
    turns/N.stderr          # raw engine stderr
    turns/N.answer.txt      # the engine's final message for that turn
    turns/N.attempt<k>.*    # a failed attempt k of turn N kept before a retry (.json, .events.jsonl, .stderr, .answer.txt)
    result.json             # written once when the session ends (resume moves it to attempts/)
    attempts/<k>.result.json  # the result each resume replaced
    stop, kill, interrupt   # markers written by stop, kill and send --interrupt
    hooks.log, hook.done    # --on-finish output, and the marker that it ran for this result
    pane.log                # process backend: the runner's stdout and stderr
    verify.<k>.out          # output of verify run k (--verify)
    judge/                  # judge inputs: brief.md, answer.md, diff.patch, schema.json
    judge.json              # the stored verdict (references/schema.md)
    worktree/               # only with --worktree
```

Every JSON file is written to a temp file and renamed into place. State
directories are mode 0700.

## Engine command lines

Prompts always go to stdin. Each engine runs in its own process group with
stdout parsed live and stderr captured to `turns/N.stderr`. The runner logs
every command line (without the prompt) to the child's `log` as `cmd:`.

claude (verified against Claude Code 2.1):

```
claude -p --output-format stream-json --verbose --session-id <uuid> [--model M]
       [--json-schema <schema text>] [--max-budget-usd X] [--max-turns N] <permission flags>
claude -p --output-format stream-json --verbose --resume <uuid> [--model M] ... <permission flags>
```

The first form starts turn 1 (and every retry attempt, with a fresh uuid); the
second is a later steerable turn, a fixup turn or `resume`. Permission flags:
`--dangerously-skip-permissions`; read-only:
`--permission-mode dontAsk --tools Read,Grep,Glob --strict-mcp-config`.
`--max-budget-usd` is the smaller of `--budget-usd` and what the tree ledger
has left. The answer is the `result` event's `result` field; `session_id`,
`total_cost_usd`, `usage`, `modelUsage`, `num_turns` and `structured_output`
come from the same event.

codex (verified against codex-cli 0.162):

```
codex exec --json --skip-git-repo-check -o turns/N.answer.txt [-m M] [--output-schema <path>] <sandbox flags> -
codex exec resume <thread id> --json --skip-git-repo-check -o turns/N.answer.txt [-m M] [--output-schema <path>] <sandbox flags> -
```

Sandbox flags: `--dangerously-bypass-approvals-and-sandbox`; read-only:
`-c sandbox_mode="read-only"`. The `-c` form is used on every turn because
`codex exec resume` has no `--sandbox` option, while both forms accept the
config override. The thread id comes from the `thread.started` event; if that
event is missing, the runner takes the rollout file under `$CODEX_HOME/sessions`
for the same working directory written since the turn began, and logs that it
did so; when there is more than one (another codex run in that directory at the
same time) it takes none, so `send` fails rather than resume the wrong thread.
Codex's "resume the most recent session" shortcut is never used, because it can
pick up a conversation this child did not start. The answer is the `-o` file,
falling back to the last `agent_message` event; usage comes from
`turn.completed`.

gemini (**documented format only, not verified against the real CLI**):

```
gemini --output-format stream-json --approval-mode yolo --skip-trust [--model M] [--resume <session id>]
```

Read-only: `--approval-mode plan` in place of `yolo`. Headless mode is entered
because stdin is not a terminal. `--skip-trust` avoids exit 55 in an untrusted
folder. Events: `init` (session id), `message`, `tool_use`, `tool_result`,
`error`, `result` (`response`, `stats` for usage). Exit codes: 42 input error
(`engine_error`), 53 turn limit (`budget`), 55 untrusted folder
(`engine_error`). With `--schema`, the prompt gets a prefix asking for only a
JSON object that matches the schema. `engines` reports gemini with
`verified: false`.

## What --readonly does and does not do

It removes the engine's shell and edit tools and makes anything that would
prompt fail instead of hang. It is not a containment boundary; scope in the
prompt remains the first line of defence.

claude: `--tools Read,Grep,Glob` under `--permission-mode dontAsk`, plus
`--strict-mcp-config`. Verified against Claude Code 2.1: under `dontAsk`, a
read outside the child's working directory is denied, so a reviewer pointed at
a diff in `/tmp` or in a sibling repository fails silently. Put the material
inside `<dir>` (or name a scratch directory that holds it as `<dir>`), or skip
`--readonly`. `WebFetch` and `WebSearch` are not in the list because `dontAsk`
denies them unless the user has allow rules for them; a reviewer that needs the
web should run without `--readonly`. `--strict-mcp-config` with no
`--mcp-config` loads none of the user's MCP servers: a reviewer does not need
the user's database, cloud or browser tools, and their tool schemas are a large
share of a cheap turn's cost (a trivial haiku child with a typical set of user
MCP servers loaded cost $0.066; the live smoke's readonly haiku child, which
reads a file and answers, costs $0.002).

codex: the read-only sandbox via `-c sandbox_mode="read-only"`. Commands run,
but writes outside the sandbox's read-only policy are blocked by codex itself.
MCP servers from the user's codex config remain loaded.

## Testing

- `tests/run.sh` runs the offline suite against fake engines
  (`tests/fake-engine/`) on private tmux sockets; it spends no tokens and needs
  python3, tmux and git. It builds a private bin directory with only tmux,
  python3, git and ps and runs with `PATH=<fake engines>:<that dir>:/usr/bin:/bin`,
  so a GNU-only dependency (`timeout`, GNU `date`) fails loudly on macOS. It
  also runs `evals/check_scenarios.py`. Pass unittest arguments through
  (`tests/run.sh -k steerable`).
- `tests/run.sh --fast` runs only the quick classes (Cli, Contract, Parser,
  Schema) and the scenario checker.
- `tests/live-smoke.sh [--codex-model M] [--skip-codex] [--skip-claude] [--skip-judge]`
  runs the real engines with the smallest prompts in a private state root:
  one-shot and steerable children per engine, read-only children, a kill,
  structured output, a verify gate, a post-turn token budget, a cross-engine
  judge and `engines`, plus gemini steps when `gemini` is on PATH. It prints the
  total claude cost on its last line. It spends real tokens and is never run by
  `tests/run.sh`.
