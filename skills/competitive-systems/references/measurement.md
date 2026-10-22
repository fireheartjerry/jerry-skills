# Measurement protocol

Read section 1 for Gate 1, section 2 before the doubling test, and the rest before the A/A
run (Gate 3) and step 7; a stop at Gate 1 or 2 needs nothing past section 2. Section 8
finds the commit behind a benchmark that crept up, section 9 is the pre-flight for commands
with side effects, and section 10 is the answer when the symptom cannot be reproduced. Every number in
a report comes from this protocol; anything else is labelled "not verified". Run `dev.py`
from the project directory, where the measured commands' relative paths resolve.

Gate 1 (is it algorithmic?) and its evidence table live in SKILL.md, procedure step 1.
Gate 2 (is n big enough?) and the stop answer live in SKILL.md, procedure step 2.

## 1. Profile first

- Python: `python3 -m cProfile -s cumtime script.py` or
  `py-spy record -o profile.svg -- python3 script.py`.
- Node: `node --cpu-prof script.js`, open the `.cpuprofile` in the browser devtools.
- Go: `go test -cpuprofile cpu.out` then `go tool pprof -top cpu.out`.
- JVM: async-profiler (`asprof -d 30 -f profile.html <pid>`) or JFR
  (`-XX:StartFlightRecording=filename=rec.jfr`, then `jfr print rec.jfr`).
- Anything on Linux: `perf record -g <cmd>` then `perf report`.

Read the top five frames by inclusive time. Note whether they are your code, library code,
or syscalls (read, recv, write, futex, sleep). Note the call count of the top frame against
the number of distinct inputs it could have seen.

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

A fixture file, or a generator with a fixed seed, checked in next to the test. Two sizes:
the expected n and 10x. The same inputs for before and after. Never compare against a number
from a different input, machine, commit, or day (CI plans: section 4).

The measured command is a driver that takes n as its first argument, builds its input from a
seeded generator, runs the step and prints a short hash of the result, so `--check-output`
can compare arms run by run:

```python
import hashlib, random, sys
from mymodule import step                     # the code under test

n = int(sys.argv[1])
rng = random.Random(7)
d = max(1, int(0.017 * n))                     # hold the production distribution fixed:
rows = [rng.randrange(d) for _ in range(n)]    # 1.7% distinct at every n
out = step(rows)
print(len(out), hashlib.sha256(repr(out).encode()).hexdigest()[:16])
```

Sizes for the doubling test (n, 2n, 4n, 8n) come from regenerating, or subsampling a
shuffled dataset, with the distribution held fixed: duplicate rate, key cardinality, skew
and sortedness. Never tile or repeat a base file to reach a size: repeating 10,000 events
keeps the set of distinct keys constant, so a quadratic keep-first dedup measured 0.95
(about linear) on tiled inputs and 1.92 on fresh ones. Log the distribution figures next to
each size, and when the exponent comes out near 1, check them before calling it constant
factors. A prefix slice is valid only for shuffled or stationary data. Check that the output
size (for example the number kept) grows with n as it does in production.

Cost of the doubling test: (runs + warmup) x the sum of the medians, and the largest size is
8^e times the smallest for exponent e (64x when quadratic). When the job is already slow at
n, time below it (n/8, n/4, n/2, n), use `--runs 3` for sizes over about 5 s, and aim the
largest point at 10 to 30 s; a smallest median of about 100 ms is enough once the startup
baseline is subtracted. `dev.py scale` prints a progress line per size and an estimate of
the rest (quadratic after the first size, then the slope fitted so far), and does not start
a size whose projected cost would pass `--max-total` (300 s by default): it exits 1 naming
the largest size that fits. Raise the cap or run a long one in the background with `--json`
to a file. `bench` and `aa` apply the same cap once every arm has run once. In zsh, quote
the placeholder (`'{n}'`) when a redirect follows it.

Two outcomes the single exponent cannot express:

- The exponent stays between 1.3 and 1.7 at the largest size you can afford. A real mix
  such as a*n + b*n^2 reads about 1.5 near the size where the two terms are equal, and the
  production n may sit there. Fit no further: profile once at the production n and take the
  hot frame's share there (it feeds Gate 3), and say which regime production is in (the
  quadratic term's share of the time at that n). `dev.py scale` warns when the slopes rise
  toward the largest size inside this band.
- The pairwise slopes keep rising (`dev.py scale` prints them and warns when each is at
  least 1.3x the one before over three pairs and the last is above 2.5). That is
  exponential growth: an uncapped recursion or subset search. A fitted exponent means
  nothing there; stop fitting and route to the memoised recursion card
  (`dev.py catalog --id recursion-dp-memo`) or to a capped search (`cards-ci-tests.md`,
  Coverage under a time budget).

