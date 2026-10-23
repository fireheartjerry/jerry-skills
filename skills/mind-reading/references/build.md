# Build a model you would choose

The pipeline turns your Claude Code and Codex history into a private model of the
constraints you repeatedly leave unsaid. The product is a better brief, not a
longer profile. Keep a candidate only if you prefer its reads and need fewer
corrections afterward.

New here? Run the [offline fictional demo](quickstart.md) first. It needs only Python
3.9 or newer and reads no private transcripts.

## 1. Choose an isolated candidate and inspect the input

The CLI is `scripts/mind_reading.py` in this skill. It uses the Python standard
library and runs from any directory with `python3 -I`. In these examples, `MR` is
this shell function after cloning the repository:

```bash
MR() { python3 -I "$HOME/jerry-skills/skills/mind-reading/scripts/mind_reading.py" "$@"; }
```

Use a separate data home while building. Do not overwrite your active model.
`--home` takes precedence over `MIND_READING_HOME`, which takes precedence over the
default data home at `~/.local/share/mind-reading`.

```bash
MR --home ~/.local/share/mind-reading-candidate init --name YourFirstName --engine claude
MR --home ~/.local/share/mind-reading-candidate extract
MR --home ~/.local/share/mind-reading-candidate status
```

Use `--engine codex` for a logged-in Codex CLI, or `--engine print` to run exported
prompts through your current agent without a separate CLI login. Extraction reads
Claude Code projects and Codex sessions from the roots in the candidate's config.
Override them with `init --claude-dir PATH --codex-dir PATH` if needed. Confirm
those roots before starting a large import.

Inspect the candidate's `corpus/scrub-report.json` and spot-check scrubbed records
in `corpus/messages.jsonl` before any model call. Extraction is local. Mining,
synthesis and evaluation send their selected scrubbed content to the configured
model provider. Using a CLI subscription still consumes its usage allowance.

The corpus distinguishes typed text from drafts, pasted text, answers and imported
material. The model must not treat accepted agent wording as words you originated.
Only verified quotes from your typed messages can establish strong preferences;
repeated copies of one session do not establish independent evidence. Project
budgets, deadlines, branch names and owners belong in project memory.

For people named in transcripts, add role replacements before extraction:

```bash
MR --home ~/.local/share/mind-reading-candidate init --person 'Full Name=<teammate>'
MR --home ~/.local/share/mind-reading-candidate extract --rescrub
```

Available roles are `<teammate>`, `<stakeholder>`, `<repo-owner>`, `<vendor>`, `<friend>`
and `<family>`. Scrubbing covers common secret formats, email addresses, phone and
card numbers, credentialed URLs and home paths. It is best effort, not a guarantee.
Do not commit the corpus, model or review files to a repository.

## 2. Split before mining, then set a budget

```bash
MR --home ~/.local/share/mind-reading-candidate prep --cutoff auto
MR --home ~/.local/share/mind-reading-candidate status --estimate
```

Read `derived/prep-report.json` for duplicate sessions, re-imported copies,
unreliable timestamps, leak removals and chunk caps. Automatic splitting reserves
the last twenty percent of reliable sessions for test, subject to a minimum of ten.
There is also a development split for iteration. If the corpus is too small for a
clean split, collect more history; do not present a training result as a test.

The estimate comes from your actual chunk sizes and configured lenses. No fixed
wall-time promise applies across providers or corpora. Seven mining lenses are
available: core, defaults, reactions, followthrough, vocabulary, grants and lessons.
They trade repeated input for focused evidence gathering. `mine --single-pass`
uses one combined call per chunk; `mine --lens core --lens grants` runs a subset.

For example, start with an explicit call cap and at most two concurrent workflows:

```bash
MR --home ~/.local/share/mind-reading-candidate mine --max-calls 12 --concurrency 2
MR --home ~/.local/share/mind-reading-candidate synthesize --max-calls 3 --concurrency 2
MR --home ~/.local/share/mind-reading-candidate recall build
MR --home ~/.local/share/mind-reading-candidate lint --model --strict
```

`recall build` uses no engine. It indexes typed training-only episodes that pair a
short seed with a later clarification, so selection can retrieve specific intent
echoes as well as general preferences. Historical project details remain evidence
about that episode, not facts about the current task.

These call caps are hard limits, not a predicted total. A budget stop exits with
code 3 and prints a resume command. Read the completed output and estimate before
extending it. `--max-cost-usd` stops new calls after observed cost reaches the
threshold; it cannot know a pending call's final cost and is not a strict ceiling
on total dollars. It also depends on backend reporting and configured prices.
Keep actual usage from each run beside its results. A subscription-only task
must not inherit an API billing key or alternate provider endpoint.

For an agent acting on someone's behalf: use the budget and authority already
provided in the session. Surface the estimate before starting engine work; do not
turn one approved build into repeated permission prompts. A missing spend budget
or authority is a real boundary. Finish local preparation while it is unresolved.

