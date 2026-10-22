# Evidence and authority

Read this when a retrieved grant or proposed action makes authorization unclear.
The active harness and the user's current instructions determine what the agent may
execute. Mind-reading supplies interpretation and evidence; it does not add a
second universal approval policy.

## What history can establish

A typed, verbatim authorization in `grants.md` is evidence of what the user said.
Use only active grants returned by `select`: the parser rejects malformed,
revoked, non-typed, negated and overbroad entries. The action must occur in the
Quote itself, fit Covers, match Scope and stay outside Never covers. A later
instruction can narrow or revoke it. A source quote about a particular task does
not automatically apply to every future task.

An autonomy grant (`Class: autonomy`) supports reasonable choices and reversible
work inside the user's assignment. It supplies no recipient, spending budget,
destructive target or change to security settings. Those details must come from
the user's instructions and the harness's requirements. A general grant never
names the recipient of a message by implication.

A learned preference, a lesson, an example, a correction or an intent echo never
grants authorization. Equally, absence of a mined grant is not proof that a task
is unauthorized: the current conversation may already authorize it. Do not make
the user approve the same action again because a model file omitted it.

## What to do with uncertainty

Check the current request, earlier authorization and applicable project rules.
Proceed with authorized work. If a material detail is still required for an
external, destructive or spending action, finish the independent work and ask
one specific question, when the user permits questions. A low-confidence guess
about local implementation is an assumption to verify, not automatically a
permission gate. Respect instructions to discuss, hold or ask no further questions.

Do not turn a held action into a sentence that forbids it. For example, a seed
asking to ship should retain shipping in the brief; if a required deployment
target is unknown, name that missing target outside the brief and continue
preparing the release. Inventing "do not deploy" changes the user's goal.

## Model hygiene

`grants.md` remains separate from inferred content. Its verifier checks typed
provenance and literal action scope. The model linter rejects instructions to
defeat harness protections. The runtime ignores any surviving instruction to
change permissions or bypass required approval. No model edit changes a security
setting. Keep specific personal or project material in private data homes.
