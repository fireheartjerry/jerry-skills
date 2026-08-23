---
name: iterative-improvement
description: "Run a criteria-driven adversarial improvement loop: interview the user into a weighted, numerically scoreable criteria document, then repeatedly (1) have a hostile independent reviewer audit the work, (2) triage its findings into actionable changes, (3) execute them, and (4) have an independent scorer measure real progress against the criteria. Use this whenever the user wants something made *better* rather than made. The test is not the wording — it is whether two competent people could disagree about whether the work is finished; if yes, use this even when the request is short, scoped to a single file, or phrased without any of the words below. Typical phrasings: \"improve this\", \"polish this\", \"take it to the next level\", \"make this world class\", \"audit and fix it\", \"iterate on this\", \"this is good but…\", \"keep going until it's great\", \"run N rounds on this\" — across designs, landing pages, UI, codebases, architecture, docs, copy, or any artifact where quality is open-ended and there is no single correct answer. Reach for it especially when the user asks for repeated rounds, a set number of iterations, or improvement until a target is hit — but only when \"better\" has no existing objective oracle. Do NOT use it when: the target is one specific wrong behavior (that is debugging); the task has a definite done state like \"add a login page\" (just build it); success is already measured by a test suite, benchmark, or numeric threshold the user names, even if they ask to \"iterate until it passes\"; or the request is specifically about animation and motion quality (use improve-animations). Otherwise prefer this over a one-shot fix whenever the goal is subjective quality rather than one specific defect, including when the user names only a file or two."
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 1.0.0
  license: MIT
---

# Iterative Improvement

A loop for making something genuinely better when "better" has no test suite.

The core problem with open-ended improvement is that both the target and the
progress are invisible. An agent asked to "improve this design" will make
*changes*, feel productive, and have no idea whether it moved toward or away from
what the user wanted. Ten rounds of that produces churn, not quality.

This skill fixes both halves. First it makes the target explicit and numerical, in
collaboration with the user, so there is something to aim at. Then it runs a cycle
where an independent hostile reviewer finds what's wrong, that criticism gets
triaged into real changes, the changes get executed, and a *separate* independent
scorer measures the result. The score is the feedback signal that tells you whether
the loop is working.

```
 INTERVIEW ──► CRITERIA DOC (weighted, scoreable, user-approved)
                     │
                     ▼
        ┌──────► HOSTILE REVIEW  (independent, brutal, specific)
        │            │
        │            ▼
        │        TRIAGE          (reason about it — accept, reject, defer)
        │            │
        │            ▼
        │        EXECUTE         (make the changes, verify each)
        │            │
        │            ▼
        │        SCORE           (independent scorer, fresh context)
        │            │
        └────────────┴──► target hit / rounds spent / plateau ──► REPORT
```

## When this fits

Use it when the goal is subjective quality and there's no single correct answer:
a design that should feel better, a landing page that should be more compelling,
a codebase that should be cleaner, prose that should be sharper, an architecture
that should be more defensible.

**Don't use it for:**

- A specific bug with a specific wrong behavior — that's debugging, and a
  hostile-review loop is enormous overkill.
- A task with a definite done state ("add a login page") — just build it.
- Anything already covered by a real test suite or benchmark — you have an
  objective signal, use that instead of manufacturing one.

If you're unsure, ask yourself: *could two competent people disagree about whether
this is finished?* If yes, this loop earns its cost.

---

## Phase 1 — Build the criteria document

**This phase is the entire skill.** Everything after it is mechanical. If the
criteria are vague, the loop will faithfully optimize a vague target and you'll
burn expensive rounds producing changes the user doesn't want. Time spent here
pays back several times over.

### Interview the user

Ask questions in **batches of 3–5**, not one at a time — a slow interrogation is
exhausting and people start giving shorter answers to make it stop. Two or three
batches is usually enough.

Cover these, adapted to the domain:

- **Outcome.** What changes in the world if this succeeds? Who is looking at it
  and what do you want them to do or feel?
