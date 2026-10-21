# Worked examples

Four examples: two in full, two short. A and B reach a card and end in the report; C and D
stop at a gate and end in a plain-words answer to the user, which is what a stop looks like.
The `dev.py` outputs in A and B are real runs on the fixtures `example-a` and `example-b`,
which `python3 <skill-dir>/evals/fixtures/make_fixtures.py --only example-a,example-b --out DIR`
writes. Timings were measured with Python 3.9.6 on macOS 26.5 arm64 (15 CPUs) while other
jobs held the load average at 5 to 10, which the warnings report; they vary from run to run.
The other outputs (`bound makespan`, `compare ci-runs.json`, `oracle`, the precondition
checks) reproduce exactly on any platform. Worker job lists are cut to the first two names
and absolute paths are shortened. The production sizes and the CI history in B
(`ci-runs.json`) are the example's given data. C and D are illustrative.

## Example A, in full: quadratic dedup in a data pipeline

Situation: an ETL step removes duplicate events; 1.2M events per day; the step takes about
19 minutes. The code is

```python
kept = []
for e in events:
    if not any(same(e, k) for k in kept):
        kept.append(e)
```

where `same` compares user id, event type, and the timestamp rounded down to the second, and
never matches a NaN timestamp. About 1.7% of events are distinct, so `kept` grows to about
20,000 and the loop does roughly 0.0085 x n^2 comparisons. The fixture reproduces that with a
seeded generator (seed 7, 1.7% distinct, a NaN timestamp every 5,000 events).

Step 0, pin the problem. n = 1.2M now, 12M at 10x. Target: under 1 minute. Invariant: the
output equals the current keep-first output on the same input, in the same order. Interface:
same function signature and return type. Noise floor of the step at n = 40,000:

```
$ python3 <skill-dir>/scripts/dev.py aa --runs 20 --save -- python3 dedup_before.py 40000
a1: median 1.28 s (CV 5.0%); a2: median 1.28 s (CV 5.5%)
A/A noise floor: ratio CI 0.966 to 1.044 on 20 runs; minimum effect 7.8%
saved <run dir>/.devkit/aa.json; dev.py compare uses it for 7 days
```

Step 1, Gate 1. Profile (cProfile, n = 40,000): `dedup` takes 2.21 s of 2.26 s, 13.5M calls
to `same` for 688 distinct keys. Doubling test on the pinned generator:

```
$ python3 <skill-dir>/scripts/dev.py scale --sizes 10000,20000,40000,80000 -- python3 dedup_before.py {n}
warning: smallest median 114.9 ms is under 10x the startup baseline 19.8 ms; raise the sizes until it is at least 0.5 s and refit
n=10000    median 114.9 ms   IQR 26.9 ms    CPU 109.6 ms   (runs 7)
n=20000    median 350.4 ms   IQR 24.0 ms    CPU 346.1 ms   (runs 7)
n=40000    median 1.29 s     IQR 53.8 ms    CPU 1.28 s     (runs 7)
n=80000    median 5.08 s     IQR 255.0 ms   CPU 5.02 s     (runs 7)
Baseline: median 19.8 ms for python3 -c pass, subtracted before the fit
Exponent: 1.91 (about quadratic; 95% CI 1.83..1.97)
Gate 1 at n=80000: CPU 5.02 s of 5.08 s wall -> CPU-bound
```

Exponent 1.91 (95% CI 1.83..1.97) and CPU-bound: algorithmic. The budget agrees: 0.0085 x
80,000^2 = 5.4e7 comparisons at 1e7/s is 5.4 s; measured 5.08 s. (The warning asks for a
smallest size of at least 0.5 s; refitting on the three larger sizes gives 1.97.)

Step 2, Gate 2. `dev.py bound ops --n 1200000 --ops "0.0085*n**2" --path batch`: 1.22e10
comparisons, 1,224 s at 1.2M (the reported 19 minutes) and 1.22e12, about 34 hours, at 10x:
proceed. The measured exponent gives 5.08 s x 15^1.91, about 15 minutes, at 1.2M; both say the
same.

