# Scheduling cards

Covers makespan on parallel workers, jobs with dependencies, and deadlines. Every card needs
measured durations (p50 of the last k runs); with none, the first step is to record two or
three runs, not to schedule. A worker is a runner, machine, or process slot that runs one job
at a time (glossary); m is the number of workers. If the jobs have no durations and the
question is only "what order", go to Topological order in `cards-graphs-impact.md`.

## LPT across workers

**Problem shape:** m runners or process slots, independent jobs of uneven length, wall time
far above total/m; jobs split alphabetically or round-robin.

**Precondition test:** m >= 2; jobs independent; a duration estimate per job (p50 of the last
k runs; reject the card when durations are absent and go measure them first). Then, from the
data file itself, in this order:

1. Validate: every job has a duration, every duration is positive, no job appears twice.
   Replace an invalid or missing duration with the p50 of the valid ones and list the jobs
   you changed; a negative or zero duration is a recording bug worth reporting on its own.
2. Replay the current plan: assign the jobs by the rule the user described (alphabetical
   contiguous slices, round-robin) and sum each worker's durations. The replayed makespan
   should match the observed wall time within about 10%. If it does not, the durations or
   the assumed rule are wrong, and no plan computed from them can be trusted; say so.
3. Compute LB = max(sum/m, longest job, total duration of jobs pinned to one worker) and
   name which term binds. If the current makespan is within 10% of LB, stop: scheduling
   cannot help; lower LB instead (below).

The binding term decides what helps after scheduling. If sum/m binds, only less total work
(fewer jobs, cheaper jobs) or more workers lowers LB; splitting the longest job changes
nothing until m exceeds sum/longest (160 minutes of work with a 14-minute longest job: the
long job binds only from m = 12). If the longest job binds, split it or shorten it; more
workers do nothing. Report these as separate levers, never folded into the plan's number:
fewer jobs (Impact-based test selection, `cards-ci-tests.md`), cheaper jobs (Content-hash
cache, `cards-ci-tests.md`), more workers, splitting the longest job.

**Complexity:** O(n log n): sort descending, assign each job to the least-loaded worker via a
heap. Guarantee: makespan <= (4/3 - 1/(3m)) x optimal for identical workers; for m = 8 that is
1.29x. In practice LPT lands within a few percent of LB when no job dominates.

**Sketch:**

```python
import heapq

def lpt(jobs, m, pinned=()):           # jobs: [(duration, name)], m >= 2; pinned: names kept
    heap = [(0.0, w, []) for w in range(m)]                    # together on worker 0
    rest = []
    for d, name in sorted(jobs, key=lambda j: (-j[0], j[1])):
        if name in pinned:
            load, w, bucket = heap[0]
            heap[0] = (load + d, w, bucket + [name])
        else:
            rest.append((d, name))
    heapq.heapify(heap)
    for d, name in rest:
        load, w, bucket = heapq.heappop(heap)
        bucket.append(name)
        heapq.heappush(heap, (load + d, w, bucket))
    return [(load, bucket) for load, _, bucket in sorted(heap, key=lambda h: h[1])]

def lower_bound(jobs, m, pinned=()):   # pinned: names that must share one worker
    total = sum(d for d, _ in jobs)
    longest = max((d for d, _ in jobs), default=0.0)
    pinned_total = sum(d for d, n in jobs if n in pinned)
    return max(total / m, longest, pinned_total)
```

**Oracle:** Brute force all m^n assignments for n <= 8, m <= 3; assert every job appears
exactly once, LPT makespan <= (4/3 - 1/(3m)) x optimal, and determinism (same input, same
output).

**Traps:**
- m = 1 makes order irrelevant: the makespan is the sum, and the card does not apply.
- Heavy jobs pinned to one worker cap the gain: report LB with and without the pins
  (`lower_bound(jobs, m, pinned)` versus `lower_bound(jobs, m)`), and name the trade-off
  (serialising heavy jobs costs makespan, buys stability). Un-pin heavy jobs only when the
  measured peak memory and CPU of the heavy jobs that would then share a host fit that host
  with headroom; a guess is not a measurement, and the fallback is to keep them pinned.
- Using means instead of p50 when durations have outliers.
- Per-runner startup cost: add it as a fixed job per worker.
- New jobs with no history: assign the p50 of all jobs.
- Unstable output order: sort by (-duration, name) so the plan is reproducible.
- Jobs that used to share a worker, and so never overlapped, now run at the same time on
  different workers: the two longest jobs usually. Check what they share outside their
  worker (a database, a port, a rate-limited API, a licence) before shipping the plan; if
  they conflict, pin them together and report LB with the pin.
