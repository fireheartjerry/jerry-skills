---
name: iterative-improvement
description: "Criteria-driven improvement loop for work that has no test oracle. Not for debugging one defect, tasks with a definite done state, or work already measured by tests or a benchmark. Use when the user says improve this, polish this, make it world class, take it to the next level, this is good but, keep going until it's great, iterate on this, or run N rounds, and two competent people could disagree on whether it is finished. Interviews the user into weighted, scoreable criteria with gates, then cycles hostile independent review, triage, fixes and isolated scoring until target, budget or plateau."
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 2.1.0
  license: MIT
---

# Iterative Improvement

The target becomes a weighted, scoreable document the user approves; the critic and the
scorer are independent of the agent doing the work and of each other.

```
INTERVIEW -> CRITERIA DOC -> ROUND 0 (snapshot, score twice, classify gates)
  -> [ HOSTILE REVIEW -> TRIAGE -> EXECUTE -> SCORE -> DECIDE ] x N -> REPORT
```

## 1. Fit and mode

Use the loop only when **two competent people could disagree about whether the work is
finished**. Not for one wrong behaviour (debug it), a definite done state such as "add a
login page" (build it), or work a named test suite, benchmark or threshold already
measures (run that, even when asked to "iterate until it passes").

Pick a mode: **full** if any of these hold, otherwise **lite**:

| Signal | Mode |
|---|---|
| User names a round count or a target score | full |
| Artifact spans more than 5 files, or more than one page or screen | full |
| Work ships to other people (public page, release, client deliverable) | full |
| User says "world class", "next level", "as good as it can be" or similar | full |
| A lite round found 3 or more `major` or `critical` findings | escalate |
| Everything else (one file, one component, one doc, no numbers asked) | lite |

**Lite mode** is the same loop with the ceremony removed. One message proposes 3 to 5
criteria, 1 or 2 gates and the budget defaults (2 rounds, no commits, off-limits =
Constraints), ending "reply ok or edit"; that message is Phases 1 and 2 together. Then
one baseline score, one blind review, a triage table, the fixes, one score; round 2 if
run is criteria-aware; a short report. No noise measurement, `T = 3`. The criteria doc
stays in LOOP unless the user wants it kept. Everything below applies unless marked
*full only*. Say which mode you chose and why in one line.

## 2. Where things live

- **ROOT**: the git repo root, or outside a repo the folder holding the artifact.
- **LOOP**: `ROOT/.iterative-improvement/<slug>/`: round log (`rounds.md`), briefs,
  reviews, scores, evidence, snapshots. In a repo add `.iterative-improvement/` to
  `.git/info/exclude`. Never commit it, never show it to a child.
- **Criteria doc**: `ROOT/docs/criteria/<slug>-criteria.md` in a repo (user-facing,
  commit it); `ROOT/<slug>-criteria.md` outside one; lite: in LOOP.
- **Snapshots**: `scripts/snapshot.py create --root ROOT --out LOOP/snapshots/round-N`
  copies the artifact without `.git`, LOOP, criteria docs, gitignored or secret-shaped
  files. Outside a repo always pass `--path` per artifact file or folder, or the whole
  folder goes to a third-party CLI.
- **Children** never see ROOT, LOOP or a snapshot path: `scripts/spawn_child.sh` runs
  them on a copy in a fresh temp dir outside any repo (agent instruction files renamed
  `*.as-data`) and deletes it.

Tell the user both paths at sign-off.

## 3. Phase 1: criteria

Read **`references/criteria-guide.md`** now (template, worked criteria, weighting).

