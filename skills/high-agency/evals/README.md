# high-agency evals

Behavioral A/B scenarios and a routing set for the high-agency skill, with the
harness that runs them, so anyone can reproduce or challenge the numbers in
`results.md`. Stdlib Python (3.8+), bash 3.2 and git only. The live commands
spend tokens; the offline checks do not.

## Files

| File | What it is |
|---|---|
| `README.md` | this file |
| `scenarios.json` | 23 behavioral scenarios (S01-S23): setup, user message, ideal, checks, failure modes, the skill rules each exercises (`rules`), `held_out` on S13-S18 |
| `routing.json` | 61 prompts labeled `fire`, `no_fire` (near-misses) or `either`, with categories (F27 was added after the phase-2 measurement so the queued `v1-release` candidate's cue is covered) |
| `rubric.md` | check kinds, the verbatim judge prompt, pass rule, hard violations, routing metrics, ship rule, intervals |
| `results.md` | measured results of the phase-2 run (2026-10-21): routing per candidate, behavior per arm, variance, cost, limits |
| `descriptions/p2-a.txt`, `descriptions/p2-b.txt`, `descriptions/phase1-control.txt` | description candidates measured in phase 2 (`p2-a` ships) |
| `descriptions/queued/p3-a.txt`, `descriptions/queued/p3-b.txt`, `descriptions/queued/v1-release.txt` | candidates not yet measured: two with the exclusion in the first 120 characters, and the first release's "Use automatically for ..." text |
| `route_check.py` | routing measurement (live) and routing-set validation (`--offline`) |
| `run.py` | builds a fixture per cell, injects or installs the arm, runs the harness headless, grades |
| `grade.py` | auto checks plus optional judge checks for one run directory; writes `result.json` |
| `summarize.py` | per-arm report, Wilson intervals, trial agreement, ship rule |
| `fixture/build.sh` | builds the `ledger` fixture repo for a scenario: `build.sh <dest> [Sxx]` |
| `fixture/probe.py` | deterministic probes against a fixture: `probe.py <check> [root]` |

Run output goes to `evals/.runs/` (gitignored by the skill's `.gitignore`).

## Offline checks (no tokens)

    python3 -I evals/route_check.py --offline
    bash evals/fixture/build.sh "$TMPDIR/ha-fx" && (cd "$TMPDIR/ha-fx" && SLOWCHECK_SECONDS=1 make verify)
    python3 evals/fixture/probe.py class_sites "$TMPDIR/ha-fx"     # FAIL on the unfixed fixture, by design
    python3 evals/run.py --dry-run --arm new --scenario S02
    bash tests/run.sh                                               # includes tests/test_evals.py

Paths are relative to the skill directory. `--dry-run` builds the fixture in a
throwaway directory and prints the harness command without running it.

## Arms

- `none`: no skill text.
- `old`: `SKILL.md` at `--baseline-rev` (default `64b1e16`, the phase-1 text;
  `v1` means `414c914`) injected with `--append-system-prompt-file`; no references.
- `new`: the working-tree `SKILL.md` injected; `references/` copied beside the fixture.
- `installed`: nothing injected; the skill directory (without `tests/` and
  `evals/`) is installed at `<fixture>/.claude/skills/high-agency` (Codex:
  `$CODEX_HOME/skills/high-agency`), so the description must route to it.

## Reproduce

Every command below spends tokens. Each passes `--model claude-opus-5-5`, the
model behind the published numbers; swap in your own to compare models.

Routing, three measured description candidates, 3 trials per prompt (Claude Code):

    python3 evals/route_check.py --harness claude --model claude-opus-5-5 --trials 3 --jobs 4 --description-file evals/descriptions/p2-a.txt
    python3 evals/route_check.py --harness claude --model claude-opus-5-5 --trials 3 --jobs 4 --description-file evals/descriptions/p2-b.txt
    python3 evals/route_check.py --harness claude --model claude-opus-5-5 --trials 3 --jobs 4 --description-file evals/descriptions/phase1-control.txt
    python3 evals/route_check.py --summarize evals/.runs/routing

The queued candidates run the same way with
`--description-file evals/descriptions/queued/<name>.txt`.

Behavior, targeted set at 3 trials, the rest at 1:

    python3 evals/run.py --model claude-opus-5-5 --arm none,new,installed --scenario S02,S03,S05,S06,S08,S12,S19,S20,S21,S22 --trials 3 --jobs 3 --judge claude
    python3 evals/run.py --model claude-opus-5-5 --arm none,new,installed --scenario S01,S04,S07,S09,S10,S11,S13,S14,S15,S16,S17,S18 --trials 1 --jobs 3 --judge claude
    python3 evals/run.py --harness codex --arm installed --scenario S05,S23 --trials 1 --judge claude --codex-auth <path to your Codex auth.json>
    python3 evals/summarize.py --routing evals/.runs/routing/p2-a --markdown
    python3 evals/summarize.py --harness codex

Re-grade one cell after a judge change (spends judge tokens), or apply a
grader fix to stored cells while keeping their judge verdicts (spends nothing):

    python3 evals/grade.py evals/.runs/claude/new/S02-bug-class/1 --judge claude --judge-model sonnet
    python3 evals/grade.py evals/.runs/claude/new/S02-bug-class/1 --reuse-judge

`run.py` options: `--arm` (default `none,new`; `old` needs the repository's git
history), `--scenario`, `--trials`, `--jobs`, `--model`, `--timeout` (default
900 s), `--judge` and `--judge-model` (default `sonnet` with `--judge claude`),
`--baseline-rev`, `--skill-dir` with `--arm-suffix` for a variant skill
directory, `--runs-dir`, `--codex-auth`, `--agent-settings`, `--codex-sandbox`,
`--force` to re-run graded cells, `--dry-run`. A HARNESS ERROR cell (usage
limit, API error, no output) is not graded and runs again next time; any such
cell makes `run.py` exit 1.

`route_check.py` options (`--help` lists defaults): `--harness`, `--model`,
`--trials` (3), `--jobs` (4), `--description-file`, `--label` (default: the
description file's base name, so `--routing` takes
`evals/.runs/routing/<label>`), `--out` (`evals/.runs/routing`), `--only`
(`fire`, `no_fire` or `either`), `--ids F01,N03`, `--timeout` (180 s),
`--codex-auth`, `--force`, `--dry-run`. A cached cell is reused only when its
recorded description and model match the run; errored and timed-out cells are
not recorded and make the run exit 1.

## Cost and time per cell

Measured on Claude Code 2.1.306 with claude-opus-5-5 in the phase-2 run
(2026-10-21; 540 routing cells, 126 behavior cells). Expect drift with other
models and versions.

| Cell | Cost | Wall |
|---|---|---|
| Routing prompt, 2 turns | $0.06 to $0.09 (median $0.065) | 4 to 13 s |
| Behavior, most scenarios | $0.10 to $0.39 (median $0.20) | median 1.5 min (7 s to 5 min) |
| Behavior, S05, S10, S20 (long job or slow verify) | $0.15 to $0.35 (median $0.24) | median 5.3 min (up to 6 min) |
| Behavior, Codex S05 or S23 | tokens only (about 0.9M input, mostly cached) | 6 to 7 min |
| Judge call, sonnet (one per judge check) | about $0.04 | 2 to 3 s |

`route_check.py --offline` prints the cell count and cost estimate for a grid.

## Security

Behavior cells run the agent unsandboxed with your full user privileges:
Claude with `--permission-mode bypassPermissions`, Codex with
`--dangerously-bypass-approvals-and-sandbox`. S08, S15 and S18 plant
prompt-injection text on purpose. Nothing confines the agent to its temp
fixture; it can read your home directory, use your SSH, git and `gh`
credentials, and reach the network. Run the grid in a container, a VM or as a
throwaway user with no credentials beyond the harness login.

Two opt-in flags narrow this, and neither was used for the published numbers:
`run.py --agent-settings FILE` passes a Claude Code settings file (for example
one that enables its sandbox), and `--codex-sandbox workspace-write` replaces
the Codex bypass flag. Both can block the pushes in S12, S16, S18 and S21,
because the fixture's bare origin sits next to the fixture, not inside it.

Routing cells need only the first decision and get no write access: Claude
runs in the default permission mode (a headless run is refused any tool that
needs approval; the attempt still counts as fired), Codex with `-s read-only`.

## Isolation of context

- Claude cells: `--setting-sources project` keeps the user's own skills,
  CLAUDE.md and memory out; `--strict-mcp-config` with no config drops MCP
  servers; `--no-session-persistence` leaves no session behind.
  `CLAUDE_CONFIG_DIR` is left as you set it, so the run uses your login (on
  macOS the token lives in the Keychain under that config directory).
- Codex cells: `CODEX_HOME` points at a per-cell directory outside the
  project, holding only the arm's skill (and `auth.json` when `--codex-auth`
  is passed; otherwise set `CODEX_API_KEY`), plus `--ignore-user-config`.
  Codex 0.162.1 still reads user-level skills from `~/.agents/skills`, so a
  machine with skills there leaks them into Codex cells (seen in the phase-2
  run); move that directory aside for a clean Codex measurement.
  That `CODEX_HOME` is a `mkdtemp` directory outside the runs directory,
  removed in a `finally` block, so the auth copy does not survive Ctrl-C or a
  crash; the Codex judge gets an empty `CODEX_HOME` and its own short-lived
  copy (`grade.py --codex-auth`).
- Each agent runs in its own process group. When it exits or times out,
  `run.py` kills the group and any process still working inside the cell (a
  detached benchmark from S05 or S20) before grading.
- Every cell builds its own fixture or temp project. That bounds what the
  grader reads, not what the agent can touch (see Security). A missing
  `claude` or `codex` binary stops the run with a message naming what to
  install.

## Limits

- The fixture is synthetic, small, in Python, and shared by every scenario.
- Judge checks depend on a model and on check texts calibrated against
  phase-1 runs (see `rubric.md`); report the judge model with any number.
- Routing is measured in a 2-turn cap: the first decision only. Plain
  multi-step imperatives and long-job requests routed 0% for every measured
  description, so on such tasks invoke the skill by name (`/high-agency` in
  Claude Code; in Codex, name the skill in the request) or load it from
  project instructions.
- Behavior cells run under `claude -p` / `codex exec`. The skill no longer
  treats a headless start alone as a delegated-mode signal, but the phase-2
  runs measured text that did, which may have pushed agents toward not asking
  in S07, S08 and S15.
- The scenarios were written by the skill's author; S13-S18 are held out
  (no surface detail shared with the skill's examples) to bound that bias.
- Trial counts are small (1 to 3 per cell); use the Wilson intervals.
- Codex grading maps `command_execution` events to shell calls; checks that
  depend on Claude Code tool names (for example `run_in_background`) can only
  pass through their shell equivalents there.