Step 3, Gate 3. `dev.py bound amdahl --p 0.98 --s 1000`: editing a 98% share caps the
end-to-end gain at 50x. Proceed; any measured speedup above 50x would be a bug.

Step 4, card. `dev.py catalog --symptom "nested loop duplicates"` ranks Hash-key dedup
(`cards-lookup-dedup.md`) first. Precondition: `key(e) = (e.user_id, e.type,
math.floor(e.ts))`, with NaN rows keyed by their own identity because the old `same` never
matched them, reproduces `same(a, b)` on 10,000 random pairs of distinct rows including NaN:
pass. (A first attempt that mapped NaN to a sentinel failed at pair 365: two NaN rows with
the same user and type got equal keys, but the old code never merged NaN rows.)

Step 5: eight lines, set-based keep-first, same return order, no new dependency; the old
function stays callable as the reference.

Step 6, oracle before timing:

```
$ python3 <skill-dir>/scripts/dev.py oracle --ref dedup_before:dedup --new dedup_after:dedup --gen custom:gen_events:case --cases 300 --seed 7 --out test_oracle_dedup.py
wrote test_oracle_dedup.py (300 cases + edge cases, seed 7, exact)
run it before any timing: python3 -m unittest -v test_oracle_dedup.py
$ python3 -m unittest -v test_oracle_dedup.py
test_anti_cache_mutated_input (test_oracle_dedup.OracleTest) ... ok
test_matches_reference (test_oracle_dedup.OracleTest) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.076s

OK
```

Step 7, measure. Ten interleaved runs per arm, output hashes compared:

```
$ python3 <skill-dir>/scripts/dev.py bench --arm before="python3 dedup_before.py 40000" --arm after="python3 dedup_after.py 40000" --runs 10 --check-output --input events.py --out bench-40k.json
warning: before: 2 of 10 runs are Tukey outliers (over 10%); kept, check for interference
warning: before: times trend with run index (Spearman rho -0.78): warmup unfinished or drift
warning: after: CV 11.5% is over 5%; the machine is noisy, close other work or add runs
warning: load average 10.4 exceeds half the CPU count (15) at measurement time
arm      n    median     IQR        MAD        CV     min        max        outliers rho
before   10   1.37 s     42.6 ms    21.3 ms    4.0%   1.27 s     1.47 s     2/0      -0.78
after    10   47.6 ms    4.6 ms     2.6 ms     11.5%  44.4 ms    62.5 ms    1/0      -0.43
Effect: ratio of medians 28.779x (95% CI 25.873x..30.034x, 5000 resamples, seed 0); Hodges-Lehmann shift 1.32 s saved; Mann-Whitney one-sided p 5.41e-06 (faster) / 1 (slower), two-sided 1.08e-05, exact
min effect 7.8% (from .devkit/aa.json measured 2026-10-21T00:59:22-04:00); alpha 0.05
verdict: FASTER: CI lower bound 25.873x > 1.078x and one-sided p 5.413e-06 < 0.05
Paste into the report:
Before: median 1.37 s (IQR 42.6 ms, runs=10); After: median 47.6 ms (IQR 4.6 ms, runs=10).
Verdict: 28.78x faster (95% CI 25.87x..30.03x, p=5.41e-06)
wrote bench-40k.json
```

The warnings are honest: the machine was busy and the before arm drifted. The verdict still
holds by a wide margin (CI lower bound 25.9x against a 7.8% minimum effect) and is inside
the 50x cap. The class changed: `dev.py scale --sizes 100000,200000,400000,800000 -- python3
dedup_after.py {n}` gives `Exponent: 0.96 (about linear; 95% CI 0.91..1.12)` (599 ms at
800,000 events).

At 1.2M events the new step takes 970.9 ms (IQR 35.7 ms, 10 runs) against the reported 19
minutes. The report, from `dev.py report` with the hand-written fields passed as `--set`:

```
Problem: ETL dedup step takes 19 min/day. Input: n=1.2M events now, 12M at 10x. Target: under 1 min.
Gate 1: exponent 1.91 at n=10k..80k (95% CI 1.83..1.97); CPU 5.02 s of 5.08 s wall -> algorithmic.
Gate 2: brute force at 10x n ~ 1.2e12 ops ~ 34 h by budget (1.2M: 1,224 s, matching the reported 19 min) -> proceed.
Gate 3: Amdahl cap 50x for share 0.98 (profile: dedup 2.21 s of 2.26 s) -> proceed.
Noise floor: A/A ratio CI 0.966..1.044 on 20 runs; min effect 7.8%.
Card: Hash-key dedup (exact duplicates) (cards-lookup-dedup.md). Precondition: key(e) reproduces same() on 10,000 random pairs of distinct rows incl. NaN: pass.
Card facts: none listed for this card; Gate 2 numbers quoted instead.
Invariant: keep-first output identical, same order; NaN rows never merged.
Oracle: `python3 -m unittest test_oracle_dedup.py`: exit 0 (pass); last line: OK.
Before: median 1.37 s (IQR 42.6 ms, runs=10); After: median 47.6 ms (IQR 4.6 ms, runs=10).
Class: exponent 1.91 (95% CI 1.83..1.97 at n=10000..80000) -> 0.96 (95% CI 0.91..1.12 at n=100000..800000).
Verdict: 28.78x faster (95% CI 25.87x..30.03x, p=5.41e-06; medians 1.37 s -> 47.6 ms).
Guards: oracle before timing pass (incl. anti-cache); fresh inputs yes (new process and regenerated events per run); harness untouched yes; implausibility within Amdahl cap (28.8x < 50x).
Kept: signature, return order, NaN rows never merged. Changed: set-based keep-first. New dependency: none.
Environment: Python 3.9.6 on macOS-26.5-arm64-arm-64bit; 15 CPUs; load 10.4; git 64b1e16 (dirty).
Protocol: metric wall; runs 10/10; min effect 7.8% (from .devkit/aa.json measured 2026-10-21T00:59:22-04:00); alpha 0.05; 5000 bootstrap resamples, seed 0; Mann-Whitney exact.
Not verified: the 12M-row run (projected from the exponent); the 1.2M before time is the user's figure, not rerun; measured with load average 10 on the machine (warnings above).
```

## Example B, in full: CI at 40 minutes on 8 runners

310 jobs; the p50 durations from the last 10 runs come in `durations.json`; the current split
is alphabetical, contiguous slices; the user says the wall time is "about 40 minutes".

Step 0 starts with the file, not the summary:

```
$ python3 <skill-dir>/scripts/dev.py bound makespan --durations durations.json --current alphabetical
jobs 310, total 9632.00 s, longest 840.00 s (test_worker_301), workers 8
replaced: test_api_002 (non-positive) -> p50 20
current plan (alphabetical): makespan 2538.00 s (worker 8 binds)
LB 1204.00 s (total/workers binds; the longest job binds from m = 12)
LPT plan: makespan 1208.00 s = 1.00x LB; guarantee <= 1.29x optimal (4/3 - 1/(3m), m = 8)
  worker 1: 1208.00 s  test_worker_285, test_worker_001, ... (27 jobs)
  worker 2: 1204.00 s  test_worker_301, test_auth_192, ... (26 jobs)
  worker 3: 1204.00 s  test_billing_242, test_db_063, ... (42 jobs)
  worker 4: 1204.00 s  test_core_308, test_core_098, ... (43 jobs)
  worker 5: 1203.00 s  test_db_209, test_ui_246, ... (43 jobs)
  worker 6: 1203.00 s  test_api_141, test_auth_304, ... (43 jobs)
  worker 7: 1203.00 s  test_worker_009, test_worker_234, ... (43 jobs)
  worker 8: 1203.00 s  test_db_052, test_api_194, ... (43 jobs)
```

One job, `test_api_002`, is recorded as -12 s, a timing bug rather than a duration; it is
replaced by the p50 of the valid jobs (20 s) and named in the report. The replayed
alphabetical split puts both 840 s `test_worker_*` jobs in the last slice: 2,538 s, 42.3
minutes, against "about 40", so the data describes the problem.

