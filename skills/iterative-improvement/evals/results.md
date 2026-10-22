# Results

Arms are named by skill version and commit: `1.0.0` (`414c914`), `2.1.0` (`64b1e16`),
`2.3.0` (`b25c190`) and `2.4.0` (the current files, uncommitted when recorded).

## 1. 2.1.0 against 1.0.0 (recorded)

- Date: 2026-10-20. Blind behavioural A/B, n = 12 scenarios (S1 to S12), one judge
  model, one pass per transcript. It grades process compliance (the ideal items and
  failure modes), not the quality of the final artifact.
- The scores were assigned before the bands in `rubric.md` were written down and do
  not follow them: six 1.0.0 scenarios that hit a failure mode (S1, S6, S7, S8, S10,
  S12) score above the 0 to 3 band. As recorded: mean 5.7 (1.0.0) against 9.2 (2.1.0),
  2.1.0 won 10, tied 2, lost 0. With every failure-mode score capped at 3, 1.0.0's mean
  is 4.4; 2.1.0 hit no failure mode, so its 9.2 and the tally stand.
- Triggering: a reading of the description against the installed skill list.

| Scenario | 1.0.0 | 2.1.0 | Winner |
|---|---|---|---|
| S1 | 5.5 | 9.5 | 2.1.0 |
| S2 | 9 | 9 | tie |
| S3 | 9 | 9.5 | 2.1.0 |
| S4 | 3 | 9 | 2.1.0 |
| S5 | 3 | 8.5 | 2.1.0 |
| S6 | 6.5 | 9.5 | 2.1.0 |
| S7 | 5 | 9.5 | 2.1.0 |
| S8 | 4 | 9.5 | 2.1.0 |
| S9 | 2 | 9.5 | 2.1.0 |
| S10 | 5 | 9 | 2.1.0 |
| S11 | 9 | 9 | tie |
| S12 | 7.5 | 9 | 2.1.0 |

Failure modes 1.0.0 hit: loop state in `docs/` (S1, S5); a full interview for a one-file
request (S4); running past the budget while a gate fails (S6); a repeated N/A carried
forward (S7); a rejected finding re-argued (S8); a fixed 2-point threshold and a last
round run because budget remained (S9); no manifest check on its staged copy (S10);
criteria without sources in a headless run (S12).

## 2. Offline

- 2026-10-22, 2.4.0, macOS 26.5 (arm64), bash 3.2.57, Python 3.9.6: `tests/run.sh`
  passes (the count is in its output; scripts are exercised against stub engines, no
  network, no tokens). The repository validator reports 0 errors. Description: 560
  characters, `Not for` at index 104. CI runs the same suite on `ubuntu-latest` and
  `macos-latest`; no Linux result is recorded here.
- 2026-10-21, 2.3.0: 108 tests passed, also under Python 3.12 to 3.14; Python 3.8
  checked only by parsing at `feature_version=(3, 8)`.

## 3. Live

Preflights, 2026-10-21 about 18:55 UTC, on 2.3.0's `spawn_child.sh`:

| Engine and flags | Result |
|---|---|
| `codex --model gpt-6.1-sol` | `preflight ok: codex (codex-cli 0.163.0) as scorer, model gpt-6.1-sol, 17s` |
| `codex` | `preflight ok: codex (codex-cli 0.163.0) as reviewer, 12s` |
| `claude --same-engine` | `preflight ok: claude (2.1.306 (Claude Code)) as reviewer, 12s` |
| `claude --same-engine --model sonnet` | `preflight ok: claude (2.1.306 (Claude Code)) as scorer, model sonnet, 11s` |
| `claude` (inside Claude Code) | refused, exit 7 wrapping exit 5: asks for `--same-engine` |
| `kimi --unsandboxed` | failed, exit 7: provider 403 (account has no access); kimi is not verified live |

Isolation probe, same day: a Claude child (`--same-engine --model haiku`) read the
staged file and was denied a file outside the stage, a grep outside it and a home rc
file; a Codex child read all three. Footers: `not reported` and `tokens used: 11,188`.

P1 on 2.3.0 before its review fixes (`claude -p "make our README better"`, Opus
orchestrator, Sonnet subagents, 1.9 KB README, headless): three runs, 8 to 11.5
minutes and USD 3.00 to 4.40 each for the whole session. All three said "lite",
derived sourced criteria and ran a blind review, then escalated to full mode on the
reviewer's severity labels (the rule of that revision) and attempted a repo write
before any "ok". One judge, not blind to the arm: 2, 2 and 2, fail.

| Scenario | Arm | Score | Status |
|---|---|---|---|
| P1 | 2.3.0, pre-fix | 2, 2, 2 | fail |
| P1 | 2.4.0 | - | not run |
| P2 to P15 (P2, P3, P4, P5, P6, P7, P8, P9, P10, P11, P12, P13, P14, P15) | 2.4.0 | - | not run |

The escalation fix and the proposal stop (P14) are untested live: a rerun costs about
USD 3 to 4.5, above the spend cap set for this evaluation. P1's premise (user present)
cannot be reproduced headless.

## 4. Routing (simulated)

2026-10-22, by a review of 2.3.0: `claude -p` (haiku and sonnet) given only skill
names and descriptions, two catalogs (this repo's skills; plus five common ones), three
shuffles each, so n = 12 per prompt, about USD 0.5 for 72 calls. 2.3.0's description
fired on 14 of 180 hard negatives, including "build me a world-class landing page ...
from scratch" (3 of 12) and "iterate on the prompt until the eval pass rate is above
90%" (3 of 12); the 2.4.0 description fired on 5 of 180, with recall on 15 should-fire
prompts 169 of 180 before and 168 of 180 after. "write a next-level README for this new
project" still fired 4 of 12 (haiku). This is a model reading descriptions, not the
host's router; `run_routing.py` repeats it with the current `triggers.json`.
