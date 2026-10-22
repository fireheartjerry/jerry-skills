# Repeated-work cards

Covers the same computation done many times for the same inputs, within a run or across
runs, including a recursion that solves the same subproblem many times. Signal: the profile shows a function's call count far above its number of distinct
inputs, or a step whose inputs rarely change reruns every time. Gate 1 evidence for this file
is that ratio (calls over distinct inputs, or reruns over changed inputs), not a doubling
test. If the work is not repeated but genuinely superlinear, go back to the symptom index. A
graph rebuilt on every call or run is repeated work and is handled here (Content-hash cache
or Incremental recompute); traversing a graph that is already built is in
`cards-graphs-impact.md`.

## Memoise pure functions

**Problem shape:** A function called many times with few distinct arguments: parsing the same
config, resolving the same path, computing the same hash.

**Precondition test:** The function is pure (same inputs, same output, no side effects you
rely on) and its arguments are hashable or can be reduced to a hashable key. A function that
reads time, environment or files is not pure by its arguments: key on the content instead,
or this card fails.

**Complexity:** calls x cost -> distinct x cost + calls x O(1).

**Sketch:**

```python
import functools

@functools.lru_cache(maxsize=4096)      # bounded: at most 4096 distinct texts are kept
def parse_config(text):                 # pure in its argument; _parse returns a tuple, so
    return _parse(text)                 # callers cannot mutate the cached value

def parse_config_file(path):            # key on content, not path: the text is the key, so
    with open(path, "rb") as f:         # an edited file is a new entry, never a stale one
        return parse_config(f.read().decode())
```

**Oracle:** Run cached and uncached versions on the same call sequence and compare outputs;
include a caller that mutates what it receives, then calls again.

**Traps:**
- Returning a mutable cached object (list, dict): one caller's mutation corrupts every later
  call. Return immutable values (tuples, frozen dataclasses) or `copy.deepcopy` on the way out.
- Mutable arguments (lists, dicts): reduce to tuples or a content hash.
- Methods: the cache holds `self` alive; cache a module-level function instead.
- Unbounded memory: set `maxsize`; a plain dict as the cache grows without limit.
- Functions that read the clock or the environment have no content to key on: pass the value
  in as an argument, or this card fails.

**Not when:**
- Distinct inputs are nearly as many as calls: no reuse.
- The function is cheap.

## Content-hash cache across runs

**Problem shape:** A build or data step reruns for all inputs although few changed.

**Precondition test:** The step's inputs can be enumerated (files, tool version, flags, the
environment variables it reads) and the step is deterministic given them. If it cannot be
made deterministic, this card fails.

**Complexity:** cost(all) every run -> cost(changed) + hashing.

**Sketch:**

```python
import hashlib, json, os

def item_key(content, tool_version, flags, env):   # env: {name: value} the step reads
    h = hashlib.sha256(content)
    h.update(json.dumps([tool_version, flags, env], sort_keys=True).encode())
    return h.hexdigest()

def process_all(items, manifest_path, compute, tool_version, flags, env):
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
    out, hits = {}, 0
    for name, content in sorted(items.items()):
        key = item_key(content, tool_version, flags, env)
        if manifest.get(name, {}).get("key") == key:
            out[name], hits = manifest[name]["result"], hits + 1
            continue
        out[name] = compute(content)                     # a raise stores nothing
        manifest[name] = {"key": key, "result": out[name]}
    tmp = manifest_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, sort_keys=True)
    os.replace(tmp, manifest_path)                       # atomic: a crash leaves the old manifest
    return out, hits
```

The CI-step version (whole output directories under `cache/<key>`, renamed into place) is in
`cards-ci-tests.md`.

**Oracle:** Periodic full recompute with the cache disabled, compared to the cached outputs;
report the hit rate.

**Traps:**
- mtime keys: wrong across checkouts and in CI.
- Hidden inputs: the tool's own version, environment, locale.
- Caching failures: store only on success.
- Non-atomic writes: a crash mid-`json.dump` corrupts the manifest and every later run starts
  cold or, worse, reads a truncated file. Write to a temporary name and `os.replace`.
- Forgetting dependents: when results depend on other items, use the next card.

**Not when:**
- The step is nondeterministic and cannot be fixed.
- The step is cheap.

## Incremental recompute via dependents

**Problem shape:** Outputs depend on other items (module A's result depends on B's), so
caching per item is not enough: when B changes, A must recompute too.

**Precondition test:** A complete dependency graph among items, with the same completeness
test as `cards-graphs-impact.md` (name the edge kinds the graph misses; fail closed on items
that use them).

**Complexity:** recompute all -> recompute the reverse-BFS impact set of the changed items.

**Sketch:**

```python
def incremental(items, deps, manifest, compute, key_of):
    def stale(n):                                         # content or dependency list changed
        m = manifest.get(n)
        return m is None or m["key"] != key_of(items[n]) or m["deps"] != sorted(deps.get(n, ()))
    changed = {n for n in items if stale(n)}
    deleted = {n for n in manifest if n not in items}
    rev = reverse_graph(deps)                             # cards-graphs-impact.md
    affected = impact_set(rev, changed | deleted) & set(items)
    for n in deleted:
        del manifest[n]                                   # removed items leave the manifest
    for n in topo_order({n: deps.get(n, ()) for n in items}):
        if n in affected:
            manifest[n] = {"key": key_of(items[n]), "deps": sorted(deps.get(n, ())),
                           "result": compute(n, {d: manifest[d]["result"] for d in deps.get(n, ())})}
    return manifest, sorted(affected)
```

