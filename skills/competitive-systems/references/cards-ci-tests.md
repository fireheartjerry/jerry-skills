# CI and test-selection cards

Covers running fewer tests safely, skipping unchanged steps, and choosing optional checks
under a time budget. Splitting the selected set across runners is in `cards-scheduling.md`.
Every card here keeps the safety fallbacks from the glossary: direct mappings and the
sensitive-file fallback are mandatory, and selection fails closed when it cannot prove a
test is unaffected. Gate 1 evidence for this file is the selection ratio (tests a correct
selector would run, over all tests) estimated on the last 20 or so merged diffs, not a
doubling test.

## Impact-based test selection

Repo tool: `dev.py affected --base main --explain` implements this card, failing closed, for
Python, TypeScript/JavaScript, Go and Rust, and `dev.py affected --history 20` gives the
selection ratio over recent commits; the sketch is for other stacks.

**Problem shape:** Every PR runs the full suite; the user asks "which tests do we actually
need to run".

**Precondition test:** (1) A dependency graph exists from test files to source files
(imports, build targets, or an explicit mapping file) and you can produce it from the
checkout with no network. (2) You can write down the sensitive-file list for this repo:
lockfiles, build and CI config, dependency manifests, shared fixtures and conftest-style
files, code generators and their templates, environment files. (3) You can write down the
ignore list: paths whose changes affect no test (docs, `*.md`, licence files, images under a
docs tree); anything not on either list and not in the graph fails closed. (4) A full run
happens on some schedule (nightly or on main) so escapes can be detected. If any of the four
fails, this card fails.

**Complexity:** Full suite every PR -> O(V+E) selection plus the selected subset.

**Sketch:**

```python
def select_tests(base, head, graph, tests, is_sensitive, is_ignored):
    diff = git_diff_names(base, head)                 # renames: both old and new paths
    if any(is_sensitive(p) for p in diff):            # before ignore: a path on both lists
        return sorted(tests)                          # is sensitive (fail closed)
    changed = [p for p in diff if not is_ignored(p)]
    rev = reverse_graph(graph)                        # every node: as a key or as a dependency
    if any(p not in rev for p in changed):
        return sorted(tests)                          # fail closed: unmapped or deleted file
    affected = impact_set(rev, changed)
    selected = {t for t in tests if t in affected}
    selected |= {t for t in tests if t in changed}    # new or edited test files always run
    return sorted(selected)
```

`impact_set` and `reverse_graph` are the reverse BFS from `cards-graphs-impact.md`. The
membership check is against `rev`, which holds every node, not only files that have
dependencies of their own: a changed leaf source file that appears only as a dependency is
mapped. The graph is built from the head checkout, so a deleted file is absent from it and
fails closed (full run); that is deliberate. To select on deletions, also build the graph at
`base` and treat the deleted node's old dependents as affected.

**Oracle:** On the last k merged commits, compute the selection and compare it to the
full-run results: any test that failed in the full run but was not selected is an escape.
Report escapes (target 0) and the selection ratio (selected / total).
`dev.py affected --history 20` replays the selection over the last 20 first-parent commits
and prints each commit's selected/total, the median and 90th-percentile ratio and the share
that fell back to a full run; compare its per-commit targets with the failures in your CI
history for the escape count. Gate 1 passes when the median ratio is at most 0.5 and under a
third of commits fall back to full. Also property-test the reverse BFS as in
`cards-graphs-impact.md`.

**Traps:**
- Tests that read data files or fixtures by path: map those paths explicitly, or make the
  directory sensitive.
- New or renamed test files must always run.
- Environment and flag changes are not files; they need a manual full-run trigger.
- Dynamic imports, plugins loaded by name, and generated code are invisible to an import
  graph: list them and fail closed on the modules that use them.
- A stale graph from a previous checkout: rebuild per run, or key it by content hash.
- Checking `p in graph` against the graph's keys only: a changed leaf file that is only ever
  a dependency value forces a full run for no reason.
- An empty ignore list: docs-only PRs run the full suite, which hides the gain.
- A path on both the sensitive and the ignore list is sensitive: check sensitive first.
- An ignore glob such as `*.md` that also matches golden files or snapshots under a test
  directory: `dev.py affected` never ignores a path under a test, fixture, testdata or
  snapshot directory.