- **References.** Name two or three things you think are excellent, and say what
  *specifically* about each. ("Make it like Linear" is unusable; "Linear's empty
  states explain what to do next instead of apologizing" is a criterion.)
- **Anti-references.** What's an example of this done badly? What tells you
  instantly that something was phoned in?
- **Hard constraints.** What must not change? Brand rules, tech stack, existing
  decisions you don't want relitigated, files that are off-limits.
- **The rejection question.** *What would make you reject this even if everything
  else were perfect?* The answers become **gates** — binary, non-negotiable.
  This is the single highest-yield question in the interview; ask it explicitly.
- **Priority.** If you could only fix three things, which three?
- **Done.** What would you have to see to say "that's it, stop"?

If context already answers some of these — earlier in the conversation, an existing
spec, a design doc, prior audit — extract those answers first and bring them to the
user for confirmation rather than asking cold. Coming prepared respects their time
and gets better answers.

### Write the criteria document

Save to `docs/criteria/<slug>-criteria.md` relative to the repo root. Create the
directory if needed. It lives in the repo on purpose: it survives across sessions,
it's reviewable in a diff, and any agent picking up the work later can find it by
convention.

Read **`references/criteria-guide.md`** for the full template, worked examples of
good vs. useless criteria, and how to set weights. The short version:

- **6–12 weighted criteria** summing to 100 points. Fewer is too coarse to show
  progress; more turns scoring into noise.
- Every criterion needs a **How to check it** line that a stranger with no context
  could actually execute. If you can't write that line, you've written a wish, not
  a criterion — rewrite it into its observable consequences.
- **Gates are separate from points.** A failed gate is reported alongside the
  score, never averaged into it, because "94% but it doesn't build" is a lie.
- Include at least one criterion the work currently fails badly and one it already
  passes. Without both you have no way to tell whether the scorer is calibrated.

### When there is no user to interview

Phase 1 assumes a responsive human. If you're running headless (`-p`/`exec` mode),
in an auto-permission mode that forbids asking questions, or the user has gone
offline mid-task, you cannot skip this phase — but you must not silently pretend it
happened either. Running the loop against criteria the agent invented, while the
report implies the user approved them, is the worst failure this skill can produce:
every round afterward optimizes a guess, and the score lends that guess false
authority.

Do this instead:

1. **Derive the criteria from real evidence, not invention.** Mine the existing
   spec, design doc, README, prior audit, issue thread, commit messages, and
   earlier conversation. Cite the source next to each criterion.
2. **Mark the document's status explicitly** at the top:
   `**Status:** DERIVED — not user-approved. Sources: <list>.`
3. **Flag every criterion you had to guess at** with `[ASSUMED]` and say what you
   assumed. These are the ones most likely to be wrong.
4. **Lead the final report with it.** The first line the user reads should say the
   criteria were never approved and which ones are assumptions.

A derived criteria doc is still far better than no criteria — it makes the target
inspectable, so the user can correct it in one pass instead of relitigating every
round. It just must never masquerade as an approved one.

### Get explicit sign-off

Show the user the criteria document and ask them to change anything that's wrong
before the loop starts. Do not skip this. The criteria are supposed to be a joint
artifact — a document the agent wrote alone is just the agent's guess with extra
steps, and every round afterward compounds that guess.

---

## Phase 2 — Set the loop budget

Ask two things, then run unattended:

1. **Stop condition** — a target score (e.g. "stop at 90") or a round count
   (e.g. "run 5 rounds"). Offer a sensible default: *5 rounds, or stop early at 90.*
2. **Off-limits** — anything the loop must not touch.

Then run the whole loop without interrupting them. They'll get one report at the
end. (Exception: stop and ask if a round would require a decision only they can
make — a real tradeoff between two criteria, or a change that breaks a stated
constraint.)

---

## Phase 3 — The cycle

### Round 0 — baseline (once, before you change anything)

Run the scorer exactly as 3d describes, against the *unmodified* work. Record the
total, the per-criterion breakdown, and gate results as the criteria doc's
`Baseline score` and row 0 of the score log.

Without it the round log's `score 72 → 81` header, the per-criterion `Before`
column, and the report's "starting score" are all unfillable, and the regression and
plateau rules in 3e can't fire until round 3. It also tells you immediately whether
the criteria discriminate at all — a baseline of 95 means they're too soft to steer
with, a baseline of 5 means they're aspirational rather than diagnostic. Either way
you'd rather learn it now than after two rounds of work.


### 3a — Hostile review

Get an audit from a reviewer that is genuinely independent of you. Independence is
what makes the criticism worth anything; a reviewer holding your context will
rationalize your choices.

**Preference order:**

1. **A different agent CLI.** Different model, zero shared context — the most
   independent option available and usually worth the extra minute. See
   `references/cross-agent.md` for exact invocations (`codex exec`, `kimi -p`,
   `claude -p`) with the flags that let them run unattended.
2. **A subagent** with a fresh context, if your harness has one.
3. **Inline, as a last resort** — and say so in the final report, because at that
   point you are grading your own homework and the user deserves to know.

Give the reviewer the brief in **`references/reviewer-brief.md`**. It's written to
produce criticism that's brutal *in service of accuracy*. That distinction matters:
a reviewer that's theatrically mean but vague is worse than no reviewer, because it
generates feedback you can't act on and quietly teaches you to distrust the loop.
The brief demands that every criticism cite a specific location, state a concrete
consequence, and survive the author pushing back.

**Independence is a claim until you check it.** Declaring a reviewer independent
doesn't make it so — a subagent in the same workspace can still read your changed
files and your notes, and a same-model process shares your blind spots. Before you
act on any review, verify it actually met the work cold:

- Does every finding cite something real — a file:line, a quoted string, a
  measurement, a screenshot observation? A review with no citations means the
  reviewer never reached the artifact and is pattern-matching on your prompt.
  Discard it and fix the access problem.
- Does it disagree with you anywhere? A reviewer that validates every choice you
  made either saw your reasoning or didn't look hard.
- Spot-check two findings against the artifact yourself. If both are wrong, the
  rest probably are too.

Independence you didn't verify is independence you don't have — and the whole
loop's honesty rests on it.

**Round 1 is blind to the rubric** — the reviewer sees the Constraints and Out of
scope sections, but not the criteria, weights, anchors, or any score. A blind
reviewer finds problems the criteria never anticipated, and those findings are how
you discover the criteria doc is incomplete. When round 1 surfaces something
important that no criterion covers, add the criterion (tell the user you did, and
why) before continuing.

**Round 2 onward is criteria-aware** — hand the reviewer the criteria and the
current score, and point it at the gap.

### 3b — Triage

This is the thinking step, and it's where the loop earns its quality. Do not pipe
review findings straight into edits.

Reason about each finding on its merits:

- **Is it actually true?** Verify it against the artifact. Hostile reviewers
  overreach — that's a feature of the prompt, and it means some findings are wrong.
- **Does it matter for the criteria?** A real flaw that no criterion covers and
  the user didn't ask about is a distraction. (Unless it's important enough to
  amend the criteria — see above.)