- Measuring the gain as "this week against last week": different commits are different
  inputs. Before comes from the recorded history under the current plan; the plan number
  comes from the replay; the verdict comes from reruns of the same commit under both plans,
  stopping when the noise rule passes (`measurement.md`, section 5, expensive runs).

**Not when:**
- Jobs have dependencies: Critical-path list scheduling.
- m = 1.
- The longest job exceeds sum/m: that job is the bottleneck; split it or run it first and
  accept LB.
- The current makespan is already within 10% of LB: the only levers are the ones that lower
  LB (precondition step 3).

## Critical path

**Problem shape:** A pipeline DAG of stages; "which stage should we shorten", "why does this
take 25 minutes when most stages are 2".

**Precondition test:** A DAG of stages with measured durations. A cycle means the input is
wrong: detect and report it first.

**Complexity:** O(V+E): longest path by dynamic programming in topological order.

**Sketch:**

```python
def critical_path(dur, preds, topo):   # topo from cards-graphs-impact.md
    finish, back = {}, {}
    for v in topo:
        best, arg = 0.0, None
        for u in sorted(preds.get(v, ())):
            if finish[u] > best:
                best, arg = finish[u], u
        finish[v] = dur[v] + best
        back[v] = arg
    end = max(topo, key=lambda v: (finish[v], v))
    path = []
    while end is not None:
        path.append(end)
        end = back[end]
    return finish[path[0]], list(reversed(path))
```

**Oracle:** Enumerate all paths on DAGs with V <= 12 and take the longest.

**Traps:**
- Cycles: detect and report, do not loop.
- Stages whose duration depends on the number of inputs: use measured values at the real
  input size.
- Shortening a non-critical stage changes nothing, so always report the path, not just the
  length.

**Not when:**
- No dependencies: LPT across workers.

## Critical-path list scheduling

**Problem shape:** Dependent jobs on m workers (a build graph on a pool of agents).

**Precondition test:** DAG, m >= 2, durations known.

**Complexity:** O(n log n + E). Guarantee: any list schedule is within (2 - 1/m) of optimal;
priority by remaining critical-path length is the usual good choice.

**Sketch:**
1. Compute `tail[v]` = longest path from v to a sink (critical path on the reversed DAG).
2. Keep a ready heap ordered by (-tail, name) and a worker heap ordered by (free_time, id).
3. Event loop: pop the earliest-free worker; if nothing is ready, advance time to the next
   job completion and release its successors (a worker idling while nothing is ready is
   correct, not a bug); otherwise assign the ready job with the largest tail, record (job,
   worker, start, end).
4. A job becomes ready when all predecessors have finished.
5. Makespan is the largest end time; report it next to LB = max(sum/m, critical path).

**Oracle:** Brute force on n <= 7, m = 2; check feasibility (no job starts before its
predecessors end) and the bound.

**Traps:**
- Ties broken nondeterministically: order by (-tail, name).
- Forgetting that a worker may idle while no job is ready.
- Using the plain longest-job priority instead of the remaining critical path.

**Not when:**
- No dependencies: LPT is simpler and has a better bound.

## Fewest workers for a deadline

**Problem shape:** "How many runners do we need to finish in D minutes".

**Precondition test:** Every job's duration <= D; jobs independent. A job longer than D makes
the deadline infeasible: say so.

**Complexity:** First-fit decreasing bin packing, O(n log n) with a tree, O(n^2) naive; uses
at most 11/9 OPT + 6/9 bins (Dosa's bound, tight), so never more than OPT + 1 for OPT <= 3
and within about 22% above that.

**Sketch:**

```python
def fewest_workers(jobs, D):           # jobs: [(duration, name)]
    workers = []                       # each: [load, [names]]
    for d, name in sorted(jobs, key=lambda j: (-j[0], j[1])):
        if d > D:
            raise ValueError("job %s (%.1f) exceeds deadline %.1f" % (name, d, D))
        for w in workers:
            if w[0] + d <= D:
                w[0] += d
                w[1].append(name)
                break
        else:
            workers.append([d, [name]])
    return workers
```

**Oracle:** Brute-force minimum bins for n <= 8.

**Traps:**
- D smaller than the longest job is infeasible (say so, do not silently add a worker).
- Per-worker startup cost: subtract it from D.

**Not when:**
- m is fixed: use LPT and report the makespan.

See also: the order inside one worker, when jobs have dependencies, comes from Topological
order in `cards-graphs-impact.md`; which jobs to run at all is `cards-ci-tests.md`.