**Not when:**
- The graph cannot be produced from the checkout.
- The suite is under about 5 minutes: overhead and risk exceed the gain.
- No scheduled full run exists to catch escapes.
- The build or test tool already selects affected tests (Nx `affected`, Turborepo
  `--filter=...[origin/main]`, Bazel `rdeps` queries, `jest --changedSince`,
  pytest-testmon): use it and measure its selection ratio instead of building a second
  selector.

## Content-hash cache (skip unchanged steps)

**Problem shape:** A step (build, lint, codegen, embedding, image build) reruns on unchanged
inputs every time.

**Precondition test:** The step is deterministic given an enumerable input set: files, tool
version, flags, and the environment variables it reads. If you cannot list the inputs, the
card fails. The general pipeline version of this card, with per-item manifests and
dependents, is `cards-repeated-work.md`.

**Complexity:** cost(step) every run -> cost(hash inputs) on a hit.

**Sketch:**

```python
import hashlib, json, os, shutil, subprocess

def step_key(paths, tool_version, flags, env_names):
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            h.update(p.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    env = {k: os.environ.get(k, "") for k in sorted(env_names)}   # only what the step reads
    h.update(json.dumps([tool_version, flags, env], sort_keys=True).encode())
    return h.hexdigest()

def run_cached(key, cache_dir, out_dir, cmd):
    hit = os.path.join(cache_dir, key)
    if os.path.isdir(hit):
        shutil.copytree(hit, out_dir, dirs_exist_ok=True)
        return "hit"
    subprocess.run(cmd, check=True)             # a failure raises: nothing is stored
    tmp = "%s.tmp-%d" % (hit, os.getpid())
    shutil.copytree(out_dir, tmp)
    try:
        os.replace(tmp, hit)                    # atomic: a partial copy never becomes a hit
    except OSError:
        shutil.rmtree(tmp)                      # another run stored the same key first
    return "miss"
```

**Oracle:** Run with the cache disabled on a sample of commits and compare outputs byte for
byte (or through a normalising comparator when timestamps are embedded).

**Traps:**
- mtime-based keys are wrong across checkouts and in CI.
- Hidden inputs: clock, network, environment, the tool's own version.
- Caching failures: store only on success.
- Non-atomic stores: an interrupted copy under the final name is a corrupt hit forever.
  Write to a temporary name and rename into place.
- Unbounded cache growth: evict by age or size.

**Not when:**
- The step is already a small share of wall time.
- The step is not deterministic and cannot be made so.

## Coverage under a time budget

