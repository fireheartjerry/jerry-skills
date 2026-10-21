# Guards: correctness before speed, and fake speedups

Read before procedure step 6, and again whenever a speedup looks large or the diff touches
tests, fixtures, timeouts, CI config or validation. An optimisation that changes the output
is a bug, not a speedup, so the oracle runs before the timing, not after.

## Oracle stack, cheapest first

Use the first one that can catch the change's failure mode, and add the next whenever the
change is riskier than it looks.

1. Existing tests: run them; a green suite is necessary, rarely sufficient (suites seldom
   cover the sizes and edge cases a rewrite changes).
2. Differential test against the reference implementation on seeded random inputs plus the
   card's edge cases: `dev.py oracle --ref old_mod:f --new new_mod:f` writes one (at least
   200 cases, sizes 0 to about 50).
3. Property and metamorphic tests where no reference exists or the output is a set or an
   order: permutation invariance, idempotence, invariants the card names (every job assigned
   once, total tokens <= B, no line sent twice).
4. Bitwise or tolerance comparison on recorded production inputs (a day of logs, last week's
   manifests), with the tolerance stated when floats are involved.
5. Forced-recompute comparison for caches: run with the cache disabled on a sample and compare
   outputs byte for byte; report the hit rate next to it.
6. Repeated stress runs for concurrency: the same input many times under threads or
   processes; any divergence is a failure, not noise.

## Anti-gaming checklist

Nine checks; each one closes a way a measured speedup can be fake.

1. The harness (benchmark script, inputs, timer) is outside the candidate's write scope, and
   its hash goes in the report.
2. The timer is external: a separate process (`dev.py bench`) measures wall and CPU, never a
   number the candidate prints about itself.
3. Every timed iteration's output is validated or hashed against the reference
   (`dev.py bench --check-output` hashes stdout per run).
4. Inputs are fresh, or mutated in place, on every iteration, which defeats caches keyed on
   object identity or on "same as last call".
5. Held-out inputs are scored once, at the end, after the candidate is frozen.
6. No work is deferred past the clock: check for new threads, background processes, lazy
   evaluation or writes flushed after the timed region.
7. Types and precision are unchanged (float64 not downgraded, integers not truncated, sorting
   still stable).
8. Any edit to tests, fixtures, timeouts, CI config or validation carries a one-line
   justification in the report.
9. Implausibility is presumed a bug: a speedup beyond the Amdahl cap for the edited share
   (`dev.py bound amdahl`), beyond what the hardware can do, or 10x beyond what the profile
   share allows, is investigated before it is reported.

## Two fake speedups and the test that exposes each

- Output reuse passing the check: the candidate memoises the result of the benchmark's one
  fixed input, so every timed run after the first returns instantly and the output hash still
  matches. Exposing test: swap the run order, or feed a different seeded input each iteration;
  the "speedup" disappears or the hash differs.
- Cache keyed on object identity: `compute(rows)` caches by `id(rows)`, and the harness reuses
  one list object across iterations, so the candidate is 40x faster on the benchmark and
  wrong in production, where the same list is filled with new rows. Exposing test: call it,
  mutate the input in place, call again and compare with the reference on the mutated input;
  the result is stale. `dev.py oracle` generates exactly this check
  (`test_anti_cache_mutated_input`).

When a check fails, the answer says so in plain words, names the mechanism, refuses the claim,
and offers a correct version measured with fresh inputs if one exists.
