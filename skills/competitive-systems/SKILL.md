---
name: competitive-systems
description: "Fix code whose run time grows too fast with input size: bound the gain, prove the fix with an oracle and A/B timing. Not for I/O or network waits, SQL or database tuning, constant-factor tuning (SIMD, allocations, micro-optimisation), tail latency, lock contention or inputs small enough for brute force: it says when to stop. Covers quadratic loops, dedup, slow CI and test selection, dependency impact, job scheduling across workers, LLM context packing, multi-pattern scans, repeated work, benchmark regressions. Use for 'this is O(n^2)', 'CI takes 40 minutes', 'is this speedup real'."
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 3.0.0
  license: MIT
---

# Competitive Systems

Makes software faster with algorithmic changes it can prove, and says in plain words
when the bottleneck is not algorithmic or n is too small. Tool:
`python3 <skill-dir>/scripts/dev.py <command>` (`<skill-dir>` holds this file; stdlib
Python 3.8+; `dev.py selftest` checks an install offline).

## Procedure

0. Pin the problem from the data. If files came with the request,
   open them before writing anything and compute every number from them, not the summary:
   flag impossible values (negative, zero, missing, duplicated) and how each was handled;
   reproduce the symptom. Write down n now and at 10x, target, invariant, interface. A bare
   question ("which tests must run") runs only the commands that answer it. Check the side
   effects of every command you will repeat; if the symptom cannot be reproduced here,
   estimate, ask for one profile or two timings, claim no speedup (`measurement.md` 9, 10).

1. Gate 1: is it algorithmic? Profile (top five frames by inclusive time); compare CPU to
   wall (`dev.py bench` prints both): wall far above user+sys means waiting. Evidence:

   | Symptom | Evidence | Proceed when |
   |---|---|---|
   | code whose time grows with input | `dev.py scale` at four sizes, pinned inputs | exponent >= 1.7 (below) |
   | many jobs on m workers | `dev.py bound makespan` against LB | makespan > 1.1 x LB |
   | full suite on every PR | selection ratio: `dev.py affected --history 20` | median ratio <= 0.5 |
   | context over a token budget | `dev.py context` | must-keep + candidates exceed B |
   | same work repeated | profile call count vs distinct inputs | calls >= 10x distinct |

   Regenerate sizes with the production distribution held fixed, never tile
   (`measurement.md` section 2). Verdicts: exponent >= 1.7 and CPU-bound is algorithmic; 1.3
   to 1.7, raise the sizes, then profile at the production n; pairwise slopes that keep
   rising (`scale` warns) are exponential: stop fitting, use the `recursion-dp-memo` card or
   cap the search; about 1 and CPU-bound is constant factors; wall >> user+sys, or top
   frames in read/recv/sleep/lock, is waiting; contention and tail latency (p99 >> p50) are
   not this skill. On "not this skill", give a short plain-words verdict, open no card; if
   the user asked for speed and the fix is local and reversible, make it and measure it
   (steps 6, 7), else stop at the verdict (`references/worked-stops.md`, Example C).

2. Gate 2: is n big enough?
   `dev.py bound ops --n N --time-at-n S --exponent E --path request|batch` from one timing
   at a small n; without one, `--ops "<count>"` at 1e7 interpreted steps/s, or
   `--runtime c-builtin` (2e8/s) for elements scanned in C. Stop if brute force at 10x n
   takes under about 100 ms per call (request path, hot loop) or 1 s (batch job). Tiers:
   free (dict, set, sort, heap, prefix sums, bisect); cheap (union-find, topological sort,
   BFS); expensive (Aho-Corasick, SCC, bitsets, LSH, big DP, exact subset search).
   A stop refuses the cheap and expensive tiers; a free-tier change of a few lines may still
   go in, with the oracle and no speedup claim. The stop answer gives, in plain words: the
   operation count; time at 1x and 10x (one timing for scale, or a labelled estimate); the
   flip size and the card's threshold, not applied. Offer to measure; never call timing
   unnecessary; no report.

3. Gate 3: bound the gain. `dev.py bound amdahl --p <profiled share> --s <hoped speedup>`
   gives the cap 1/(1 - p), p at the size of the claim (a superlinear share grows with n).
   Under 1 + 2 x a known spread (CI history, an earlier A/A), stop (Example E). Before a
   timing claim, measure the floor: `dev.py aa --save -- <cmd>` (42 runs; slow:
   `measurement.md` section 4).

4. Card: `dev.py catalog --symptom "<words>"` prints precondition, reject-when, repo tool
   and each fact with its `card says:` sentence. A `weak match` or `no card` line means no
   card: a stock-structure fix (deque, heapq, str.join, set) goes in under Gates 1 to 3 and
   steps 6 and 7. Otherwise open only that card file and run its precondition test; on
   failure try the next card or report none applies. Never apply an unchecked precondition.
   Put every `card says:` claim in the answer in plain words.

