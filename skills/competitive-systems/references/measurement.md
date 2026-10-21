# Measurement protocol

Read this before the A/A run in procedure step 0 and again before step 7. Every number in a
report comes from this protocol; anything else is labelled "not verified". Run `dev.py` from
the project directory, where the measured commands' relative paths resolve.

Gate 1 (is it algorithmic?) and its evidence table live in SKILL.md, procedure step 1.
Gate 2 (is n big enough?) and the stop answer live in SKILL.md, procedure step 2.

## 1. Profile first

- Python: `python3 -m cProfile -s cumtime script.py` or `py-spy record -o profile.svg -- python3 script.py`.
- Node: `node --cpu-prof script.js`, open the `.cpuprofile` in the browser devtools.
- Go: `go test -cpuprofile cpu.out` then `go tool pprof -top cpu.out`.
- JVM: async-profiler (`asprof -d 30 -f profile.html <pid>`) or JFR
  (`-XX:StartFlightRecording=filename=rec.jfr`, then `jfr print rec.jfr`).
- Anything on Linux: `perf record -g <cmd>` then `perf report`.

Read the top five frames by inclusive time. Note whether they are your code, library code, or
syscalls (read, recv, write, futex, sleep). Note the call count of the top frame against the
number of distinct inputs it could have seen.

Then name the bottleneck class; only the first two are this skill's work.

| Class | Signature | Confirm with | First move |
|---|---|---|---|
| Algorithmic | time x4 or worse when n x2; CPU close to wall | `dev.py scale`: exponent >= 1.7, `CPU-bound` | the card the symptom index names |
| Repeated work | call count far above distinct inputs; unchanged inputs reprocessed | profile call counts against distinct arguments | `cards-repeated-work.md` |
| I/O or waiting | wall much larger than user+sys; top frames in read, recv, sleep, lock | the `Gate 1:` line says `appears to be waiting` | batching, bounded concurrency, caching the I/O; not this skill |
| Constant factor | exponent about 1, CPU-bound, the hot frame is your own code | `dev.py scale` exponent under 1.3 | profile the hot frame; not this skill |
| Contention | throughput falls as workers rise; lock or futex frames grow | throughput at 1, 2, 4 and 8 workers | not this skill: say so (shard the lock, cut shared state) |
| Tail latency | p99 far above p50 while p50 is fine | percentiles from an open-loop load generator | not this skill: say so (GC pauses, queueing, cold caches) |

## 2. Pinned inputs

A fixture file, or a generator with a fixed seed, checked in next to the test. Two sizes: the
expected n and 10x. The same inputs for before and after. Never compare against a number from
a different input, machine, commit, or day (CI plans: section 4).

## 3. Runs and statistics

- Warm-up: at least one run discarded (page cache, imports); three for JIT runtimes (Node,
  JVM, PyPy).
- Count: at least 10 runs per arm; 20 when the claimed gain is under 1.5x. Interleave the
  arms (A B A B ...) so drift (thermal, background jobs) hits both equally.
- Record wall, CPU (user+sys of the child) and peak RSS for every run; report n, median, IQR
  (25th to 75th percentile) and CV. Means and standard deviations are not used: timing
  distributions are skewed.
- Effect: the ratio of medians (before/after) with a seeded bootstrap 95% CI, the
  Hodges-Lehmann shift (median of all pairwise before - after differences), and the
  Mann-Whitney p. Tukey outliers (beyond 1.5 and 3 IQR) are flagged and kept, never removed.
- Noise floor first: an A/A run of the unchanged command (`dev.py aa`, 20 runs) gives the
  ratio CI that noise alone produces; the minimum effect is the larger of the user's
  threshold and twice its half-width (2% at least). A claim smaller than the minimum effect
  is not made, whatever the medians say.
- Decision rule, one only (`dev.py compare`): FASTER when the CI lower bound exceeds
  1 + min effect and the one-sided p is below 0.05; SLOWER mirrored; NO DETECTABLE CHANGE when
  the whole CI lies inside [1/(1 + min effect), 1 + min effect]; INCONCLUSIVE otherwise, and
  the fix is more runs or a larger input (double the runs when the CI half-width is under
  twice the minimum effect, quadruple beyond), never a softer claim.
- Hand rule, the fallback when no tool can run (the noise rule): a speedup is real only if
  median_after < median_before, the two IQR intervals do not overlap, and
  (median_before - median_after) > 2 x max(IQR_before, IQR_after). Start at 7 runs per arm;
  if it fails and the observed gap is under 2x, go to 15; under 1.2x, 30; still failing is
  "no measurable change". A 2x gap with IQRs of a few percent passes at 7 runs; the rule, not
  the tier, decides. `scripts/bench.py compare` still applies this rule.
- Medians under about 20 ms are dominated by process startup and timer resolution: raise the
  input size or loop inside the process.
- Instruction counts (`perf stat`, Cachegrind) are a fallback for very noisy machines,
  labelled as proxies, never reported as time.
- Services: report latency percentiles from an open-loop load generator at a fixed arrival
  rate; never average percentiles across runs or hosts.
- Write run counts as `runs=10` in the report; `n` is reserved for the input size.

## 4. Expensive runs

Expensive runs (a CI pipeline, a multi-hour batch): the protocol above is for runs that cost
seconds. When one run costs 20 minutes, measure in three layers and label each:

1. Before arm from history: the wall times of the last k runs under the current plan are
   already recorded (CI history, job logs). Report their median and IQR; no new runs.