### Compiled and JIT runtimes

Time a prebuilt binary, never the build. `go run`, `cargo run`, `mvn exec:java`, `ts-node`
and `npx tsx` compile inside every sample, so the timing is mostly the compiler. Build once
with the production flags (release, optimisations on, the same target) for both arms, and
point both arms at the binaries: `go build -o bin/step example.com/proj/cmd/step`,
`cargo build --release`, `tsc` then `node dist/step.js`, `javac` then `java -cp`. A debug
build is not the production code: its timings, and the ratio between two of them, do not
transfer.

`dev.py bench`, `aa` and `scale` start a fresh process for every run, so the discarded
warm-up runs warm the disk cache and nothing else: a JIT (Node, the JVM, PyPy) starts cold in
every timed process. Two ways to measure steady state instead:

1. Loop inside the driver: K untimed iterations, then M timed ones, printing the M per-call
   times one per line; feed both arms to `dev.py compare --samples before=FILE
   --samples after=FILE`. The language's harness (JMH, `go test -bench`, criterion) does the
   same with better controls; its per-iteration samples go to `--samples` the same way.
2. Raise n until steady state dominates the process lifetime, and keep timing whole runs.

Either way, the driver prints n and a hash of the result, so `--check-output` (or a
`dev.py oracle --ref-cmd` run) can compare the arms. Minimal drivers that read n from the
first argument and build a seeded input:

```js
// step.mjs: node step.mjs 40000
import { createHash } from "node:crypto";
import { step } from "proj";                  // the package under test, built once
const n = Number(process.argv[2]);
let s = 7;                                       // fixed-seed LCG: same input every run
const rows = Array.from({ length: n }, () => (s = (s * 48271) % 2147483647) % Math.max(1, Math.floor(0.017 * n)));
const out = step(rows);
console.log(out.length, createHash("sha256").update(JSON.stringify(out)).digest("hex").slice(0, 16));
```

```go
// cmd/step/main.go: go build -o bin/step example.com/proj/cmd/step && bin/step 40000
package main

import ("crypto/sha256"; "fmt"; "math/rand"; "os"; "strconv"; "example.com/proj/step")

func main() {
	n, _ := strconv.Atoi(os.Args[1])
	r := rand.New(rand.NewSource(7))
	rows := make([]int, n)
	for i := range rows { rows[i] = r.Intn(max(1, n*17/1000)) }
	out := step.Step(rows)
	fmt.Printf("%d %x\n", len(out), sha256.Sum256([]byte(fmt.Sprint(out))))
}
```

```rust
// src/bin/step.rs: cargo build --release && target/release/step 40000
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
fn main() {
    let n: usize = std::env::args().nth(1).unwrap().parse().unwrap();
    let (mut s, d) = (7u64, (n * 17 / 1000).max(1) as u64);
    let rows: Vec<u64> = (0..n).map(|_| { s = s.wrapping_mul(6364136223846793005).wrapping_add(1); (s >> 33) % d }).collect();
    let out = proj::step(&rows);
    let mut h = DefaultHasher::new();
    out.hash(&mut h);
    println!("{} {:016x}", out.len(), h.finish());
}
```

```java
// Step.java: javac Step.java && java Step 40000
import java.util.*;
public class Step {
    public static void main(String[] a) throws Exception {
        int n = Integer.parseInt(a[0]);
        Random r = new Random(7);
        int[] rows = new int[n];
        for (int i = 0; i < n; i++) rows[i] = r.nextInt(Math.max(1, n * 17 / 1000));
        List<Integer> out = Proj.step(rows);
        byte[] d = java.security.MessageDigest.getInstance("SHA-256").digest(out.toString().getBytes());
        System.out.printf("%d %s%n", out.size(), HexFormat.of().formatHex(d, 0, 8));
    }
}
```

`DefaultHasher` in the Rust driver is stable within one toolchain, which is all
`--check-output` needs (both arms run on the same build machine); `HexFormat` needs Java 17.

