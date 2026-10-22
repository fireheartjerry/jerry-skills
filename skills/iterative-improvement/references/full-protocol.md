# Full protocol

Full mode of the iterative-improvement loop. SKILL.md sections 3 to 5 (paths, shared
rules, report) apply throughout; this file adds what full mode needs. Section numbers
below are cited from other files as `full-protocol.md 4b`. The exact command sequence
is in `references/commands.md`.

## 1. Terms

- **orchestrator**: you, the agent running this skill; you make the fixes and run the
  gate and evidence commands.
- **ROOT**: the git repo root, or outside a repo the folder holding the artifact.
- **LOOP**: the loop's private state, made by `loop.py init`: inside the git dir in a
  repo, `ROOT/.iterative-improvement/<slug>/` outside one.
- **snapshot**: a copy of the artifact's files at the end of a round, `LOOP/snapshots/round-N`.
- **gate**: a pass/fail requirement with a `check:` command, reported apart from the score.
- **evidence**: the output of a gate's or criterion's check line, run once by you into
  `LOOP/evidence/round-N/` and logged in its `manifest.md`.
- **n** (noise floor): the spread between scorer runs on one unmodified snapshot.
- **T** (move threshold): `T = max(3, 2n)`; a move smaller than T is within noise.
- **score log**: the table at the end of the criteria doc, one row per scorer run.
- **series**: the comparable score list handed to `decide`, reference first.
- **reference**: the baseline, or after a rubric amendment the rescored snapshot.
- **rubric version**: `v1`, `v2`, ... in the criteria doc; scores compare only within one.
- **PRESERVE / ACHIEVE**: gate classes set at round 0 (passes now / must pass by the end).
- **N/A**: a criterion the scorer could not check; it leaves the total, the adjusted max
  shrinks, and the scaled total is `round(points * 100 / adjusted max)`.
- **child run**: one `spawn_child.sh` invocation (reviewer or scorer).

## 2. Phase 1: criteria

Read `references/criteria-guide.md` now (template, worked criteria, weighting).

