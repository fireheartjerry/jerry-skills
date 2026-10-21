# Results: 2026-10-21

With the skill, the agent scored higher than without it on 11 of 12 scenarios by the
mechanical probes (one tie) and on 12 of 12 by the independent grader. The mean score per run
went from 2.38 to 4.33 (mechanical) and from 0.62 to 4.08 (grader). Without the skill, the
phase-1 failure in S-J (a dict or set change to an I/O-bound loop) recurred in 2 of 2 runs
by both measures. With the skill the mechanical probe found no edit of that kind; the grader
counted one, a dedup line in proposed code (see Findings). n is 2 per cell, so treat the
per-scenario numbers as indicative.

Seven mechanical probes and eight grader musts can only be met by running one of the skill's
own commands (`dev.py init`, `affected`, `aa`, `bench`, `bound makespan`, `changepoint`,
`smart`), which the `none` arm does not have. Leaving those out, the mean per run goes from
2.38 to 3.83 (mechanical) and from 0.62 to 3.50 (grader), and the skill arm scores higher on
10 of 12 scenarios mechanically (S-F and S-I tie) and 12 of 12 by the grader. No `none`
transcript touched an installed copy of the skill (0 of 24 mention `SKILL.md` or
`competitive-systems` in a tool call).

This run evaluated an earlier SKILL.md and an earlier prompt format; the shipped version
differs (see "Changes after the run"), and those changes have not been re-run.

## Method