- **What's the cheapest change that actually resolves it?** Reviewers propose
  solutions; the proposal is usually not the best one available.
- **Does it conflict?** With a stated constraint, or with another finding. Say so
  and pick, with a reason.

Sort into four buckets: **DO NOW** (high criteria impact, cheap), **DO** (high
impact, expensive), **DEFER** (low impact), **REJECT** (wrong, or out of scope).

Write the triage into the round log, including a one-line reason for every
rejection. Recording rejections is what stops the same finding from being
re-litigated in round 3, round 4, and round 5.

**Rejecting findings is normal and healthy.** A round where you accept 100% of a
hostile review means you weren't evaluating, you were transcribing.

### 3c — Execute

Work DO NOW, then DO. Make each round's changes individually undoable before you
score — a commit, a stash, or just a copy of the touched files. 3e's revert path
depends on there being something to revert to.

After each change, confirm the edit landed, then re-run every `check:` line in the
criteria doc's **Gates** section. If a gate flips to FAIL, fix or revert before
moving on — never carry a broken gate into scoring. A round that raises three
criteria and silently breaks a gate is a net loss.

Keep a short changelog of what you changed and which criterion it targets.

### 3d — Score

Spawn an **independent scorer** with a fresh context, using the brief in
**`references/scorer-brief.md`**. The scorer sees exactly two things: the criteria
document **with the score log and baseline score stripped out**, and the current
state of the artifact.

That deletion is not fussiness. From round 2 the criteria doc contains every prior
total, so pasting it whole hands the scorer the exact anchor the whole design exists
to avoid — one careless paste undoes the isolation the number's credibility rests on.

It must **not** see the reviewer's findings, your triage, your changelog, or the
previous round's score. Every one of those anchors the score upward — an agent told
"we just fixed the typography" will find the typography improved. Withholding them
is the whole reason the number is trustworthy.

The scorer returns per-criterion points with cited evidence, plus gate pass/fail.
Log the total.

**Pin the scorer for the whole loop:** same CLI, same model, same flags, same
working directory, same prompt file — recorded verbatim in the criteria doc. If the
configuration must change, re-score the *previous* round's artifact under the new
configuration before comparing. Otherwise the delta you're reading is instrument
variance, not work, and it can easily exceed the 2-point plateau threshold.

