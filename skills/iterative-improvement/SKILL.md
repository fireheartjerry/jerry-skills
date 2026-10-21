---
name: iterative-improvement
description: "Improvement loop for designs, pages, UI, code, architecture, docs or copy with no test oracle. Not for debugging one defect, a definite done state, explaining, planning or deploying, or work a test suite or benchmark measures, even if asked to iterate until it passes. Use when the user says improve this, polish this, world class, next level, this is good but, audit and fix it, iterate on this, run N rounds or keep going until it is great, and two competent people could disagree whether it is finished. Weighted criteria, hostile review, isolated scoring."
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 2.3.0
  license: MIT
---

# Iterative Improvement

```
CRITERIA -> ROUND 0 (snapshot, baseline, gates)
  -> [ HOSTILE REVIEW -> TRIAGE -> EXECUTE -> SCORE -> DECIDE ] x N -> REPORT
```

## 1. Fit and mode

Use it only when **two competent people could disagree about whether the work is finished**,
never when a named test suite, benchmark or threshold decides it.

**Full** if any row matches, otherwise **lite**:

| Signal | Mode |
|---|---|
| A round count, a target score or a money or time budget is named | full |
| Over 5 files, or more than one page or screen | full |
| Ships to others (public page, release, client work) | full |
| "world class", "next level", "as good as it can be" or similar | full |
| Lite triage verified 3+ DO NOW or DO rows needing files outside the one file or component | escalate to full |
| Everything else (one file, component or doc) | lite |

Severity alone never escalates. Say which mode and why in one line.
**Lite:** section 2 and nothing else. **Full:** read
`references/full-protocol.md` before asking the user anything. Sections 3 to 5 apply to
both. Interrupt the user only for a tradeoff between criteria or a constraint-breaking
change.

## 2. Lite card

Read `references/reviewer-brief.md`, `references/scorer-brief.md` and
`references/cross-agent.md` first; `references/troubleshooting.md` when a step fails. One
engine may fill both roles in separate processes; the scorer keeps one `--model`.

1. **Propose criteria in one message.** 3 to 5 criteria summing to 100, each with a one-line
   **How to check it** a stranger could run and `0`/`half`/`full` anchors; 1 or 2 gates
   with a `check:` command; Out of scope; Constraints (off-limits); budget line
   "2 rounds max, no commits, T = 3". Derive it from the artifact and conversation, with one
   criterion the work fails today and one it passes. End with "reply ok or edit". No user
   reachable: `**Status:** DERIVED, not user-approved`, a `**Source:**` line per criterion,
   `[ASSUMED: what]` on guesses; the report opens with that (`references/troubleshooting.md`,
   "No user to interview").
2. **Set up.** Create LOOP (section 3); in a repo add `.iterative-improvement/` to
   `.git/info/exclude`. `scripts/spawn_child.sh --preflight <engine> [--model M]` for reviewer
   and scorer.
3. **Round 0.** `scripts/snapshot.py create` to `snapshots/round-0`; `scripts/strip_criteria.py`
   to `criteria-for-scorer.md`; evidence (section 4) into `evidence/round-0/`; one scorer run
   via `spawn_child.sh ... --evidence evidence/round-0` is the baseline. Mark each gate
   `[PRESERVE]` (passes) or `[ACHIEVE]` (fails) in the doc.
4. **Round 1.** Blind review (no criteria shown), CONSTRAINTS and OUT OF SCOPE filled.
   Independence check, triage table (section 4). Check the escalation row now. Fix DO NOW,
   then DO; re-run PRESERVE gates after each change. Snapshot `round-1`, regenerate stripped
   doc and evidence, one scorer run.
5. **Amend, if needed.** A verified blind-review defect no criterion covers: add it,
   rebalance to 100, bump the rubric version, tell the user in one line, rescore the
   `round-0` snapshot once under the new version and start the series there
   (`references/full-protocol.md` section 5). Then `scripts/score_math.py decide
   --threshold 3 --target <user's, else 90> --max-rounds 2 --scores <reference>,<round 1>
   --rounds-used 1 --gates PASS|FAIL`; act on the word (section 4).
6. **Round 2** only on CONTINUE: criteria-aware review with per-criterion scores and the
   PRIOR FINDINGS block filled; same steps; `--rounds-used 2`. Then report (section 5).

Escalation (table row, or rounds, target or budget asked): read
`references/full-protocol.md` and `references/troubleshooting.md` "Lite mode escalation".

## 3. Where things live

- **ROOT**: the git repo root, or outside a repo the folder holding the artifact.
- **LOOP**: `ROOT/.iterative-improvement/<slug>/` (`rounds.md`, `prompts/`, `reviews/`,
  `scores/`, `evidence/`, `snapshots/`, `criteria-for-scorer.md`). Never commit or show it
  to a child.