Repo tool: `dev.py smart --budget SECONDS` (affected targets and `always_run` required, then
this card's knapsack over recently failed targets, from recorded durations).

**Problem shape:** "We have B minutes; which optional suites should run", with a value per
suite (failure rate, coverage of changed code) and a measured duration.

**Precondition test:** Mandatory items (direct mappings, the sensitive-file fallback) are
identified, excluded from the choice, and their duration is subtracted from B first.
Durations are measured (p50 of the last k runs), not guessed. Decide whether value is
additive (each suite's value independent) or overlapping (suites cover the same code, so
value is submodular). If durations are guesses, the card fails: go measure.

**Complexity:** Three regimes; pick by the shape of the value function and n.
1. Additive value, integer costs: 0/1 knapsack DP, O(n*B) with B in seconds. n = 2000 suites
   and B = 2400 s is about 5M cells, well under a second. Exact.
2. Overlapping coverage (monotone submodular value): greedy by marginal gain per second
   until the budget is full, then return the better of that set and the best single feasible
   item. The guarantee is (1 - 1/e)/2, about 0.32, of optimal (Leskovec et al. 2007). Ratio
   greedy alone has no constant guarantee under a knapsack constraint: with items (x: value
   2, cost 1) and (y: value 10, cost 10) and B = 10 it picks x for 2 against an optimum of
   10. The full (1 - 1/e) needs partial enumeration over all triples first (Sviridenko
   2004), which costs O(n^3) greedy runs and is rarely worth it in CI. O(n^2) value calls
   naive, O(n log n) with lazy evaluation.
3. Exact search over subsets: only when n <= 18 optional candidates (2^18 = 262,144 subsets,
   about 5e5 nodes, inside the 1e6 node budget below); the cutoff is a node budget, never a
   wall clock, so the chosen set is the same on every machine. On a budget hit fall back to
   regime 2. Regime 1 covers the common exact case, so regime 3 is rarely needed.

**Sketch:**

```python
EXACT_SEARCH_MAX = 18        # candidates; the largest n whose 2^(n+1) nodes fit NODE_BUDGET
NODE_BUDGET = 1_000_000      # deterministic cutoff for exact search

def knapsack(items, B):                     # items: [(name, value, int_seconds)], additive value
    items = sorted(items)                   # by name: fixed tie-break
    best = [0.0] * (B + 1)
    choice = [[False] * (B + 1) for _ in items]
    for i, (_, v, c) in enumerate(items):
        for b in range(B, c - 1, -1):
            if best[b - c] + v > best[b]:
                best[b] = best[b - c] + v
                choice[i][b] = True
    chosen, b = [], B
    for i in range(len(items) - 1, -1, -1):
        if choice[i][b]:
            chosen.append(items[i][0])
            b -= items[i][2]
    return sorted(chosen), best[B]

def greedy_submodular(items, value, B):     # value(list of items) -> float, monotone submodular
    chosen, spent, cur, remaining = [], 0, value([]), sorted(items)
    while True:
        scored = [((value(chosen + [it]) - cur) / it[2], it)
                  for it in remaining if spent + it[2] <= B]
        if not scored:
            break
        _, pick = max(scored, key=lambda s: (s[0], s[1][0]))   # ties: name
        chosen.append(pick)
        spent += pick[2]
        remaining.remove(pick)
        cur = value(chosen)
    single = min((it for it in items if it[2] <= B),
                 key=lambda it: (-value([it]), it[0]), default=None)
    if single is not None and value([single]) > cur:
        return [single]                     # better of ratio greedy and best single item
    return chosen

def exact_best(items, value, B):            # items sorted by name; (chosen, value) or None
    best, nodes, stack = ([], value([])), 0, [(0, [], 0)]
    while stack:                            # iterative DFS, include then exclude
        i, chosen, spent = stack.pop()
        nodes += 1
        if nodes > NODE_BUDGET:
            return None                     # budget hit: caller uses greedy_submodular
        if i == len(items):
            v = value(chosen)
            names = [c[0] for c in chosen]
            if v > best[1] or (v == best[1] and names < [c[0] for c in best[0]]):
                best = (chosen, v)
            continue
        if spent + items[i][2] <= B:
            stack.append((i + 1, chosen + [items[i]], spent + items[i][2]))
        stack.append((i + 1, chosen, spent))
    return best
```

**Oracle:** Brute force over all subsets for n <= 12 for both regimes; assert the DP matches
the optimum exactly and `greedy_submodular` returns at least 0.31 x the optimum (the (1 -
1/e)/2 bound, on random monotone submodular instances built from coverage sets). Plus a
timing test for `exact_best` at n = EXACT_SEARCH_MAX with B >= the sum of all costs (no
pruning, the worst case) asserting it finishes under 1 s, and the same at n =
EXACT_SEARCH_MAX + 1 asserting it returns None; if the 1 s assertion fails on the CI box,
lower the constant, do not add a wall clock.

**Traps:**
- Subtract the mandatory set's duration from B first; if the mandatory set alone exceeds B,
  report that and stop. Never drop mandatory items.
- Ties broken by a fixed order (name), or the plan changes between runs.
- Durations drift: refresh p50 each run.
- A growing candidate list silently crossing the exact-search cutoff: the cutoff must be a
  constant in code with a test at the boundary.
- A wall-clock cutoff in exact search: the plan then depends on machine load. Use the node
  budget.
- Quoting (1 - 1/e) for ratio greedy under a knapsack constraint: that bound is for equal
  costs (cardinality) only.
- Fractional seconds in the DP: round costs up to whole seconds.

**Not when:**
- There is no real budget: just run everything.
- Value cannot be estimated: select by impact only (first card).

See also: splitting the selected set across runners is `cards-scheduling.md`.
