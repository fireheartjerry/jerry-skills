# Worked examples

Four examples: one in full, three short. A and B reach a card and end in the report template;
C and D stop at a gate and end in a plain-words answer to the user, which is what a stop looks
like. All numbers are illustrative; they are internally consistent (the quadratic case really
is quadratic between the sizes shown, and the timings match the 1e7 ops/s CPython budget) but
were not produced on any particular machine.

## Example A, in full: quadratic dedup in a data pipeline

Situation: an ETL step removes duplicate events; 1.2M events per day; the step takes about
19 minutes. The code is

```python
kept = []
for e in events:
    if not any(same(e, k) for k in kept):
        kept.append(e)
```

where `same` compares user id, event type, and the timestamp rounded down to the second.
The data is duplicate-heavy: about 1.7% of events are distinct, so `kept` grows to about
20,000 and the loop does roughly 0.0085 x n^2 comparisons (with no duplicates it would be
n^2 / 2, about 7e11 at 1.2M, which is 20 hours at 1e7/s, not 19 minutes: when the stated
time and the budget disagree by 60x, the model of the loop is wrong, so check the profile).

Step 0, pin the problem. n = 1.2M now, 12M at 10x. Target: under 1 minute. Invariant: the
output equals the current keep-first output on the same input, in the same order. Interface:
same function signature and return type.

Gate 1. Profile: 97% of time in `same`, called about 1.2e10 times for 1.2M inputs and about
20k distinct keys. Doubling test on a pinned generator (seed 7) at 100k and 200k events,
startup baseline subtracted: 8.1 s and 32.6 s, exponent 2.0. CPU-bound: user+sys 8.0 s of
8.1 s wall. Verdict: algorithmic. (Budget check: 0.0085 x (1e5)^2 = 8.5e7 comparisons at
1e7/s is 8.5 s, matching the measurement.)

Gate 2. Brute force at 10x n: 0.0085 x (12M)^2 = 1.2e12 comparisons, about 34 hours by the
budget; by the measured exponent, 1,166 s x 10^2.0 = 32 hours. The measured extrapolation is
the one reported, and both say proceed.

Card: Hash-key dedup (`cards-lookup-dedup.md`). Precondition test: write
`key(e) = (e.user_id, e.type, math.floor(e.ts))` and check `same(a, b) == (key(a) == key(b))`
on 10,000 random pairs. The first attempt failed on NaN timestamps: the old `same` returned
False for them (NaN != NaN), so NaN rows were never merged with anything. Keying NaN rows by
their own identity preserves that, and the report states it. Precondition: pass after the NaN
rule.

Implement: eight lines, set-based keep-first, same return order, no new dependency.

Oracle: brute-force keep-first versus the new function on 300 seeded random cases of 0 to 60
events, including rows differing only in case, whitespace, float versus int, and NaN: pass.

Measure. `bench.py compare` at n = 100k, 7 runs each, interleaved: before median 8.10 s
(IQR 0.12 s), after 90.3 ms (IQR 4.0 ms). At n = 1.2M: before 1,166 s (one run only, kept
because it agrees with the exponent; labelled as such), after 1.08 s (IQR 0.03 s, 7 runs),
which is 12x the 100k figure for 12x the input, as a linear step should be. `bench.py scale`
on the new code at 100k, 200k, 400k: exponent 1.0. Verdict: about 90x at 100k, about 1,000x
at 1.2M, and the class changed from quadratic to linear.

Report:

```
Problem: ETL dedup step takes 19 min/day. Input: n=1.2M events now, 12M at 10x. Target: under 1 min.
Gate 1: exponent 2.0 at n=100k..200k; CPU 8.0s of 8.1s wall -> algorithmic.
Gate 2: brute force at 10x n ~ 1.2e12 ops ~ 34 h by budget, 32 h by measured exponent (1,166 s x 100) -> proceed.
Card: Hash-key dedup (cards-lookup-dedup.md). Precondition: key(e) == (user_id, type, floor(ts)) reproduces same() on 10,000 random pairs incl. NaN: pass.
Invariant: keep-first output identical, same order. Oracle: nested-loop keep-first, 300 random cases, sizes 0..60, seed 7: pass.
Before: median 8.10 s (IQR 0.12 s, runs=7) at n=100k; After: median 90.3 ms (IQR 4.0 ms, runs=7).
Class: exponent 2.0 -> 1.0. Verdict: about 90x faster at 100k, about 1000x at 1.2M.
Kept: signature, return order, NaN rows never merged. Changed: set-based keep-first. New dependency: none.
Not verified: 12M-row run (projected from the exponent only); the 1.2M before figure is a single run.
```