- **Criteria doc**: `ROOT/docs/criteria/<slug>-criteria.md` in a repo (commit it);
  `ROOT/<slug>-criteria.md` outside one; lite: in LOOP unless the user wants it kept. Tell
  the user both paths.
- **Snapshots**: `scripts/snapshot.py create --root ROOT --out LOOP/snapshots/round-N` skips
  `.git`, LOOP, criteria docs, gitignored and secret-shaped files. Outside a repo pass
  `--path` per artifact file or folder.

## 4. Rules both modes share

**Children.** Every reviewer and scorer run goes through `scripts/spawn_child.sh`: it stages
a copy in a temp dir outside any repo and names only that dir in the prompt; records the
command in `<out>.cmd` and output and timing in `<out>.log`; discards the output (exit 4)
if the copy or ROOT changed: rerun once, then switch engine. Enforcement differs: Claude
children read only the stage; Codex children cannot write but can read anything you can
(only the prompt keeps ROOT and LOOP out of view); kimi has no sandbox and needs
`--unsandboxed`, for trusted artifacts only. Never edit the
artifact while a child runs. Independence order: another agent CLI; the same CLI isolated
(`--same-engine`); a harness subagent on a staged dir; kimi; inline.

**Scorer pin.** Engine, `--model`, flags and brief fixed; every scorer `.cmd`
byte-identical. A new pin is a new instrument: rescore the previous snapshot under it first.

**Evidence.** Every gate `check:` and every check line the scorer cannot run: run it as
written, once per round, into `evidence/round-N/` with a `manifest.md` row (check, command,
UTC time, attempts, retake reason). No choosing among captures. Gates PASS or FAIL from
these runs; the scorer reads them and reports UNKNOWN when one is missing. Evidence you
produce is fine; judgment is not.

**Independence check** before acting on a review: every finding cites a real location, the
review disagrees with you somewhere, two spot-checked findings hold. No citations: discard
and fix access.

**Triage.** One row per reviewer finding and per scorer Note, in `rounds.md`:

```
| F# | Sev | Bucket | Verified | Criterion | Action or reason |
|----|-----|--------|----------|-----------|------------------|
| F1 | major | DO NOW | yes: h2 is 22px at Hero.astro:154 | C3 | raise h2 to 36px |
| F4 | minor | REJECT | yes: alt text present at :88 | C8 | claim false |
| F6 | major | REJECT (repeat) | n/a | - | re-raised without new evidence; see R1 F3 |
```

Buckets: **DO NOW** (high impact, cheap), **DO** (high impact, costly), **DEFER**,
**REJECT**. Each row says how you verified. Take the cheapest change that resolves the
finding, not the reviewer's proposal. Name any conflict with a constraint or another finding
in the row and pick. Outside every criterion and the user's interest: a distraction unless
worth an amendment. Re-raised without new evidence: `REJECT (repeat)`, no re-analysis.
Accepting 100% of a review is transcribing, not evaluating.

**Execute.** DO NOW, then DO; one changelog line per change naming its criterion. A PRESERVE
gate that flips to FAIL is fixed or reverted before scoring; ACHIEVE gates may still fail.
Revert from the previous snapshot (`scripts/snapshot.py restore --dry-run` first; stop if it
would remove files the loop did not create) or `git restore` when commits were allowed.
Never `git stash` as the undo: it hides the round's work from the next snapshot.

**Decide.** `scripts/score_math.py decide` prints one word. CONTINUE: next round. STOP:
report (reason: target-hit, rounds-exhausted, plateau or regression). HALT: a gate still
fails and the budget is used up; report, never extend. CONFIRM: one more scorer run on the
same snapshot, then `--confirm-score`. BLOCKED: a criterion was N/A twice; produce its
evidence or amend it out, do not read the number. A move under T (3 in lite) is noise, never
progress.

## 5. Report

Lead with the result: baseline -> final (raw and, if any N/A, scaled), T, per-criterion
movement, gates with class and status, what changed, what was rejected or deferred and why,
each child's tier (`--same-engine`, inline grading), rounds used against the ceiling, child
runs, wall time per round from the `.log` durations, spend when the CLI reported it (else
"not measured"), whether more rounds are worth it. DERIVED criteria: the first line says so
and names each assumption.

## 6. Files

`references/full-protocol.md` (full mode), `references/criteria-guide.md` (template,
weighting), `references/reviewer-brief.md`, `references/scorer-brief.md`,
`references/cross-agent.md` (invocations, what each engine enforces),
`references/example-loop.md` (a full loop), `references/troubleshooting.md`. Scripts take
`--help`; `tests/run.sh` is offline; `evals/` has triggers, scenarios, rubric, results.
