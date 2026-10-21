# Full protocol

Full mode of the iterative-improvement loop. SKILL.md sections 3 to 5 (paths, shared rules,
report) apply throughout; this file adds what full mode needs. Section numbers below are
cited from other files as `full-protocol.md 4b`.

## 1. Terms

- **ROOT**: the git repo root, or outside a repo the folder holding the artifact.
- **LOOP**: `ROOT/.iterative-improvement/<slug>/`, the loop's private state.
- **snapshot**: a copy of the artifact at the end of a round, `LOOP/snapshots/round-N`.
- **n** (noise floor): the spread between scorer runs on one unmodified snapshot.
- **T** (move threshold): `T = max(3, 2n)`; a move smaller than T is within noise.
- **series**: the comparable score list handed to `decide`, reference first.
- **reference**: the baseline, or after a rubric amendment the rescored previous snapshot.
- **rubric version**: `v1`, `v2`, ... in the criteria doc; scores compare only within one.
- **PRESERVE / ACHIEVE**: gate classes set at round 0 (passes now / must pass by the end).
- **N/A**: a criterion the scorer could not check; it leaves the total, the adjusted max
  shrinks, and the scaled total is `round(points * 100 / adjusted max)`.
- **child run**: one `spawn_child.sh` invocation (reviewer or scorer).

## 2. Phase 1: criteria

Read `references/criteria-guide.md` now (template, worked criteria, weighting).