**Make sure the scorer can actually run the checks.** A separate process doesn't
inherit your environment — if a criterion says "screenshot at 1440px and blur it",
a scorer without browser tooling will score it 0 and report "check failed". Two
rounds of that looks exactly like a plateau, and you'll stop a loop that was
working. Before scoring, confirm the scorer has what each check needs (browser
access, a running dev server, the right working directory) and hand it explicit
instructions to reach them. If a check genuinely can't be run in that environment,
run that one yourself and hand the scorer the resulting artifact — the screenshot,
the measurement, the command output — rather than letting it guess or zero out.
Evidence you produce is fine; *judgment* you produce is not.

### 3e — Decide

- **Any gate FAILs** → the round is a net loss regardless of the score. Fix or
  revert whatever broke it before anything else. A failing gate can never satisfy
  *target hit* or *rounds exhausted* — the loop continues until gates pass, or halts
  and reports failure. Check this **before** you look at the number, or you'll ship
  the "94% but it doesn't build" outcome the gate separation exists to prevent.
- **Target hit** → stop and report.
- **Rounds exhausted** → stop and report.
- **Score went down** → stop and investigate before continuing. Diff the
  per-criterion scores to find which one dropped, then decide whether to revert
  that change or whether the *previous* score was wrong. A regression you scroll
  past becomes the baseline for every later round.
- **Adjusted max changed** (the scorer marked criteria `N/A` this round but not
  last, or vice versa) → the rounds aren't comparable; neither the regression nor
  the plateau rule fires. Close the tooling gap and re-score rather than reading a
  number that measures your environment.
- **Score flat** (the total moved less than 2 points in each of the last two rounds,
  in either direction) → **plateau; stop and tell the user.** Either the criteria are saturated and the remaining gap needs a
  decision only they can make, or the loop is churning. Grinding rounds against a
  plateau is the main way this skill wastes real money — call it early, explain
  what's left, and let them redirect.

---

## Round log

Append one block per round to `docs/criteria/<slug>-rounds.md`:

```markdown
## Round N — score 72 → 81 (+9)
**Reviewer:** codex exec (blind)
**Accepted:** 6 · **Rejected:** 3 · **Deferred:** 2

### Changes
- <what changed> → targets C3, C5
- <what changed> → targets C1

### Deferred (carried forward)
- <finding> — revisit when <condition>

### Rejected findings
- "<finding>" — <why rejected, one line>

### Per-criterion
| # | Criterion | Before | After | Note |
|---|---|---|---|---|
| C1 | … | 6/12 | 10/12 | … |
```

## Final report

Give the user: starting score → final score, the per-criterion movement, what
changed across all rounds, which gates pass, what's still unaddressed and why, and
an honest read on whether the remaining gap is worth more rounds. If the reviewer
or scorer ever had to run inline rather than independently, say so — it changes how
much the numbers are worth.

---

## Failure modes

These are the ways this loop goes wrong in practice. Watch for them:

- **The agent wrote the criteria alone.** The single most common failure. The
  score then measures agreement with the agent's own opening guess.
- **Score climbs while the work visibly doesn't improve.** The scorer is inflating.
  Re-read the criteria's "How to check it" lines — they're probably too soft to
  discriminate. Tighten them and rescore.
- **The reviewer repeats last round's findings verbatim.** Either nothing actually
  changed, or the reviewer is seeing a stale copy of the artifact. Check which.
- **Churn.** The same area rewritten every round with no score movement. Stop
  touching it; the problem is elsewhere, or the criterion is unachievable as stated.
- **Scope creep via the reviewer.** Hostile reviewers love proposing rewrites.
  Constraints from Phase 1 outrank any reviewer finding — reject and log.
- **Death by round count.** Five rounds isn't a target to hit. If round 3 hits the
  score and round 4 has nothing meaningful to do, stop at 3 and say so.

---

## Reference files

- `references/criteria-guide.md` — criteria template, worked good/bad examples,
  weighting, gates. Read this during Phase 1.
- `references/reviewer-brief.md` — the hostile reviewer prompt. Read at 3a.
- `references/scorer-brief.md` — the independent scorer prompt. Read at 3d.
- `references/cross-agent.md` — exact CLI invocations for spawning an independent
  reviewer or scorer as a different agent (Codex, Kimi, Claude), including
  unattended-mode flags.