Gate 1 for a scheduling symptom is makespan against the lower bound, not a doubling test:
LB = max(9,632 / 8 = 1,204 s, longest 840 s) = 20.1 minutes, sum/m binding. 42.3 minutes is
2.1x LB, far over the 1.1x threshold. Gate 2: 310 jobs on 8 workers, heap LPT is free tier.
Gate 3: the plan moves the whole wall time. Card: `dev.py catalog --symptom "CI 40 minutes
runners"` ranks LPT across workers first and prints the facts to quote: the (4/3 - 1/(3m))
bound and m >= 2. Precondition: m = 8 >= 2, independent jobs, measured durations, replay
matches: pass. The LPT plan is 1,208 s, 0.3% above LB and far inside the (4/3 - 1/24) = 1.29
bound. The two 840 s jobs now run at the same time on different workers; the report asks
whether they share anything outside their runner (a database, a port, a rate limit) before
the plan ships.

What helps next is decided by the binding term. sum/m binds, so splitting the 14-minute jobs
changes nothing at m = 8; the longest job binds only from m = 12 (9,632 / 840 = 11.5;
`dev.py bound makespan --workers 12` gives LB 840 s, longest job binding). Below 20 minutes
needs less total work or more runners, listed as separate levers and never added into the
plan's number: Impact-based test selection, Content-hash cache, more runners.

Measured in the three layers for expensive runs (`measurement.md`, section 4): before from
CI history under the alphabetical split (10 runs), the plan from the replay (1,208 s), after
from 7 reruns of the same commit under LPT, alternating with the old plan. `ci-runs.json`
holds those wall times as a bench file, and `compare` decides:

```
$ python3 <skill-dir>/scripts/dev.py compare ci-runs.json --min-effect 0.05
warning: after: only 7 runs; the protocol asks for at least 10 per arm
arm      n    median     IQR        MAD        CV     min        max        outliers rho
alphabetical 10   2424.00 s  150.00 s   75.00 s    4.0%   2290.00 s  2610.00 s  0/0      0.22
lpt      7    1236.00 s  25.00 s    11.00 s    1.3%   1212.00 s  1262.00 s  0/0      -0.21
Effect: ratio of medians 1.961x (95% CI 1.894x..2.031x, 5000 resamples, seed 0); Hodges-Lehmann shift 1186.50 s saved; Mann-Whitney one-sided p 5.14e-05 (faster) / 1 (slower), two-sided 0.000103, exact
min effect 5% (--min-effect); alpha 0.05
verdict: FASTER: CI lower bound 1.894x > 1.050x and one-sided p 5.142e-05 < 0.05
Paste into the report:
Before: median 2424.00 s (IQR 150.00 s, runs=10); After: median 1236.00 s (IQR 25.00 s, runs=7).
Verdict: 1.96x faster (95% CI 1.89x..2.03x, p=5.14e-05)
```

The only warning is the run count: 7 reruns at 20 minutes each, accepted because the CI lower
bound (1.89x) is far above the 5% minimum effect set by hand. There is no exponent for a
scheduling card; the class metric is makespan over LB, 2.01 -> 1.03.