5. Implement the narrowest change. Keep the old code callable as the oracle's reference, the
   interface, the output order (or document the new one) and every safety fallback
   (fail-closed, sensitive file, must-keep set); stdlib first.

6. Prove correctness before timing: `dev.py oracle --ref old_mod:f --new new_mod:f` (seeded,
   edge and anti-cache cases; `f(*case)` for several arguments; any language: `--ref-cmd`),
   then point the new side at a broken copy once and see it fail. Add `references/guards.md`
   checks when the speedup is large or the diff touches tests, fixtures, timeouts or
   validation.

7. Measure after:
   `dev.py bench --arm before=... --arm after=... --check-output --oracle-cmd <test> --out bench.json`,
   then `dev.py compare bench.json --json > compare.json` (medians, IQR; differing outputs:
   INVALID; external timings: `--samples`). Scaling cards also `dev.py scale` both arms (the
   exponent must drop); others report makespan/LB, selection ratio, hit rate or tokens.
   Report with `dev.py report --compare compare.json` or `references/report-template.md`.
   Leave the oracle test (reference inlined) and a pinned benchmark; drop the old module.

## Symptom index

| Symptom | First card | File |
|---|---|---|
| Nested loop over one collection; `x in list` in a loop | Hash-key dedup, Dict index join | cards-lookup-dedup.md |
| "Duplicates" that are similar, not equal | Blocking for near-duplicates | cards-lookup-dedup.md |
| Range or interval join; dedup in tight memory | Sort then scan | cards-lookup-dedup.md |
| Windowed aggregate recomputed per window | Prefix sums and sliding window | cards-lookup-dedup.md |
| Repeated min, rank or range queries, points within r | Heap, Ordered index, Spatial grid | cards-lookup-dedup.md |
| "What depends on X", change impact | Reverse-graph impact set | cards-graphs-impact.md |
| Many reachability queries, cyclic graph | SCC condensation, Bitset closure | cards-graphs-impact.md |
| Build or deploy order, longest chain | Topological order, Critical path | cards-graphs-impact.md, cards-scheduling.md |
| Linked records to merge, connected components | Union-find for grouping | cards-graphs-impact.md |
| CI runs the whole suite on every PR | Impact-based test selection | cards-ci-tests.md |
| Fixed CI time budget, choose optional checks | Coverage under a time budget | cards-ci-tests.md |
| CI wall time on several runners | LPT across workers | cards-scheduling.md |
| Jobs with dependencies on several workers | Critical-path list scheduling | cards-scheduling.md |
| Runners needed to finish in D minutes | Fewest workers for a deadline | cards-scheduling.md |
| Thousands of patterns checked per line | Aho-Corasick, Literal prefilter for regex sets | cards-multi-pattern.md |
| LLM prompt over budget, or wrong files retrieved | Exact-first retrieval, Pack under a token budget | cards-llm-context.md |
| Same computation repeated across calls or runs | Memoise, Content-hash cache, Incremental recompute | cards-repeated-work.md |

A claimed speedup needs no card: steps 0, 3 (noise floor only), 6 and 7. A benchmark that
crept up: `dev.py changepoint history.csv`, then `dev.py bisect`.

## Tooling

Repository commands (`affected`, `check`, `context`...) cover Python, TS/JS, Go
and Rust; `dev.py init` writes the `tools/dev.py` shim and `tools/dev.json`. `affected`
fails closed: a sensitive, fixture or unresolvable change runs everything. A measured
command takes n as an argument (quote `'{n}'` in zsh), seeds its input and prints a result
hash; compiled and JIT code: `measurement.md` section 2.

## Hard rules

- A speedup claim needs repeated interleaved runs, same machine and inputs; one labelled
  timing is for scale only. A stop writes no report, oracle test or `.devkit` file and runs
  no A/A; a local fix after it is ordinary work (Example C).
- Never repeat a command that mutates shared data or calls a paid or production service
  without asking (step 0).
- Never remove a safety fallback to hit a number; if mandatory work alone exceeds the
  budget, report that.
- Output must be deterministic: fixed tie-breaks, stable sorts, seeded randomness, no
  wall-clock cutoffs.
- Say "not verified" for anything not executed.
- Write to the user in plain words; "Gate", "card", "tier" and "precondition" stay in the
  report block.
- A speedup beyond the Amdahl cap or far beyond the profile is a bug until explained. Ratios
  always come with both absolute medians.

## References

Load on demand: `references/measurement.md` (section 2 before the doubling test, the rest
before Gate 3); `references/guards.md` before step 6; `references/worked-stops.md` at a
stop; `references/worked-example.md` and `references/report-template.md` for a report;
`references/tooling.md` for every command; `references/glossary.md` for a term;
`references/cards-*.md` only as the index or `dev.py catalog` names
(`references/techniques.json`).