*Full only:* interview in batches of 3 to 5 questions, usually two batches. Cover:
outcome (who looks, what they should do or feel), references with the specific thing
that is excellent about each, anti-references, hard constraints, **the rejection
question** ("what would make you reject this even if everything else were perfect"; the
answers are gates), priorities ("only three fixes"), and done ("what would make you say
stop"). Extract answers already in the conversation, a spec or a prior audit first and
bring them for confirmation instead of asking cold.

Write the document: 6 to 12 criteria (lite: 3 to 5) summing to 100, each with a **How to
check it** line a stranger could execute and three anchors; gates listed separately with
a `check:` command; Out of scope; Constraints. A check needing tooling the scorer lacks
(browser, build, network) names the exact command you will run as evidence. Include one
criterion the work fails badly and one it passes, or the scorer cannot be calibrated.

**Sign-off**: show the doc and ask for corrections before anything runs. No user
reachable (headless `-p`/`exec`, auto-permission mode, offline)? Do not stop and do not
pretend: derive each criterion from a real source (README, docs, issues, commits, prior
audit) with its own `**Source:**` line, set `**Status:** DERIVED, not user-approved.
Sources: ...` at the top, tag every guess `[ASSUMED: what]` (weights and thresholds
included), and open the final report by saying the criteria were never approved and
naming each assumption (`references/troubleshooting.md`).

## 4. Phase 2: budget

*Full only* (lite folds these into the proposal). Ask three things, then run unattended
until a stop rule fires:

1. **Stop condition**: target score or round count. Default: 5 rounds, stop early at 90.
   A round count is a ceiling, not a goal; say so.
2. **Off-limits**: files, decisions, areas the loop must not touch (goes into Constraints).
3. **Commits** (repo only): may the loop commit each round on the current branch?
   Default no; snapshots give the revert path either way.

Interrupt only for a decision only the user can make: a real tradeoff between two
criteria, or a change that would break a constraint.

## 5. Phase 3: the loop

Before round 0, read **`references/cross-agent.md`**, pick the reviewer and scorer
engines and run `scripts/spawn_child.sh --preflight <engine> [--model M]` for each. The
scorer is **pinned** for the whole loop: engine, `--model`, flags, brief file. Every
child runs through `spawn_child.sh`, which records the command to `scores/round-N.cmd`
(byte-identical across rounds for the scorer) and the log to `scores/round-N.log`.

### Round 0: baseline (once)

1. Snapshot the unmodified artifact to `snapshots/round-0`.
2. Strip the doc: `scripts/strip_criteria.py <criteria> LOOP/criteria-for-scorer.md`.
3. Run every gate's `check:` and every check line the scorer cannot run, once each,
   into `evidence/round-0/` with its `manifest.md` (5d step 3).
4. *Full only:* run the scorer **twice** on the same snapshot and evidence
   (`scores/round-0a.md`, `round-0b.md`). `scripts/score_math.py baseline A B` returns
   the baseline (mean, half up), the noise floor `n = |A - B|` and `T = max(3, 2n)`. If
   `n > 8` the criteria do not discriminate: tighten the check lines where the two runs
   disagreed most and run both again.
5. Classify every gate from your gate runs: **PRESERVE** (PASS at baseline, must stay
   PASS) or **ACHIEVE** (FAIL at baseline, must PASS by the end); write the class next to
   each gate in the doc. ACHIEVE gates are round 1's first DO NOW items.
6. Record baseline, `n`, `T`, scorer pin and (repo) baseline SHA in the doc's loop-only
   block, and row 0 of the score log.

### 5a. Hostile review

Independence order: a different agent CLI; the same CLI as an isolated process
(`spawn_child.sh --same-engine`); a harness subagent given only a staged dir; inline.
Fill every slot of **`references/reviewer-brief.md`**:
CONSTRAINTS, OUT OF SCOPE, how to view the artifact, and from round 2 the criteria,
per-criterion scores and PRIOR FINDINGS (title, location, status: addressed, deferred,
or rejected with its one-line reason). A rejected finding may return only with new
evidence, tagged `[re-raised: new evidence]`.

Round 1 is **blind**: no criteria, weights or scores. It finds what the criteria missed;
when it does, amend them (section 5d) before continuing.

Verify independence before acting on a review: every finding cites a real location; the
review disagrees with you somewhere; two spot-checked findings hold. A review with no
citations never reached the artifact: discard it and fix access.

### 5b. Triage

Write a table into `rounds.md` with **one row per reviewer finding**:

```
| F# | Sev | Bucket | Verified | Criterion | Action or reason |
|----|-----|--------|----------|-----------|------------------|
| F1 | major | DO NOW | yes: h2 is 22px at Hero.astro:154 | C3 | raise h2 to 36px |
| F4 | minor | REJECT | yes: alt text present at :88 | C8 | claim false |
| F6 | major | REJECT (repeat) | n/a | - | re-raised without new evidence; see R1 F3 |
```

Buckets: **DO NOW** (high impact, cheap), **DO** (high impact, costly), **DEFER**,
**REJECT**. Every row says how you verified. Pick the cheapest change that resolves the
finding, not the reviewer's proposal by default. If a finding conflicts with a
constraint or another finding, name the conflict in the row and pick. A finding outside
every criterion and the user's stated interest is a distraction unless it is important
enough to amend the criteria. A re-raised finding without new evidence is `REJECT
(repeat)` with no re-analysis. Accepting 100% of a hostile review means you transcribed,
not evaluated. The scorer's Notes are findings too; give them rows.

### 5c. Execute

Work DO NOW, then DO. Keep a changelog line per change naming its criterion. After each
change re-run every PRESERVE gate's `check:`; a PRESERVE gate that flips to FAIL is fixed
or reverted before scoring. ACHIEVE gates may still fail. Revert path: the previous
snapshot, or `git restore` when commits were allowed. Run `scripts/snapshot.py
restore --dry-run` first and stop if it would remove files the loop did not create (a
real restore backs up what it replaces).
Never `git stash` as the undo: it hides the round's work from the next snapshot.

### 5d. Score

1. Snapshot the current artifact to `snapshots/round-N`.
2. Regenerate `criteria-for-scorer.md` with `strip_criteria.py`.
3. Run every gate's `check:` and every check line the scorer cannot run, **exactly as
   written, once**, into `evidence/round-N/`, logged in its `manifest.md`: check,
   command, timestamp, attempts, reason for any retake. No choosing among captures. Gate
   results come from these runs; the scorer only reads them (PASS, FAIL, or UNKNOWN when
   you forgot one). Evidence you produce is fine; judgment is not. Every round, or the
   scorer marks N/A.
4. Spawn the pinned scorer with **`references/scorer-brief.md`**:
   `spawn_child.sh <engine> scorer <brief> <snapshot> scores/round-N.md --model M --evidence evidence/round-N`.
   It sees the stripped criteria (pasted in the brief), the staged copy and the
   evidence, nothing else.
5. `spawn_child.sh` re-hashes the staged copy and ROOT's git status afterwards. If either
   changed, the output is discarded (exit 4): rerun the stricter variant (Claude with
   `--no-bash`, kimi replaced). Do not edit the artifact while a child runs.
6. Log total, adjusted max, scaled total (`round(points * 100 / adjusted max)`), N/A list
   and gate results to the score log, and a per-criterion table to `rounds.md`:
   `| # | Before | After | Delta | Change that moved it |`.

**Amending criteria** (new criterion found by the blind review, or a dead criterion):
bump the rubric version, rebalance to 100, tell the user, then **rescore the previous
round's snapshot** with the pinned scorer under the new rubric (full mode: twice, and
recompute `n` and `T` from the pair). Log `58 -> 54 (rubric v2, rescored)` and compare
later rounds with the v2 series. Never re-tally by hand; that is the orchestrator scoring
its own work.