2. Plan number from a replay: simulate both the current plan and the new one on the recorded
   per-job durations (for scheduling, the sum of each worker's assigned p50s). The replay is
   deterministic and free. Check it first against reality: the replayed current plan must
   land near the observed median (within about 10%); if it does not, the durations or the
   assumed assignment rule are wrong and the new plan's number is not trustworthy either.
3. Verdict from real reruns: rerun the same commit under the new plan, alternating with the
   old one when the runner pool drifts, and stop as soon as the noise rule passes. For a gap
   near 2x that is usually 5 to 7 runs per arm; the 15 and 30 tiers are for gaps under 2x and
   1.2x, which a scheduling change rarely produces. Different commits are different inputs
   (section 2), so "this week against last week" is not a comparison.

Report the three numbers as what they are: history (before), replay (plan), reruns (after).

## 5. Confirm the class changed

For scaling cards (dedup, joins, graphs, patterns): measure both arms at two sizes. The
exponent after should be lower than before (for example 2.0 -> 1.0). If both exponents are
the same and only the constant moved, label the result "constant-factor gain", not an
algorithmic one. For scheduling, selection, caching and packing cards there is no exponent to
drop; report the domain metric before and after instead: makespan against LB, selection
ratio, cache hit rate, or tokens used against B.

## 6. Pitfalls

- Timing I/O inside the measured region when the change is about compute: read into memory
  first.
- Warm OS page cache on the second arm only: warm both, or drop caches for both.
- Logging at debug level in one arm.
- Laptop on battery or thermally throttled: note it; repeat on mains if the gap is small.
- Background indexers, browsers, sync clients.
- Comparing a cached run to an uncached one.
- Different Python or Node versions between arms.
- Measuring with the profiler attached: its per-call overhead inflates call-heavy code
  (usually the slow version) and exaggerates the gain. Profile to find the hot spot; time
  without it.
- Layout bias: link order, environment size and ASLR move timings a few percent between
  launches; sample across separate process launches (each `dev.py bench` run is one).
- Cloud and shared machines vary 3 to 10% with no change; A/A on the same machine and day.
- JIT runtimes (Node, JVM, PyPy): warm-up runs exist for this, and warm-up can fail to
  settle; `compare` flags time trending with run index (Spearman rho over 0.5): add warm-up
  runs or measure steady state in-process.

## 7. Using dev.py

Real output: Python 3.9.6, macOS 26.5 arm64, 15 CPUs, other jobs running (load 5 to 10);
paths shortened to `<repo>`. aa and compare time a report step whose "8% faster" pull request
changed `total += x` to `total = total + x`.

Noise floor first:

```
$ python3 <skill-dir>/scripts/dev.py aa --runs 20 --save -- python3 before.py
a1: median 272.2 ms (CV 8.4%); a2: median 272.1 ms (CV 5.7%)
A/A noise floor: ratio CI 0.959 to 1.045 on 20 runs; minimum effect 8.6%
saved <repo>/.devkit/aa.json; dev.py compare uses it for 7 days
```

Then `dev.py bench --arm before="python3 before.py" --arm after="python3 after.py" --runs 20
--check-output --out bench.json` and the verdict, which uses the saved minimum effect:

```
$ python3 <skill-dir>/scripts/dev.py compare bench.json
warning: before: CV 5.4% is over 5%; the machine is noisy, close other work or add runs
arm      n    median     IQR        MAD        CV     min        max        outliers rho
before   20   268.6 ms   22.1 ms    8.1 ms     5.4%   260.0 ms   302.5 ms   0/0      -0.13
after    20   269.5 ms   16.8 ms    7.8 ms     4.8%   257.4 ms   311.5 ms   1/0      -0.21
Effect: ratio of medians 0.997x (95% CI 0.970x..1.040x, 5000 resamples, seed 0); Hodges-Lehmann shift 228 us saved; Mann-Whitney one-sided p 0.43 (faster) / 0.58 (slower), two-sided 0.86, normal
min effect 8.6% (from .devkit/aa.json measured 2026-10-21T01:11:21-04:00); alpha 0.05
verdict: NO DETECTABLE CHANGE: CI 0.970x..1.040x lies inside the band 0.921x..1.086x
Paste into the report:
Before: median 268.6 ms (IQR 22.1 ms, runs=20); After: median 269.5 ms (IQR 16.8 ms, runs=20).
Verdict: no detectable change (95% CI 0.97x..1.04x, min effect 8.64%)
```

The one-run "8%" is inside the noise: the answer is "no detectable change" with both
medians. The doubling test subtracts the startup baseline before the fit (a fixed startup
cost pulls the raw exponent toward 0):

```
$ python3 <skill-dir>/scripts/dev.py scale --sizes 1000,2000,4000,8000 -- python3 -c "n={n}; sum(i*j for i in range(n) for j in range(n))"
warning: smallest median 48.0 ms is under 10x the startup baseline 19.3 ms; raise the sizes until it is at least 0.5 s and refit
n=1000     median 48.0 ms    IQR 24.4 ms    CPU 44.6 ms    (runs 7)
n=2000     median 124.1 ms   IQR 12.1 ms    CPU 120.9 ms   (runs 7)
n=4000     median 451.3 ms   IQR 20.5 ms    CPU 445.0 ms   (runs 7)
n=8000     median 1.70 s     IQR 128.0 ms   CPU 1.67 s     (runs 7)
Baseline: median 19.3 ms for python3 -c pass, subtracted before the fit
Exponent: 1.97 (about quadratic; 95% CI 1.70..2.01)
Gate 1 at n=8000: CPU 1.67 s of 1.70 s wall -> CPU-bound
```

One-arm `bench` on a command that sleeps 0.2 s printed `CPU 24.3 ms of 235.3 ms wall ->
appears to be waiting`. `--json` prints one `devkit-<command>/1` object instead.
