# Graph and change-impact cards

Covers "what depends on this", change impact, reachability, cycles, and build order. A
dependency graph is only as good as its completeness: when it cannot be made complete, the
consumer must fail closed (treat unknown nodes as affected). If the time is actually spent
fetching or rebuilding the graph rather than traversing it, go to `cards-repeated-work.md`. If
you need a schedule across workers rather than an order, go to `cards-scheduling.md`.

## Reverse-graph impact set

**Problem shape:** "Which services, tests or modules are affected if X changes", currently
answered by recomputing everything, or by a forward DFS from every node.

**Precondition test:** You can enumerate all edges from a source of truth (imports, build
files, dependency manifests), and you can list which edge kinds that source misses (dynamic
imports, reflection, string-named plugins, generated code). If the missing kinds cannot be
bounded, the consumer must fail closed on nodes that use them; if it cannot fail closed, this
card fails.

**Complexity:** O(V*(V+E)) or worse -> O(V+E) per query (BFS on the reversed graph), V nodes,
E edges.

**Sketch:**

```python
from collections import deque

def reverse_graph(deps):              # deps: node -> iterable of nodes it depends on
    rev = {}
    for node, ds in deps.items():
        rev.setdefault(node, [])
        for d in ds:
            rev.setdefault(d, []).append(node)
    return rev

def impact_set(rev, changed):
    seen, q = set(changed), deque(changed)
    while q:
        u = q.popleft()
        for v in rev.get(u, ()):
            if v not in seen:
                seen.add(v)
                q.append(v)
    return seen                       # includes the changed nodes themselves
```

**Oracle:** Transitive closure by repeated DFS (or Floyd-Warshall) on random graphs with
V <= 30, including cycles and self-loops; compare impact sets for every start node.

**Traps:**
- Stale graph: rebuild from source each run, or key the cache by the content hash of the
  sources.
- Renames and deletes: treat a deleted node as changed, and its old dependents as affected.
- Nodes absent from the graph: fail closed (report everything as affected, and say why).
- Cycles are fine for BFS, but a graph builder that recurses may not terminate on them.

**Not when:**
- The graph cannot be made complete and the consumer cannot fail closed.
- V is tiny and the time is spent fetching the graph: cache the fetch
  (`cards-repeated-work.md`).

## SCC condensation

**Problem shape:** Many reachability queries on a graph with cycles; or you need "groups that
depend on each other" for release ordering.

**Precondition test:** Cycles exist (a plain topological sort fails) and queries are many. If
the graph is a DAG, skip this card. If there is one query, use Reverse-graph impact set.

**Complexity:** O(V+E) once (Tarjan or Kosaraju), then every query runs on a smaller DAG.

**Sketch:**

```python
def kosaraju(adj):                    # adj: node -> list of successors; returns node -> comp id
    nodes = sorted(adj)
    order, seen = [], set()
    for s in nodes:                   # pass 1: finishing order, iterative
        if s in seen:
            continue
        stack = [(s, iter(adj.get(s, ())))]
        seen.add(s)
        while stack:
            u, it = stack[-1]
            for v in it:
                if v not in seen:
                    seen.add(v)
                    stack.append((v, iter(adj.get(v, ()))))
                    break
            else:
                stack.pop()
                order.append(u)
    rev = {u: [] for u in nodes}
    for u in nodes:
        for v in adj.get(u, ()):
            rev.setdefault(v, []).append(u)
    comp, cid = {}, 0
    for s in reversed(order):         # pass 2: DFS on the reversed graph
        if s in comp:
            continue
        stack = [s]
        comp[s] = cid
        while stack:
            u = stack.pop()
            for v in rev.get(u, ()):
                if v not in comp:
                    comp[v] = cid
                    stack.append(v)
        cid += 1
    return comp
```

Then build the condensation DAG: `cond_edges = {(comp[u], comp[v]) for u in adj for v in
adj[u] if comp[u] != comp[v]}` (a set, so edges are deduplicated). For deterministic component
ids, renumber by the smallest member of each component.

**Oracle:** Brute-force "u reaches v and v reaches u" on V <= 25 graphs; the equivalence
classes must equal the SCCs.

**Traps:**
- Recursive Tarjan hits Python's recursion limit near 1000 deep; use an iterative version.
- Forgetting to deduplicate condensation edges.
- Component ids must be assigned deterministically (sort by smallest member) or every run
  produces a different plan.

**Not when:**
- The graph is a DAG: Topological order and cycle detection.
- One-off query: Reverse-graph impact set.

## Bitset closure for many queries

**Problem shape:** Thousands of "does a reach b" queries per run on a DAG (or on a
condensation).