Interview in batches of 3 to 5 questions, usually two batches. Cover: outcome (who
looks, what they should do or feel), references with the specific thing that is
excellent about each, anti-references, hard constraints, **the rejection question**
("what would make you reject this even if everything else were perfect"; the answers
are gates), priorities ("only three fixes"), and done ("what would make you say
stop"). Extract answers already in the conversation, a spec or a prior audit first and
bring them for confirmation instead of asking cold.

Write the document: 6 to 12 criteria summing to 100, each with a **How to check it**
line a stranger could execute and three numeric anchors; gates listed separately with
a `check:` command, the first the repo's verify command as PRESERVE (or "no verify
command found"; `references/criteria-guide.md`, "Gates are not criteria"); Out of
scope; Constraints. A check needing tooling the scorer lacks
(browser, build, network) names the exact command you will run as evidence. Include
one criterion the work fails badly and one it passes, or the scorer cannot be
calibrated. `score_math.py check-criteria <doc>` must print `ok`.

**Sign-off**: show the doc and ask for corrections before anything runs. No user
reachable (headless `-p`/`exec`, auto-permission mode, offline)? Do not stop and do not
pretend: derive each criterion from a real source (README, docs, issues, commits, prior
audit) with its own `**Source:**` line, set `**Status:** DERIVED, not user-approved.
Sources: ...` at the top, tag every guess `[ASSUMED: what]` (weights and thresholds
included), and open the final report by saying the criteria were never approved and
naming each assumption (`references/troubleshooting.md`, "No user to interview").

## 3. Phase 2: budget

Ask four things, then run unattended until a stop rule fires:

1. **Stop condition**: target score or round count. Default 5 rounds, stop early at 90.
   A round count is a ceiling, not a goal; say so.
2. **Money or time** (optional): "about 90 minutes" or "around 15 dollars of API spend"
   becomes a second ceiling. Section 6 says how it is measured and converted; until
   round 1 has run, the conversion is an estimate and you say so.
3. **Off-limits**: files, decisions, areas the loop must not touch (into Constraints).
4. **Commits** (repo only): may the loop commit each round on the current branch?
   Default no; snapshots give the revert path either way. The answer covers the
   criteria doc too: uncommitted unless commits are allowed.

With the questions, state what the loop will spend and send: the reviewer and scorer
engines and their vendors, the files in the snapshot (`snapshot.py create --dry-run`),
about 16 to 18 child runs for five rounds (section 6) at 6 to 10 minutes each, the
per-run spend cap (`--max-usd` on a claude child), and that each child run is a billed
model call that sends those files to that vendor. The user's answer is consent for
that plan; ask again before an engine switch or rounds beyond the ceiling. Pass the
answers to `loop.py init` (`--rounds`, `--commits`, `--budget`, `--engines`,
`--off-limits`) so they survive a pause.

## 4. Phase 3: the loop

Before round 0: `loop.py init --mode full` with a `--scope` per artifact path and per
file it is checked against, read `references/cross-agent.md`, pick the engines, run
`spawn_child.sh --check`, then `--preflight <engine> [--model M]` for each. The scorer
is **pinned** for the whole loop: engine, `--model`, flags, brief file. Every child runs
through `spawn_child.sh`, which records the command to `<out>.cmd` (byte-identical
across rounds for the scorer apart from the CLI patch version; any other change is a new
instrument, so rescore the previous snapshot under it before comparing) and the log to
`<out>.log`.

### Round 0: baseline (once)

1. Snapshot the unmodified artifact to `snapshots/round-0` with `--path` for the scope.
2. Strip the doc: `scripts/strip_criteria.py <criteria> LOOP/criteria-for-scorer.md`.
3. Run every gate's `check:` and every check line the scorer cannot run, once each,
   into `evidence/round-0/` with `evidence.py run` (SKILL.md section 4, Evidence).
4. Run the scorer **twice** on the same snapshot and evidence (`scores/round-0a.md`,
   `round-0b.md`), then `score_math.py baseline A B` and follow its detail line: on
   `third-run=needed` score `round-0c.md` and run `baseline A B C`. An outlier gets
   logged in the score log and its per-criterion table diffed against the others.
   `soft=true` means the check lines do not discriminate: tighten the criteria with the
   largest per-criterion disagreement and take two fresh baseline runs, **once**. If
   that set is soft too, proceed with the T it prints, tell the user the instrument is
   noisy and name the criteria that disagree. Lite mode skips all of this: `T = 3`.
5. Classify every gate from your gate runs: **PRESERVE** (PASS at baseline, must stay
   PASS) or **ACHIEVE** (FAIL at baseline, must PASS by the end); write the class next
   to each gate in the doc. ACHIEVE gates are round 1's first DO NOW items.
6. Record baseline, `n`, `T`, scorer pin and (repo) baseline SHA in the doc's loop-only
   block, and row 0 of the score log.

### 4a. Hostile review

The round-N reviewer sees the state at the end of the previous round:

```
spawn_child.sh <engine> reviewer LOOP/prompts/reviewer-round-N.md LOOP/snapshots/round-<N-1> \
  LOOP/reviews/round-N.md --evidence LOOP/evidence/view-round-<N-1>
```

Fill every slot of `references/reviewer-brief.md`: CONSTRAINTS, OUT OF SCOPE, how to
view the artifact, and from round 2 the stripped criteria, per-criterion scores, PRIOR
FINDINGS (title, location, status: addressed, deferred, or rejected with its one-line
reason) and PROTECTED (the KEEP list). A rejected finding may return only with new
evidence, tagged `[re-raised: new evidence]`.

Round 1 is **blind**: no criteria, weights or scores, and an evidence view without the
manifest. It finds what the criteria missed; when it does, amend them at triage, before
round 1 is scored (section 5).

Run the independence check (SKILL.md section 4) before acting on any review.

### 4b. Triage

The table and bucket rules are in SKILL.md section 4; in full mode also write the
per-criterion Before/After table after scoring (4d step 6).

### 4c. Execute

A PRESERVE gate that flips to FAIL is fixed or reverted before scoring; ACHIEVE gates
may still fail. Order, sibling sweep, changelog, revert path and the `git stash` rule:
SKILL.md section 4.

### 4d. Score

1. Snapshot the current artifact to `snapshots/round-N`.
2. Regenerate `criteria-for-scorer.md` with `strip_criteria.py`.
3. Run every gate's `check:` and every check line the scorer cannot run, **exactly as
   written, once**, into `evidence/round-N/` with `evidence.py run --snapshot`, which
   logs check, command, UTC time, attempt and any retake reason in `manifest.md`. No
   choosing among captures. Gate results come from these runs; the scorer only reads
   them (PASS, FAIL, or UNKNOWN when you forgot one). Every round, or the scorer marks
   N/A.
4. Spawn the pinned scorer with `references/scorer-brief.md`:
   `spawn_child.sh <engine> scorer <brief> snapshots/round-N scores/round-N.md --model M
   --evidence evidence/round-N`. It sees the stripped criteria, the staged copy and the
   evidence, nothing else.
5. On exit 4 the output is discarded; the message says whether the child changed its
   copy or someone changed the artifact's files in ROOT (`references/cross-agent.md`,
   "After every child run"). Do not edit the artifact while a child runs.
