---
name: iterative-improvement
description: "Improvement loop for existing designs, pages, UI, code, architecture, docs or copy with no test oracle. Not for building from scratch, a one-line edit, debugging one defect, a definite done state, explaining, planning or deploying, or work a test suite, linter or metric measures, even if asked to iterate until it passes. Use when the user says improve this, polish this, world class, next level, this is good but, audit and fix it, iterate on this, run N rounds or keep going until it is great, and two competent people could disagree whether it is finished."
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 2.4.0
  license: MIT
  requires: bash, python3 (3.8+), git (optional), and the codex or claude CLI for independent children (kimi experimental, not verified)
---

# Iterative Improvement

```
CRITERIA -> ROUND 0 (snapshot, baseline, gates)
  -> [ HOSTILE REVIEW -> TRIAGE -> EXECUTE -> SCORE -> DECIDE ] x N -> REPORT
```

**Paths.** `scripts/` and `references/` are relative to the folder holding this SKILL.md
(under `~/.claude/skills`, `~/.codex/skills` or a plugin cache), not the project: resolve
it once as `SKILL_DIR` and run `"$SKILL_DIR/scripts/<name>"`.

## 1. Fit and mode

Use it only on existing work when **two competent people could disagree about whether it
is finished**, never when a named test suite, benchmark or threshold decides it.

**Full** if any row matches, otherwise **lite**:

| Signal | Mode |
|---|---|
| A round count, a target score or a money or time budget is named | full |
| Over 5 files, or more than one page or screen | full |
| Delivered to others: a launch, release or client deliverable (a public repo alone is not) | full |
| "world class", "next level", "as good as it can be" or similar | full |
| Lite triage verified 3+ DO NOW or DO rows needing files outside the one file or component | escalate to full |
| Everything else (one file, component or doc) | lite |

Severity alone never escalates. Say which mode and why in one line.
**Lite:** sections 2 to 5. **Full:** read `references/full-protocol.md` before asking
the user anything, then sections 3 to 5. Interrupt the user only for a tradeoff between
criteria, a constraint-breaking change or a growing plan (engine switch, more rounds).

## 2. Lite card

Read `references/reviewer-brief.md`, `references/scorer-brief.md` and
`references/cross-agent.md` first; `references/commands.md` has every command,
`references/troubleshooting.md` every failure. One engine may fill both roles in
separate processes; the scorer keeps one `--model`.

1. **Propose, then stop.** One message: 3 to 5 criteria summing to 100, each with a
   one-line **How to check it** a stranger could run and numeric anchors (0, half,
   weight), one the work fails today and one it passes; 1 or 2 gates with a `check:`
   command, the first the repo's verify command (package.json scripts, Makefile, CI,
   AGENTS.md or CLAUDE.md) as PRESERVE, or "no verify command found"; Out of scope;
   Constraints; the scope (`--path` list: the artifact and what it is checked against;
   `snapshot.py create --dry-run` lists the files); the budget "2 rounds max, no
   commits, T = 3"; the spend: engines and vendors, 2 preflights and 3 to 7 child runs
   of 6 to 10 minutes each, each a billed call sending those files to that vendor.
   End with "reply ok or edit" and end your turn:
   nothing is created or spawned before the reply.
   No user reachable: do not stop. `**Status:** DERIVED, not user-approved`, a
   `**Source:**` per criterion, `[ASSUMED: what]` on guesses; only engines already
   configured, within the run count; say both first in the report
   (`references/troubleshooting.md`, "No user to interview").
2. **Set up.** `scripts/loop.py init --root ROOT --slug S --scope <path> ...` makes
   LOOP and records what was agreed (`--engines`, `--off-limits`).
   `scripts/spawn_child.sh --check`, then `--preflight <engine> [--model M]` for
   reviewer and scorer. Write the doc from the template in
   `references/criteria-guide.md`; `score_math.py check-criteria <doc> --mode lite`
   passes.
3. **Round 0.** `scripts/snapshot.py create --path ...` to `snapshots/round-0`;
   `scripts/strip_criteria.py` to `criteria-for-scorer.md`; evidence (section 4) into
   `evidence/round-0/`; one scorer run (`spawn_child.sh ... --evidence
   evidence/round-0`, then `score_math.py check-scorer`) is the reference. Mark each
   gate `[PRESERVE]` (passes) or `[ACHIEVE]` (fails) in the doc.
4. **Round 1.** Blind review of `round-0` (no criteria; the evidence view), CONSTRAINTS
   and OUT OF SCOPE filled. Independence check, triage (section 4), the escalation row.
   A verified defect no criterion covers: amend before scoring, and rescore `round-0`
   once under the new version: the new reference (full-protocol.md section 5). Fix DO
   NOW, then DO; re-run PRESERVE gates after each change. Snapshot `round-1`, regenerate
   stripped doc and evidence, one scorer run.
5. **Decide.** `scripts/score_math.py decide --threshold 3 --target 90 --max-rounds 2
   --scores <reference>,<round 1> --rounds-used 1 --gates PASS|FAIL`; act on the word
   (section 4). T = 3 is a floor, not measured noise: before acting on `STOP
   regression`, score the same snapshot once more.