| Item | Value |
|---|---|
| Date | 2026-10-21 (America/Toronto) |
| Runner | `claude -p` (Claude Code 2.1.306), `--output-format stream-json`, tools Read, Glob, Grep, Write, Edit, Bash, `--disable-slash-commands`, `--setting-sources project`, `--max-budget-usd 3` |
| Model under test | `claude-opus-5-5` (the model the CLI reported in every run's init event) |
| Arms | `none` (no skill text) and `skill` (SKILL.md appended to the system prompt, skill snapshot added with `--add-dir`) |
| Scenarios | all 12, S-A to S-L (`scenarios.json`) |
| Repeats | 2 per arm and scenario (n per cell = 2), 48 runs in total |
| Fixtures | `fixtures/make_fixtures.py`, copied into a fresh work directory under the system temp directory for each run |
| Machine | macOS 26.5 arm64, Python 3.9.6 |
| Mechanical grading | `grade.py --transcripts`: must probes matched minus failure probes matched |
| Grader | `claude-sonnet-5-5` via `claude -p --model sonnet --tools ""`: a fresh session per transcript, no tools, no settings, given only the `grade.py --prompt` output (musts, failure modes and transcript, no regexes). The skill's implementer did not grade. |
| Trigger routing | `run_eval.py --trigger`: 18 prompts (9 should fire, 9 should not), the model chose among this skill and 3 decoys |

Command lines: `run_eval.py --runner claude --arms none,skill --seeds 2 --jobs 4`,
`run_eval.py --runner claude --trigger`, then `grade.py --transcripts`, the grader loop in
README.md, and `grade.py --merge`. Transcripts, raw event streams, grader replies and the
grade tables are in `eval-runs/competitive-systems/phase2-runs/` of the skills repository
(git-ignored scratch, not shipped).

## Mechanical score per scenario and arm

Mean over 2 runs, range in brackets; max is the number of must probes.

| Scenario | max | none | skill |
|---|---|---|---|
| S-A init-ts-repo | 5 | 1.5 (1..2) | 5 (5..5) |
| S-B monorepo-selection-conftest | 4 | 1.5 (1..2) | 3 (3..3) |
| S-C is-8-percent-real | 6 | 2 (2..2) | 5 (4..6) |
| S-D services-impact-10-minutes | 6 | 2.5 (2..3) | 5 (5..5) |
| S-E ci-40-minutes-8-runners | 7 | 2.5 (2..3) | 6 (6..6) |
| S-F too-good-to-be-true | 5 | 4.5 (4..5) | 4.5 (4..5) |
| S-G tiny-input-aho-corasick | 4 | 1.5 (1..2) | 3.5 (3..4) |
| S-H context-packer-overlap | 6 | 3 (3..3) | 5.5 (5..6) |
| S-I perf-regression-history | 5 | 3.5 (3..4) | 4.5 (4..5) |
| S-J io-bound-import | 3 | 1 (1..1) | 3 (3..3) |
| S-K hot-function-12-percent | 3 | 2 (2..2) | 3 (3..3) |
| S-L exact-budget-40-suites | 6 | 3 (3..3) | 4 (4..4) |
| All, per run | | 2.38 | 4.33 |

Must probes matched: 59 of 120 without the skill, 104 of 120 with it. Failure probes
matched: 2 without (S-J `dict-set`, both runs), 0 with.

## Independent grader score per scenario and arm

Musts judged yes minus failure modes judged yes; max is the number of musts.

| Scenario | max | none | skill |
|---|---|---|---|
| S-A init-ts-repo | 5 | 1.5 (1..2) | 5 (5..5) |
| S-B monorepo-selection-conftest | 5 | 0.5 (0..1) | 2.5 (2..3) |
| S-C is-8-percent-real | 6 | 0.5 (0..1) | 5 (5..5) |
| S-D services-impact-10-minutes | 6 | 0 (0..0) | 3.5 (3..4) |
| S-E ci-40-minutes-8-runners | 8 | 0.5 (-1..2) | 7 (7..7) |
| S-F too-good-to-be-true | 6 | 2.5 (2..3) | 3 (3..3) |
| S-G tiny-input-aho-corasick | 6 | 0.5 (0..1) | 4 (4..4) |
| S-H context-packer-overlap | 8 | 0 (-1..1) | 7 (6..8) |
| S-I perf-regression-history | 5 | 0.5 (0..1) | 3.5 (3..4) |
| S-J io-bound-import | 5 | 2 (2..2) | 4 (3..5) |
| S-K hot-function-12-percent | 3 | -1 (-2..0) | 2 (2..2) |
| S-L exact-budget-40-suites | 7 | 0 (0..0) | 2.5 (2..3) |
| All, per run | | 0.62 | 4.08 |

Musts judged yes: 43 of 140 without the skill, 104 of 140 with it. Failure modes judged yes:
28 without, 6 with.

## Outcome score (skill-tool checks left out)

Mean over 2 runs; max excludes the tagged checks (`requires_skill_tool` probes,
`skill_tool_musts`). Only the scenarios with tagged checks are listed; the others equal the
tables above.

| Scenario | mech. max | mech. none | mech. skill | grader max | grader none | grader skill |
|---|---|---|---|---|---|---|
| S-A init-ts-repo | 3 | 1.5 | 3 | 3 | 1.5 | 3 |
| S-B monorepo-selection-conftest | 3 | 1.5 | 2 | 4 | 0.5 | 1.5 |
| S-C is-8-percent-real | 4 | 2 | 4 | 4 | 0.5 | 3 |
| S-D services-impact-10-minutes | 6 | 2.5 | 5 | 5 | 0 | 2.5 |
| S-E ci-40-minutes-8-runners | 6 | 2.5 | 5 | 8 | 0.5 | 7 |
| S-I perf-regression-history | 4 | 3.5 | 3.5 | 4 | 0.5 | 2.5 |
| S-L exact-budget-40-suites | 6 | 3 | 4 | 6 | 0 | 2.5 |
| All, per run | | 2.38 | 3.83 | | 0.62 | 3.50 |

Reproduce from the kept transcripts, offline: `grade.py --transcripts OUT --out
OUT/grades.md`, then `grade.py --merge OUT/grades.json`.

## Trigger routing

| Measure | Value |
|---|---|
| Prompts | 9 should fire, 9 should not (`trigger.json`) |
| Chose this skill | 9, all on should-fire prompts |
| Precision | 1.0 (9 of 9) |
| Recall | 1.0 (9 of 9) |
| Model | `claude-opus-5-5`, one `claude -p` call per prompt with a router system prompt and no tools |

The negatives went to the decoys (`code-maintenance` 7, `web-performance` 2, including the
I/O-bound "45 s for 3,000 records" prompt). The decoy set is small and easy to separate, so
this checks the description's exclusions, not routing among many similar skills.

## Cost

| Item | USD |
|---|---|
| 48 scenario runs (the transcripts graded above) | 6.61 |
| 4 superseded skill-arm runs (S-A, S-B before the SKILL.md change below) | 0.51 |
| 18 trigger prompts | 0.65 |
| 52 independent grades | 1.31 |
| Total | 9.08 |

Per scenario run: 0.05 to 0.53 USD; the skill arm cost 4.18 USD for 24 runs, the `none` arm
2.43 USD.

## Findings

- Gaps that remain with the skill (mechanical, runs missing the probe out of 2): S-L never
  stated the 18-candidate cap for exact subset search or why 2^40 is not run (2 of 2);
  S-B never named the escape oracle, a scheduled full run (2 of 2); S-E never stated
  m >= 2 (2 of 2); S-D never named the reverse graph or BFS (2 of 2). The grader agrees on
  S-L (F5, 18 cap not stated, in both runs).
- S-J with the skill: both runs diagnosed the I/O wait and proposed `get_records` batching.
  One run's proposed code deduplicated with `dict.fromkeys`. The mechanical probe only
  inspects file edits, so it did not count that line; the grader did.
- S-F: every run in both arms refused to ship and named the identity-keyed cache; the
  Amdahl cap appeared in 1 of 2 runs without the skill and 2 of 2 with it. The grader
  judged that both skill runs timed the candidate before checking correctness (F2).

## Changes after the run (not re-run)

- The prompt suffix is now its own paragraph after a full stop. The published run sent the
  message and the suffix joined by one space with no full stop between them (for example
  `make it fast The relevant files are in the current directory.`), so every prompt in it was
  slightly malformed.
- S-A's message no longer says "from the skill", which named a skill the `none` arm lacks.
- `dev.py catalog` prints, after each fact, the card sentence that states it (`card says:`),
  and SKILL.md step 4 tells the agent to put each of those claims in the answer. This targets
  the open gaps below (S-L 18-candidate cap, S-E m >= 2, S-D reverse graph and BFS, S-B
  scheduled full run; the last two cards gained the facts that carry them). Not verified:
  no live run measured whether the gaps closed.
- SKILL.md was cut from 9,968 to about 9,100 bytes (the Classes paragraph, the tooling table
  and a reference list that repeated the procedure).
- Tool-presence checks are tagged and reported separately (above); the published scores
  were not changed.

## Changes during the run

- After the 48 runs, SKILL.md's tooling table gained one row, `dev.py affected --base main
  --explain` (the skill-arm S-B runs had reasoned by hand without the tool). The 4 skill-arm
  runs of S-A and S-B were rerun on the final SKILL.md and replace the first 4 in every
  table; the other 20 skill-arm runs used SKILL.md without that row. The superseded runs
  scored 4 and 2 (mechanical) and 4.5 and 2 (grader) for S-A and S-B.
- Four probes were corrected after reading the transcripts, before the final grading, and
  applied to both arms: S-F `confirms-40` matched "don't ship it" (negation now excluded);
  S-F `refuses` missed "wouldn't ship"; S-G `stops` missed "I'd use neither"; S-E
  `week-compare` matched advice against week-over-week comparison and was removed. S-C
  `confirms-8` got the same negation guard; it matched nothing before or after.

## Limits

- Small n: 2 runs per cell. One-point differences in a single cell are within run-to-run
  spread (see the ranges).
- One model family: the model under test, the router and the grader are all Claude models.
  The codex runner exists but was not run (not run: time and spend kept to one runner).
- The grader is a model: it read the transcripts once, without a second grader or a human
  check of agreement.
- Mechanical probes detect facts, numbers and commands in the agent's words and actions, not
  the quality of its reasoning, and they were tuned once after reading these transcripts
  (listed above).
- Fixtures are synthetic and small; timings the agents measured came from one laptop.
- Trigger routing used 3 decoys; precision and recall against a full skill catalog are not
  measured.
- Seven probes and eight grader musts check for the skill's own commands; the headline
  scores include them, which favours the skill arm by construction. The outcome scores above
  leave them out.
- The claude runner ran Bash unsandboxed as the user on the author's machine. Settings, MCP
  servers and skills were disabled, and no `none` transcript touched an installed skill copy,
  but Bash could have reached one.
- The shipped SKILL.md, catalog output and prompt format differ from the evaluated ones
  (listed above); the deltas here describe the previous version.
