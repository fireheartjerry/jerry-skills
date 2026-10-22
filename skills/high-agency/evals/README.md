# high-agency evals

Behavioral A/B scenarios and a routing set for the high-agency skill, with the
harness that runs them, so anyone can reproduce or challenge the numbers in
`results.md`. Needs stdlib Python 3 (tested on 3.9 and later; the code is
syntax-checked for 3.8 but not run there), bash 3.2, git and make; live runs
also need the `claude` or `codex` CLI. The live commands spend tokens; the
offline checks do not.

Terms used below: "phase 1" is the skill text at commit `64b1e16`; "phase 2"
is the measured run of 2026-10-21 (working tree on top of `f65aab6`); "v1" is
the first release, `414c914`. `p2-a`, `p2-b` and `phase1-control` are the
description candidates measured in phase 2 (`p2-a` ships); the files in
`descriptions/queued/` are candidates not yet measured.

## Files

| File | What it is |
|---|---|
| `README.md` | this file |
| `scenarios.json` | 41 behavioral scenarios (S01-S41): setup, user message, ideal, checks, failure modes, the skill rules each exercises (`rules`), `held_out` on S13, S14, S16-S18, S32 and S33. S24-S33 were written after phase 2 and have not run live: a hook that blocks a named commit, an investigation with a decoy, a shared dirty tree, a red baseline, an unstated-budget paid run, "keep going" with no plan, a rejected push, a flaky test, a JavaScript bug class and a log cleanup. S34-S41 were written after the fourth review and have not run live either: a delegated message grant with no text, a machine-level install, a harness-denied push, an unfixable failure, a widening CLAUDE.md line by another author, a headless request that needs a question, a production-only real path and a 25-site sweep |
| `routing.json` | 81 prompts labeled `fire`, `no_fire` (near-misses) or `either`, with categories. F27 to F31 and N27 to N42 were added after the phase-2 measurement (60 prompts then): the queued `v1-release` cue, investigation requests, a near-miss for every quoted cue of the shipped and queued descriptions (`route_check.py --offline` checks each candidate), knowledge questions, and prompts a sibling skill owns |
| `rubric.md` | check kinds, the verbatim judge prompt, pass rule, hard violations, routing metrics, ship rule, intervals |
| `results.md` | measured results of the phase-2 run (2026-10-21): routing per candidate, behavior per arm, variance, cost, limits |
| `descriptions/p2-a.txt`, `descriptions/p2-b.txt`, `descriptions/phase1-control.txt` | description candidates measured in phase 2 (`p2-a` ships) |
| `descriptions/queued/p3-a.txt`, `descriptions/queued/p3-b.txt`, `descriptions/queued/v1-release.txt`, `descriptions/queued/lead-cues.txt` | candidates not yet measured: two with the exclusion in the first 120 characters, the first release's "Use automatically for ..." text, and one that leads with the task shape as a load-time condition (more than a few steps, a verify command, a job that outlives a turn), followed by the cues that routed |
| `route_check.py` | routing measurement (live) and routing-set validation (`--offline`) |
| `run.py` | builds a fixture per cell, injects or installs the arm, runs the harness headless, grades |
| `grade.py` | auto checks plus optional judge checks for one run directory; writes `result.json` |
| `summarize.py` | per-arm report, Wilson intervals, trial agreement, ship rule |
| `fixture/build.sh` | builds the `ledger` fixture repo for a scenario: `build.sh <dest> [Sxx]`; the grader's ground truth goes to `<dest>.state/`, the bare origin to `<dest>.origin.git` |
| `fixture/probe.py` | deterministic probes against a fixture: `probe.py <check> [root]` |

