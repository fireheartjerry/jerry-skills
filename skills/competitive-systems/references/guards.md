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
   200 cases, sizes 0 to about 50). For a function of several arguments the generator returns
   a tuple and the test calls `f(*case)` (`--call args`, chosen automatically). Sensitivity
   step: before trusting a pass, run the oracle once against a deliberately broken copy (a
   stub returning `[]`, or one that drops the last element) and require it to fail. A pass
   that a stub also gets proves nothing.
3. Property and metamorphic tests where no reference exists or the output is a set or an
   order: permutation invariance, idempotence, invariants the card names (every job assigned
   once, total tokens <= B, no line sent twice).
4. Bitwise or tolerance comparison on recorded production inputs (a day of logs, last week's
   manifests), with the tolerance stated when floats are involved.
5. Forced-recompute comparison for caches: run with the cache disabled on a sample and compare
   outputs byte for byte; report the hit rate next to it.
6. Repeated stress runs for concurrency: the same input many times under threads or
   processes; any divergence is a failure, not noise.

## Outside Python

`dev.py oracle --ref` imports Python callables. For any other language the differential
test is two commands on the same generated input:
`dev.py oracle --ref-cmd 'bin/step-old' --new-cmd 'bin/step-new' --gen-cmd 'python3 gen.py {seed}' --cases 200`
runs the generator once per seed, feeds its stdout to both builds on stdin, and requires the
same exit code and the same stdout byte for byte (`--tol 1e-9` compares numeric tokens with
that relative tolerance and every other token exactly). It stops at the first differing
seed with both outputs, fails a vacuous run (the reference fails on most cases, or prints the
same output every time), and prints the line to rerun in CI. The sensitivity step is the
same: point `--new-cmd` at a deliberately broken build once and require a failure.
`dev.py bench --check-output` repeats the output check on every timed run. Add the
language's property-test library (fast-check, gopter, proptest, jqwik) for invariants. The
card sketches are Python, and these semantics differ elsewhere:

| Trap | Python | Elsewhere |
|---|---|---|
| NaN as a key | `float("nan")` keys never merge unless the same object | a JS `Set` and `Map` treat NaN as equal to NaN |
| iteration order | dict keeps insertion order | Go map order is randomised per run; sort before output |
| stable sort | `sorted` is stable | C++ `std::sort` and Rust `sort_unstable` are not |
| integer overflow | none | 32-bit counters wrap in Go, Rust release builds, C, Java |
| Unicode keys | compare code points; normalise first if needed | the same, and JS strings are UTF-16 code units |

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
  the result is stale. `dev.py oracle` generates this check
  (`test_anti_cache_mutated_input`) whenever an input, or a member of the argument tuple, is a
  list or dict; for immutable inputs it skips and says so, and fresh inputs per timed
  iteration are the defence instead.

When a check fails, the answer says so in plain words, names the mechanism, refuses the claim,
and offers a correct version measured with fresh inputs if one exists.