### 5e. Decide

`scripts/score_math.py decide --threshold T --target X --max-rounds R --scores <series>
--rounds-used N --gates PASS|FAIL`: the series starts at the baseline or the rescored
reference, `N` is the real round number of the last entry (the series is shorter than
the loop after an amendment), `--gates` comes from your gate runs. First match wins:

1. **Any gate FAIL and rounds exhausted**: halt, report with gates listed. Never extend
   the budget yourself.
2. **Regression**: the last delta is at or beyond `-T`, or the last two deltas sum to
   at or beyond `-T`: stop, diff the per-criterion scores, revert or explain.
3. **Target reached** (score >= target, zero N/A, all gates PASS): run the scorer once
   more on the same snapshot; target hit if the mean of both runs >= target. Report.
4. **Rounds exhausted**: report.
5. **N/A in two consecutive rounds for the same criterion**: protocol violation. Produce
   the evidence or amend the criterion out; do not read the number.
6. **Plateau**: the last two deltas are each inside `T` and together do not exceed `T`
   (`58, 71, 76, 79` at `T=8`: +5 and +3, total 8). Stop even with budget left; say what
   is left and which gap needs the user. Two sub-T deltas adding up past `T` are a slow
   real trend: continue.
7. Otherwise continue. A single move inside `T`, up or down, is "within noise", never
   progress toward the target.

## 6. Report

Lead with the result: baseline -> final (raw and, if any N/A, scaled), `T`, per-criterion
movement, gates with class and status, what changed, what was rejected or deferred and
why, the tier each child ran at (name `--same-engine` and inline grading), and whether
more rounds are worth it. DERIVED criteria: the first line says so and names each
assumption.

## 7. Also

- `references/example-loop.md`: one complete loop with every artifact filled. Read it
  when unsure what a filled criteria doc, triage table, manifest or report looks like.
- Every script in `scripts/` has `--help`; `tests/run.sh` checks them without spending
  tokens.
