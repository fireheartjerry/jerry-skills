# Repeated-work cards

Covers the same computation done many times for the same inputs, within a run or across
runs. Signal: the profile shows a function's call count far above its number of distinct
inputs, or a step whose inputs rarely change reruns every time. Gate 1 evidence for this file
is that ratio (calls over distinct inputs, or reruns over changed inputs), not a doubling
test. If the work is not repeated but genuinely superlinear, go back to the symptom index; if
the repeated step is a graph build, the traversal cards are in `cards-graphs-impact.md`.

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
import functools, hashlib

@functools.lru_cache(maxsize=4096)
def parse_config(text):                 # pure in its argument; returns a tuple, so callers
    return _parse(text)                 # cannot mutate the cached value

_by_content = {}

def parse_config_file(path):            # key on content, not path
    with open(path, "rb") as f:
        data = f.read()
    key = hashlib.sha256(data).hexdigest()
    if key not in _by_content:
        _by_content[key] = _parse(data.decode())
    return _by_content[key]
```

**Oracle:** Run cached and uncached versions on the same call sequence and compare outputs;
include a caller that mutates what it receives, then calls again.

**Traps:**
- Returning a mutable cached object (list, dict): one caller's mutation corrupts every later
  call. Return immutable values (tuples, frozen dataclasses) or `copy.deepcopy` on the way out.
- Mutable arguments (lists, dicts): reduce to tuples or a content hash.
- Methods: the cache holds `self` alive; cache a module-level function instead.
- Unbounded memory: set `maxsize`.
- Functions that read time, env or files are not pure: key on the content.

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
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
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