A before arm from another revision (checking someone's "this PR is 8% faster"): build it in
a throwaway worktree, `git worktree add <tmpdir> <base-ref>`, and point that arm at it with
an absolute path (`--arm before="python3 <tmpdir>/step.py 40000"`), or use `--shell` with
`cd <tmpdir> && ...` for both arms. Never stash or check out in the user's tree. bench
records one revision in its metadata, so write the base revision in the report by hand.

## 3. Runs and statistics

- Warm-up: at least one run discarded (page cache, imports). Each run is a new process, so
  warm-up runs cannot warm a JIT; for Node, the JVM or PyPy loop inside the driver or raise n
  (section 2, Compiled and JIT runtimes).
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
  is not made, whatever the medians say. `dev.py` enforces this: a saved floor applies only
  to the command it was measured on (sizes aside), and `--min-effect` never lowers the
  effect below it. `dev.py aa` runs the command 2 x (runs + warmup) times, 42 by default:
  when one run costs more than about a minute, skip it, write "Noise floor: n/a" and set
  `--min-effect` by hand (Example B), or run the A/A on a proxy size that takes about a
  second (Example A).
- The decision rule (`dev.py compare`): FASTER when the CI lower bound exceeds 1 + min
  effect and the one-sided p is below 0.05; SLOWER mirrored; NO DETECTABLE CHANGE when the
  whole CI lies inside [1/(1 + min effect), 1 + min effect]; INCONCLUSIVE otherwise, and the
  fix is more runs or a larger input (double the runs when the CI half-width is under twice
  the minimum effect, quadruple beyond), never a softer claim.
- Hand rule, only when dev.py cannot run (the noise rule): a speedup is real only if
  median_after < median_before, the two IQR intervals do not overlap, and
  (median_before - median_after) > 2 x max(IQR_before, IQR_after). Start at 10 runs per arm;
  if it fails and the observed gap is under 2x, go to 15; under 1.2x, 30; still failing is
  "no measurable change". The rule, not the run count, decides. The deprecated
  `scripts/bench.py compare` still applies it.
- Outputs: pass `--check-output` so every run's stdout is hashed; arms whose outputs never
  agree get the verdict INVALID (exit 1, no paste block), because they did different work.
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
   old one when the runner pool drifts, and stop as soon as `dev.py compare` returns FASTER
   or SLOWER (the noise rule by hand only without dev.py). For a gap near 2x that is usually
   5 to 7 runs per arm; fewer than 10 is accepted for runs this expensive, with compare's
   run-count warning kept in the report. Gaps under 2x and 1.2x need the 15- and 30-run
   steps, which a scheduling change rarely produces. Different commits are different inputs
   (section 2), so "this week against last week" is not a comparison.

Report the three numbers as what they are: history (before), replay (plan), reruns (after).

## 5. Confirm the class changed

For scaling cards (dedup, joins, graphs, patterns): measure both arms at two sizes. The
exponent after should be lower than before (for example 2.0 -> 1.0). If both exponents are
the same and only the constant moved, label the result "constant-factor gain", not an
algorithmic one; `dev.py report` writes that on the Class line when the after exponent's CI
does not sit below the before one's, and says when the two fits cover different sizes. For
scheduling, selection, caching and packing cards there is no exponent to drop; report the
domain metric before and after instead: makespan against LB, selection ratio, cache hit
rate, or tokens used against B.

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
- JIT runtimes (Node, JVM, PyPy): each timed process starts cold, so the JIT's warm-up sits
  inside every sample; measure steady state in the driver and compare the samples
  (section 2). Inside one process warm-up can fail to settle: `compare` flags time trending
  with sample index (Spearman rho over 0.5); add untimed iterations.

## 7. Using dev.py

Real output: Python 3.9.6, macOS 26.5 arm64, 15 CPUs, other jobs running (load 5 to 10);
paths shortened to `<repo>`. aa and compare time a report step whose "8% faster" pull
request changed `total += x` to `total = total + x`.

Noise floor first:

```
$ python3 <skill-dir>/scripts/dev.py aa --runs 20 --save -- python3 before.py
note: aa runs the command 42 times (2 arms x (20 runs + 1 warmup))
note: aa will run `python3 before.py` 42 times; every run repeats its side effects (writes, network calls, spend), so measure a pure command or a stub (SKILL.md step 0)
a1: median 272.2 ms (CV 8.4%); a2: median 272.1 ms (CV 5.7%)
A/A noise floor: ratio CI 0.959 to 1.045 on 20 runs; minimum effect 8.6%
saved <repo>/.devkit/aa.json; dev.py compare uses it for 7 days
```

Then
`dev.py bench --arm before="python3 before.py" --arm after="python3 after.py" --runs 20 --check-output --out bench.json`
and the verdict, which uses the saved minimum effect:

```
$ python3 <skill-dir>/scripts/dev.py compare bench.json
warning: before: CV 5.4% is over 5%; the machine is noisy, close other work or add runs
arm      runs median     IQR        MAD        CV     min        max        outliers rho
before   20   268.6 ms   22.1 ms    8.1 ms     5.4%   260.0 ms   302.5 ms   0/0      -0.13
after    20   269.5 ms   16.8 ms    7.8 ms     4.8%   257.4 ms   311.5 ms   1/0      -0.21
Effect: ratio of medians 0.997x (95% CI 0.970x..1.040x, 5000 resamples, seed 0); Hodges-Lehmann shift 228 us saved; Mann-Whitney one-sided p 0.43 (faster) / 0.58 (slower), two-sided 0.86, normal
min effect 8.6% (from .devkit/aa.json on `python3 before.py`, 20 runs, measured 2026-10-21T01:11:21-04:00); alpha 0.05
verdict: NO DETECTABLE CHANGE: CI 0.970x..1.040x lies inside the band 0.921x..1.086x
Paste into the report:
Before: median 268.6 ms (IQR 22.1 ms, runs=20); After: median 269.5 ms (IQR 16.8 ms, runs=20).
Verdict: no detectable change (95% CI 0.97x..1.04x, min effect 8.64%)
```

The one-run "8%" is inside the noise: the answer is "no detectable change" with both
medians. The doubling test subtracts the startup baseline before the fit (a fixed startup
cost pulls the raw exponent toward 0):

```
$ python3 <skill-dir>/scripts/dev.py scale --quiet --sizes 1000,2000,4000,8000 -- python3 -c "n={n}; sum(i*j for i in range(n) for j in range(n))"
warning: smallest median 48.0 ms is under 5x the startup baseline 19.3 ms; raise the sizes until it is at least 96.5 ms and refit
n=1000     median 48.0 ms    IQR 24.4 ms    CPU 44.6 ms    (runs 7)
n=2000     median 124.1 ms   IQR 12.1 ms    CPU 120.9 ms   (runs 7)
n=4000     median 451.3 ms   IQR 20.5 ms    CPU 445.0 ms   (runs 7)
n=8000     median 1.70 s     IQR 128.0 ms   CPU 1.67 s     (runs 7)
Baseline: median 19.3 ms for python3 -c pass, subtracted before the fit
Exponent: 1.97 (about quadratic; 95% CI 1.70..2.01)
Pairwise slopes: 1.87, 2.04, 1.96
Gate 1 at n=8000: CPU 1.67 s of 1.70 s wall -> CPU-bound
```

One-arm `bench` on a command that sleeps 0.2 s printed
`CPU 24.3 ms of 235.3 ms wall -> appears to be waiting`. `--json` prints one
`devkit-<command>/1` object instead.

## 8. Finding the commit: bisect

A benchmark that crept up over many commits: `dev.py changepoint history.csv` (or
`--perf NAME`) names the step in the series of medians and the commits on each side. Then
find the commit inside that range:

```
dev.py bisect --good <last fast commit> --bad <first slow one> --runs 5 [--build "make"] -- python3 bench.py 40000
```

It adds a throwaway `git worktree` (the user's tree, index and branch are never touched),
times the two ends, and refuses to search unless the bad median is above the good one by
more than twice the minimum effect (the A/A floor saved for this command, or
`--min-effect`, 5% by default). The classifier is the midpoint of the two medians: a probed
commit is bad when its median is above it. A probe within one minimum effect of the midpoint
is rerun with twice the runs before it is classified, a commit that fails to build or run is
skipped for its nearest untested neighbour, and the worktree is removed at the end. It
walks the first-parent history, runs the command in the worktree (so relative paths name
that commit's files), and prints the first slower commit with both medians, or the range
left when skipped commits hide it. Confirm the result with `dev.py bench` on the two commits
(both checked out in worktrees, interleaved runs) before naming it in a report; never run
`git bisect` in the user's tree.

## 9. Side effects and shared systems

`bench`, `aa`, `scale`, `bisect` and the doubling test run a command tens of times, and
`check` and `test` run the project's suite. Before the first run, classify each command:

- Pure or read-only (computes from local inputs, writes only to stdout or a scratch
  directory): measure it as it is.
- Writes outside a scratch directory, calls a network service, costs money per call, or
  touches a production or shared system (a database, a queue, a vendor API, a CI cache):
  measure it only against a stub, a recorded replay, a copy of the data or a disposable
  worktree. If none exists, ask before the first run and give the call count it would
  make: runs x calls per run (an A/A is 42 runs; a 3,000-call loop is 126,000 calls).
- A suite whose fixtures delete or rewrite data: run `check` and `test` only in a
  disposable worktree.

Every measuring command prints how many times it will run the command before the first run,
so the count is in front of the user when you ask.

## 10. When the symptom cannot be reproduced here

Production-only data, a database or a cluster the session cannot reach, or private inputs:
never invent a measurement and never stall.

1. Argue the complexity from the code and give a labelled estimate:
   `dev.py bound ops --n <production n> --ops "<count>"` prints the operation count and the
   time at n and 10n marked `estimate`.
2. Ask for exactly one artifact, with the command that produces it: a profile at the
   production size (`py-spy record -o profile.svg -- <command>`, or the section 1 command
   for the language), or two timings at different n (`time <command> <n>` and the same at
   2n), which give the exponent.
3. If the user wants the patch anyway, deliver it with its oracle (step 6), make no speedup
   claim, and write `Verdict: not measured` in the report with the reason.