```
Problem: CI wall time 40 min on 8 runners, alphabetical split. Input: 310 jobs, 160.5 min total work (one -12 s duration replaced by the p50, 20 s). Target: near LB.
Gate 1: replayed makespan 42.3 min vs LB 20.1 min (2.11x), sum/m binding; CPU n/a (runner wall time) -> scheduling (algorithmic).
Gate 2: n=310 jobs, m=8; heap LPT is free tier -> proceed.
Gate 3: n/a: the plan changes the whole wall time (share 1.0) -> proceed.
Noise floor: n/a for CI reruns: 20 minutes a run; min effect 5% set by hand (--min-effect 0.05).
Card: LPT across workers (cards-scheduling.md). Precondition: m=8 >= 2, independent jobs, p50 durations from 10 runs, replay 42.3 min vs observed about 40: pass. LB = max(20.1, 14.0) = 20.1 min.
Card facts: (4/3 - 1/(3m)) = 1.29 at m = 8; m >= 2; the longest job binds only from m = 12.
Invariant: every job runs exactly once.
Oracle: brute force m^n for n <= 8, m <= 3 on 200 random cases, seed 3: pass (checks the algorithm); the m = 8 plan itself is checked against LB (0.3% above) and the (4/3 - 1/24) = 1.29 bound.
Before: median 2424.00 s (IQR 150.00 s, runs=10); After: median 1236.00 s (IQR 25.00 s, runs=7).
Class: makespan/LB 2.01 -> 1.03 (history -> reruns); replay 2.11 -> 1.00.
Verdict: 1.96x faster (95% CI 1.89x..2.03x, p=5.14e-05; medians 2424.00 s -> 1236.00 s).
Guards: oracle before timing pass; fresh inputs n/a (same commit rerun); harness untouched yes; implausibility within the replayed plan (1.96x measured vs 2.10x replayed).
Kept: job list, per-job commands. Changed: assignment from alphabetical to LPT, deterministic by (-duration, name). New dependency: none.
Environment: CI: 8 identical runners; history and reruns from the CI system, not this machine.
Protocol: metric wall; runs 10/7; min effect 5% (--min-effect); alpha 0.05; 5000 bootstrap resamples, seed 0; Mann-Whitney exact.
Not verified: whether the two 840 s jobs can run concurrently (shared resources); below 20 min needs fewer or cheaper jobs or 12+ runners, reported as separate levers; p50 durations drift and must be refreshed each run.
```

## Example C, short: stops at Gate 1

"Our import script is slow": 45 s for 3,000 records, "I was thinking a hash map". `time`
reports real 45.2 s, user 1.9 s, sys 0.4 s. Profile: 94% in `socket.recv`. Verdict: the
process is waiting on the network (one HTTP call per record). Not algorithmic; no card is
opened, no template is filled. The answer to the user, in full:

> The loop is not the problem. Of the 45.2 s, the CPU is busy for 2.3 s (user 1.9 + sys 0.4);
> the other 43 s the process is waiting for the network, and 94% of the profile is in
> `socket.recv`. A hash map or any other change to the loop can only touch the 2.3 s. The
> 3,000 records cost one HTTP call each, about 15 ms a call. Two fixes, in order: the API
> accepts 100 ids per request, so batching turns 3,000 calls into 30; if batching is not
> available, run the calls with bounded concurrency (8 to 16 in flight). Either should land
> under 10 s. I have not implemented or measured that; when it is in, compare before and
> after on the same input with at least 10 interleaved runs each, and read the medians
> and the confidence interval, not one run.

## Example D, short: stops at Gate 2

"Should I use Aho-Corasick or a trie here? 40 patterns, about 2,000 lines, runs nightly. Want
it optimal." The code is `for line in lines: for p in patterns: if p in line`. Step 0: the
files are present, so the current script is timed once: 4 ms for the scan, about 20 ms wall
including interpreter startup (single run, for scale). Gate 2: 40 x 2,000 = 80k C-level
substring checks now, 800k at 10x, about 40 ms, against a 1 s budget for a nightly batch.
Stop. The symptom index names `cards-multi-pattern.md`; its precondition line is read for the
threshold and nothing is applied. The answer to the user, in full:

> Neither. Keep the loop. It does 40 x 2,000 = 80,000 substring checks, each in C, and the
> scan took 4 ms when I timed it (one run, enough for scale). At ten times the input, 800,000
> checks, it is about 40 ms, against a job that runs once a night. Aho-Corasick starts to pay
> at about 50 or more literal patterns over text far longer than the patterns themselves,
> and in Python only through a compiled library (`pyahocorasick`), which is a new dependency;
> a hand-written version would be slower than `in` at this size. A trie on its own does not
> do substring search at all; it is the structure Aho-Corasick is built from. The point where
> a change would matter is about 1e7 checks a run: 5,000 patterns over these 2,000 lines, or
> 40 patterns over 250,000 lines. Below that, "optimal" is the code you have. If you want the
> evidence on your logs, I can time the matching on a real nightly file; the verdict will
> hold.
