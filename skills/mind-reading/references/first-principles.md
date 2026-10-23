# Source-first personal intent studies

Use this workflow when asked to build a personal model from raw conversations and
outcomes, or when a source-first candidate is already configured. An ordinary
invocation retrieves evidence; it does not rebuild, train, or install anything.

The system models two different things: what the person actually wrote next, and
what their own words suggest they value in the current situation. A continuation
may be a reaction to poor agent work. It is not automatically the desired outcome.
The host reasons about the current mission using both channels.

## Build in a private study directory

`B` below means `python3 -I <skill>/scripts/intent_build.py --home <new-private-study>`.
Never put raw logs, models, vectors, cases, study receipts or reports in the public
skill directory. Choose an explicit study home; the builder refuses a symlink home.
It does not switch a live model, change skills, edit always-on instructions or
schedule work. Honor whatever installation authority the user actually gave.

1. `B ingest --source claude=<projects-dir> --source codex=<sessions-dir>` accepts
   Claude, Codex including zstd, Kimi, Grok and derivative memory documents. Repeat
   `--source kind=PATH` for other roots. `--machine NAME` records the machine.
   Files freeze at their first byte-prefix snapshot; a new source version needs a
   new study. A parser revision cannot silently reuse an older file receipt.
   Legacy worker files and workflow journals go into a separate `agent-tape.sqlite`.
   Their copied prompts never count as independent human preference observations.
2. `B git --root <code-directory>` reads Git metadata and change counts. Verified
   checkout/worktree aliases resolve project identity. A shared basename does not.
   Commits, retention and reverts are observations, not endorsement or deployment.
3. `B prepare` creates whole-family chronological partitions and a protocol. It
   excludes straddling families, invalid clocks and copied evidence across fences.
   Short seeds have 2–60 words. The input ends at the seed; intervening agent work
   is stored separately for reaction analysis. It refuses an existing frozen split.
4. `B encode --encoder-path <local-snapshot> --model <model-id> --revision <commit-sha>`
   creates frozen representations, separately for train, validation and test. It
   uses local weights only. `--device auto` chooses MPS, CUDA or CPU. Encoding the
   held-out candidate texts with a frozen encoder does not fit the predictor on them.
5. `B train` fits ridge maps, family-balanced low-rank nonlinear adapters, semantic
   retrieval and controls. `--no-adapters` omits Torch training. Validation selects
   the deployable predictor and best baseline, and calibrates score temperature.
   The training function never opens the test files. The candidate contains only
   training cases/vectors and opaque partition metadata.
6. `B seal` hashes the protocol, code, candidate and fitted models. `B evaluate`
   verifies the seal, claims the single test look, then runs frozen predictors.
   A second evaluation is refused, including after a failed look. Report a failed
   attempt; do not retune on that test set or relabel it as fresh.
7. After evaluation, `B refit --output <different-candidate-dir>` gathers all
   eligible observations, including the former holdouts, and fits the same
   validation-selected recipe. The tested snapshot and receipts stay unchanged.
   The new manifest identifies its parent evaluation and explicitly says it has
   no fresh held-out score. This keeps an honest measurement while letting future
   work use the full observed history. It never selects a recipe from test metrics.

The SQLite tape is resumable and source files stay read-only. `trace_store.import_bundle`
accepts transactional gzip event bundles parsed on another user-authorized machine.
Pin its parser fingerprint and verify the transferred bundle digest. Remote raw
hashes are receipts from that machine, not a claim that local raw bytes were read.

## Optional local dependencies and weights

The ordinary skill and lexical candidate runtime use the standard library. The
local learning path needs Python 3.12+ and the packages in
[requirements-local.txt](../scripts/requirements-local.txt), installed in a project
or scratch virtual environment. No paid inference service is required.

One supported encoder is
[Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B), with an
optional [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B).
Download weights explicitly, pin their repository revision and record their
license and file digests. Recall never fetches missing weights. The adapter trains
on frozen embeddings; this does not fine-tune the 600-million-parameter encoder.

## Recall and inspect

`R` means `python3 -I <skill>/scripts/intent.py --candidate <candidate-dir>`.
Use the local virtual environment's Python for optional learned retrieval.

- `R status` verifies the case membership, split and digests without loading an
  encoder. `R packet` reads `{seed, context, project}` as JSON on stdin. Context
  can include goal, phase, verified facts, preceding user/agent turns, tool status,
  current session/family and an explicit presentation preference.
- `R packet --local` adds a learned forecast to prefix-based evidence retrieval.
  The exact current seed remains unchanged. The packet carries artifact provenance,
  alternate historical continuations, bounded excerpts, source locations, nearby
  outcomes and exact preference-wording quotes with their original situations.
- `R packet --local --rerank-path <local-reranker>` reranks past **prefixes**, not
  historical answers, using a local yes/no cross-encoder-style score. This score
  is not a calibrated probability that the person wants a particular outcome.
- `R counterfactual` holds the seed fixed and removes preceding turns/tools from
  retrieval. It shows changed evidence ids. `R corrections` returns possible
  correction pairs, with counterexamples where available. Neither invents a rule.
- `R explain --case-id <id>` returns the full original training case when a bounded
  packet excerpt leaves a concrete question unresolved. It cannot open test cases.

Historical continuations are possibilities, not new assignments or permission.
Exact human preference wording is evidence of what was said in that situation.
Never turn a situational constraint into a global prohibition without support.
The current conversation and project instructions determine the requested outcome.
Time, mood or delay labels cannot rewrite it; a late timestamp alone establishes
neither low energy nor a preference for brevity.

## Interpret the experiment

Every method ranks the same actual next human turn against seven unique
continuations from other held-out families with similar seeds. Prefer the same
project/engine where enough alternatives exist, and report fallback strata. This
is a closed-candidate forecasting task, not free-form mind reading or a measure of
reflective endorsement. Its temperature calibrates that task only.

Report family-macro top-1 and MRR, micro top-1/top-3/MRR, calibrated log loss, the
whole-family paired bootstrap interval, source/project/stratum results and excluded
families. Freeze the comparison baseline on validation. The shuffled-training-target
control tests whether training labels matter. The post-agent reaction diagnostic
has later information and is ineligible for pre-action selection. A winner may be
a baseline; extra trainable parameters do not guarantee improvement.

These local baselines isolate mechanisms. They are not hosted product evaluations
or faithful reproductions of every published memory system. Memory QA and actual
next-behavior prediction have different targets, so their percentages cannot be
compared as one leaderboard. Relevant research includes history retrieval in
[LaMP](https://arxiv.org/abs/2304.11406), long-horizon memory QA in
[LongMemEval](https://arxiv.org/abs/2410.10813), evidence/belief separation in
[Hindsight](https://arxiv.org/abs/2512.12818), dynamic factors alongside history in
[PersonaFactor](https://arxiv.org/abs/2610.16372), and temporal latent personalization
in [LATTE](https://arxiv.org/abs/2605.26612). A single-user dataset does not provide
the comparable peer observations LATTE uses for peer anchoring.
