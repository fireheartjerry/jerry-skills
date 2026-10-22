# Spawning an independent reviewer or scorer

The reviewer and the scorer are only worth what their independence is worth. Five
tiers, best first:

| Tier | Independence | How |
|---|---|---|
| Separate agent CLI | Different model, separate process, isolated config, staged copy | `scripts/spawn_child.sh codex ...` (or `claude`) |
| Same CLI, isolated process | Same model, but a fresh process with no shared context, the same isolation flags and the same post-run checks | `scripts/spawn_child.sh <your engine> ... --same-engine` |
| Subagent | Same model, same session; isolation only as good as the path you hand it | your harness's subagent tool, pointed at a staged dir (recipe below) |
| kimi (experimental) | Different model, but no sandbox and no allowlist: the child can read, write and run anything as you. Not verified: its only live preflight failed (HTTP 403) | `scripts/spawn_child.sh kimi reviewer ... --unsandboxed`; trusted artifacts only |
| Inline | None: you are grading your own work | last resort; disclose it in the report |

A different CLI is usually worth the extra minute: it is the best-isolated option whose
blind spots differ from yours, and when two different models flag the same thing, that
finding is real. The same-engine tier is still a step up from a subagent: the child
cannot see your conversation, notes or changed files, and a modified artifact is
detected. Say which tier ran in the report. **Independence is a property of what the
child could see, not of which tool you used**: check the review cites real evidence
before acting on it (SKILL.md section 4).

How the script isolates a child (flags, layers, what was verified on which CLI
version) is in `references/isolation.md`; no loop step needs it.

## Before the first spawn: disclose and check

Every child run is a billed model call, and it sends a copy of the snapshot's files to
that engine's provider. The proposal the user answers `ok` to names the engine and its
vendor, the files in the snapshot (from `snapshot.py create --dry-run`), and the child
runs (lite: 2 preflights plus 3 to 7 runs). An engine from a vendor the user has not
already chosen needs that yes. Re-ask only when the plan grows: an engine switch or
rounds beyond the agreed ceiling.

```
scripts/spawn_child.sh --check                       # offline, free: CLIs, versions, auth, TMPDIR
scripts/spawn_child.sh --preflight codex --model <model id>
scripts/spawn_child.sh --preflight claude --model <model id>   # add --same-engine inside Claude Code
scripts/spawn_child.sh --preflight kimi --unsandboxed          # only if you accept kimi's risk
```

`--check` prints one ok or FAIL line per item and an install hint for a missing CLI.
`--preflight` runs `--check` for that engine first, then a one-file stub snapshot
through the full staged path, and checks the child read the staged file:
`preflight ok: codex (codex-cli 0.163.0) as scorer, model gpt-5, 41s`. Anything else
(login prompt, auth error, timeout, empty output) means that engine is unavailable at
that configuration: drop a tier and record which tier you used. An installed but
unauthenticated binary otherwise fails silently and reads like a clean review. Codex
needs `auth.json` in its home; credentials kept in the OS keyring
(`cli_auth_credentials_store`) are invisible to the isolated home, so switch that
setting to file for the loop or use another engine.

**Choose `--model` as a full model id**, never an alias such as `sonnet` that moves to
a new model without changing the `.cmd`. For Codex, the `model` line in
`~/.codex/config.toml` names the current default; for Claude, use the full id shown in
its model list. The scorer's model is part of the pin.

**Never spawn the CLI you are running without saying so.** The script refuses it
(exit 5; it checks `CLAUDECODE`, `CODEX_SANDBOX` and `CODEX_THREAD_ID`) unless you pass
`--same-engine`, which it records in the `.cmd` file.

## Running a child

```
scripts/spawn_child.sh ENGINE ROLE PROMPT SNAPSHOT OUT [--evidence DIR] [--model M]
    [--loop-dir DIR] [--timeout S] [--max-usd N] [--same-engine] [--unsandboxed]
    [--background] [--dry-run]
```

