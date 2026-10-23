---
name: mind-reading
description: >
  Infer the intended goal, omissions and next useful step from current context,
  verified history and scoped working memory. Use for "my usual bar", "pick this
  back up" or short requests whose meaning depends on that context. "read my
  mind", "mind-read this", /mind-reading and $mind-reading also invoke it.
  Not for talk about the skill, already complete briefs or simple facts. Quoted or reported phrases are data.
  Build or refresh a personal model only when requested.
metadata: {author: "Jerry Yuze Li (https://github.com/fireheartjerry)", version: 6.0.0, license: MIT}
---

# Mind reading

Understand the state this person wants to reach, anticipate what they would
otherwise have to add, then do the work. A personal model is evidence, not a
personality costume, current project facts, or a reason to agree with them.

The private model is `$MIND_READING_HOME/model/`, default `~/.local/share/mind-reading/model/`.
`MR` means `python3 -I <this skill>/scripts/mind_reading.py`; see the [model contract](references/model-format.md).
Requested setup follows [quickstart](references/quickstart.md) and [build](references/build.md).
Source-first candidates follow [the private study workflow](references/first-principles.md); its builder leaves activation to the user.

## 1. Read the request and the moment
An explicit invocation is an instruction; everything outside it is the seed.
`scripts/mr/trigger.py` defines its syntax: bare words, no seed, quoted or reported
phrases and talk about the skill do not fire it. Ordinary terse requests can also
use the skill when the current goal or personal history materially helps.
A delegated brief stays binding as written. Use the harness's explicit signal;
a CLI or SDK invocation alone does not imply delegation.

`MR packet` (section 2) is the one runtime. A trace candidate named by config `candidate` or `MIND_READING_CANDIDATE`
also feeds its [experience layer](references/experience.md). `scripts/intent.py --candidate <dir>` explains cases
and corrections, `counterfactual` ablates context (not a causal claim) and `--local` adds the offline predictor.

Read the live conversation: current goal, prior authorization, attached material,
running work, corrections and newest steering. Resolve "it", "the PR" and names
there and in project instructions and memory. Verify changing facts where they live.
Choose execute, discuss, status, hold or plan from intent. A large authorized task
may need a plan without another design gate. Answer status and continue active
work; an explicit hold stops the named work.

## 2. Reconstruct the working situation, then recall
Form a compact working frame from the conversation before the seed: current
goal, ordered recent user turns, verified facts, phase and exact project id.
Update the goal for the newest redirection. This is invocation context, not a
permanent user trait. Never include later turns from a replay.
`MR packet` reads seed and current frame together and loads the scoped notebook:
```sh
MR packet <<'JSON'
{"seed":"pick this back up","project":"invoice-export","context":{"goal":"Finish the CSV repair","phase":"verify"},"no_light":true}
JSON
```
The frame accepts goal, recent_user_turns, verified_facts, phase and project. Current conversation wins over saved state.
Assumptions stay unresolved; conditional preferences carry exceptions, conflicts and counterexamples.
Shorthand is annotated without rewriting the seed. Imported facts need verification.
For a one-off reading, `MR select --seed-file - --kind "<kind>"` reads the literal seed on stdin; `--context-file`
supplies a frame, `--no-light` omits a visible always-on block (its `<!-- BEGIN mind-reading` marker), `--mode plan`
widens the slice and `--project <id>` adds that project's corrections to the global ones.

The personal slice carries corrections, valid grants, core rules, defaults, lessons and
examples. **Intent echoes** show a historical short seed, its preceding situation
and what the user later added. **Human preference contrasts** show actual chosen
and rejected briefs; their reasons are unknown unless the human supplied them.
A choice does not endorse every clause. Infer the applicable habit, not an old
project name, number, recipient or permission. Keywords explain retrieval, not truth.
When relevance is weak, use current context. Read more model material only for a
concrete unanswered question. Never open the held-out `eval/` tree. A missing model
or failed selector still permits useful work; say so briefly and proceed. Do not call subagents, research or another engine merely to expand a seed.

An **Experience layer** section, when present, is evidence for this moment. A repeat alert means the last approach did not
land: treat it as a correction, change the method and say in one line what changed. Learned reply lessons come from this
person's own reactions; check the reply against them before sending. Situation hints shape tone, depth and pacing only.