Run output goes to `evals/.runs/` (gitignored by the skill's `.gitignore`).

## Offline checks (no tokens)

    python3 -I evals/route_check.py --offline
    fx="$(mktemp -d)/ha-fx"
    bash evals/fixture/build.sh "$fx" && (cd "$fx" && SLOWCHECK_SECONDS=1 make verify)
    python3 evals/fixture/probe.py class_sites "$fx"     # FAIL on the unfixed fixture, by design
    rm -rf "$(dirname "$fx")"                              # the fixture, its .origin.git and .state
    python3 evals/run.py --dry-run --arm new --scenario S02
    bash tests/run.sh                                     # includes tests/test_evals.py

Paths are relative to the skill directory. `--dry-run` builds the fixture in a
throwaway directory and prints the harness command without running it.
`build.sh` refuses to replace a `<dest>.origin.git` or `<dest>.state` it did
not create, and clears inherited git variables (`GIT_DIR`, `GIT_INDEX_FILE`
and the like), so a run started from a git hook or a linked worktree cannot
write into the host repository.

## Arms

- `none`: no skill text.
- `old`: `SKILL.md` at `--baseline-rev` (default `64b1e16`, the phase-1 text;
  `v1` means `414c914`) injected with `--append-system-prompt-file`; no references.
- `new`: the working-tree `SKILL.md` injected; `references/` copied beside the fixture.
- `installed`: nothing injected; the skill directory (without `tests/` and
  `evals/`) is installed at `<fixture>/.claude/skills/high-agency` (Codex:
  `$CODEX_HOME/skills/high-agency`), so the description must route to it.

## Reproduce

Before running anything here: agent cells run unsandboxed with your user
privileges, and four scenarios plant prompt-injection text on purpose (see
Security); use a container, a VM or a throwaway user. The phase-2 run cost
about $73 of Claude usage (`results.md`, "Cost and time"); the routing set has
grown since, so the same commands now cost more (below).

Commands marked `# live` spend tokens; the Claude Code ones pass
`--model claude-opus-5-5`, the model behind the published numbers (swap in
your own to compare models). The Codex run uses the CLI's default model.
`summarize.py`, `route_check.py --summarize` and `grade.py --reuse-judge`
spend nothing; `grade.py --judge claude` spends judge tokens.

Routing, three measured description candidates, 3 trials per prompt (Claude
Code). Without `--ids` each command runs all 81 prompts (243 cells, about
$16.50 per candidate by `--offline`); its rates are not comparable to the
phase-2 tables, which cover 60 prompts, so compare per prompt or rerun the
phase-2 grid with `--ids` (180 cells, about $12.24 per candidate):

    python3 evals/route_check.py --harness claude --model claude-opus-5-5 --trials 3 --jobs 4 --description-file evals/descriptions/p2-a.txt             # live
    python3 evals/route_check.py --harness claude --model claude-opus-5-5 --trials 3 --jobs 4 --description-file evals/descriptions/p2-b.txt             # live
    python3 evals/route_check.py --harness claude --model claude-opus-5-5 --trials 3 --jobs 4 --description-file evals/descriptions/phase1-control.txt   # live
    python3 evals/route_check.py --summarize evals/.runs/routing

The phase-2 grid alone (F01 to F26, N01 to N26, E01 to E08), for one
candidate; give it its own `--label` so it does not share cells with a full run:

    python3 evals/route_check.py --harness claude --model claude-opus-5-5 --trials 3 --jobs 4 --description-file evals/descriptions/p2-a.txt --label p2-a-phase2 --ids F01,F02,F03,F04,F05,F06,F07,F08,F09,F10,F11,F12,F13,F14,F15,F16,F17,F18,F19,F20,F21,F22,F23,F24,F25,F26,N01,N02,N03,N04,N05,N06,N07,N08,N09,N10,N11,N12,N13,N14,N15,N16,N17,N18,N19,N20,N21,N22,N23,N24,N25,N26,E01,E02,E03,E04,E05,E06,E07,E08   # live

The queued candidates run the same way with
`--description-file evals/descriptions/queued/<name>.txt`. Add `--siblings`
to install the other skills of this repository beside high-agency (label
suffix `-siblings`): a prompt another skill owns can then route there, and the
cell records which skill loaded. Without it the false-fire rate is a
single-skill number.

Behavior, targeted set at 3 trials, the rest at 1:

    python3 evals/run.py --model claude-opus-5-5 --arm none,new,installed --scenario S02,S03,S05,S06,S08,S12,S19,S20,S21,S22 --trials 3 --jobs 3 --judge claude   # live
    python3 evals/run.py --model claude-opus-5-5 --arm none,new,installed --scenario S01,S04,S07,S09,S10,S11,S13,S14,S15,S16,S17,S18 --trials 1 --jobs 3 --judge claude   # live
    python3 evals/run.py --harness codex --arm installed --scenario S05,S23 --trials 1 --judge claude --codex-auth <path to your Codex auth.json>   # live

The scenarios written after phase 2 (S24-S33; not part of the published
numbers), one trial each for a first calibration pass, about $8 for the
three arms at the per-cell medians below (30 cells at $0.20, 36 judge calls):

    python3 evals/run.py --model claude-opus-5-5 --arm none,new,installed --scenario S24,S25,S26,S27,S28,S29,S30,S31,S32,S33 --trials 1 --jobs 3 --judge claude   # live
    python3 evals/run.py --model claude-opus-5-5 --arm none,new --scenario S34,S35,S36,S37,S38,S39,S40,S41 --trials 1 --jobs 3 --judge claude   # live

The short always-on form (`core`, which injects `../core.md`) and the
project-instruction pointer (`pointer`, which injects the README's pointer
paragraph and installs the skill) against the shipped body, on the scenarios
that decided the phase-2 outcomes:

    python3 evals/run.py --model claude-opus-5-5 --arm new,core,pointer --scenario S08,S12,S15,S21,S24,S26,S28,S30,S33 --trials 3 --jobs 3 --judge claude   # live
    python3 evals/summarize.py --routing evals/.runs/routing/p2-a --markdown
    python3 evals/summarize.py --harness codex

Re-grade one cell after a judge change (spends judge tokens), or apply a
grader fix to stored cells while keeping their judge verdicts (spends nothing):

    python3 evals/grade.py evals/.runs/claude/new/S02-bug-class/1 --judge claude --judge-model sonnet   # live
    python3 evals/grade.py evals/.runs/claude/new/S02-bug-class/1 --reuse-judge

`run.py` options: `--arm` (default `none,new`; also `old`, which needs the
repository's git history, `core`, `pointer` and `installed`), `--scenario`,
`--trials`, `--jobs`, `--model`, `--timeout` (default 900 s), `--judge` and
`--judge-model` (default `sonnet` with `--judge claude`), `--baseline-rev`,
`--skill-dir` with `--arm-suffix` for a variant skill directory, `--runs-dir`,
`--codex-auth`, `--agent-settings`, `--codex-sandbox`, `--force` to re-run
graded cells, `--dry-run`; `--flag=value` works. A live run first prints the
cell count and a cost estimate from the medians below. A HARNESS ERROR cell
(usage limit, API error, no output) is not graded and runs again next time; a
grader that crashes or writes no `result.json` is an ERROR; either makes
`run.py` exit 1. A graded cell whose recorded skill SHA-256 or `--model`
differs from the run is skipped and reported as STALE (`--force` re-runs it).
Ctrl-C, SIGTERM or SIGHUP stops the grid at any `--jobs`: no new cell starts,
running agents and graders are killed, temp credential copies are removed, and
the run exits 130.

`route_check.py` options (`--help` lists defaults): `--harness`, `--model`,
`--trials` (3), `--jobs` (4), `--description-file`, `--label` (default: the
description file's base name, so `--routing` takes
`evals/.runs/routing/<label>`), `--out` (`evals/.runs/routing`), `--only`
(`fire`, `no_fire` or `either`), `--ids F01,N03`, `--timeout` (180 s),
`--codex-auth`, `--siblings`, `--force`, `--dry-run`. A cached cell is reused
only when its recorded harness, model, description (SHA-256 of the candidate,
or of the SKILL.md description) and `--siblings` setting match the run, so an
edit to the SKILL.md description re-runs its cells; errored and timed-out
cells are not recorded and make the run exit 1. Give each harness its own
`--label`; the summary warns when one label mixes harnesses.

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
`--dangerously-bypass-approvals-and-sandbox`. S08, S15, S18 and S33 plant
prompt-injection text on purpose. Nothing confines the agent to its temp
fixture; it can read your home directory, use your SSH, git and `gh`
credentials, and reach the network. Run the grid in a container, a VM or as a
throwaway user with no credentials beyond the harness login.

Two opt-in flags narrow this, and neither was used for the published numbers:
`run.py --agent-settings FILE` passes a Claude Code settings file (for example
one that enables its sandbox), and `--codex-sandbox workspace-write` replaces
the Codex bypass flag. Both can block pushes to the fixture's bare origin,
which sits next to the fixture, not inside it. S12, S16 and S30 need a push
and then fail. In S18, S21 and S38 a push is the violation, so a blocked push
makes `no_push` pass without measuring consent, and in S30 it makes
`no_force` and `remote_commit_kept` pass the same way; do not read those
results as a consent measurement. S36 carries its own deny rule in the
fixture's `.claude/settings.json`.

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
  removed when the cell ends and, on Ctrl-C, SIGTERM or SIGHUP, by the main
  thread at any `--jobs` (only a SIGKILL of `run.py` itself skips that); the
  Codex judge gets an empty `CODEX_HOME` and its own short-lived copy
  (`grade.py --codex-auth`) in a temp directory outside the cell, which
  `run.py` removes itself when it kills the grader.
- Each agent runs in its own process group. When it exits or times out,
  `run.py` kills the group and any process still working inside the cell (a
  detached benchmark from S05 or S20) before grading.
- Every cell builds its own fixture or temp project. That bounds what the
  grader reads, not what the agent can touch (see Security). A missing
  `claude` or `codex` binary stops the run with a message naming what to
  install.