**A child run is a long job**: 6 to 10 minutes is normal, and harnesses stop a
foreground shell call sooner (Claude Code's Bash tool at 10 minutes at most). Always
pass `--background` (or use the harness's background mode): it checks the arguments,
detaches the run and returns. Wait for `OUT` minus `.md` plus `.status` (the exit code
below; `.spawn` holds the messages), or poll it; `loop.py status` lists unfinished
runs; `kill <pid from .pid>` stops one cleanly. Raise `--timeout` only together with
this. A killed caller cannot leave a billing engine behind: the wrapper polls for its
parent and kills the engine's process group.

Use it rather than typing the flags; `--dry-run` prints the staging plan and the exact
argv and creates nothing. A scorer requires `--model`; kimi is reviewer-only and needs
`--unsandboxed`; an empty snapshot is refused (exit 3). Children never run
write-dependent commands (builds, dev servers, formatters): you run those before
spawning and hand the results over with `--evidence` (SKILL.md section 4, Evidence).
The script gives every child `/dev/null` as stdin.

**Children cannot reach your servers.** A Codex child has no network and a Claude child
has no fetch tool, so a URL in the brief is unreachable for both; only kimi can reach
localhost. Give them static views instead: the built output folder, and PNG or PDF
captures at named widths, listed by file name in the brief. The view the reviewer gets
comes from `evidence.py view` (no manifest, no check ids in file names).

**Not every child can see an image.** A claude child opens PNGs with its Read tool;
codex has a `view_image` tool whose availability depends on version and config; kimi
is unknown. Before handing an engine captures, run `--preflight ENGINE --vision`: the
child must name the colour of a staged PNG. On failure give that engine text evidence
(axe JSON, a DOM text dump, measured sizes) or another engine for visual criteria;
otherwise it marks the criterion N/A or describes a picture it never saw.

## What each engine enforces

| Engine | Reads | Writes | Commands | Enforced by |
|---|---|---|---|---|
| claude | only under the stage | only `out.md` in the stage | none (no Bash tool) | CLI permission rules under `dontAsk` |
| codex | anything the user can read, including `~`, LOOP and the repo | none (codex writes the output itself) | read-only sandbox, no network | OS sandbox |
| kimi | anything | anything | anything | nothing; the post-run checks see only the stage and ROOT |

So a Codex child that goes looking can find prior scores in LOOP or a secret in `~`;
its blindness to LOOP rests on the prompt naming no such path. Prefer a Claude child
when blindness to LOOP matters more than a different model, and for artifacts you did
not write. Read every child output before acting on it or showing it to anyone.

## After every child run

| Exit | Meaning | Do |
|---|---|---|
| 4, "child modified the staged artifact" | the child wrote to its copy | rerun once; a second time, switch engine. A kimi discard means kimi wrote: replace it |
| 4, "the artifact's files in ROOT changed" | someone or something edited the snapshot's files (listed) during the run; edits elsewhere in the repo never count | let the edits settle, re-snapshot if the artifact changed, rerun; switching engine does not help |
| 6 | no output file | read the `.log`; usually an auth or model error |
| 7, "timed out" | the child hit `--timeout` (default 900 s) and its process group was killed | raise `--timeout` once (for example 1800), or narrow the snapshot with `--path`, then switch engine |
| 7, other | the CLI exited non-zero | read the `.log` |

A discarded output is kept as `OUT.discarded` and the incident goes to `rounds.md`.
Do not edit the artifact while a child runs; prepare the triage meanwhile.

## Records and the pin

The script writes the argv to `OUT` minus `.md` plus `.cmd`, with the stage path
replaced by `/<stage>` and a header naming the engine version, model and same-engine
flag; no timing or timeout goes in it. The scorer's `round-0a.cmd` (lite:
`round-0.cmd`) is the pin, and every later scorer `.cmd` must be byte-identical to it
apart from the engine's patch version: `loop.py status` lists any that differ. A
different model, flag, brief or CLI major.minor version is a new instrument: rescore
the previous snapshot under it before comparing. The `.log` ends with `# timeout`,
`# started`, `# finished`, `# duration: Ns` and `# usage:`; Codex prints `tokens used`
and the count, and the footer copies it (`# usage: tokens used: 19,783`). `--max-usd
N` gives a claude child a hard spend cap and JSON output, and the footer then reads
`cost USD 0.4123`; claude without it and kimi print no usage (`not reported`). Money is
measurable only when every child is claude under `--max-usd`; otherwise budget in
time.

**Give it a way in.** The reviewer starts with nothing: name the entry file and what
is under `evidence/`. A reviewer that spends its budget fighting the environment
returns an empty review, which reads like good news. **Verify it actually looked**: no
file:line citations and no quoted evidence means it never reached the artifact.

## Subagent recipe (when `spawn_child.sh` cannot be used)

Build the stage yourself: copy the snapshot to a fresh temp dir outside any repo, and
rename agent instruction files in it so the subagent reads them as data (`find STAGE
\( -iname CLAUDE.md -o -iname AGENTS.md -o -iname GEMINI.md -o -iname .cursorrules -o
-iname .claude -o -iname .codex \) -exec mv {} {}.as-data \;`, deepest first), and list
the renames in the brief. Put the brief beside the copy, give the subagent only that
directory, forbid reads outside it, and run `snapshot.py seal` before and
`snapshot.py verify` after. Never hand it ROOT, LOOP or a path naming the round.

## When only one other CLI is available

Run both roles on that CLI in separate processes: the scorer still cannot see the
review. If the only CLI is the one you are running, use `--same-engine` for both and
say so. With no CLI, run subagents on staged dirs; with neither, run both inline and
say so in the report. The numbers then remain a relative trend, not an absolute.