Interview in batches of 3 to 5 questions, usually two batches. Cover: outcome (who looks,
what they should do or feel), references with the specific thing that is excellent about
each, anti-references, hard constraints, **the rejection question** ("what would make you
reject this even if everything else were perfect"; the answers are gates), priorities ("only
three fixes"), and done ("what would make you say stop"). Extract answers already in the
conversation, a spec or a prior audit first and bring them for confirmation instead of asking
cold.

Write the document: 6 to 12 criteria summing to 100, each with a **How to check it** line a
stranger could execute and three anchors; gates listed separately with a `check:` command;
Out of scope; Constraints. A check needing tooling the scorer lacks (browser, build, network)
names the exact command you will run as evidence. Include one criterion the work fails badly
and one it passes, or the scorer cannot be calibrated.

**Sign-off**: show the doc and ask for corrections before anything runs. No user reachable
(headless `-p`/`exec`, auto-permission mode, offline)? Do not stop and do not pretend: derive
each criterion from a real source (README, docs, issues, commits, prior audit) with its own
`**Source:**` line, set `**Status:** DERIVED, not user-approved. Sources: ...` at the top,
tag every guess `[ASSUMED: what]` (weights and thresholds included), and open the final
report by saying the criteria were never approved and naming each assumption
(`references/troubleshooting.md`, "No user to interview").

## 3. Phase 2: budget

Ask four things, then run unattended until a stop rule fires:

1. **Stop condition**: target score or round count. Default 5 rounds, stop early at 90. A
   round count is a ceiling, not a goal; say so.
2. **Money or time** (optional): "about 90 minutes" or "around 15 dollars of API spend"
   becomes a second ceiling. Section 6 says how it is measured and converted; until round 1
   has run, the conversion is an estimate and you say so.
3. **Off-limits**: files, decisions, areas the loop must not touch (into Constraints).
4. **Commits** (repo only): may the loop commit each round on the current branch? Default no;
   snapshots give the revert path either way.

## 4. Phase 3: the loop

Before round 0, read `references/cross-agent.md`, pick the reviewer and scorer engines and run
`scripts/spawn_child.sh --preflight <engine> [--model M]` for each. The scorer is **pinned**
for the whole loop: engine, `--model`, flags, brief file. Every child runs through
`spawn_child.sh`, which records the command to `scores/round-N.cmd` (byte-identical across
rounds for the scorer) and the log to `scores/round-N.log`.

### Round 0: baseline (once)

1. Snapshot the unmodified artifact to `snapshots/round-0`.
2. Strip the doc: `scripts/strip_criteria.py <criteria> LOOP/criteria-for-scorer.md`.
3. Run every gate's `check:` and every check line the scorer cannot run, once each, into
   `evidence/round-0/` with its `manifest.md` (SKILL.md section 4, Evidence).
4. Run the scorer **twice** on the same snapshot and evidence (`scores/round-0a.md`,
   `round-0b.md`). `scripts/score_math.py baseline A B` prints the baseline (mean, half up),
   `n`, `T` and `third-run=needed|no`. When it says `needed` (the two runs are identical, or
   more than 8 apart), run the scorer a third time on the same snapshot and evidence
   (`round-0c.md`) and run `baseline A B C`: the script keeps the two runs that agree, names
   the outlier if there is one (log it in the score log, diff its per-criterion table against
   the others and tighten those criteria at the next amendment), or prints `soft=true` when
   no two runs agree within 8. Soft means the check lines do not discriminate: tighten the
   criteria with the largest per-criterion disagreement and run two fresh baseline runs
   before round 1. Lite mode skips all of this and fixes `T = 3`.
5. Classify every gate from your gate runs: **PRESERVE** (PASS at baseline, must stay PASS)
   or **ACHIEVE** (FAIL at baseline, must PASS by the end); write the class next to each gate
   in the doc. ACHIEVE gates are round 1's first DO NOW items.
6. Record baseline, `n`, `T`, scorer pin and (repo) baseline SHA in the doc's loop-only
   block, and row 0 of the score log.

### 4a. Hostile review

Independence order: a different agent CLI; the same CLI as an isolated process
(`spawn_child.sh --same-engine`); a harness subagent given only a staged dir; kimi
(`--unsandboxed`, trusted artifacts only); inline. Fill
every slot of `references/reviewer-brief.md`: CONSTRAINTS, OUT OF SCOPE, how to view the
artifact, and from round 2 the criteria, per-criterion scores and PRIOR FINDINGS (title,
location, status: addressed, deferred, or rejected with its one-line reason). A rejected
finding may return only with new evidence, tagged `[re-raised: new evidence]`.

Round 1 is **blind**: no criteria, weights or scores. It finds what the criteria missed; when
it does, amend them (section 5) before continuing.

Run the independence check (SKILL.md section 4) before acting on any review.

### 4b. Triage

The table and bucket rules are in SKILL.md section 4; in full mode also write the
per-criterion Before/After table after scoring (4d step 6).

### 4c. Execute

A PRESERVE gate that flips to FAIL is fixed or reverted before scoring; ACHIEVE gates may
still fail. Order, changelog, revert path and the `git stash` rule: SKILL.md section 4.

### 4d. Score

1. Snapshot the current artifact to `snapshots/round-N`.
2. Regenerate `criteria-for-scorer.md` with `strip_criteria.py`.
3. Run every gate's `check:` and every check line the scorer cannot run, **exactly as
   written, once**, into `evidence/round-N/`, logged in its `manifest.md`: check, command,
   timestamp, attempts, reason for any retake. No choosing among captures. Gate results come
   from these runs; the scorer only reads them (PASS, FAIL, or UNKNOWN when you forgot one).
   Evidence you produce is fine; judgment is not. Every round, or the scorer marks N/A.
4. Spawn the pinned scorer with `references/scorer-brief.md`:
   `spawn_child.sh <engine> scorer <brief> <snapshot> scores/round-N.md --model M --evidence evidence/round-N`.
   It sees the stripped criteria (pasted in the brief), the staged copy and the evidence,
   nothing else.
5. `spawn_child.sh` re-hashes the staged copy and ROOT's git status afterwards. If either
   changed, the output is discarded (exit 4): check nothing edited ROOT, rerun once, and
   switch engine on a second discard. Do not edit the artifact while a child runs.
6. Log total, adjusted max, scaled total (`round(points * 100 / adjusted max)`), N/A list and
   gate results to the score log, and a per-criterion table to `rounds.md`:
   `| # | Before | After | Delta | Change that moved it |`.

### 4e. Decide

Run `scripts/score_math.py decide --threshold T --target X --max-rounds R --scores <series>
--rounds-used N --gates PASS|FAIL` after every scorer run. `<series>` starts at the reference
(baseline, or the rescored number after an amendment) and ends with this round. `N` is the
real round number. `R` is the total round ceiling, or, under a money or time budget,
the `max-rounds` that `score_math.py budget --rounds-used N` prints (section 6): a total
on the same scale as `N`, already capped by the ceiling. `--gates` comes from your own gate runs. The script knows
the rule order; you act on the word it prints:

| Printed | Meaning | What you do |
|---|---|---|
| `HALT gate-fail-rounds-exhausted` | A gate still fails and the budget is used up | Report with the gates first. Never add rounds. |
| `STOP regression` | The score fell by T or more, in one round or across two | Diff the per-criterion tables, revert from the previous snapshot or explain, then report. |
| `STOP plateau` | Two rounds in a row moved less than T each and less than T together | Stop even with budget left. Say what is left and which gap needs the user. |
| `STOP rounds-exhausted` | The ceiling is reached | Report. |
| `CONFIRM target-reached` | The number meets the target with zero N/A and all gates PASS | Run the scorer once more on the same snapshot; rerun decide with `--confirm-score`. `STOP target-hit` means the mean of both runs meets the target; `CONTINUE target-unconfirmed` means it did not. |
| `BLOCKED na-repeated` | The same adjusted max two rounds running: a criterion was N/A twice | Produce its evidence by running the check line, or amend it out and rescore. Do not read the number. |
| `CONTINUE` | Next round | The detail line says `+d`, `within-noise`, `slow real trend` or `incomparable` (adjusted max changed, no delta). A within-noise move is never progress toward the target. |

Worked instance at `T = 8`: series `58, 71, 76, 79` prints `STOP plateau` (+5 and +3 are each
inside T and total 8, not more than T). `58, 71, 77, 83` prints `CONTINUE` with `slow real
trend` (+6 and +6 add up past T).

## 5. Amending criteria

A rubric amendment happens when the blind review finds a defect no criterion covers, or a
criterion never moves. Steps:

1. Add or remove the criterion, rebalance to 100, bump the rubric version, write one line in
   `## Amendments`, tell the user in one line.
2. **Rescore the previous round's snapshot** under the new version with the pinned scorer and
   that round's evidence: twice in full mode, once in lite. `score_math.py baseline A B` gives
   the new reference, `n` and `T`; a third run when it says `third-run=needed`.
3. Log the rows `N a | vK`, `N b | vK`, `N | vK | reference` and `61 -> 58 (rubric v2,
   rescored)` in the score log. The drop from adding a criterion the work fails is
   bookkeeping, never a regression.
4. Start a new series at the reference. From the next round pass
   `--scores <reference>,<new scores>` and `--rounds-used <real round number>`; the detail
   line prints `K of R rounds used` so you can check it.
5. Never re-tally by hand: re-weighting non-linear anchors has no arithmetic, and scoring the
   new criterion yourself is the orchestrator grading its own work.

Worked instance: v1 series `43, 49/80` (round 1 had two N/A). Amend to v2 after round 1.
Rescore `snapshots/round-1` under v2 with the evidence now supplied: 56 and 60, so the
reference is 58, `n = 4`, `T = 8`. Round 2 scores 73:
`decide --threshold 8 --target 85 --max-rounds 5 --scores 58,73 --rounds-used 2 --gates PASS`.

## 6. Cost and time per round

Count child runs, not tokens, when planning; measure the rest.

| Event | Child runs |
|---|---|
| Round 0 | 2 scorer runs (3 when `third-run=needed`), plus your gate and evidence runs |
| Each round | 1 reviewer run + 1 scorer run, plus your fixes and evidence runs |
| Rubric amendment | 2 scorer runs on the previous snapshot (3 when needed) |
| Target reached | 1 confirmation scorer run |
| A discarded run (exit 4) | 1 rerun, on another engine after a second discard |

A five-round full loop with one amendment and a confirmed target is therefore 15 to 17 child
runs; lite is 3 to 7 (round 0, one or two rounds of two runs, plus a confirmation run
and an amendment rescore). Every child run ends its `.log` with `# started`, `# finished`,
`# duration: Ns` and `# usage:`: codex's token count, or `not reported` for claude and
kimi, which print no usage in the mode the script runs them. Round 0 gives you the time per scorer run; round 1 gives the time
per round including your own work. Record both in `rounds.md`.

When the user set a money or time budget: after each round run
`scripts/score_math.py budget --total <budget> --spent <measured so far> --per-round
<measured round cost> --rounds <round ceiling> --rounds-used <rounds run>` and pass its
`max-rounds` to `decide --max-rounds` unchanged. Run budgets on time (the `.log`
durations) by default. Neither script path measures money: codex reports tokens, not
dollars, and claude and kimi report nothing. Convert a money budget to time or rounds
with the user, say so, and mark any dollar figure "estimated". Never start a round the remaining budget cannot
finish; `rounds-affordable=0` means stop now and report. Before round 1 has run, any
conversion is an estimate: say "estimated" until it is measured. Do not invent durations or
spend in the report; quote the `.log` footers.
