# Writing criteria that can actually be scored

The criteria document is the target the whole loop aims at. If it is soft, the loop
optimizes softness: the score will climb while the work stays mediocre, because a
vague criterion can be argued into "mostly satisfied" by any change at all.

The test for every criterion is the same: **could a stranger, given only this
document and the artifact, arrive at roughly the same score as you?** If not, the
criterion is not finished.

---

## The template

Save at `docs/criteria/<slug>-criteria.md` under the repo root. Outside a git repo,
save as `<slug>-criteria.md` in the folder holding the artifact (ROOT). In a repo,
commit the criteria doc; it is the spec the loop ran against. In lite mode keep it in
the loop dir unless the user asks to keep it. Never commit `.iterative-improvement/`.

The `<!-- loop-only -->` markers are load bearing: `scripts/strip_criteria.py` removes
everything between them, plus the `## Score log` and `## Amendments` sections and the
gate class tags, before the scorer sees the document. Anything the scorer must not
read goes inside a marked block; the markers may appear more than once. The strip
check is whole-word, so "around 16px" and "<baseline SHA>" are fine, but a check line
should carry the literal SHA anyway (the scorer has to be able to run it).

```markdown
# Criteria: <what we are improving>

**Artifact:** <path, URL, or description of what is scored>
**Status:** APPROVED by user on <date>   (or: DERIVED, not user-approved. Sources: <list>)
**Rubric version:** v1

<!-- loop-only -->
**Mode:** full | lite
**Target:** <score>/100   **Budget:** <N rounds | until target>
**Baseline score:** <mean of two runs>   **Noise floor:** n=<a-b>   **Move threshold:** T=<max(3,2n)>
**Baseline SHA:** <git rev-parse HEAD at round 0, or "none">
**Scorer pin:** <engine, model; flags as recorded in .iterative-improvement/<slug>/scores/round-0a.cmd>
**Loop dir:** .iterative-improvement/<slug>/
<!-- /loop-only -->

## Context

<2 to 4 sentences: what this is, who it is for, what success changes. Written so a
stranger with no memory of the interview can score against it.>

## Gates (binary, reported separately from the score)

A failed gate means the work is not deliverable regardless of points. Gate checks are
run by the orchestrator before scoring; their output is supplied as evidence.
<!-- loop-only -->
Class is set at round 0: PRESERVE passes at baseline and must stay passing; ACHIEVE
fails at baseline and must pass by the end.
<!-- /loop-only -->

- **G1.** [PRESERVE|ACHIEVE] <condition> | *check:* <exact command>
- **G2.** [PRESERVE|ACHIEVE] <condition> | *check:* <exact command>

## Criteria (100 points)

### C1. <Short name> (<n> pts)
**Source:** <interview answer, spec section, README heading, issue or commit; required
on every criterion when the doc is DERIVED, and the tag `[ASSUMED: what]` follows the
title of any criterion, weight or threshold that is a guess>
**What it means:** <one or two sentences, concrete>
**How to check it:** <an exact action or command a stranger can perform; this line is
the only legal way to produce evidence for this criterion. If the scorer's environment
cannot perform it (browser, build tool, network), it must be an exact command the
orchestrator can run and hand over as evidence.>
**Anchors:**
- `0`: <what total failure looks like>
- `half`: <what partial looks like>
- `full`: <what success looks like>

### C2. ...

## Out of scope

<What is deliberately not judged, so the reviewer does not spend rounds on it and the
scorer does not silently penalise it.>

## Constraints

<Hard rules the loop must not break: brand rules, stack, off-limits files, decisions
already made.>

## Score log

| Round | Rubric | Raw | Adj max | Scaled | Delta | N/A | Gates | Note |
|-------|--------|-----|---------|--------|-------|-----|-------|------|
| 0a    | v1     |     | 100     |        |       |     |       | baseline run A |
| 0b    | v1     |     | 100     |        |       |     |       | baseline run B |
| 0     | v1     |     | 100     |        |       |     |       | baseline = mean, n, T |

## Amendments

<One line per rubric version: what changed, which criteria lost points, and the
rescored total of the previous snapshot under the new version.>
```

A filled copy of this template, with every round logged, is in
`references/example-loop.md`.

---

## Good vs. useless criteria

The difference is always the same: useless criteria describe a *feeling*, good
criteria describe the *observable consequence* of that feeling.

**Useless:** "The design looks modern and premium."
Nobody can score this. Every reviewer will project their own taste, and the score
will swing 20 points between rounds for no reason.

**Good:**
```
### C3. Typographic hierarchy (12 pts)
**What it means:** A reader scanning for 3 seconds can identify the page's single
most important message without reading body copy.
**How to check it:** Screenshot at 1440px, blur to 8px (`convert shot.png -blur 0x8
blurred.png`). The primary message should still be the first thing the eye lands on,
by size or weight contrast alone.
**Anchors:**
- `0`: blurred, nothing dominates; three elements compete at the same weight
- `6`: one element dominates but a secondary element competes at the same level
- `12`: a clear single focal point, with a deliberate second and third tier
```

---

**Useless:** "The code should be clean and maintainable."