New items have no manifest entry, so they count as changed. Deleted items are dropped from
the manifest and their old dependents are affected: one that still names the deleted item is
reached through `rev`; one whose edge the graph builder dropped (builders often skip edges to
missing files) has a different dependency list from the one in the manifest, so it is stale.
Recompute in topological order so each item sees fresh results from its dependencies.

**Oracle:** Full recompute compared to incremental on random change sets over a small graph,
including deletions and additions.

**Traps:**
- An incomplete graph gives stale outputs silently: schedule a periodic full rebuild as the
  oracle.
- Recomputing in the wrong order uses stale dependency results.
- Leaving deleted items in the manifest: their stale results get served to dependents.

**Not when:**
- Items are independent: the previous card suffices.

## Index once, query many

**Problem shape:** Each query scans the full data: linear search, or re-reading a file per
lookup.

**Precondition test:** Measured queries per rebuild x per-query saving > build cost; the data
changes less often than it is queried. Fewer than about 10 queries per rebuild fails.

**Complexity:** O(n) per query -> O(log n) (sorted array + bisect) or O(1) (dict) per query,
plus an O(n) or O(n log n) build.

**Sketch:**

```python
import bisect, hashlib

class RangeIndex:                       # answers "which interval contains x"
    def __init__(self, intervals):      # [(start, end, tag)], non-overlapping
        self.rows = sorted(intervals)
        self.starts = [s for s, _, _ in self.rows]
        self.version = hashlib.sha256(repr(self.rows).encode()).hexdigest()

    def lookup(self, x):
        i = bisect.bisect_right(self.starts, x) - 1
        if i >= 0 and self.rows[i][0] <= x < self.rows[i][1]:
            return self.rows[i][2]
        return None
```

Keep the index alive across calls (module level or on the long-lived object) and invalidate
by content hash when the data changes.

**Oracle:** Linear scan per query on random data; compare answers.

**Traps:**
- Rebuilding the index per query (keep it alive across calls).
- Invalidation: key the index by a content hash of its source, not by time.

**Not when:**
- Fewer than about 10 queries per rebuild.
- The data changes on every query.

## Coalesce identical requests

**Problem shape:** N identical fetches or computations issued concurrently or within a short
window (same URL, same key).

**Precondition test:** Requests are identical by key and the result can be shared within the
window. If results are user-specific, the user must be part of the key.

**Complexity:** N x cost -> 1 x cost + N x O(1).

**Sketch:**

```python
import threading

class Coalescer:
    def __init__(self, fetch):
        self.fetch, self.inflight, self.lock = fetch, {}, threading.Lock()

    def get(self, key):
        with self.lock:
            ev = self.inflight.get(key)
            if ev is None:
                ev = self.inflight[key] = {"done": threading.Event()}
                leader = True
            else:
                leader = False
        if leader:
            try:
                ev["result"] = self.fetch(key)
            except Exception as e:
                ev["error"] = e
            finally:
                with self.lock:
                    del self.inflight[key]      # failures are not kept past the window
                ev["done"].set()
        ev["done"].wait()
        if "error" in ev:
            raise ev["error"]
        return ev["result"]
```

**Oracle:** Count backend calls under a test harness with repeated keys; one call per
distinct key per window.

**Traps:**
- Sharing a failed result: do not cache failures past the in-flight window.
- A key that omits a parameter which changes the result.
- Sharing a mutable result object between the waiting callers (same trap as Memoise).

**Not when:**
- Results are user-specific and the key cannot include the user.

See also: the reverse BFS and topological order used above are in `cards-graphs-impact.md`.

## Memoised recursion and dynamic programming

**Problem shape:** A recursive function whose run time explodes with the input: naive
Fibonacci, counting paths or ways, edit distance, 0/1 knapsack, "best split" recursions.
Pairwise slopes from the doubling test keep rising instead of settling on an exponent.

**Precondition test:** The recursion is pure and its result depends only on a small state
(its arguments, or indices into fixed inputs), and the same states recur: count calls
against distinct argument tuples on a small input; calls >= 10x distinct passes. The number
of distinct states must fit in memory. If states are subsets of n items (2^n of them), this
card fails beyond n of about 20: use a capped search or another card.

**Complexity:** exponential -> O(states x transitions per state).

**Sketch:**

```python
import functools

def count_ways_memo(n, steps):                # top-down: the naive recursion plus a cache
    steps = tuple(sorted(set(steps)))           # defined inside the call, keyed on k

    @functools.lru_cache(maxsize=None)
    def ways(k):
        if k == 0:
            return 1
        return sum(ways(k - s) for s in steps if 0 < s <= k)

    return ways(n)                              # recursion depth grows with n

def count_ways(n, steps):                     # bottom-up: the same table, no depth limit
    steps = sorted(set(s for s in steps if s > 0))
    table = [1] + [0] * n
    for k in range(1, n + 1):
        table[k] = sum(table[k - s] for s in steps if s <= k)
    return table[n]

def edit_distance(a, b):                      # bottom-up: two rows, no recursion depth
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]
```

**Oracle:** The naive recursion on small inputs (n <= 20, strings up to 7 characters, random
step sets), compared exactly.

**Traps:**
- Recursion depth: a top-down memo deeper than Python's limit (1,000 frames by default)
  raises RecursionError; `count_ways_memo(5000, [1])` does. Fill the table bottom-up in
  dependency order instead of raising the limit.
- A cache defined at module level and keyed on part of the input (k but not steps) serves
  stale answers when the rest changes: define it inside the call, or key on every input.
- Unhashable arguments (lists): key on a tuple or on indices into the fixed input.
- Floats summed in a different order top-down and bottom-up: compare with a tolerance, or
  use integers.

**Not when:**
- Subproblems do not overlap (merge sort, binary search): memoising only adds overhead.
- The function is not pure (reads files, the clock, or globals that change).
