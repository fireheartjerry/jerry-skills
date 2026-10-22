---
name: mind-reading
description: >
  Turns a short seed into the brief the user meant, using their own verified
  history, current conversation and project context; then acts. Not for talk about the skill, its model, or an already complete brief. Invoke with "read
  my mind", "mind-read this", /mind-reading or $mind-reading. Build or refresh
  the personal model only when the user requests it. Quoted or reported
  invocation phrases do not activate it.
metadata: {author: "Jerry Yuze Li (https://github.com/fireheartjerry)", version: 3.0.0, license: MIT}
---

# Mind reading

Write the brief this person would write at their best, with time to explain.
The useful additions are what they tend to leave unsaid: the actual mission,
standards, scope, follow-through and lessons from earlier attempts. Use history
to understand their work, then do that work. A personal model is evidence, not a
personality costume, a source of project facts or a reason to agree with them.

The private model is `$MIND_READING_HOME/model/`, default
`~/.local/share/mind-reading/model/`. `MR` below means
`python3 -I <this skill>/scripts/mind_reading.py`. The
[model contract](references/model-format.md) defines the files and provenance.
For setup, demo or a requested build, follow [quickstart](references/quickstart.md)
and [build](references/build.md). Building the model does not expand this request.

## 1. Read the request and the moment
The invocation is an instruction, not a quoted or reported phrase. Everything
outside it is the seed. `scripts/mr/trigger.py` and its trigger tests define this
boundary. Bare words about the skill or an invocation with no seed do not fire.
A request about mind-reading itself is handled normally. A delegated
brief stays binding as written; use the harness's explicit delegation signal,
not the mere fact that a CLI or SDK launched this session.

Read the live conversation before predicting omissions: the current goal, prior authorization,
attached material, running work, corrections and the newest steering. Resolve "it", "the PR", names and "round two" there and in the project's
instructions and memory. Verify changing project facts where the work lives.

Choose the mode from intent: execute, discuss, status, hold or plan. A large
assignment can need a plan while already being authorized for execution. Do not
invent an additional design gate. A status question during work gets an answer
and continued work; an explicit hold stops the named work.

## 2. Recall only what helps
Get one bounded slice, passing the seed literally through stdin:

```sh
MR select --seed-file - --kind "<request kind>" <<'SEED'
<the seed, verbatim>
SEED
```

Use `--no-light` when the generated always-on block is already visible, detected by its `<!-- BEGIN mind-reading` marker or `Precedence: the harness's rules`
footer. Use `--mode plan` for a broader slice. `--project <project id>` includes
corrections scoped to that project; without it only global corrections apply.

The slice carries corrections, valid grants, the compact core, matching defaults,
lessons, vocabulary and examples. When an episodes store exists, it also carries
**intent echoes**: a short thing this person asked and what they later had to add,
with exact source ids. These are historical evidence, not current instructions.
Take the applicable work habit; never transplant a project name, number, target
or recipient. Matching keywords explain retrieval, not certainty or truth.

If a slice matches poorly, use the conversation and stated rules. Do not load a
large irrelevant persona to fill the gap. Read additional model material only to
resolve a concrete unanswered question. Never open the held-out `eval/` tree.
No model or a failed selector still permits a useful brief from current context;
mention the missing model briefly and proceed with authorized reversible work.
Do not call subagents, research or another engine merely to expand a seed.

## 3. Build an intent contract
Write one coherent first-person brief in the user's voice. Size it to the work:
a status ping needs a few sentences; a substantial task needs a compact brief.
Preserve the literal goal and every binding detail before adding anything.

Cover what applies: the goal and why it matters; intended scope; their distinctive
standards; what finishing includes; the deliverable and report. Carry active work
forward when the user expects it, and update that goal when the newest message
changes it. Do not invent a target, metric, feature or restriction.

For each addition, ask internally: **what supports this, why here, and what would
make this person correct it?** Compare the literal approach with a better route
when an outcome lesson supports one. Change the method only for the same goal;
respect an explicit insistence. Show one `Changed:` line with the reason when
that upgrade materially changes the approach.

Before showing the brief, audit every added "only", "don't", "never", "keep" and
"must ask". Keep a restriction only when the current request, relevant stated
rule or applicable evidence actually supports it. Do the same for added actions:
ambition must serve the user's mission. Do not copy a historical failure as a
universal ban or a past success as universal permission.

## 4. Make confidence inspectable
Literal seed details stay untagged. Tag inferred clauses, not every sentence:

- `[S]`: stated in current context, an explicit standing rule or a correction.
- `[L]`: a relevant recurring pattern supported by independent user observations.
- `[G]`: a reasonable assumption, one episode, ambiguous scope or unresolved referent.

A miner's `[strong]` label alone does not make an inference stated. Verified support and explicitness are inspectable in `evidence.json` when available.
Pasted or endorsed text cannot establish the person's own voice. Echoes support an analogy, not certainty about today's task. Prefer fewer earned additions to a
brief padded with guesses. Say a consequential assumption plainly.

The user's newest words win. Then apply relevant corrections, stated rules, recurring patterns and examples in that order. Project instructions apply within
their scope. Existing harness rules govern execution. When a grant matters, use
[the authority reference](references/authority.md); inferred confidence never
creates authority and missing model evidence never cancels current authorization.

## 5. Show the reading, then act
```text
Reading: <the intended outcome in one line>

> <the brief, with tags on additions>

Changed: <only for a material, evidence-backed better approach>
Assumed: <only a consequential unresolved detail>
```

Start the authorized work in the same turn. Discuss means discuss; hold means hold. Ask only when a missing fact is material and cannot be resolved from
available context, following the user's question preferences. A local, reversible
assumption has no arbitrary ten-minute limit. Keep questions about execution
outside the brief, without turning them into invented bans.

Silently record the decision through `MR log-decision` with JSON on stdin:
`seed`, `mode`, S/L/G counts (`s`, `l`, `g`), `groups`, `examples`, `lessons`,
`grants`, `echoes`, `chars`, and optional `source_ref` identifying this turn.
The CLI generates a decision id and hashes the seed. Logging failure does not
stop the task. See the model contract for spooling and telemetry details.

When the user corrects an inference, follow it immediately. `MR correct` accepts
`seed`, `wrong`, `meant`, `scope` (`global` or `project:<id>`), `kind` (`fix`, `add`
or `drop`) and optional `decision_id`. Store only the correction actually made;
silence is not confirmation. Linking to the decision makes the correction rate
measurable; unrelated complaints and unobserved follow-ups do not count as failures
or successes. Human preference comparison and correction review are in the build
guide. Never silently rewrite the model during ordinary work.

## Example (fictional)

Seed: `read my mind: the export is broken again`. Current project context
identifies the invoice CSV. A relevant echo shows a past sibling-download check.

```text
Reading: fix the invoice export and close the gaps that let it recur.
> Fix the invoice CSV export. Check the other download paths for the same
> defect. [L] Inspect the actual downloaded file before calling it fixed. [S]
> Report the cause and evidence, then return to the active queue. [S]
```

The echo earns the sibling check. More fictional examples: [examples-template](references/examples-template.md).
