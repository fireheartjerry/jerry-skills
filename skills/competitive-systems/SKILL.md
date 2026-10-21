---
name: competitive-systems
description: "Make real software faster, cheaper or more scalable: profile, bound the gain, apply an algorithmic technique whose precondition holds, prove it with an oracle and A/B timing. Not for I/O or network waits, config, micro-optimisation or inputs small enough for brute force: it says when to stop. Covers quadratic loops, dedup, slow CI and test selection, dependency impact, LLM context packing, multi-pattern scans, repeated work, perf regressions, and whether a claimed speedup is real. Use for 'this is O(n^2)', 'CI takes 40 minutes', 'which tests must run for this PR', 'is this speedup real'."
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 3.0.0
  license: MIT
---

# Competitive Systems

Makes real software faster with algorithmic changes it can prove, and says in plain words
when the bottleneck is not algorithmic or n is too small. Tool: `python3
<skill-dir>/scripts/dev.py <command>` (stdlib Python 3.8+; `<skill-dir>` holds this file;
after `dev.py init`, `python3 tools/dev.py <command>`).

## Procedure

0. Pin the problem from the data. If files came with the request,
   open them before writing anything and compute every number from them, not the summary:
   flag impossible values (negative, zero, missing, duplicated) and how each was handled;
   reproduce the symptom. Write down n now and at 10x, target, invariant, interface. Then
   `dev.py aa --save -- <cmd>` measures the noise floor before any claim.

1. Gate 1: is it algorithmic? Profile (top five frames by inclusive time); compare CPU to
   wall (`dev.py bench` prints both): wall far above user+sys means waiting. Evidence:

   | Symptom | Evidence | Proceed when |
   |---|---|---|
   | code whose time grows with input | doubling test: `dev.py scale` at n, 2n, 4n, 8n on pinned inputs | exponent >= 1.7 (below) |
   | many jobs on m workers | `dev.py bound makespan`: current makespan vs LB = max(sum/m, longest job) | makespan > 1.1 x LB |
   | full suite on every PR | selection ratio (impacted / all tests) on recent diffs: `dev.py affected --base main --explain` | ratio well under 1 |
   | context over a token budget | must-keep tokens + candidate tokens vs B | candidates do not fit in B minus must-keep |
   | same work repeated | profile call count vs distinct inputs | calls far exceed distinct inputs: `cards-repeated-work.md` |

   Doubling-test verdicts: exponent >= 1.7 and CPU-bound is algorithmic, continue; 1.3 to 1.7
   is inconclusive, raise sizes until the smallest median is >= 0.5 s and refit; about 1 and
   CPU-bound is constant factors, not this skill (say so; profile the hot frame); wall >>
   user+sys, or top frames in read/recv/sleep/lock, is I/O or waiting, not this skill (say so;
   batching, concurrency or caching the I/O); an uncapped search over subsets or permutations
   is exponential, go to `cards-ci-tests.md` (budgeted search) or `cards-scheduling.md`.
   Contention (throughput falls as workers rise) and tail latency (p99 >> p50) are not this
   skill either. On "not this skill", stop with a short plain-words verdict; open no card.

2. Gate 2: is n big enough? `dev.py bound ops --n N --ops "<count>" --path request|batch`
   (1e7 ops/s CPython, 1e9 compiled; a measured exponent wins). Stop if brute force at 10x n
   takes under about 100 ms per call (request path, hot loop) or 1 s (batch job). The stop
   answer keeps the code and gives, in plain words: the operation count; time at 1x and 10x
   (one timing, or a labelled estimate); the flip size and the indexed card's threshold,
   not applied; the free tier only.
   Offer to measure; never call timing unnecessary; no report. Tiers: free (dict, set, sort,
   heap, prefix sums, bisect); cheap (union-find, topological sort, BFS); expensive
   (Aho-Corasick, SCC, bitsets, LSH, big knapsack DP, exact subset search).

3. Gate 3: bound the gain. `dev.py bound amdahl --p <profiled share> --s <hoped speedup>`
   gives the cap 1/(1 - p); under 1 + 2 x the noise floor, stop and say the edit cannot
   move the end-to-end number.

4. Card: `dev.py catalog --symptom "<words>"` prints precondition, reject-when and each
   fact with the `card says:` sentence stating it. Open only that card file, run its
   precondition test; on failure try the next card or report none applies. Never apply an
   unchecked precondition. Put every `card says:` claim (caps, bounds, cutoffs, schedules)
   in the answer in plain words, not only in the report.

5. Implement the narrowest change. Keep the old code callable as the oracle's reference, the
   interface, the output order (or document the new one) and every safety fallback
   (fail-closed, sensitive file, must-keep set); stdlib first.

6. Prove correctness before timing: `dev.py oracle --ref old_mod:f --new new_mod:f` (seeded,
   edge and anti-cache cases) or a property test; add `references/guards.md` checks when the
   speedup is large or the diff touches tests, fixtures, timeouts or validation.

7. Measure after: `dev.py bench --arm before=... --arm after=... --runs 10 --out bench.json`,
   then `dev.py compare bench.json` (medians, IQR, ratio CI; its Verdict line goes in the
   report). Scaling cards also `dev.py scale` both arms (the exponent must drop); others
   report makespan/LB, selection ratio, hit rate or tokens. Expensive runs:
   `references/measurement.md` section 4. Report with `dev.py report` or
   `references/report-template.md`; stops in plain words (`references/worked-example.md`,
   Examples C and D).

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

## Tooling

`dev.py init` writes `tools/dev.py` so a repository's agents and CI share these commands;
`dev.py affected` fails closed (a lockfile, config or fixture change runs everything). History
of medians: `dev.py changepoint history.csv`. `references/tooling.md` lists the commands.

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
- Oracle before timing: never time a candidate whose output has not been compared with the
  reference on fresh inputs.
- A speedup beyond the Amdahl cap for the edited share, or far beyond the profile, is a bug
  until explained.
- Ratios never appear without the absolute medians next to them.

## References

Load on demand: `references/measurement.md` before step 0's A/A and step 7;
`references/guards.md` before step 6; `references/report-template.md` when writing the
report; `references/worked-example.md` on first use or when unsure how a report or a stop
looks; `references/tooling.md` for options, exit codes and schemas;
`references/glossary.md` for an unclear term; `references/cards-*.md` only for the file the
index or `dev.py catalog` names (`references/techniques.json` is the catalog).