6. `score_math.py check-scorer scores/round-N.md --criteria <doc>`; log total, adjusted
   max, scaled total, N/A list and gate results to the score log, and a per-criterion
   table to `rounds.md`: `| # | Before | After | Delta | Change that moved it |`.

### 4e. Decide

Run `scripts/score_math.py decide --threshold T --target X --max-rounds R --scores
<series> --rounds-used N --gates PASS|FAIL` after every scorer run. `<series>` starts at
the reference and ends with this round. `N` is the real round number. `R` is the total
round ceiling, or under a money or time budget the `max-rounds` that `score_math.py
budget --rounds-used N` prints (section 6). `--gates` comes from your own gate runs.
The script knows the rule order; you act on the word it prints:

| Printed | Meaning | What you do |
|---|---|---|
| `HALT gate-fail-rounds-exhausted` | A gate still fails and the budget is used up | Report with the gates first. Never add rounds. |
| `STOP regression` | The score fell by T or more, in one round or across two | Diff the per-criterion tables, revert from the previous snapshot or explain, then report. |
| `STOP plateau` | Two rounds in a row each moved less than T and together at most T (a total equal to T still stops) | Stop even with budget left. Say what is left and which gap needs the user. |
| `STOP rounds-exhausted` | The ceiling is reached | Report. |
| `CONFIRM target-reached` | The number meets the target with zero N/A and all gates PASS | Score the same snapshot once more; rerun decide with `--confirm-score`. `STOP target-hit`: the mean of both meets the target; `CONTINUE target-unconfirmed`: it did not. |
| `BLOCKED na-repeated` | The same adjusted max two rounds running: a criterion was N/A twice | Produce its evidence by running the check line, or amend it out and rescore. Evidence supplied but unread (an image): `--preflight ENGINE --vision`, a text form, or another engine. Do not read the number. |
| `CONTINUE` | Next round | The detail line says `+d`, `within-noise`, `slow real trend` or `incomparable` (adjusted max changed, no delta). A within-noise move is never progress. |

Worked instance at `T = 8`: series `58, 71, 76, 79` prints `STOP plateau` (+5 and +3
are each inside T and total 8, not more than T). `58, 71, 77, 83` prints `CONTINUE`
with `slow real trend` (+6 and +6 add up past T).

