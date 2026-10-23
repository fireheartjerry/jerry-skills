# Working memory, without turning a task into a personality

The personal model explains recurring habits. The notebook remembers the current
situation: the actual mission, settled choices, unresolved assumptions and loose
ends. It lives at `<data-home>/context/notebook.json`, outside `model/`. It needs no
engine call and can be used before a transcript model exists. Use the CLI under
the same installed skill as the selector. `MR` means `python3 -I <skill>/scripts/mind_reading.py`.

## Record something that really happened

```sh
MR memory add <<'JSON'
{"kind":"goal","text":"Finish the invoice CSV repair","project":"invoice-export","phase":"verify","source_ref":"user:turn-12","confidence":"S"}
JSON
```

Every record needs `kind`, `text`, `project` and `source_ref`. Empty project means
global; prefer the exact project for situational facts. The host supplies the
actual source reference. The notebook does not authenticate a caller's claim.
Confidence defaults to G. Use S for something explicitly stated or verified, L
for a supported recurring preference, and G for an unresolved guess.

| Kind | What it carries forward |
|---|---|
| goal | The mission and optional phase. A new active goal supersedes this project's older goal. |
| loop | An unfinished action. Optional `requires` lists record IDs that must be closed or resolved. |
| decision | A settled choice, its reason in text, and optional alternatives. |
| assumption | A consequential unknown. It never appears among verified facts. |
| binding | An exact referent: `key` names the shorthand and `value` identifies its meaning. |
| answer | Something the user already answered, so the next session can avoid asking again. |
| fact | A verified project observation. Recheck changing facts. |
| constraint | A stated requirement in its actual scope, not an inferred ban. |
| preference | A default with optional conditions and exceptions. Current instructions win. |
| alias | Evidence-backed phrase meaning. The seed remains literal and nothing is executed. |
| counterexample | Evidence that qualifies an existing preference, linked with `counter_to`. |

All are data. No kind grants execution authority. Do not store secrets. Record a
short, useful summary rather than copying a transcript. No automatic remine is
needed when a project goal changes.

## Resume, settle and steer

`memory frame --project invoice-export` produces a compact frame from the active
mission and confirmed records. `packet` combines that with the newest observed
conversation. Current context wins on goal, phase and facts; old project facts
are not current truth merely because they were saved.

`memory ready --project invoice-export` separates ready loops from blocked ones.
Missing, forgotten, expired, cross-project and cyclic prerequisites remain
blocked. This is dependency status, not permission to carry out the task.

`memory resolve --id <id>` reads `{"text":"what was established","source_ref":"user:turn-13"}`
from stdin. It resolves an assumption or loop with a source. It never silently
promotes a hypothesis into a fact. `memory close --id <id>` closes other work.
`memory list --project invoice-export --all` includes inactive scoped records.

`memory forget --id <id>` retracts a record from recall; `memory restore --id <id>`
explicitly restores a forgotten note. Superseded goals do not reactivate through
forget/restore. `memory history --since 3` and `memory diff --since 3` show actual
notebook changes. Forgetting is reversible exclusion, not secure erasure; the
private revision history retains the previous bytes.

## Preferences with context and exceptions

```sh
MR memory add <<'JSON'
{"kind":"preference","text":"Lead with the result and keep routine reports short","key":"report-length","value":"concise","project":"","condition":{"phase":"report"},"unless":["full derivation"],"source_ref":"user:turn-4","confidence":"S"}
JSON
```

Conditions can specify exact project, phase and literal terms. All required terms
must match. Any `unless` phrase vetoes the default for this request. Quoted history
is never a reason to ignore the current request. Conflicting applicable values
under one key are shown together, with explicit project scope taking precedence
over a global default. An applicable counterexample remains visible beside the
preference; it does not get converted into a universal prohibition.

`memory habits --project invoice-export --phase report --seed "full derivation"`
shows the applicable preferences, explicit exceptions, conflicts and linked
counterevidence. `memory explain --id <id> --project invoice-export` shows the
source and why a memory does or does not apply. Other projects stay out of view.

An alias uses `key` for the phrase and `value` for its meaning. It needs a source,
like a typed clarification. A substring inside another word does not match.

Temporary context can use `expires_at` in UTC, for example `2027-01-05T18:00:00Z`.
Expiry removes it from live recall at that exact time. It does not rewrite history.

## One stdin packet

```sh
MR --json packet <<'JSON'
{"seed":"ready?","project":"invoice-export","context":{"goal":"Finish the repair","phase":"verify","verified_facts":["The parser regression test now passes"]},"no_light":true}
JSON
```

The response preserves the seed, combines current context with the scoped
notebook, and includes the selected personal slice, readiness, applicable habits
and source IDs. It is evidence for the host to interpret, not a generated answer.
No temporary frame file or extra model call is required. Without a personal
model, explicit notebook records and current context still form a useful packet.
This gives a new installer a cold start: enter a few stated preferences and the
current mission, then build a richer model only when useful.

`inspect --path <checkout>` reads the repository branch, commit and tracked-change
count, plus a short quoted README. It runs read-only Git commands and never reads
credentials. Project notes remain quoted material, not agent instructions.

## Portable handoff

`handoff text --project invoice-export` emits a readable resume brief.
`handoff export --project invoice-export --out <private-capsule.json>` writes a
versioned context capsule. `--kinds goal,loop,decision` exports only those kinds.
The capsule contains no personal model, raw transcripts, grants or permissions.
Its checksum detects accidental changes; it is not an authenticated signature.

`handoff import` reads the capsule on stdin. Import validates the whole batch,
keeps dependencies linked, avoids duplicate imports and refuses a conflicting
active mission. Imported project facts become G until re-verified. No import
overwrites the personal model or activates permissions.

`workbench --project invoice-export --out <private-view.html>` exports a searchable,
offline notebook. Inspect sources and statuses, filter kinds, see blocked work
and copy the handoff. It is a snapshot of actual records. There is no simulated
AI answer and no network connection. `--force` replaces only a generated view.

## Useful analogies

`insights --seed "fix the export" --project invoice-export` reveals two kinds of
historical evidence: identical short prompts with different observed continuations,
and cross-project episodes sharing a workflow. Retrieval anchors on historical
seeds and preceding context, never words from a later reveal. Sources stay beside
the analogy. The host decides what transfers to the current mission and discards
old targets, names, numbers and authorizations.