**Good:**
```
### C2. Change locality (10 pts)
**What it means:** A typical feature change touches few files, and the structure makes
it obvious which ones.
**How to check it:** For each of the three change requests below (fixed at sign-off),
read the tree and list the files you would have to edit, excluding tests and lockfiles.
Report the three counts and their median. (a) Add a field to the user profile and show
it on the settings page. (b) Add a new notification channel next to email. (c) Change
the date format everywhere a date is displayed.
**Anchors:**
- `0`: median > 12 files, or the set cannot be determined from the structure
- `5`: median 6 to 12 files
- `10`: median <= 5 files, all within one or two coherent modules
```

This one moves when the loop restructures the code, and the scorer can do it from the
snapshot alone. A check that reads git history (`git log --stat`) cannot: the snapshot
has no `.git`, and the loop's own commits would pollute the sample.

---

**Useless:** "Better user experience on mobile."

**Good:**
```
### C5. Mobile scroll integrity (8 pts)
**What it means:** Every screen of scroll on a phone shows something. No dead
stretches, no horizontal overflow.
**How to check it:** `node scripts/frames.mjs http://localhost:4321 390 844 300`
writes a screenshot every 300px to `frames/` and prints the document scrollWidth and
the indices of identical consecutive frames.
**Anchors:**
- `0`: any horizontal overflow, or > 2 screens of identical frames
- `4`: no overflow, one dead stretch under a screen
- `8`: no overflow, no dead stretch, every frame carries content
```

The check names a command rather than "take screenshots" because the scorer has no
browser: the orchestrator runs exactly that command once and hands over the frames.

---

## Rules of thumb

**6 to 12 criteria, summing to 100** (lite mode: 3 to 5). Fewer and the score is too
coarse to show round-over-round progress: you will move real work and the number will
not budge, which destroys the feedback signal. More and scoring becomes noisy
guesswork, and the scorer starts pattern-matching instead of checking.

**Weight by what the user said matters, not by what is easy to check.** The
temptation is to over-weight measurable things (bundle size, contrast ratios)
because they score cleanly. Resist it. If the user cares most about how the hero
*feels*, that criterion gets the most points, and you do the harder work of writing
a real check for it.

**Include one criterion the work currently fails badly, and one it already passes.**
Without a known-fail you cannot tell whether the scorer discriminates. Without a
known-pass you cannot tell whether it is just being harsh across the board. These two
calibrate everything else.

**Write the anchors before you write the weight.** If you cannot describe what `0`
and `full` look like, you do not understand the criterion well enough to weight it.

**Gates are not criteria.** "It builds", "no accessibility regressions", "brand
colors unchanged" are pass/fail conditions, not things you get partial credit for.
Averaging a broken build into a 94% produces a number that actively misleads.
Keep them separate and report them separately. Every gate check is a command the
orchestrator runs before each scoring run; the scorer reads the result from the
evidence directory and reports PASS, FAIL or UNKNOWN (no evidence supplied).

**Gates that fail today are fine.** The rejection question often names things the
work currently gets wrong ("I would reject it if it still overflows on a phone").
Those become ACHIEVE gates: they are scored alongside from round 0, they are the first
DO NOW items in round 1, and the loop halts with them listed if they still fail when
the budget runs out. PRESERVE gates are the ones that pass today and must keep
passing. The report always shows each gate's class and status.

**Prefer checks that produce evidence.** A check that yields a screenshot, a
measurement, a count, or a quoted line lets the scorer cite something. A check that
yields only an opinion invites inflation. If the check needs tooling the scorer lacks,
write the exact command; "take screenshots every 300px" cannot be run once and logged,
`node scripts/frames.mjs ... 300` can. A check that reads git history must pin its
range to commits before the baseline, with the literal SHA in the check line (as the
example's G3 does); otherwise the loop scores its own activity.

---

## Amending criteria mid-loop

The round-1 blind review exists partly to find gaps in the criteria. When it
surfaces something genuinely important that no criterion covers:

1. Add the criterion, with anchors.
2. Rebalance weights back to 100 (say which criteria lost points).
3. Tell the user what you added and why. A criteria document that quietly mutates
   makes the score log meaningless.
4. Bump the rubric version and add a line to `## Amendments`.
5. **Rescore the previous round's snapshot under the amended rubric.** The snapshot is
   in `.iterative-improvement/<slug>/snapshots/round-N/`. Run the pinned scorer on it
   with the v2 criteria (stripped as usual). Full mode: run it twice, take the mean as
   the new reference and recompute `n` and `T` with `score_math.py baseline`, because
   the rubric is a new instrument. Lite: once. Log it as `58 -> 54 (rubric v2,
   rescored)` and use the v2 series for every later comparison, passing the real round
   number to `score_math.py decide --rounds-used`. Do not re-tally by hand: re-weighting
   non-linear anchors has no defined arithmetic, and scoring the new criterion yourself
   is the orchestrator grading its own work.

   Adding a criterion the work fails, funded by shaving points off criteria near full
   marks, lowers the total mechanically. That drop is bookkeeping, never a regression;
   the rescored v2 number is the new reference point.

Do **not** amend criteria to make a change you already made look better. That is
moving the goalposts, and it is the fastest way to turn the score into decoration.
If a change does not score, that is information: either the change was not valuable,
or the criteria were wrong *before* you made it. Only the second one justifies an
amendment, and only if you would have made the same amendment beforehand.
