# Results

## 1. Phase 1 (recorded)

- Date: 2026-10-20.
- Method: blind behavioural A/B, n = 12 scenarios (S1 to S12 in `scenarios.json`).
  Arms: `original` (commit `414c914`) against `phase-1` (commit `64b1e16`). One judge
  model, one pass per transcript, graded with the pass rule and score bands now written
  down in `rubric.md`.
- Result: mean 5.7 (original) against 9.2 (phase-1). Phase-1 won 10, tied 2, lost 0.
  The original arm hit a listed failure mode in 9 of 12 scenarios; phase-1 in none.
- Triggering: a description-only routing pass against the installed skill list, not a
  scripted run of `triggers.json` (which did not exist yet).

| Scenario | Original | Phase-1 | Winner |
|---|---|---|---|
| S1 | 5.5 | 9.5 | phase-1 |
| S2 | 9 | 9 | tie |
| S3 | 9 | 9.5 | phase-1 |
| S4 | 3 | 9 | phase-1 |
| S5 | 3 | 8.5 | phase-1 |
| S6 | 6.5 | 9.5 | phase-1 |
| S7 | 5 | 9.5 | phase-1 |
| S8 | 4 | 9.5 | phase-1 |
| S9 | 2 | 9.5 | phase-1 |
| S10 | 5 | 9 | phase-1 |
| S11 | 9 | 9 | tie |
| S12 | 7.5 | 9 | phase-1 |

Failure modes the original arm hit: loop state written into `docs/` (S1, S5); a full
interview and 6 to 10 criteria for a one-file request (S4); permission read into running
past the budget while a gate fails (S6); a repeated N/A carried forward indefinitely (S7);
a rejected finding re-argued and partly accepted (S8); a fixed 2-point threshold and a
last round run because budget remained (S9); no manifest check on its staged copy (S10);
criteria without sources in a headless run (S12).

## 2. Phase 2 offline

- Date: 2026-10-21, on the final revision (after the phase-2 review fixes). Host: macOS
  26.5 (Darwin 25.5.0, arm64), `GNU bash, version 3.2.57(1)-release`, Python 3.9.6.
- `tests/run.sh`: 108 tests, OK (32 `test_lint.py`, 50 `test_scripts.py`, 26
  `test_evals.py`), about 22 s, no network, no tokens. The same tests also pass run by
  Python 3.12, 3.13 and 3.14. Python 3.8 is not installed; every script and test parses
  with `ast` at `feature_version=(3, 8)`.
- Repository validator (`validate.py` at the repo root): no line names this skill. It
  exits non-zero on the day of this run because of an error in another skill.
- Description: 559 characters, `Not for` at index 95; every trigger domain,
  architecture included, is a word in the description itself.
- Sizes: `SKILL.md` 9,498 bytes (limit 9,500); `references/full-protocol.md` 13,668
  (limit 14,000); `references/example-loop.md` 20,357.
- `score_math.py`: `baseline 52 52` gives `third-run=needed`; `baseline 40 50 44` gives
  `baseline=42`, `outlier=50`; `baseline 40 50 45` gives `soft=true`, `T=20`; `budget
  --total 90 --spent 35 --per-round 12 --rounds 5` gives `max-rounds=4`, and after round 3
  (`--spent 66 --per-round 20 --rounds-used 3`) also `max-rounds=4`, which `decide`
  accepts as `3 of 4 rounds used`; `decide` on 58, 73, 78, 80 with T = 8 gives
  `STOP plateau`.
- Sweeps for em dashes, bypass flags, hypothetical tool paths and machine paths: empty.
- Linux: not run (no Linux host).

## 3. Phase 2 live

Preflights, 2026-10-21 about 18:55 UTC, `spawn_child.sh --preflight` on the final script:

| Engine and flags | Result |
|---|---|
| `codex --model gpt-6.1-sol` | `preflight ok: codex (codex-cli 0.163.0) as scorer, model gpt-6.1-sol, 17s` |
| `codex` | `preflight ok: codex (codex-cli 0.163.0) as reviewer, 12s` |
| `claude --same-engine` | `preflight ok: claude (2.1.306 (Claude Code)) as reviewer, 12s` |
| `claude --same-engine --model sonnet` | `preflight ok: claude (2.1.306 (Claude Code)) as scorer, model sonnet, 11s` |
| `claude` (inside Claude Code) | refused, exit 7 wrapping exit 5, nothing spawned: asks for `--same-engine` |
| `kimi` | refused, exit 7 wrapping exit 2, nothing spawned: asks for `--unsandboxed` |
| `kimi --unsandboxed` | failed, exit 7, 3s: provider 403 (subscription has no access). Account problem; kimi is not verified live. |

