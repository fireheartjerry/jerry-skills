# Predict the missing intent

The core question is what the user wants to be true, given the current situation.
History supplies analogies and decision patterns. It does not turn an old project's
facts or a guessed future request into today's assignment.

## A single host pass

Before retrieving, form the current frame from the live conversation and verified
project state. Preserve which parts were stated and which you inferred. Update a
goal when the latest message redirects it; a status question can leave it running.

Compare the literal interpretation with the mission-level interpretation and any
plausible repair. Ask what the user would object to first. Use a concrete current
detail or historical episode to resolve the difference. There is no need for an
extra provider call, subagent debate or permanent personality update per request.

Examples below are fictional:

| Seed and situation | Useful anticipation | Unsupported addition |
|---|---|---|
| `ready`, checkout details already specified | Finish at a real confirmation and report the actual result | Assume payment details exist |
| `send a simple replacement`, earlier text was garbled | Repair the recipient's confusion and honor the current review instruction | Re-send stale content because it appeared earlier |
| `how many?`, the goal was raised from 50 to 100 | Report verified work against the latest target and continue unblocked work | Treat the status request as cancelling the goal |
| `remove these`, curating a publication list | Resolve what set is being edited from the current mission | Assume an ambiguous verb names permanent local deletion |

An observable finish line makes the reading useful. A forecast of the next wanted
step helps prepare or explain the work. It does not create a separate assignment.
If a request is already clear, the best reading can add nothing.

## Current context and choice memory

The optional `--context-file` contains the goal, recent preceding user turns,
verified facts, phase and exact project. Use current evidence, not guessed state.
Do not put later replay messages into the frame. Context rescues terse seeds and
conditions episode retrieval; explicit seed routing still takes priority.

`preference_memory.json` carries completed, hash-verified human choices. Learn it
only in a separate still-building candidate through `preferences learn`. Retain
both actual briefs and the human's choice. A missing rationale stays missing;
choosing a brief does not prove that every sentence was wanted.

Relevant contrasts can teach conditional decisions that a broad preference misses.
For example, one pair may favor decisive follow-through and another a narrower
scope because the live goal differs. Do not collapse that into a global ban or a
universal autonomy preference. The host still reasons about this request.

## Freeze a forecast before the later turn

Record a concrete forecast after forming it, before observing a later response.
Use the actual current source reference. The seed is hashed rather than copied
into telemetry. Predictions and supporting references remain private.

```bash
MR foresight record <<'JSON'
{
  "seed": "the export is broken again",
  "source_ref": "codex:sample-session:sample-turn",
  "predictions": [
    {
      "text": "The user will want the actual downloaded file checked, beyond a green unit test.",
      "support": ["episode:sample-export"],
      "confidence": "G"
    }
  ]
}
JSON
```

The CLI assigns the forecast and prediction ids, UTC time, model/runtime hashes
and a payload hash. It accepts no caller timestamp or future-target field. An
empty prediction list records abstention. If an ordinary decision id is supplied,
it must already exist in this model home's decision journal.

The journal is local and append-only through this API. Hashes expose accidental
alteration; they are not a cryptographic witness against an owner deliberately
rewriting the file. Logging failure does not stop authorized task work.

## Observe without turning hindsight into foresight

An actual later user turn and explicit human labels establish an outcome:

```bash
MR foresight observe <<'JSON'
{
  "forecast_id": "<id returned by record>",
  "reviewer": "human",
  "source_ref": "codex:sample-session:later-turn",
  "observed_at": "<actual later UTC timestamp>",
  "labels": [
    {"prediction_id": "<returned prediction id>", "outcome": "wanted"}
  ]
}
JSON
MR foresight report
```

Labels are `wanted`, `unwanted`, or `unresolved`. Use what the human actually
confirmed or corrected. Do not infer an unwanted prediction merely because the
next message did not mention it. Detector guesses and silence are not labels.
The event must follow the registration, differ from its source, and not be in
the future. The latest explicit label is retained as an append-only revision.

The report's precision is wanted over wanted plus unwanted predictions. Pending
and unresolved predictions stay outside that denominator. Abstentions and invalid
rows are visible. Recall is not measured without knowing other wanted items.

## Historical development and new evidence

`foresight replay --after <UTC timestamp> --out <private directory>` writes
generation inputs and later user turns into separate files with byte hashes and
source references. It excludes training sessions when a split exists. Add consumed
review sessions with `--exclude-sessions <JSON list>`. Mirrored long prompts are
excluded even when source session ids differ. It reports the actual cohort size.

Replay can reveal useful and embarrassing misses. It cannot prove the forecast
was registered before the real event. A later request also may reflect a changed
mind, rather than an initial omission. Never call a development replay a clean
prospective result or a reused human review a fresh independent test.

Compare against the frozen previous runtime, a context-only host and the new
personal layer. Keep the engine, prior context and output budget the same. Hide
actual later turns during generation. Separate a better reasoning scaffold from
the gain contributed by personal history. Automatic word coverage is diagnostic;
human preference and explicitly reviewed unwanted additions remain the yardsticks.
