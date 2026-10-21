---
name: competitive-systems
description: "Find and apply algorithmic speedups to CPU-bound bottlenecks. Not for I/O, network or config slowness, micro-optimisation, or inputs small enough for brute force: it profiles first and says when to stop. Covers quadratic loops and dedup, slow CI (test selection, runner splitting), dependency-graph impact, LLM context packing under a token budget, multi-pattern log scans, repeated work. Use for 'this is O(n^2)', 'CI takes 40 minutes', 'which tests must run for this PR', 'context is over budget'."
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 2.2.0
  license: MIT
---

# Competitive Systems

This skill finds and applies asymptotic speedups to real bottlenecks and refuses them when
the bottleneck is not algorithmic. `references/` holds technique cards by symptom and a
measurement protocol; `scripts/bench.py` (stdlib Python 3.8+) does the timing: run
`python3 <skill-dir>/scripts/bench.py` from the project directory, `<skill-dir>` being the
directory containing this SKILL.md.

## Procedure

0. Pin the problem from the data. If files came with the request (durations, a log, a
   profile, the slow code), open them before writing anything and compute every number below
   from them, not from the user's summary: flag impossible values (negative, zero, missing,
   duplicated) and say how each was handled, and reproduce the stated symptom (replay the
   current plan on the recorded durations; time the current code). Estimates from the summary
   are the fallback when no data exists, labelled as such. Then write down: input sizes now
   and at 10x (n, m, k, bytes); the latency or wall-time target; the correctness invariant
   (what the new output must equal, usually "identical to the current output on the same
   input", with permitted differences such as ordering listed); and the public interface that
   must not change.

1. Gate 1: is it algorithmic? Profile first (top five frames by inclusive time). Compare CPU
   to wall time (`bench.py` prints both): wall much larger than user+sys means the process is
   waiting (I/O, network, locks, sleeps) and no algorithm change will help. Then collect the
   evidence for the symptom:

   | Symptom | Evidence | Proceed when |
   |---|---|---|
   | code whose time grows with input | doubling test: `bench.py scale` at n, 2n, 4n on pinned inputs | exponent >= 1.7 (below) |
   | many jobs on m workers | current makespan vs LB = max(sum/m, longest job) | makespan > 1.1 x LB |
   | full suite on every PR | selection ratio (impacted tests / all) estimated on recent diffs | ratio well under 1 |
   | context over a token budget | must-keep tokens + candidate tokens vs B | candidates do not fit in B minus must-keep |
   | same work repeated | profile call count vs distinct inputs | calls far exceed distinct inputs: `cards-repeated-work.md` |

   Doubling-test verdicts: exponent >= 1.7 and CPU-bound is algorithmic, continue; 1.3 to 1.7
   is inconclusive, raise sizes until the smallest median is >= 0.5 s and refit; about 1 and
   CPU-bound is constant factors, not this skill (say so; profile the hot frame); wall >>
   user+sys, or top frames in read/recv/sleep/lock, is I/O or waiting, not this skill (say so;
   batching, concurrency or caching the I/O); an uncapped search over subsets or permutations
   is exponential, go to `cards-ci-tests.md` (budgeted search) or `cards-scheduling.md`. On
   "not this skill", stop with a short plain-words verdict; open no card.

2. Gate 2: is n big enough? Estimate brute-force operations at 10x the expected n. Budget:
   about 1e7 simple operations per second in CPython, 1e9 compiled; a measured exponent, if
   any, wins over the budget. Stop rule: stop if the brute force at 10x n takes under about
   100 ms per call on a request path or inside a hot loop, or under about 1 s for a one-shot
   batch job. A stop answer keeps the simple code and tells the user, in plain words: the
   operation count; the time at 1x and 10x (one timing of the current code when it is at
   hand, otherwise a labelled estimate); the size at which the rule flips (solve ops / budget
   for n, and quote the threshold of the card the symptom index names, e.g. "k >= 50 patterns
   and far more text" for Aho-Corasick, without applying the card); and the free tier only.
   Offer to measure if the user wants more evidence; never call timing unnecessary. No report
   template on a stop.

   Tiers: free (dict, set, sort, heap, prefix sums, bisect); cheap (union-find, topological
   sort, BFS on a graph you already have); expensive (Aho-Corasick, SCC and bitset closure,
   LSH, knapsack DP over a large budget, exact subset search).

3. Match the symptom to a card. Use the symptom index below, open only that file, and run the
   card's precondition test. If it fails, reject the card and try the next card listed, or
   report that no technique applies. Never apply a card whose precondition you could not check.

4. Implement the narrowest version that satisfies the invariant. Keep the public interface,
   the output ordering (or document the new order) and every safety fallback (glossary:
   fail-closed, sensitive file, must-keep set). Prefer stdlib; a new dependency needs one
   sentence of justification in the report.

5. Prove correctness with the card's oracle: a property test comparing the new code to the
   brute force on seeded random inputs (at least 200 cases, sizes 0 to about 50, with the
   card's edge cases), plus one worst-case timing test at any cutoff the card names.

6. Measure after, the same way as before: pinned inputs, warm-up discarded, interleaved A/B,
   7 runs per arm to start, median and IQR, via `python3 <skill-dir>/scripts/bench.py
   compare`. Noise rule: a speedup is real only if the after median is below the before
   median, the IQRs do not overlap, and the gap exceeds twice the larger IQR. If it fails and
   the gap is under 2x, go to 15 runs; under 1.2x, 30; still failing is "no measurable
   change". Scaling cards (dedup, joins, graphs, patterns) also measure at two sizes and show
   the exponent dropped (same exponent = constant-factor gain, labelled as such); other cards
   report makespan vs LB, selection ratio, hit rate or tokens before and after. When one run
   is expensive (a CI run, a long batch): before arm = recorded history; plan number =
   deterministic replay on the recorded inputs; real reruns of the same commit stop once the
   noise rule passes (`references/measurement.md` section 5).

7. Report with the template below.

## Symptom index

| Symptom | First card | File |
|---|---|---|
| Nested loop over one collection; `x in list` inside a loop; time x4 when n x2 | Hash-key dedup, Dict index join | cards-lookup-dedup.md |
| "Duplicates" that are similar, not equal | Blocking for near-duplicates | cards-lookup-dedup.md |
| Range or interval join; dedup under tight memory or wanting sorted output | Sort then scan | cards-lookup-dedup.md |
| Rolling or windowed aggregate recomputed per window | Prefix sums and sliding window | cards-lookup-dedup.md |
| "What depends on X", "what is affected by this change" | Reverse-graph impact set | cards-graphs-impact.md |
| Many reachability queries on a graph with cycles | SCC condensation, Bitset closure | cards-graphs-impact.md |
| Build or deploy order, longest chain | Topological order, Critical path | cards-graphs-impact.md, cards-scheduling.md |
| Linked records to merge, connected components | Union-find for grouping | cards-graphs-impact.md |
| CI runs the whole suite on every PR | Impact-based test selection | cards-ci-tests.md |
| Fixed CI time budget, choose optional checks | Coverage under a time budget | cards-ci-tests.md |
| CI wall time with several runners and uneven jobs | LPT across workers | cards-scheduling.md |
| Jobs with dependencies on several workers | Critical-path list scheduling | cards-scheduling.md |
| "How many runners to finish in D minutes" | Fewest workers for a deadline | cards-scheduling.md |
| Thousands of patterns checked per line | Aho-Corasick, Literal prefilter for regex sets | cards-multi-pattern.md |
| LLM prompt over token budget, or wrong files retrieved | Exact-first retrieval, Pack under a token budget | cards-llm-context.md |
| Same computation repeated across calls or runs | Memoise, Content-hash cache, Incremental recompute | cards-repeated-work.md |

## Report template

Fill in whenever the work reaches a card (step 3 onward); a stop at Gate 1 or 2 is answered
in plain words (`references/worked-example.md`, Examples C and D).

```
Problem: <one line>. Input: n=<...> now, <...> at 10x. Target: <...>.
Gate 1: <exponent x.x at n=a..b | makespan m vs LB l | selection ratio r | must-keep t + candidates c vs B | calls k for d distinct>; CPU <user+sys>s of <wall>s wall -> <algorithmic | I/O | constants | repeated work>.
Gate 2: brute force at 10x n ~ <ops> ops ~ <time> -> <proceed | stop>.
Card: <name> (<file>). Precondition: <test and result>.
Invariant: <what must be equal>. Oracle: <brute force>, <N> random cases, sizes <lo>..<hi>, seed <s>: pass.
Before: median <t> (IQR <q>, runs=<k>) at n=<...>; After: median <t> (IQR <q>, runs=<k>).
Class: exponent <x.x> -> <y.y> (scaling cards) | <makespan/LB, ratio, hit rate or tokens> <before> -> <after> (other cards). Verdict: <k>x faster | no measurable change.
Kept: <interface, ordering, fallbacks>. Changed: <...>. New dependency: <none | name, why>.
Not verified: <...>.
```

## Hard rules

- Never present a single timing as evidence, and never compare runs from different machines
  or inputs.
- Never remove a safety fallback to hit a number. If mandatory work alone exceeds the budget,
  report that; do not optimise the mandatory set away.
- Never apply an expensive-tier technique without the Gate 1 and Gate 2 lines filled in.
- Output must be deterministic: fixed tie-breaks, stable sorts, seeded randomness, and no
  wall-clock cutoffs (use node or step budgets).
- Say "not verified" for anything not executed.
- Write to the user in plain words; "Gate", "card", "tier" and "precondition" stay in the
  report block.

## Glossary

- precondition: the fact that must be true for a card to apply (e.g. "equality is decided by
  a hashable canonical key"). Checked, not assumed.
- oracle: the obviously-correct slow version used to check the fast one.
- property test: run oracle and fast version on many seeded random inputs and assert equality.
- fail-closed: when the fast path cannot prove it is safe (unmapped file, incomplete graph,
  missing measurement), fall back to the full, slow, safe path.
- direct mapping: a test linked to a changed file by the dependency graph (import, build
  target, or an explicit mapping) rather than by a heuristic.
- sensitive file: a file whose change invalidates the mapping itself (lockfiles, build and CI
  config, dependency manifests, shared fixtures, code generators); changing one forces the
  full run.
- heavy job: a job that needs a large share of a host's resources, so it is pinned to one
  worker or run alone; pinning caps the gain from scheduling.
- makespan: wall time until the last job finishes. worker: a runner, machine, or process slot
  that runs one job at a time; m is the number of workers.
- must-keep set (LLM context): the items kept in the prompt even over budget: direct targets
  (files and symbols named in the task), verification commands (how to run and test), hard
  boundaries (constraints the user stated).

## References

- `references/measurement.md`: read before Gate 1 and before the after-measurement: protocol,
  pitfalls, the noise rule, expensive runs such as CI, `bench.py` usage.
- `references/worked-example.md`: read when first using the skill, or when unsure what a report
  or a stop answer looks like: one full example and three short ones, including stops at
  Gate 1 and Gate 2.
- `references/cards-*.md`: open only the file the symptom index names.