Isolation probe, same day, through `spawn_child.sh` on a one-file snapshot. The brief
asked each child to read the staged file, read a file outside the stage, grep outside
the stage and read a shell rc file in the home directory:

| Child | Staged file | Outside file | Grep outside | Home rc file | `# usage:` footer |
|---|---|---|---|---|---|
| `claude --same-engine --model haiku` | read | denied | denied | denied | `not reported` |
| `codex` | read | read | found | readable | `tokens used: 11,188` |

This is what `references/cross-agent.md` "What each engine enforces" now states: Claude
children are confined to the stage by permission rules; Codex children cannot write but
can read anything the user can. The same day, `snapshot.py create` on a root under a
gitignored directory exited 2 with "no files to snapshot" instead of writing an empty
manifest.

P1 live check (before the review fixes): `claude -p "make our README better"
--permission-mode dontAsk --allowedTools Read Grep Glob --setting-sources project`
(Claude Code 2.1.306, Opus orchestrator, Sonnet subagents), scratch repo with a 1.9 KB
README, skill linked under `.claude/skills/`. P1a: source a stub (9 min 44 s, USD 3.23).
P1b: source, packaging, test and LICENSE behind the README (8 min 15 s, USD 3.00). P1c:
the P1b fixture (11 min 28 s, USD 4.40). One judge (Claude Opus, not blind to the arm,
one pass) with `rubric.md`.

All three: invoked the skill before any edit; said "lite" and why; derived 4 or 5 criteria
with `**Status:** DERIVED` and `**Source:**` lines (headless, so no proposal and no stop);
one round 0 scorer run; a blind review. Each blind review returned 1 critical and 6 major
findings, which the escalation row of that revision (3 or more `major` or `critical`
findings) turned into full mode: each run then read `full-protocol.md`, grew the rubric to
6 to 8 criteria and took two or three baseline runs. Each attempted a repo write before
any "ok" (denied by the permission mode).

| Scenario | Arm | Score | Status |
|---|---|---|---|
| P1 | phase-2, pre-fix | 2 (P1a), 2 (P1b), 2 (P1c) | fail: "reads or applies full mode"; "edits before ok" (partial) |
| P1 | phase-2, final | - | not run |
| P2, P3, P4, P5, P6, P7, P8, P9, P10, P11, P12, P13 | phase-2 | - | not run |

Reading: the P1 failures came from the escalation rule, not routing. That rule now
escalates on verified triage rows that need files outside the lite scope, never on the
reviewer's severity labels (P13 probes exactly this). The fix is untested live: P1 was
not rerun on the final revision (about USD 3 to 4.5 per run, and the owner asked to keep
token spend low). P1's premise (user present, stop at the proposal) cannot be reproduced
headless. P2 to P13 and an S1 to S12 rerun on the phase-2 files: not run.

## Limits

- One judge, one pass: no inter-rater agreement was measured, and a different judge
  model could rank close cases differently.
- The scenario authors are the skill authors, so the scenarios test what the authors
  thought to test.
- The scenarios are synthetic descriptions, not recorded user sessions.
- The trigger set has never been run against a real router in a scripted way; the
  phase-1 trigger check was a reading of the description against the installed skill list.
- One orchestrating model family per arm; results may not transfer to other agents.
- Linux: not run unless a section above states a Linux run with its versions. Phase 1
  ran on macOS only.
- Live child runs are limited to the `--preflight` checks unless a section above states
  otherwise; no full loop with real reviewer and scorer children is recorded here.
- The phase-2 P1 grade was made by a judge that knew the arm, on three runs with
  `Bash` and repo writes denied, so steps that need the scripts were done by hand, and
  on a revision whose escalation rule has since changed. A rerun needs a blind judge
  and Bash allowed to the skill's scripts.
- Scores are ordinal judgments on a 0 to 10 scale, not measurements. Judge variance was
  not measured, so a 0.5 difference (S3) is not evidence of a real difference.