**Precondition test:** V <= about 20,000 (memory is V^2/8 bytes: 20k nodes is 50 MB) and the
number of queries per rebuild is far larger than V. If either fails, use per-query BFS.

**Complexity:** O(V*E/64) word operations to build. Per query: O(V/64) in CPython with int
bitsets (the shift allocates a V-bit int), O(1) with a fixed-width row (a `bytes` row per
node, as in the sketch, or numpy if the platform allows it).

**Sketch:**

```python
def reach_bitsets(succ, topo):        # topo: topological order, sources first
    idx = {u: i for i, u in enumerate(topo)}
    reach = [0] * len(topo)
    for u in reversed(topo):          # sinks first, so successors are done
        bits = 1 << idx[u]
        for v in succ.get(u, ()):
            bits |= reach[idx[v]]
        reach[idx[u]] = bits
    return idx, reach

def freeze(reach):                    # once after the build: fixed-width rows for O(1) tests
    nbytes = (len(reach) + 7) // 8
    return [r.to_bytes(nbytes, "little") for r in reach]

def reaches(idx, rows, a, b):         # rows from freeze(); one byte read, no allocation
    j = idx[b]
    return (rows[idx[a]][j >> 3] >> (j & 7)) & 1 == 1
```

**Oracle:** Per-query BFS on random DAGs with V <= 60; compare every (a, b) pair.

**Traps:**
- Memory at V = 50k is 312 MB.
- Rebuilding on every change defeats it: rebuild only when the graph changes.
- Python int shifts and ORs are O(V/64) each, so the build is slow in CPython above about
  20k nodes, and `(reach[a] >> b) & 1` per query is O(V/64) too: freeze the rows to bytes
  after the build.

**Not when:**
- Few queries: per-query BFS.
- V above the memory bound: per-query BFS, or condense first and check the condensation size.

## Topological order and cycle detection

**Problem shape:** Build order, migration order, "run these in dependency order", and "is
there a cycle".

**Precondition test:** Edges are known, and you need an order, not a schedule across workers
(for that see `cards-scheduling.md`).

**Complexity:** O(V+E) with Kahn's algorithm (plus a log factor for the sorted ready set).

**Sketch:**

```python
import heapq

def topo_order(deps):                 # deps: node -> nodes it depends on
    nodes = set(deps)
    for ds in deps.values():
        nodes.update(ds)
    indeg = {u: 0 for u in nodes}
    users = {u: [] for u in nodes}
    for u, ds in deps.items():
        for d in ds:
            indeg[u] += 1
            users[d].append(u)
    ready = [u for u in nodes if indeg[u] == 0]
    heapq.heapify(ready)              # smallest name first: deterministic
    out = []
    while ready:
        u = heapq.heappop(ready)
        out.append(u)
        for w in sorted(users[u]):
            indeg[w] -= 1
            if indeg[w] == 0:
                heapq.heappush(ready, w)
    if len(out) != len(nodes):
        left = sorted(u for u in nodes if indeg[u] > 0)
        raise ValueError("cycle among: %s" % left)
    return out
```

To name one cycle, walk from any leftover node along its unfinished dependencies until a node
repeats.

**Oracle:** Check that the output order satisfies every edge, on random DAGs with V <= 40;
inject one back edge and assert the cycle is reported.

**Traps:**
- Nondeterministic order from set iteration: always sort the ready set.
- Reporting "a cycle exists" without naming it.

**Not when:**
- You need makespan on m workers: Critical-path list scheduling in `cards-scheduling.md`.

## Union-find for grouping

**Problem shape:** "Merge records that are linked", connected components, "which tests share
a fixture group"; currently done by repeated list merging.

**Precondition test:** The relation is symmetric (undirected). For directed reachability use
the cards above.

**Complexity:** O(n^2) list merging -> near O(n) with path halving and union by size.

**Sketch:**

```python
class DSU:
    def __init__(self, n):
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]   # path halving
            x = self.parent[x]
        return x

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b:
            return
        if self.size[a] < self.size[b]:
            a, b = b, a
        self.parent[b] = a
        self.size[a] += self.size[b]
```

**Oracle:** BFS components on random undirected graphs with n <= 50; compare the partition.

**Traps:**
- Forgetting union by size degenerates to O(n) per find on adversarial input.
- Using it for directed edges gives wrong answers silently.
- Component labels depend on union order; for output, relabel by smallest member.

**Not when:**
- Edges are directed.

See also: scheduling the resulting order across workers is `cards-scheduling.md`; caching the
graph build is `cards-repeated-work.md`.