## 3. Review the evidence before using the model

The model contains `light.md`, `profile.md`, `examples.md`, optional `grants.md`, an
index and a manifest. The [model contract](model-format.md) documents their format.
Read the candidate as a person checking whether it describes them accurately:

- Every strong preference needs genuine, independent typed evidence, not repeated
  imports, an assistant's suggestion or a story inferred from a few short replies.
- General preferences belong in the model. Project-specific facts stay in project
  memory. A useful omission is better than a convincing invention.
- Lessons must connect an approach to your later words about its outcome. They can
  suggest a better approach, but cannot authorize an action or override your latest
  instruction.
- Grants contain verbatim authorizations and their exact scope. Review each one.
  Preferences and examples never supply permission. Revoke or remove anything you
  no longer mean; tombstones preserve that decision through rebuilds.

Fix an incorrect line or record a correction. After a manual edit, rebuild the
index and manifest without an engine call:

```bash
MR --home ~/.local/share/mind-reading-candidate synthesize index manifest
MR --home ~/.local/share/mind-reading-candidate lint --model --strict
```

Try representative seeds with `select --seed-file -` and a quoted heredoc, as in
the quickstart. Verify the retrieved constraints, unresolved referents and proposed
scope. A large output is not evidence of a good read.

For an interactive view of the receipts, export a private intent atlas:

```bash
MR --home ~/.local/share/mind-reading-candidate atlas --out '<private atlas.html>'
```

It shows the exact short request beside its later addition, filters by request kind
and signal, and opens the confidence ledger with source references and typed quotes.
Search is local word matching, not an intent prediction. Nothing is sent anywhere.
An empty store stays empty; the atlas never invents an insight. Existing unrelated
files are preserved. `--force` rebuilds a previously generated atlas after the model
changes. The file contains private evidence and should stay private.

## 4. Measure the reads, with a human as the authority

Use the development split while iterating. Compare the same seeds with no model,
the light model and the full runtime slice:

```bash
MR --home ~/.local/share/mind-reading-candidate eval --dev --max-sessions 8 --max-calls 56 --concurrency 2
```

The report's requirement coverage, contradictions and wrong upgrades are diagnostic.
An automated judge has not established accuracy by grading its own style. Treat its
scores as untrusted until calibrated against blind human preferences. The primary
outcomes are human wins, ties and losses, and subsequent corrections per read, each
with its denominator. Inspect harmful overreach separately from missed preferences.

Export a blind review from the results directory printed by eval:

```bash
MR --home ~/.local/share/mind-reading-candidate preferences build --baseline-results '<results directory>' --baseline-arm none --candidate-results '<results directory>' --candidate-arm full --out '<private review directory>'
```

Open the generated review page locally. Choose the brief you would actually want,
including a tie or neither when appropriate; export its answers, then import them
using the generated private key and the downloaded answer file:

```bash
MR --home ~/.local/share/mind-reading-candidate preferences import --key '<private review directory>/private/preference-key.json' --answers '<downloaded answers.json>'
```

Keep that key private so reviewers cannot identify the arms. A small review is a
pilot, not a reliable population estimate. Report the observed counts and uncertainty
rather than promoting a model on a convenient score.

When the candidate is fixed, evaluate the held-out test once:

```bash
MR --home ~/.local/share/mind-reading-candidate eval --max-sessions 8 --max-calls 56 --concurrency 2
```

The ledger enforces one clean look per model version and split. If you change the
model after seeing the test, that test is spent for the revised model. Continue on
dev, then mint a later clean split when enough new sessions exist. `--force` records
a tuned look; it does not restore a clean test. Automated comparisons should include
absolute latency, usage and cost beside quality, not quality alone.

The default evaluation gives each arm a context-free first message. `eval --mode
context` gives every arm the same earlier user messages for a short mid-session
seed. Neither mode supplies project memory or full agent state. Read the report's
limitations before extrapolating to live use.

## 5. Activate deliberately, then track corrections

### Learn completed human choices in a candidate

Keep the exact `--pairs` JSON used to build the blind page. After the human
finishes the review, a separate still-building candidate can learn the actual
chosen-versus-other briefs:

```bash
MR --home ~/.local/share/mind-reading-candidate preferences learn \
  --pairs '<frozen-pairs.json>' --key '<private preference-key.json>' \
  --answers '<human answers.json>'
```

This validates every content hash and label, archives the previous model and
updates its index and manifest. It refuses an activated or frozen model. Ties and
neither stay distinct; the reason for a choice is not invented. The runtime
retrieves relevant contrasts as historical examples, not per-clause approval or
new authority. The completed review becomes development feedback for the next
candidate, so it cannot also prove that candidate is better.

### Activate the reviewed version

A candidate does not activate itself. After reviewing it, explicitly choose the
model home in the agent environment. For example, to use the reviewed candidate:

```bash
export MIND_READING_HOME="$HOME/.local/share/mind-reading-candidate"
MR status
MR refresh --always-on
```

That last command previews the always-on block. Review its exact wording and target
before writing global instructions. An agent may write an already authorized target;
otherwise it should show the concrete block and obtain the missing authorization.
The write command backs up an existing target first:

```bash
MR refresh --always-on --write-to ~/.claude/CLAUDE.md
MR refresh --always-on --write-to ~/.codex/AGENTS.md
```

The same model home must be visible to the agent that runs the skill. Do not put a
personal block or model in a shared repository. The current request, project rules
and harness constraints still take precedence.

The runtime's `correct` and `log-decision` commands keep explicit corrections and
counts locally. A correction applies on the next read; it need not wait for a
rebuild. Track correction rate with a read count, along with scope errors and the
human preference sample. `tracking report` reports recorded outcomes; absence of a
record is unknown, not success. The pipeline's estimate of missing intent is not a
substitute for the user's verdict.

## 6. Refresh candidates weekly

A weekly refresh should produce a new candidate from new history and corrections,
run checks and prepare evidence for review. It must leave the active model and
global instructions unchanged:

```bash
MR refresh --candidate-home ~/.local/share/mind-reading-weekly --extend --max-calls 12 --concurrency 2
```

Inspect the candidate's changes, deterministic checks, usage and human evidence
before choosing it. `--extend` keeps the existing split frozen unless a new cutoff
is explicitly requested. New post-cutoff training data changes what the original
cutoff measures; mint a fresh split when enough new history exists.

To generate a scheduler plan and runnable command:

```bash
MR schedule plan --out '<private schedule directory>' --weekday 0 --hour 9 --timezone UTC
```

Use your timezone and inspect the generated command. This creates a plan; it does
not install a scheduler or promise notifications. The scheduler must actually run
the candidate command, stop on failure and make the result available for review.
No weekly job should auto-promote a candidate.

The generated weekly runner advances only the candidate's training boundary with
`--train-before <run date>`. That date means midnight UTC: the deployment candidate
can learn from completed preceding days, while the active model and its frozen
evaluation split remain unchanged. This candidate has no held-out test split; use
a separate clean evaluation build for claims about test performance. Ordinary
`refresh --extend` retains its existing split unless an explicit boundary is given.

Planning resolves the configured CLI executable and freezes its lookup path in the
runner and launchd file, so a scheduler's minimal environment can still find it.
An unavailable CLI stops planning before files are written. Review a new plan if
the CLI installation moves; existing plans are preserved rather than overwritten.

## 7. Privacy, isolation and recovery

Every data-home directory is created private (0700), and CLI-written files use
0600. `status` flags loose permissions. Filesystem modes do not isolate the data
from another process running as you; the runtime's rule never to open `eval/` and
harness guards are separate protections.

Only the lessons lens sees a bounded `agent_context` slice beside typed messages:
a short prior reply and outcome markers. It is untrusted context, not the user's
voice. Set `extract.agent_context_chars` to zero before extraction to disable it.
The model requires verified typed evidence regardless of what the assistant said.

The Claude engine disables tools, session persistence and settings sources. Codex
uses an isolated `CODEX_HOME` for engine calls so global personalization does not
contaminate the no-model control. Review backend configuration if you change it.
`--engine print` exports a prompt and schema per unit with a requested output path;
run those units with only the needed read/write access, then use the printed
`--collect --run <run id>` command. Eval prompts contain held-out truth and stay
under the eval tree; use its generated review/report interfaces rather than reading
that truth into the runtime session.

For Claude Code, a path-specific Read deny rule can guard the eval directory. Use
`Read(~/.local/share/mind-reading/eval/**)` for the default home, or the corresponding
path for your chosen home. A single leading slash has different settings semantics;
`status` diagnoses the guard. Do not edit permission settings as a side effect of a
model build.

Common recovery paths:

| Symptom | Action |
|---|---|
| Budget stop or failed unit | Read the actual error, then use the printed resume command; completed units are retained |
| CLI login missing | Log in to the chosen CLI; print mode is available without a second CLI login |
| Unknown transcript tags | Inspect their origin before changing filters; add a fixture for a new format |
| Invalid model or grant | Run `lint --model --strict`; fix or revoke the named record, then rebuild index and manifest |
| Stale index | Run `synthesize index manifest`; select can rebuild it in memory meanwhile |
| Correction or decision spooled | Use a writable data home or spool; the next writable run drains pending records |
| No lessons | Zero verified lessons is valid; inspect dropped evidence before mining more |
| Demo target refused | Choose an empty separate directory; never force a demo into a private model |

To uninstall, remove the generated block from each target, remove the skill through
the installer, and remove private data only after deciding what to retain. Do not
silently delete a user's transcript corpus, model or review history.
