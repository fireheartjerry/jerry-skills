# Measurement protocol

Read this before Gate 1 and again before the after-measurement. Every number in a report
comes from this protocol; a number produced any other way is labelled "not verified".
`<skill-dir>` below is the directory containing SKILL.md. Run bench.py from the project
directory so the measured command's relative paths (`python3 step.py`, `< in.txt`) resolve.

## 1. Profile first

One line per runtime:

- Python: `python3 -m cProfile -s cumtime script.py` or `py-spy record -o profile.svg -- python3 script.py`.
- Node: `node --cpu-prof script.js`, open the `.cpuprofile` in the browser devtools.
- Go: `go test -cpuprofile cpu.out` then `go tool pprof -top cpu.out`.
- JVM: async-profiler (`asprof -d 30 -f profile.html <pid>`) or JFR
  (`-XX:StartFlightRecording=duration=60s,filename=rec.jfr`, then `jfr print --events jdk.ExecutionSample rec.jfr`).
- Anything on Linux: `perf record -g <cmd>` then `perf report`.

Read the top five frames by inclusive time. Note whether they are your code, library code, or
syscalls (read, recv, write, futex, sleep). Note the call count of the top frame against the
number of distinct inputs it could have seen.

## 2. Gate 1: is it algorithmic?

Evidence depends on the symptom (the first table in SKILL.md): a doubling test for code
whose time grows with input; makespan versus LB for jobs on workers; selection ratio for a
CI suite; must-keep and candidate tokens versus B for context packing; call count versus
distinct inputs for repeated work. Only the doubling test needs timing.

The doubling test: pick n and 2n (and 4n if cheap) on pinned inputs and run

```
python3 <skill-dir>/scripts/bench.py scale --sizes 1000,2000,4000 -- python3 step.py --n {n}
```

bench.py also times a startup baseline (`python3 -c pass` when the command is python,
`node -e 0` for node, or the command named with `--baseline 'CMD'`) and subtracts its median
before fitting, because a fixed startup cost pulls the raw exponent toward 0. Read the fitted
exponent:

| Exponent | Reading | Do |
|---|---|---|
| under 1.3 | about linear | constant factors: not this skill |
| 1.3 to 1.7 | between linear and quadratic (a log factor, mixed phases, or sizes too small) | inconclusive: raise sizes until the smallest median is at least 0.5 s and refit |
| 1.7 to 2.5 | about quadratic | algorithmic |
| 2.5 to 3.5 | about cubic | algorithmic |
| over 3.5 | worse than cubic, or exponential | algorithmic; check for uncapped subset or permutation search |

CPU versus wall: every bench.py subcommand reports the child's user+sys (from `getrusage`)
next to wall time and prints a `Gate 1:` fragment that says `CPU-bound` or `appears to be
waiting (wall > 1.5x CPU)`. Paste it into the report. Fallback without bench.py: `time <cmd>`
in the shell, or `/usr/bin/time -l` on macOS and `/usr/bin/time -v` with GNU time (often
absent from minimal Linux and container images; busybox lacks `-v`). If wall is more than
about 1.5x user+sys, the process is waiting (I/O, network, locks, sleeps) and algorithmic
work will not fix it.

Repeated-work signal: in the profile, a function's call count is far above its number of
distinct inputs. Exponential signal: the code enumerates subsets or permutations with no cap.

Write the verdict in one line before continuing:
`Gate 1: exponent 2.0 at n=100k..200k; CPU 8.0s of 8.1s wall -> algorithmic.`

## 3. Gate 2: is n big enough?

Estimate the brute-force operation count at 10x the expected n and divide by the budget:
about 1e7 simple operations per second in CPython, about 1e9 in compiled code (both
order-of-magnitude figures). When a measured exponent exists, extrapolate from the
measurement instead: time at 10x n = measured time x 10^exponent. The measured figure wins
over the generic budget whenever both are available.

Stop rule, the same everywhere in this skill: stop if the brute force at 10x n takes under
about 100 ms per call on a request path or inside a hot loop, or under about 1 s for a
one-shot batch job. Stopping still offers the free tier (a `set` instead of a list for
membership); it refuses the cheap and expensive tiers.

A stop answer is written for the user, not for the report, and carries four things:

1. The operation count now and at 10x, and the time each takes. When the code is at hand,
   time it once (`bench.py run` or `time`); the number is for scale, not a before/after
   claim, so one run is acceptable here and is labelled as a single run. Without the code,
   estimate from the budget and say it is an estimate.
2. The size at which the rule flips: solve ops(n) / budget = 100 ms (request path) or 1 s
   (batch) for n, and quote the precondition threshold of the card the symptom index names
   (for example Aho-Corasick: k >= 50 literal patterns and text far longer than the patterns)
   so the user knows when to come back. Opening that card to read its precondition is fine;
   applying it is not.
3. The free-tier change, if one exists, and the statement that the current code stays.
4. An offer to measure properly if the user still wants evidence. Never write that timing is
   unnecessary: the user asked for optimal, and the measurement is what shows the current
   code already is.

No report template, no "Gate", "card" or "tier" vocabulary in that answer. Examples C and D
in `worked-example.md` show the form.

Worked arithmetic (nested loop, n^2 comparisons):

- n = 5,000 rows: 25M comparisons now; at 10x n, 2.5e9, about 4 minutes in CPython. Proceed.
- n = 200 rows: at 10x n (2,000 rows) 4M comparisons, about 0.4 s in CPython. One-shot batch
  job: stop, offer the set. Request path: proceed, 0.4 s per call is over 100 ms.
- k = 40 patterns over 2,000 lines, nightly: 80k C-level substring checks now, 800k at 10x,
  milliseconds either way against a 1 s batch budget. Stop. Flip size: 1 s of `str in` checks
  is about 1e7 of them, so 5,000 patterns over 2,000 lines, or 40 patterns over 250k lines.

Tiers: free (dict, set, sort, heap, prefix sums, bisect); cheap (union-find, topological
sort, BFS on a graph you already have); expensive (Aho-Corasick, SCC and bitset closure, LSH,
knapsack DP over a large budget, exact subset search). The expensive tier needs the Gate 1
evidence and the 10x-n estimate written in the report.

## 4. Pinned inputs

A fixture file, or a generator with a fixed seed, checked in next to the test. Two sizes: the
expected n and 10x. The same inputs for before and after. Never compare against a number from
a different input, machine, commit, or day. For CI plans that means rerunning the same commit
under both plans, not comparing the next week to the last.

## 5. Runs and statistics

- Warm-up: one run discarded (JIT, page cache, DNS).
- Count: 7 per arm to start. Apply the noise rule; if it fails and the observed gap is
  under 2x, go to 15; under 1.2x, 30. bench.py warns when the run count is below the tier the
  observed gap needs. The tiers exist because noise of a few percent can hide a small gap;
  they are not a claim that 7 runs cannot show a large one. A 2x gap with IQRs of a few
  percent passes the noise rule at 7 runs, and the rule, not the tier, decides.
- Interleave arms (A B A B ...) so drift (thermal, background jobs) hits both equally.
- Report median and IQR (25th to 75th percentile), plus min and max. Means and standard
  deviations are not used: timing distributions are skewed.
- Noise rule: a speedup is real only if median_after < median_before, the two IQR intervals do
  not overlap, and (median_before - median_after) > 2 x max(IQR_before, IQR_after). Anything
  else is "no measurable change". If the user wants to resolve it, increase n or the run count,
  not the claim.
- Medians under about 20 ms are dominated by process startup and timer resolution: increase
  the input size or loop inside the process.
- Write run counts as `runs=7` in the report; `n` is reserved for the input size.

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
   (section 4), so "this week against last week" is not a comparison.

Report the three numbers as what they are: history (before), replay (plan), reruns (after).

## 6. Confirm the class changed

For scaling cards (dedup, joins, graphs, patterns): measure both arms at two sizes. The
exponent after should be lower than before (for example 2.0 -> 1.0). If both exponents are
the same and only the constant moved, label the result "constant-factor gain", not an
algorithmic one. For scheduling, selection, caching and packing cards there is no exponent to
drop; report the domain metric before and after instead: makespan against LB, selection
ratio, cache hit rate, or tokens used against B.

## 7. Report template

The template lives in SKILL.md (Report template) and a filled example in
`worked-example.md` (Example A). Neither is repeated here, so there is one copy of each.

## 8. Pitfalls

- Timing I/O inside the measured region when the change is about compute: read into memory
  first.