## Example B, short: CI at 40 minutes on 8 runners

310 jobs; the p50 durations from the last 10 runs sum to 160 minutes; the longest two jobs
are 14 minutes (840 s) each; the current split is alphabetical, contiguous slices; the user
says the wall time is "about 40 minutes". A `durations.json` came with the request.

Step 0 starts with the file, not the summary. Loading it: 310 jobs, one of them
(`test_api_002`) recorded as -12 s, which is a timing bug, not a duration; it is replaced by
the p50 of the valid jobs (20 s) and named in the report. Total after the fix 9,632 s (160.5
minutes). Replaying the alphabetical split on the recorded durations gives slices of 16 to
18 minutes and one of 42.5 minutes: the last slice holds both 840 s `test_worker_*` jobs.
42.5 agrees with "about 40", so the data describes the problem.

Gate 1 for a scheduling symptom is makespan against the lower bound, not a doubling test:
LB = max(9,632 / 8 = 1,204 s = 20.1 min, longest 840 s = 14 min) = 20.1 minutes, and sum/m
is the binding term. The replayed 42.5 minutes is 2.1x LB, well over the 1.1x threshold, so
scheduling can help. Gate 2: 310 jobs on 8 workers, heap LPT is free tier. Precondition for
LPT (`cards-scheduling.md`): m = 8 >= 2, jobs independent, durations measured: pass. LPT with
a heap, ties broken by name, gives worker loads of 1,203 to 1,208 s, makespan 20.1 minutes:
0.3% above LB and far inside the (4/3 - 1/(3m)) = 1.29x bound. The two 840 s jobs land on
different workers and now run at the same time; the report asks whether they share anything
outside their runner (a database, a port, a rate limit) before the plan ships.

What helps next is decided by the binding term. sum/m binds, so splitting the two 14-minute
jobs changes nothing at m = 8; the longest job only starts to bind from m = 12 (9,632 / 840 =
11.5). Below 20 minutes needs less total work or more runners. The levers are listed
separately and never added into the plan's number: run only the tests a change affects
(Impact-based test selection), skip unchanged steps (Content-hash cache), add runners (the
floor drops to 14 minutes at m = 12, then stalls until the 840 s jobs are split).

Measured in the three layers for expensive runs (`measurement.md`, section 5). Before, from
CI history under the alphabetical split: median 40.4 minutes (IQR 2.3, last 10 runs). Plan,
from the replay: 20.1 minutes. After, from reruns of the same commit, alternating the two
plans, 7 each: median 20.6 minutes (IQR 0.8). The noise rule passes at 7 runs (no IQR
overlap, gap 19.8 > 2 x 2.3); the gap is near 2x, so the 15-run tier is not needed. There is
no exponent for a scheduling card; the class metric is makespan over LB, 2.01 -> 1.03.

```
Problem: CI wall time 40 min on 8 runners, alphabetical split. Input: 310 jobs, 160.5 min total work (one -12 s duration replaced by the p50, 20 s). Target: near LB.
Gate 1: replayed makespan 42.5 min vs LB 20.1 min (2.1x), sum/m binding; CPU n/a (runner wall time) -> scheduling (algorithmic).
Gate 2: n=310 jobs, m=8; heap LPT is free tier -> proceed.
Card: LPT across workers (cards-scheduling.md). Precondition: m=8 >= 2, independent, p50 durations from 10 runs, replay matches observed wall time: pass. LB = max(20.1, 14) = 20.1 min.
Invariant: every job runs exactly once. Oracle: brute force m^n for n<=8, m<=3, 200 cases, seed 3: pass; bound (4/3 - 1/24) = 1.29 holds.
Before: median 40.4 min (IQR 2.3, runs=10) from CI history; plan (replay) 20.1 min; After: median 20.6 min (IQR 0.8, runs=7), same commit, alternating.
Class: makespan/LB 2.01 -> 1.03. Verdict: 2.0x faster.
Kept: job list, per-job commands. Changed: assignment from alphabetical to LPT, deterministic by (-duration, name). New dependency: none.
Not verified: whether the two 840 s jobs can run concurrently (shared resources); the plan below 20 min needs fewer or cheaper jobs or 12+ runners, reported as separate levers; p50 durations drift and must be refreshed each run.
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
> after on the same input with 7 interleaved runs each (`bench.py compare`), and read the
> median and IQR, not one run.

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