6. **Round 2** only on CONTINUE: review of `round-1` with the stripped criteria,
   per-criterion scores, PRIOR FINDINGS and PROTECTED filled; same steps;
   `--rounds-used 2`. Then report (section 5).

Escalation (table row, or rounds, target or budget asked): read
`references/full-protocol.md` and `references/troubleshooting.md` "Lite mode escalation".

## 3. Where things live

- **ROOT**: the git repo root, or outside a repo the folder holding the artifact.
- **LOOP**: made by `loop.py init` inside the git dir (`$(git rev-parse
  --absolute-git-dir)/iterative-improvement/<slug>/`, invisible to repo tools, fine in
  worktrees), outside a repo `ROOT/.iterative-improvement/<slug>/`: `rounds.md`
  (kickoff, triage, KEEP list), `prompts/`, `reviews/`, `scores/`, `evidence/`,
  `snapshots/`, `tools/`. Never show it to a child.
- **Criteria doc**: full mode `ROOT/docs/criteria/<slug>-criteria.md` (repo) or
  `ROOT/<slug>-criteria.md`; lite: in LOOP. Uncommitted unless commits were allowed;
  tell the user the path.
- **Snapshots**: `snapshot.py create --root ROOT --path <scope> --out
  LOOP/snapshots/round-N`, always with `--path`; it names every file it skips
  (gitignored, credential-shaped, the criteria doc).
- **Resume** after a pause or compaction: `loop.py status --loop LOOP`, then
  `rounds.md`; never rely on chat.

## 4. Rules both modes share

**Children.** Every reviewer and scorer run goes through `scripts/spawn_child.sh`: it stages
a copy outside any repo, names only that dir in the prompt, records `<out>.cmd` and
`<out>.log`. Exit 4 discards the output: the child changed its copy (rerun once, then
switch engine) or someone changed the snapshot's files in ROOT (let edits settle, rerun;
another engine cannot help). A run takes 6 to 10 minutes, past most harnesses'
foreground limit: always pass `--background` (or use the harness's background mode)
and wait for `<out>.status`. What each engine can reach: `references/cross-agent.md`;
kimi has no sandbox and needs `--unsandboxed`. Never edit the artifact while a child
runs. Independence order: another agent CLI; the same CLI isolated (`--same-engine`); a
harness subagent on a staged dir; kimi; inline.

**Scorer pin.** Engine, full `--model` id, flags and brief fixed; scorer `.cmd` files
identical but for the CLI patch version. A new pin is a new instrument: rescore the
previous snapshot under it first.

**Evidence.** Every gate `check:` and every check line the scorer cannot run:
`scripts/evidence.py run` runs it as written, once per round, into `evidence/round-N/`
and logs it in `manifest.md`; a retake only when the tool failed before measuring. Gates
PASS or FAIL from these runs; the scorer reports UNKNOWN when one is missing. Children
reach no server: hand over built output and captures. Evidence you produce is fine;
judgment is not.

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
finding, not the reviewer's proposal. Name any conflict with a constraint, another finding
or a KEEP item in the row and pick. Outside every criterion: a distraction unless worth an
amendment. Accept what verification supports, all of it if every row verifies. The
review's "What is working" goes into the KEEP list.

**Execute.** DO NOW, then DO. A verified finding is a class: search for its other
instances (grep, selector, the check line), fix those in scope in the same row, log the
count. One changelog line per change naming its criterion; spot-check KEEP items before
scoring. A PRESERVE gate that flips to FAIL is fixed or reverted before scoring; ACHIEVE
gates may still fail. Revert with `snapshot.py restore --only <files the loop changed>`
(`restore --dry-run` first; stop if it lists a file the loop did not change) or `git
restore` when commits were allowed. Never `git stash` as the undo.

**Decide.** `scripts/score_math.py decide` prints one word. CONTINUE: next round. STOP:
report. HALT: a gate still fails and the budget is used up; report, never extend.
CONFIRM: one more scorer run on the same snapshot, then `--confirm-score`. BLOCKED: a
criterion was N/A twice; produce its evidence or amend it out. A move under T is noise.
After each round post one line: round, score, gates, decision word, elapsed time.

## 5. Report

First, `snapshot.py restore --snapshot LOOP/snapshots/round-<last scored> --root ROOT
--dry-run` must list nothing (after a revert, name the round the score belongs to).
Full mode then runs the blind judge (`references/judge-brief.md`) and reports its
verdict beside the score; a baseline win means stop and tell the user.

Lead with the result: reference -> final (scaled too if any N/A; an older baseline marked
not comparable), gates with class and status, per-criterion movement, what changed, what
was rejected or deferred and why, what needs the user. Then a short footer: T (lite: a
floor, noise not measured), each child's tier, rounds and child runs used, wall time from
the `.log` durations, spend if reported (else "not measured"). DERIVED criteria: the first
line says so and names each assumption.

## 6. Files

Also `references/criteria-guide.md` (template, evidence tools), `references/isolation.md`
(how children are isolated) and `references/example-loop.md` (a full loop), `references/example-lite.md` (a lite loop). Scripts take
`--help`; `tests/run.sh` is offline; `evals/` has triggers, scenarios, rubric, results.