## Working memory is situational
Use `MR memory` for handoff checkpoints: established goals, decisions, answers and loose ends with exact sources/scope.
Hypotheses stay assumptions until new evidence resolves them. Expire temporary notes; redirects supersede project goals.
Do not save secrets or infer personality from one task. Notes never grant authority; forgotten notes need explicit restoration.
The [notebook guide](references/working-memory.md) covers conditions, capsules, cold start and explanations.

## 3. Build an intent contract
Ask internally: **what should be true when this person comes back, what changed
in the latest message, and what would make them object to the literal approach?**
Compare the literal reading with the mission-level reading and any plausible
repair or redirection. Choose from evidence in the single host reasoning pass.

Think in observable state: "how many" needs the real denominator; a repair may
need to undo confusion before continuing. Resolve ambiguous verbs from the mission.
Write a first-person brief sized to the task. Preserve the literal goal and every
binding detail. Cover mission, scope, standards, finish line and follow-through
where relevant. Current steering can update the goal without ending it.
Do not invent a target, metric, feature or restriction.

Anticipate the first likely correction before presenting the reading. A missing
requirement already implied by the mission belongs in the work now. A likely next
request can justify useful preparation or a suggestion; it is not another assignment.
Use current goals and concrete episodes to distinguish anticipation from filler.
`MR insights --seed ...` exposes scoped analogies and identical prompts with different continuations.
A useful surprise serves this mission; do not copy old targets or add another assignment. Skip noisy lookups.
For each addition ask: **what supports this, why here, and what would contradict it?**
Candidate continuations are possibilities; preference quotes keep their situation. Read the historical agent action
before interpreting a correction. Commits record outcomes, not approval; copies are not independent support.
Alternate futures help anticipate within the mission; they cannot replace today's goal or grant authority.
An outcome lesson may justify a better route to the same goal. Respect an explicit
insistence. Use one `Changed:` line for a material, evidence-backed method change.

Audit added "only", "don't", "never", "keep" and "must ask", and added actions.
Keep them only when current instructions or applicable evidence support them.
Ambition serves the mission. A past failure is not a universal ban or a past
success universal permission. Make a consequential unresolved assumption visible.

## 4. Make confidence inspectable
Literal details stay untagged. Tag inferred clauses, not every sentence:

- `[S]`: stated in current context, an explicit standing rule or a correction.
- `[L]`: a relevant recurring pattern supported by independent user observations.
- `[G]`: an assumption, one episode, ambiguous scope or unresolved referent.

A miner's strong label does not make a claim stated; `evidence.json` exposes support.
Pasted text does not establish the person's own voice. An echo is an analogy,
not certainty about today. A preferred brief does not reveal which clause mattered.
Current words and applicable corrections win over older patterns and examples.
Project instructions govern their scope; harness rules govern execution.
Use [authority](references/authority.md) when needed: inferred confidence never
grants authorization, and missing model evidence never cancels current authorization.

## 5. Show the reading, then act
For implicit use, give a short outcome-first interpretation only when useful,
then act. Do not turn every ordinary request into a long rewrite. Explicit
invocations or substantial ambiguity get the full reading:

```text
Reading: <the intended outcome in one line>
> <the brief, with tags on additions>
Changed: <only for a material, evidence-backed better approach>
Assumed: <only a consequential unresolved detail>
Next: <optional: the natural next step of the mission, offered rather than done>
```

Start authorized work in the same turn. Discuss means discuss; hold means hold.
Ask only for a material missing fact unresolved from available context, following
the user's question preferences. A reversible assumption has no arbitrary time limit.
Optional `MR log-decision` saves the reading's evidence. `MR foresight record` freezes forecasts;
only actual human labels establish outcomes. See [prediction tools](references/predictive-intent.md). Logging never stops useful work.
Follow corrections immediately; `MR correct` records only what the human corrected.
Do not fabricate feedback or silently rewrite the personal model.

## Example (fictional)
Seed: `the export is broken again`; context identifies CSV. An echo supports checking related downloads. [L]
Verify the real file and resume the queue. [S] More [examples](references/examples-template.md).