- Warm OS page cache on the second arm only: warm both, or drop caches for both.
- First-run JIT (Node, JVM, PyPy): the warm-up run exists for this.
- Logging at debug level in one arm.
- Laptop on battery or thermally throttled: note it; repeat on mains if the gap is small.
- Background indexers, browsers, sync clients.
- Comparing a cached run to an uncached one.
- Different Python or Node versions between arms.
- Measuring with the profiler attached: a deterministic profiler adds per-call overhead, so
  call-heavy code (usually the slow version) is inflated more and the gain is exaggerated.
  Profile to find the hot spot; time without it.

## 9. Using bench.py

`python3 <skill-dir>/scripts/bench.py` times whole commands, including process startup, and
reports CPU time (user+sys of the child) next to wall time. For in-process microbenchmarks use
`timeit` with the same median-and-IQR reporting. Three subcommands; defaults are 7 runs and 1
warm-up.

`run`: time one command.

```
$ python3 <skill-dir>/scripts/bench.py run --runs 7 -- python3 step.py --n 100000
runs    7
median  8.10 s
q1      8.04 s
q3      8.16 s
iqr     120.0 ms
min     7.98 s
max     8.41 s
cpu     8.02 s (wall/CPU 1.01)
Gate 1: CPU 8.02 s of 8.10 s wall -> CPU-bound
```

`compare`: interleaved A/B. The arms are shell strings (run with `shell=True`) because
before/after are often pipelines.

```
$ python3 <skill-dir>/scripts/bench.py compare --runs 7 --before 'python3 old.py < in.txt' --after 'python3 new.py < in.txt'
arm      median      IQR         min         max         CPU         wall/CPU  runs
before   8.10 s      120.0 ms    7.98 s      8.41 s      8.02 s      1.01      7
after    90.3 ms     4.0 ms      85.1 ms     99.0 ms     86.0 ms     1.05      7
Verdict: faster (89.70x)
Before: median 8.10 s (IQR 120.0 ms, runs=7); After: median 90.3 ms (IQR 4.0 ms, runs=7).
Verdict: 89.70x faster
```

An inconclusive result prints
`Verdict: inconclusive (within noise): medians 1.02 s vs 980.0 ms, IQRs overlap` and the
paste-ready line `Verdict: no measurable change`. Warnings follow when the run count is
below the tier the observed gap needs (7, 15 under 2x, 30 under 1.2x), when a median is under
20 ms, or when an arm appears to be waiting.

`scale`: the doubling test. Every `{n}` in the command is replaced by each size. The startup
baseline is timed and subtracted before the fit; the sizes should still be large enough that
the smallest median is at least 10x the baseline (about 0.5 s for Python), or the fit is
noisy and bench.py says so.

```
$ python3 <skill-dir>/scripts/bench.py scale --sizes 1000,2000,4000 -- python3 -c "n={n}; sum(i*j for i in range(n) for j in range(n))"
n=1000     median 58.5 ms    IQR 1.5 ms     CPU 55.0 ms    (runs 7)
n=2000     median 169.2 ms   IQR 4.5 ms     CPU 165.6 ms   (runs 7)
n=4000     median 605.8 ms   IQR 9.4 ms     CPU 601.8 ms   (runs 7)
Baseline: median 22.2 ms for python3 -c pass, subtracted before the fit
Exponent: 2.00 (about quadratic)
Gate 1 at n=4000: CPU 601.8 ms of 605.8 ms wall -> CPU-bound
warning: smallest median 58.5 ms is under 10x the startup baseline 22.2 ms; raise the sizes until it is at least 0.5 s and refit
```

Without the subtraction the same command reads about 1.7 at these sizes and under 1 at
300/600/1200, which would send a truly quadratic loop to the "not this skill" row. For a
command whose interpreter bench.py does not recognise, pass `--baseline 'CMD'` with a
startup-only invocation, or `--no-baseline` and sizes large enough that startup is under 10%
of the smallest median.

`--json` on any subcommand prints one JSON object with the same numbers instead of the
table. A nonzero exit from the measured command aborts with the command, its exit code and
the last 20 lines of its stderr, exit status 3; a command that cannot be started (not on PATH)
also exits 3 with the OS error; usage errors (a size of 0, `--runs 0`, no `{n}`) exit 2.
