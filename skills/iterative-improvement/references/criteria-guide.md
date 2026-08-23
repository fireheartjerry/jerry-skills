# Writing criteria that can actually be scored

The criteria document is the target the whole loop aims at. If it's soft, the loop
optimizes softness — the score will climb while the work stays mediocre, because a
vague criterion can be argued into "mostly satisfied" by any change at all.

The test for every criterion is the same: **could a stranger, given only this
document and the artifact, arrive at roughly the same score as you?** If not, the
criterion isn't finished.

---

## The template

Save at `docs/criteria/<slug>-criteria.md`.

```markdown
# Criteria: <what we are improving>

**Artifact:** <path, URL, or description of what's being scored>
**Target:** <score>/100    **Budget:** <N rounds | until target>
**Created:** <date>    **Baseline score:** <filled in after round 0>

## Context

<2–4 sentences: what this is, who it's for, what success changes. Written so
someone with no memory of the interview can score against it.>

## Gates — binary, non-negotiable

Reported separately from the score. A failed gate means the work is not
deliverable regardless of points.

- **G1.** <condition> — *check:* <how to verify>
- **G2.** <condition> — *check:* <how to verify>

## Criteria — 100 points total

### C1. <Short name> — <n> pts
**What it means:** <one or two sentences, concrete>
**How to check it:** <an action a stranger can perform>
**Scoring anchors:**
- `0` — <what total failure looks like>
- `half` — <what partial looks like>
- `full` — <what success looks like>

### C2. …

## Out of scope

<Things deliberately not being judged, so the reviewer doesn't waste rounds
on them and the scorer doesn't silently penalize them.>

## Constraints

<Hard rules the loop must not break: brand rules, stack, files that are
off-limits, decisions already made that shouldn't be relitigated.>

## Score log

| Round | Score | Δ | Gates | Note |
|-------|-------|---|-------|------|
| 0     | —     | — | —     | baseline |
```

---

## Good vs. useless criteria

The difference is always the same: useless criteria describe a *feeling*, good
criteria describe the *observable consequence* of that feeling.

**Useless:** "The design looks modern and premium."
Nobody can score this. Every reviewer will project their own taste, and the score
will swing 20 points between rounds for no reason.

**Good:**
```
### C3. Typographic hierarchy — 12 pts
**What it means:** A reader scanning for 3 seconds can identify the page's single
most important message without reading body copy.
**How to check it:** Screenshot at 1440px, blur to 8px. The primary message should
still be the first thing the eye lands on, by size or weight contrast alone.
**Anchors:**
- `0` — blurred, nothing dominates; three elements compete at the same weight
- `6` — one element dominates but a secondary element competes at the same level
- `12` — a clear single focal point, with a deliberate second and third tier
```

---

**Useless:** "The code should be clean and maintainable."

**Good:**
```
### C2. Change locality — 10 pts
**What it means:** A typical feature change touches few files, and it's obvious
from the structure which ones.
**How to check it:** Pick the three most recent feature commits. Count files
touched per commit, excluding tests and lockfiles.
**Anchors:**
- `0` — median > 12 files, changes spread across unrelated modules
- `5` — median 6–12 files
- `10` — median ≤ 5 files, all within one or two coherent modules
```

---

**Useless:** "Better user experience on mobile."

**Good:**
```
### C5. Mobile scroll integrity — 8 pts
**What it means:** Every screen of scroll on a phone shows something. No dead
stretches, no horizontal overflow.
**How to check it:** At 390×844, screenshot every 300px from top to bottom of the
document. Flag any two consecutive screenshots that are identical, and any frame
with a horizontal scrollbar.
**Anchors:**
- `0` — any horizontal overflow, or > 2 screens of identical frames
- `4` — no overflow, one dead stretch under a screen
- `8` — no overflow, no dead stretch, every frame carries content
```

---

## Rules of thumb

**6–12 criteria, summing to 100.** Fewer and the score is too coarse to show
round-over-round progress — you'll move real work and the number won't budge, which
destroys the loop's feedback signal. More and scoring becomes noisy guesswork, and
the scorer starts pattern-matching instead of checking.

**Weight by what the user said matters, not by what's easy to check.** The
temptation is to over-weight measurable things (bundle size, contrast ratios)
because they score cleanly. Resist it — if the user cares most about how the hero
*feels*, that criterion gets the most points, and you do the harder work of writing
a real check for it.

**Include one criterion the work currently fails badly, and one it already passes.**
Without a known-fail you can't tell whether the scorer discriminates. Without a
known-pass you can't tell whether it's just being harsh across the board. These two
calibrate everything else.

**Write the anchors before you write the weight.** If you can't describe what `0`
and `full` look like, you don't understand the criterion well enough to weight it.

**Gates are not criteria.** "It builds", "no accessibility regressions", "brand
colors unchanged" are pass/fail conditions, not things you get partial credit for.
Averaging a broken build into a 94% produces a number that actively misleads.
Keep them separate and report them separately.

**Prefer checks that produce evidence.** A check that yields a screenshot, a
measurement, a count, or a quoted line lets the scorer cite something. A check that
yields only an opinion invites inflation.

---

## Amending criteria mid-loop

The round-1 blind review exists partly to find gaps in the criteria. When it
surfaces something genuinely important that no criterion covers:

1. Add the criterion, with anchors.
2. Rebalance weights back to 100 (say which criteria lost points).
3. Tell the user what you added and why — a criteria document that quietly mutates
   makes the score log meaningless.
4. Note the amendment in the score log so round-over-round comparisons stay honest.
5. **Re-tally the previous round under the amended rubric before continuing.** The
   artifact hasn't changed, so this is arithmetic, not another scorer run: re-weight
   the existing per-criterion scores, and score the new criterion against the
   artifact as it stood. Record both in the score log as `72 → 68 (rubric v2)`, and
   use the v2 series for every later comparison.

   Skipping this is a real trap. Adding a criterion the work fails, funded by
   shaving points off criteria near full marks, mechanically *lowers* the total —
   and the loop then hits "score went down, stop and investigate" and hunts a
   regression that never happened. A drop caused by adding a criterion is
   bookkeeping, never a regression.

Do **not** amend criteria to make a change you already made look better. That's
moving the goalposts, and it's the fastest way to turn the score into decoration.
If a change doesn't score, that's information — either the change wasn't valuable,
or the criteria were wrong *before* you made it. Only the second one justifies an
amendment, and only if you'd have made the same amendment beforehand.