**Before the report.** `snapshot.py restore --snapshot LOOP/snapshots/round-<last
scored> --root ROOT --dry-run` lists nothing, or the score describes another tree;
after a revert, say which round the reported score belongs to. Then run the blind
final judge (`references/judge-brief.md`): one criterion-free comparison of round 0
and the last scored round. Its verdict goes beside the score, and a baseline win
stops the claim of a gain until the user has seen it.

## 5. Amending criteria

An amendment happens when the blind review finds a defect no criterion covers, a
scorer note exposes a gap, or a criterion never moves. One rule decides which snapshot
is rescored, so a series never mixes rubric versions:

- **At triage, before round N is scored** (the usual case, and always in lite): rescore
  the snapshot the review examined, `snapshots/round-<N-1>`, under the new version: the
  new reference. Round N is then scored once, under the new version.
- **After round N was scored** (a scorer note, a later finding): rescore
  `snapshots/round-N` under the new version and start the series there.

Steps:

1. Add or remove the criterion, rebalance to 100, bump the rubric version, write one
   line in `## Amendments`, tell the user in one line.
2. **Rescore** that snapshot with the pinned scorer and its evidence: twice in full mode
   (`baseline A B` gives the new reference, `n` and `T`; a third run on
   `third-run=needed`), once in lite (the reference; T stays 3).
3. Log the rows `N a | vK`, `N b | vK`, `N | vK | reference` and `61 -> 58 (rubric v2,
   rescored)` in the score log. The drop from adding a criterion the work fails is
   bookkeeping, never a regression.
4. Pass `--scores <reference>,<later scores>` and `--rounds-used <real round number>`;
   the detail line prints `K of R rounds used` so you can check it.
5. Never re-tally by hand: re-weighting non-linear anchors has no arithmetic, and scoring
   the new criterion yourself is the orchestrator grading its own work.

Worked instance (the second case): v1 series `43, 49/80` (round 1 had two N/A). Amend
to v2 after round 1 was scored. Rescore `snapshots/round-1` under v2 with the evidence
now supplied: 56 and 60, so the reference is 58, `n = 4`, `T = 8`. Round 2 scores 73:
`decide --threshold 8 --target 85 --max-rounds 5 --scores 58,73 --rounds-used 2 --gates PASS`.

## 6. Cost and time per round

Count child runs, not tokens, when planning; measure the rest.

| Event | Child runs |
|---|---|
| Round 0 | 2 scorer runs (3 when `third-run=needed`), plus your gate and evidence runs |
| Each round | 1 reviewer run + 1 scorer run, plus your fixes and evidence runs |
| Rubric amendment | 2 scorer runs on the rescored snapshot (3 when needed) |
| Target reached | 1 confirmation scorer run |
| Before the report | 1 blind judge run |
| A discarded run (exit 4) | 1 rerun |

A five-round full loop with one amendment and a confirmed target is therefore 16 to 18
child runs; lite is 3 to 7 (round 0, one or two rounds of two runs, plus a confirmation
run and an amendment rescore). Every child run's `.log` ends with `# duration: Ns` and
`# usage:` (codex's token count, or `not reported`). Record the per-run times in
`rounds.md`.

When the user set a money or time budget: after each round run
`scripts/score_math.py budget --total <budget> --spent <now minus the Kickoff (UTC)
line in rounds.md>
--per-round <round cost> --rounds <round ceiling> --rounds-used <rounds run>` and pass
its `max-rounds` to `decide --max-rounds` unchanged. Before round 1 has run, estimate a
round as twice the slowest round-0 scorer run (a reviewer run takes about as long)
plus 5 minutes for fixes and evidence, labelled "estimated"; round 1 replaces it with
the measured time including your own work. Run budgets on time by default. Money is
measured only for claude children run with `--max-usd N` (the footer's `cost USD`;
sum the footers for `--spent`); codex reports tokens and kimi nothing. Otherwise
convert a money budget to time or rounds with the user, say so, and mark any dollar
figure "estimated". Never start a round the remaining budget cannot
finish; `rounds-affordable=0` means stop now and report. Do not invent durations or
spend in the report; quote the `.log` footers.
