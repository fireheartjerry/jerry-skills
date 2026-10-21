"""Every fenced python block in every card, executed and checked against an oracle.

Each block is exec'd in a namespace that provides `Chunk` (and, where a card says the code
comes from another card, those functions). Each top-level def or class is registered with
the test that checks it; `test_registry_covers_every_sketch` fails, naming the function,
when a card gains a sketch without an oracle here. Standard library only; no network;
seeded randomness; no wall-clock assertions.
"""
import ast
import glob
import hashlib
import itertools
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque, namedtuple
from operator import itemgetter

HERE = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(os.path.dirname(HERE), "references")
CARD_FILES = sorted(os.path.basename(p) for p in glob.glob(os.path.join(REFS, "cards-*.md")))

Chunk = namedtuple("Chunk", "path start end score")


def card_sections(fname):
    """[(heading, text)] for every `## ` heading outside code fences."""
    with open(os.path.join(REFS, fname), encoding="utf-8") as f:
        lines = f.read().splitlines()
    out, current, fence = [], None, False
    for line in lines:
        if line.startswith("```"):
            fence = not fence
        if not fence and line.startswith("## "):
            current = [line[3:].strip(), []]
            out.append(current)
            continue
        if current is not None:
            current[1].append(line)
    return [(h, "\n".join(body)) for h, body in out]


def card_blocks(fname, heading):
    """Python fenced blocks inside one `## heading` card."""
    for h, text in card_sections(fname):
        if h == heading:
            return re.findall(r"```python\n(.*?)```", text, re.S)
    raise KeyError("%s has no heading %r" % (fname, heading))


def all_blocks():
    for fname in CARD_FILES:
        for heading, text in card_sections(fname):
            for block in re.findall(r"```python\n(.*?)```", text, re.S):
                yield fname, heading, block


def top_level_names(src):
    return [n.name for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))]


def load(fname, heading, **extra):
    ns = {"Chunk": Chunk, "__name__": "card_sketch"}
    ns.update(extra)
    for block in card_blocks(fname, heading):
        exec(compile(block, "%s: %s" % (fname, heading), "exec"), ns)
    return ns


def closure(nodes, deps):
    """reach[u] = every node u depends on, transitively (Warshall over sets)."""
    reach = dict((u, set(deps.get(u, ()))) for u in nodes)
    for k in nodes:
        for i in nodes:
            if k in reach[i]:
                reach[i] |= reach[k]
    return reach


def bfs_from(succ, a):
    seen, q = {a}, deque([a])
    while q:
        u = q.popleft()
        for v in succ.get(u, ()):
            if v not in seen:
                seen.add(v)
                q.append(v)
    return seen


# Every top-level def/class in a card sketch -> the test that checks it.
REGISTRY = {
    "dedup_keep_first": "LookupDedupTests.test_dedup_keep_first_and_key",
    "key": "LookupDedupTests.test_dedup_keep_first_and_key",
    "join": "LookupDedupTests.test_join_vs_nested_loop",
    "dedup_sorted": "LookupDedupTests.test_dedup_sorted_vs_brute",
    "merge_ranges": "LookupDedupTests.test_merge_ranges_vs_nested_loop "
                    "and LlmContextTests.test_merge_and_subtract_ranges_vs_line_sets",
    "window_sums": "LookupDedupTests.test_windows_vs_recompute",
    "window_max": "LookupDedupTests.test_windows_vs_recompute",
    "window_distinct": "LookupDedupTests.test_windows_vs_recompute",
    "reverse_graph": "GraphTests.test_impact_set_vs_floyd_warshall",
    "impact_set": "GraphTests.test_impact_set_vs_floyd_warshall",
    "kosaraju": "GraphTests.test_kosaraju_vs_mutual_reachability",
    "reach_bitsets": "GraphTests.test_bitset_closure_vs_bfs",
    "freeze": "GraphTests.test_bitset_closure_vs_bfs",
    "reaches": "GraphTests.test_bitset_closure_vs_bfs",
    "topo_order": "GraphTests.test_topo_order_edges_and_cycle",
    "DSU": "GraphTests.test_dsu_vs_bfs_components",
    "select_tests": "CiTests.test_select_tests_fails_closed",
    "step_key": "CiTests.test_step_key_determinism",
    "run_cached": "CiTests.test_run_cached_hit_miss_failure",
    "knapsack": "CiTests.test_knapsack_vs_brute_force",
    "greedy_submodular": "CiTests.test_greedy_submodular_bound_and_counterexample",
    "exact_best": "CiTests.test_exact_best_optimum_and_node_budget",
    "lpt": "SchedulingTests.test_lpt_against_brute_force",
    "lower_bound": "SchedulingTests.test_pinned_jobs_share_worker_zero",
    "critical_path": "SchedulingTests.test_critical_path_vs_all_paths",
    "fewest_workers": "SchedulingTests.test_fewest_workers_vs_brute_minimum",
    "build": "MultiPatternTests.test_aho_corasick_vs_str_find",
    "scan": "MultiPatternTests.test_aho_corasick_vs_str_find",
    "required_literal": "MultiPatternTests.test_required_literal",
    "names_in": "LlmContextTests.test_names_in_and_exact_hits",
    "exact_hits": "LlmContextTests.test_names_in_and_exact_hits",
    "terms": "LlmContextTests.test_bm25_vs_direct_scoring",
    "BM25": "LlmContextTests.test_bm25_vs_direct_scoring",
    "expand": "LlmContextTests.test_expand_vs_truncated_bfs",
    "shorten": "LlmContextTests.test_pack_properties (stub replaced by a test double)",
    "subtract_ranges": "LlmContextTests.test_merge_and_subtract_ranges_vs_line_sets",
    "pack": "LlmContextTests.test_pack_properties",
    "parse_config": "RepeatedWorkTests.test_memoise",
    "parse_config_file": "RepeatedWorkTests.test_memoise",
    "item_key": "RepeatedWorkTests.test_process_all_vs_full_recompute",
    "process_all": "RepeatedWorkTests.test_process_all_vs_full_recompute",
    "incremental": "RepeatedWorkTests.test_incremental_vs_full_recompute",
    "RangeIndex": "RepeatedWorkTests.test_range_index_vs_linear_scan",
    "Coalescer": "RepeatedWorkTests.test_coalescer_one_fetch_per_key",
}


class RegistryTests(unittest.TestCase):
    def test_registry_covers_every_sketch(self):
        found = {}
        for fname, heading, block in all_blocks():
            for name in top_level_names(block):
                found.setdefault(name, "%s / %s" % (fname, heading))
        missing = sorted(set(found) - set(REGISTRY))
        extra = sorted(set(REGISTRY) - set(found))
        self.assertFalse(missing, "sketches without a registered oracle: %s" % ", ".join(
            "%s (%s)" % (n, found[n]) for n in missing))
        self.assertFalse(extra, "registered names no card defines any more: %s" % ", ".join(extra))

    def test_registered_tests_exist(self):
        mod = sys.modules[__name__]
        for name, ref in REGISTRY.items():
            for part in ref.split(" and "):
                cls, meth = part.split(" ")[0].split(".")
                self.assertTrue(hasattr(getattr(mod, cls), meth), "%s -> %s" % (name, part))

    def test_every_block_executes(self):
        for fname, heading, block in all_blocks():
            ns = {"Chunk": Chunk}
            exec(compile(block, "%s: %s" % (fname, heading), "exec"), ns)


# ---------------------------------------------------------------- lookup and dedup

Event = namedtuple("Event", "user_id type ts")


class LookupDedupTests(unittest.TestCase):
    F = "cards-lookup-dedup.md"

    def test_dedup_keep_first_and_key(self):
        ns = load(self.F, "Hash-key dedup (exact duplicates)")

        def same(a, b):                      # the old comparison: NaN never matches
            if a.ts != a.ts or b.ts != b.ts:
                return False
            return (a.user_id == b.user_id and a.type == b.type
                    and math.floor(a.ts) == math.floor(b.ts))

        rng = random.Random(11)
        for _ in range(300):
            rows = [Event(rng.choice([1, 2, 1.0]), rng.choice(["a", "A", " a", "b"]),
                          rng.choice([0.0, 0.5, 1, 1.0, -0.5, -1.0, -1.5, 2.9, float("nan")]))
                    for _ in range(rng.randint(0, 60))]
            want = []
            for e in rows:
                if not any(same(e, k) for k in want):
                    want.append(e)
            got = ns["dedup_keep_first"](rows, ns["key"])
            self.assertEqual([id(x) for x in got], [id(x) for x in want])
        # floor, not int(): -0.5 and 0.5 are different seconds
        self.assertNotEqual(ns["key"](Event(1, "a", -0.5)), ns["key"](Event(1, "a", 0.5)))

    def test_join_vs_nested_loop(self):
        ns = load(self.F, "Dict index join")
        rng = random.Random(12)
        keys = [1, 2, "1", "2", 42, "42", None, 1.0]
        for _ in range(300):
            A = [(rng.choice(keys), i) for i in range(rng.randint(0, 40))]
            B = [(rng.choice(keys), j) for j in range(rng.randint(0, 40))]
            want = [(a, b) for a in A for b in B if a[0] == b[0]]
            self.assertEqual(ns["join"](A, B, itemgetter(0), itemgetter(0)), want)

    def test_dedup_sorted_vs_brute(self):
        ns = load(self.F, "Sort then scan")
        rng = random.Random(13)
        for _ in range(300):
            rows = [(rng.randint(0, 10), i) for i in range(rng.randint(0, 50))]
            want = [next(r for r in rows if r[0] == k) for k in sorted(set(r[0] for r in rows))]
            self.assertEqual(ns["dedup_sorted"](rows, itemgetter(0)), want)

    def test_merge_ranges_vs_nested_loop(self):
        ns = load(self.F, "Sort then scan")
        rng = random.Random(14)
        for _ in range(300):
            intervals = sorted(((s, s + rng.randint(0, 10), "t%d" % i)
                                for i, s in enumerate(rng.randint(0, 40) for _ in range(rng.randint(0, 12)))),
                               key=lambda iv: iv[0])
            points = sorted(rng.randint(-5, 50) for _ in range(rng.randint(0, 30)))
            want = [(tag, p) for s, e, tag in intervals for p in points if s <= p < e]
            self.assertEqual(ns["merge_ranges"](intervals, points), want)

    def test_windows_vs_recompute(self):
        ns = load(self.F, "Prefix sums and sliding window")
        rng = random.Random(15)
        for _ in range(200):
            a = [rng.randint(-20, 20) for _ in range(rng.randint(0, 50))]
            for w in range(1, len(a) + 2):
                starts = range(len(a) - w + 1)
                self.assertEqual(ns["window_sums"](a, w), [sum(a[i:i + w]) for i in starts])
                self.assertEqual(ns["window_max"](a, w), [max(a[i:i + w]) for i in starts])
                self.assertEqual(ns["window_distinct"](a, w), [len(set(a[i:i + w])) for i in starts])


# ---------------------------------------------------------------- graphs

GRAPH_CARD = "cards-graphs-impact.md"


def graph_ns():
    return load(GRAPH_CARD, "Reverse-graph impact set")


def topo_ns():
    return load(GRAPH_CARD, "Topological order and cycle detection")


class GraphTests(unittest.TestCase):
    def test_impact_set_vs_floyd_warshall(self):
        ns = graph_ns()
        rng = random.Random(21)
        for _ in range(120):
            V = rng.randint(1, 30)
            deps = {}
            for u in range(V):
                if rng.random() < 0.85:            # the rest appear only as dependencies
                    deps[u] = [rng.randrange(V) for _ in range(rng.randint(0, 3))]
            nodes = set(deps) | set(d for ds in deps.values() for d in ds)
            reach = closure(nodes, deps)
            rev = ns["reverse_graph"](deps)
            for x in nodes:
                want = {x} | set(u for u in nodes if x in reach[u])
                self.assertEqual(ns["impact_set"](rev, [x]), want)

    def test_kosaraju_vs_mutual_reachability(self):
        ns = load(GRAPH_CARD, "SCC condensation")
        rng = random.Random(22)
        for _ in range(150):
            V = rng.randint(1, 25)
            adj = {}
            for u in range(V):
                succ = [rng.randrange(V) for _ in range(rng.randint(0, 3))]
                if succ or rng.random() < 0.7:
                    adj[u] = succ
            nodes = set(adj) | set(v for vs in adj.values() for v in vs)
            comp = ns["kosaraju"](adj)
            self.assertEqual(set(comp), nodes)
            reach = dict((u, bfs_from(adj, u)) for u in nodes)
            for u in nodes:
                for v in nodes:
                    mutual = v in reach[u] and u in reach[v]
                    self.assertEqual(comp[u] == comp[v], mutual, (adj, u, v))
            self.assertEqual(comp, ns["kosaraju"](adj))

    def test_bitset_closure_vs_bfs(self):
        ns = load(GRAPH_CARD, "Bitset closure for many queries")
        rng = random.Random(23)
        for _ in range(100):
            V = rng.randint(1, 60)
            perm = list(range(V))
            rng.shuffle(perm)
            succ = {}
            for i, u in enumerate(perm):
                later = perm[i + 1:]
                succ[u] = sorted(set(rng.choice(later) for _ in range(rng.randint(0, 3)))) if later else []
            idx, reach = ns["reach_bitsets"](succ, perm)
            rows = ns["freeze"](reach)
            for a in perm:
                seen = bfs_from(succ, a)
                for b in perm:
                    self.assertEqual(ns["reaches"](idx, rows, a, b), b in seen)

    def test_topo_order_edges_and_cycle(self):
        ns = topo_ns()
        rng = random.Random(24)
        for _ in range(200):
            V = rng.randint(1, 40)
            perm = ["n%02d" % i for i in range(V)]
            rng.shuffle(perm)
            deps = {}
            for i, u in enumerate(perm):
                if rng.random() < 0.8:
                    deps[u] = rng.sample(perm[:i], min(i, rng.randint(0, 3)))
            nodes = set(deps) | set(d for ds in deps.values() for d in ds)
            order = ns["topo_order"](deps)
            self.assertEqual(sorted(order), sorted(nodes))
            pos = dict((u, i) for i, u in enumerate(order))
            for u, ds in deps.items():
                for d in ds:
                    self.assertLess(pos[d], pos[u])
            shuffled = dict(sorted(deps.items(), key=lambda kv: rng.random()))
            self.assertEqual(ns["topo_order"](shuffled), order)
            edges = [(u, d) for u, ds in deps.items() for d in ds]
            if edges:
                u, d = rng.choice(edges)
                cyclic = dict((k, list(v)) for k, v in deps.items())
                cyclic.setdefault(d, []).append(u)            # back edge: d now needs u
                with self.assertRaises(ValueError) as cm:
                    ns["topo_order"](cyclic)
                self.assertIn("cycle", str(cm.exception))

    def test_dsu_vs_bfs_components(self):
        ns = load(GRAPH_CARD, "Union-find for grouping")
        rng = random.Random(25)
        for _ in range(300):
            n = rng.randint(1, 50)
            edges = [(rng.randrange(n), rng.randrange(n)) for _ in range(rng.randint(0, n))]
            dsu = ns["DSU"](n)
            adj = dict((i, []) for i in range(n))
            for a, b in edges:
                dsu.union(a, b)
                adj[a].append(b)
                adj[b].append(a)
            groups = {}
            for i in range(n):
                groups.setdefault(dsu.find(i), set()).add(i)
            want = set(frozenset(bfs_from(adj, i)) for i in range(n))
            self.assertEqual(set(frozenset(g) for g in groups.values()), want)


# ---------------------------------------------------------------- CI and test selection

CI_CARD = "cards-ci-tests.md"


def coverage_instance(rng, n):
    universe = list(range(12))
    sets = dict(("s%d" % i, frozenset(rng.sample(universe, rng.randint(0, 5)))) for i in range(n))
    items = [("s%d" % i, 0, rng.randint(1, 6)) for i in range(n)]

    def value(chosen):
        covered = set()
        for it in chosen:
            covered |= sets[it[0]]
        return float(len(covered))

    return items, value


def brute_best(items, value, B):
    best = value([])
    for r in range(1, len(items) + 1):
        for combo in itertools.combinations(items, r):
            if sum(it[2] for it in combo) <= B:
                best = max(best, value(list(combo)))
    return best


class CiTests(unittest.TestCase):
    def test_select_tests_fails_closed(self):
        g = graph_ns()
        ns = load(CI_CARD, "Impact-based test selection",
                  reverse_graph=g["reverse_graph"], impact_set=g["impact_set"])
        sensitive = {"conftest.py", "requirements.lock"}

        def select(changed, graph, tests):
            ns["git_diff_names"] = lambda base, head: list(changed)
            return ns["select_tests"]("base", "head", graph, tests, lambda p: p in sensitive,
                                      lambda p: p.endswith(".md"))

        rng = random.Random(31)
        for _ in range(300):
            V = rng.randint(2, 15)
            files = ["f%d.py" % i for i in range(V)]
            tests = ["test_%d.py" % i for i in range(rng.randint(1, 5))]
            graph = dict((t, rng.sample(files, rng.randint(0, min(3, V)))) for t in tests)
            for f in files:
                if rng.random() < 0.6:
                    graph[f] = rng.sample(files, rng.randint(0, 2))
            universe = files + tests + ["conftest.py", "docs/a.md", "deleted_or_new.py"]
            changed = rng.sample(universe, rng.randint(0, 4))
            relevant = [p for p in changed if not p.endswith(".md")]
            nodes = set(graph) | set(d for ds in graph.values() for d in ds)
            if any(p in sensitive or p not in nodes for p in relevant):
                want = sorted(tests)
            else:
                reach = closure(nodes, graph)
                want = sorted(t for t in tests if t in relevant or any(c in reach[t] for c in relevant))
            self.assertEqual(select(changed, graph, tests), want, (graph, changed))
        graph = {"test_a.py": ["a.py"], "a.py": ["leaf.py"]}
        tests = ["test_a.py", "test_b.py"]
        self.assertEqual(select(["leaf.py"], graph, tests), ["test_a.py"])     # leaf is mapped
        self.assertEqual(select(["gone.py"], graph, tests), tests)             # absent: full
        self.assertEqual(select(["conftest.py"], graph, tests), tests)         # sensitive: full
        self.assertEqual(select(["README.md"], graph, tests), [])              # ignored only

    def test_step_key_determinism(self):
        ns = load(CI_CARD, "Content-hash cache (skip unchanged steps)")
        step_key = ns["step_key"]
        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a.txt"), os.path.join(tmp, "b.txt")
            for p, text in ((a, "alpha"), (b, "beta")):
                with open(p, "w") as f:
                    f.write(text)
            name = "DEVKIT_SKETCH_TEST_VAR"
            old = os.environ.pop(name, None)
            try:
                k = step_key([a, b], "1.0", ["-O"], [name])
                self.assertEqual(k, step_key([b, a], "1.0", ["-O"], [name]))
                self.assertNotEqual(k, step_key([a, b], "1.1", ["-O"], [name]))
                self.assertNotEqual(k, step_key([a, b], "1.0", ["-O2"], [name]))
                os.environ[name] = "x"
                self.assertNotEqual(k, step_key([a, b], "1.0", ["-O"], [name]))
                self.assertEqual(step_key([a, b], "1.0", ["-O"], []),
                                 step_key([a, b], "1.0", ["-O"], []))
                del os.environ[name]
                with open(b, "w") as f:
                    f.write("beta2")
                self.assertNotEqual(k, step_key([a, b], "1.0", ["-O"], [name]))
            finally:
                os.environ.pop(name, None)
                if old is not None:
                    os.environ[name] = old

    def test_run_cached_hit_miss_failure(self):
        ns = load(CI_CARD, "Content-hash cache (skip unchanged steps)")
        with tempfile.TemporaryDirectory() as tmp:
            cache, out = os.path.join(tmp, "cache"), os.path.join(tmp, "out")
            os.makedirs(cache)
            writer = [sys.executable, "-c",
                      "import os, sys; os.makedirs(sys.argv[1], exist_ok=True); "
                      "open(os.path.join(sys.argv[1], 'out.txt'), 'w').write('built')", out]
            self.assertEqual(ns["run_cached"]("k1", cache, out, writer), "miss")
            self.assertTrue(os.path.isfile(os.path.join(cache, "k1", "out.txt")))
            shutil.rmtree(out)
            self.assertEqual(ns["run_cached"]("k1", cache, out, writer), "hit")
            with open(os.path.join(out, "out.txt")) as f:
                self.assertEqual(f.read(), "built")
            failing = [sys.executable, "-c", "raise SystemExit(3)"]
            with self.assertRaises(subprocess.CalledProcessError):
                ns["run_cached"]("k2", cache, out, failing)
            self.assertFalse(os.path.exists(os.path.join(cache, "k2")))
            self.assertEqual(sorted(os.listdir(cache)), ["k1"])     # no temporary leftovers

    def test_knapsack_vs_brute_force(self):
        ns = load(CI_CARD, "Coverage under a time budget")
        rng = random.Random(32)
        for _ in range(150):
            n = rng.randint(0, 10)
            items = [("t%02d" % i, float(rng.randint(0, 20)), rng.randint(1, 10)) for i in range(n)]
            B = rng.randint(0, 40)
            chosen, best = ns["knapsack"](items, B)
            want = brute_best(items, lambda c: float(sum(it[1] for it in c)), B)
            self.assertEqual(best, want)
            by = dict((it[0], it) for it in items)
            self.assertLessEqual(sum(by[c][2] for c in chosen), B)
            self.assertEqual(sum(by[c][1] for c in chosen), best)
            self.assertEqual(chosen, sorted(chosen))

    def test_greedy_submodular_bound_and_counterexample(self):
        ns = load(CI_CARD, "Coverage under a time budget")
        rng = random.Random(33)
        worst = 1.0
        for _ in range(2000):
            items, value = coverage_instance(rng, rng.randint(1, 7))
            B = rng.randint(1, 15)
            opt = brute_best(items, value, B)
            picked = ns["greedy_submodular"](items, value, B)
            self.assertLessEqual(sum(it[2] for it in picked), B)
            if opt > 0:
                worst = min(worst, value(picked) / opt)
        self.assertGreaterEqual(worst, 0.31, "greedy fell below (1 - 1/e)/2 of the optimum")
        additive = lambda chosen: float(sum(it[1] for it in chosen))
        picked = ns["greedy_submodular"]([("x", 2, 1), ("y", 10, 10)], additive, 10)
        self.assertEqual(picked, [("y", 10, 10)])

    def test_exact_best_optimum_and_node_budget(self):
        ns = load(CI_CARD, "Coverage under a time budget")
        rng = random.Random(34)
        for _ in range(150):
            items, value = coverage_instance(rng, rng.randint(0, 10))
            B = rng.randint(0, 20)
            chosen, v = ns["exact_best"](sorted(items), value, B)
            self.assertEqual(v, brute_best(items, value, B))
            self.assertLessEqual(sum(it[2] for it in chosen), B)
            self.assertEqual(value(chosen), v)
        cheap = lambda chosen: float(len(chosen))
        cap = ns["EXACT_SEARCH_MAX"]
        at_cap = [("i%02d" % i, 1, 1) for i in range(cap)]
        self.assertIsNotNone(ns["exact_best"](at_cap, cheap, cap))          # 2^(n+1) - 1 nodes fit
        past = [("i%02d" % i, 1, 1) for i in range(cap + 1)]
        self.assertIsNone(ns["exact_best"](past, cheap, cap + 1))           # budget hit: None
        self.assertLessEqual(2 ** (cap + 1) - 1, ns["NODE_BUDGET"])
        self.assertGreater(2 ** (cap + 2) - 1, ns["NODE_BUDGET"])


# ---------------------------------------------------------------- scheduling

SCHED_CARD = "cards-scheduling.md"


def brute_makespan(durations, m):
    best = math.inf
    for assign in itertools.product(range(m), repeat=len(durations)):
        loads = [0.0] * m
        for d, w in zip(durations, assign):
            loads[w] += d
        best = min(best, max(loads))
    return best


def min_bins(durations, D):
    durations = sorted(durations, reverse=True)
    for k in range(1, len(durations) + 1):
        loads = [0.0] * k

        def place(i):
            if i == len(durations):
                return True
            tried = set()
            for w in range(k):
                if loads[w] in tried or loads[w] + durations[i] > D:
                    continue
                tried.add(loads[w])
                loads[w] += durations[i]
                if place(i + 1):
                    return True
                loads[w] -= durations[i]
            return False

        if place(0):
            return k
    return 0


class SchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ns = load(SCHED_CARD, "LPT across workers")
        cls.lpt = staticmethod(ns["lpt"])
        cls.lower_bound = staticmethod(ns["lower_bound"])

    def test_lpt_against_brute_force(self):
        # worked-example.md, Example B: "brute force m^n for n <= 8, m <= 3 on 200 random
        # cases, seed 3".
        rng = random.Random(3)
        for _ in range(200):
            m = rng.randint(2, 3)
            n = rng.randint(0, 8)
            jobs = [(float(rng.randint(1, 30)), "j%d" % i) for i in range(n)]
            plan = self.lpt(jobs, m)
            names = sorted(x for _, bucket in plan for x in bucket)
            self.assertEqual(names, sorted(name for _, name in jobs))
            for load_, bucket in plan:
                self.assertAlmostEqual(load_, sum(d for d, nm in jobs if nm in bucket))
            makespan = max(load_ for load_, _ in plan)
            opt = brute_makespan([d for d, _ in jobs], m) if n else 0.0
            self.assertLessEqual(makespan, (4.0 / 3 - 1.0 / (3 * m)) * opt + 1e-9)
            self.assertGreaterEqual(makespan + 1e-9, self.lower_bound(jobs, m))

    def test_lpt_deterministic_regardless_of_input_order(self):
        rng = random.Random(5)
        jobs = [(float(rng.randint(1, 50)), "job%02d" % i) for i in range(12)]
        shuffled = list(jobs)
        rng.shuffle(shuffled)
        self.assertEqual(self.lpt(jobs, 4), self.lpt(shuffled, 4))

    def test_pinned_jobs_share_worker_zero(self):
        jobs = [(10.0, "a"), (9.0, "b"), (1.0, "c"), (1.0, "d")]
        plan = self.lpt(jobs, 2, pinned={"a", "b"})
        self.assertEqual(sorted(plan[0][1]), ["a", "b"])
        self.assertEqual(self.lower_bound(jobs, 2, pinned={"a", "b"}), 19.0)
        self.assertEqual(self.lower_bound(jobs, 2), 10.5)
        rng = random.Random(6)
        for _ in range(300):
            m = rng.randint(2, 3)
            jobs = [(float(rng.randint(1, 9)), "j%d" % i) for i in range(rng.randint(1, 7))]
            pinned = set(j[1] for j in jobs if rng.random() < 0.3)
            plan = self.lpt(jobs, m, pinned)
            self.assertTrue(all(x in plan[0][1] for x in pinned))
            self.assertGreaterEqual(max(l for l, _ in plan) + 1e-9,
                                    self.lower_bound(jobs, m, pinned))

    def test_critical_path_vs_all_paths(self):
        ns = load(SCHED_CARD, "Critical path")
        # The phase-1 defect: a predecessor finishing at 0 was dropped, losing the path start.
        self.assertEqual(ns["critical_path"]({"a": 0, "b": 0, "c": 5}, {"b": ["a"], "c": ["b"]},
                                             ["a", "b", "c"]), (5.0, ["a", "b", "c"]))
        rng = random.Random(41)
        for _ in range(400):
            V = rng.randint(1, 10)
            topo = ["s%d" % i for i in range(V)]
            rng.shuffle(topo)
            dur = dict((v, rng.choice([0, 0, 1, 2, 3, 5])) for v in topo)
            preds = {}
            for i, v in enumerate(topo):
                ps = rng.sample(topo[:i], min(i, rng.randint(0, 3)))
                if ps:
                    preds[v] = ps
            succ = {}
            for v, ps in preds.items():
                for u in ps:
                    succ.setdefault(u, []).append(v)

            def longest_from(u):
                return dur[u] + max([longest_from(v) for v in succ.get(u, ())] or [0])

            best = max(longest_from(v) for v in topo)
            length, path = ns["critical_path"](dur, preds, topo)
            self.assertEqual(length, best)
            self.assertEqual(sum(dur[v] for v in path), length)
            for u, v in zip(path, path[1:]):
                self.assertIn(u, preds.get(v, ()))
            self.assertFalse(preds.get(path[0]), "path must start at a stage with no predecessor")

    def test_fewest_workers_vs_brute_minimum(self):
        ns = load(SCHED_CARD, "Fewest workers for a deadline")
        rng = random.Random(42)
        for _ in range(300):
            n = rng.randint(1, 8)
            jobs = [(float(rng.randint(1, 10)), "j%d" % i) for i in range(n)]
            D = float(rng.randint(10, 25))
            workers = ns["fewest_workers"](jobs, D)
            names = sorted(x for _, b in workers for x in b)
            self.assertEqual(names, sorted(nm for _, nm in jobs))
            by = dict((nm, d) for d, nm in jobs)
            for load_, bucket in workers:
                self.assertLessEqual(load_, D)
                self.assertAlmostEqual(load_, sum(by[x] for x in bucket))
            opt = min_bins([d for d, _ in jobs], D)
            self.assertGreaterEqual(len(workers), opt)
            self.assertLessEqual(len(workers), 11.0 / 9 * opt + 6.0 / 9 + 1e-9)
        with self.assertRaises(ValueError):
            ns["fewest_workers"]([(5.0, "a"), (30.0, "too_long")], 20.0)


# ---------------------------------------------------------------- multi-pattern

PATTERN_CARD = "cards-multi-pattern.md"


class MultiPatternTests(unittest.TestCase):
    def test_aho_corasick_vs_str_find(self):
        ns = load(PATTERN_CARD, "Aho-Corasick for literal patterns")
        rng = random.Random(51)
        alphabet = "ab c"
        lines_checked = 0
        for _ in range(20):
            patterns = ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4)))
                        for _ in range(rng.randint(1, 12))]
            patterns.append(patterns[0])                 # duplicate patterns both report
            goto, fail, out = ns["build"](patterns)
            for _ in range(100):
                line = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
                want = []
                for pid, p in enumerate(patterns):
                    i = line.find(p)
                    while i != -1:
                        want.append((i + len(p) - 1, pid))
                        i = line.find(p, i + 1)
                got = list(ns["scan"](line, goto, fail, out))
                self.assertEqual(sorted(got), sorted(want), (patterns, line))
                lines_checked += 1
        self.assertEqual(lines_checked, 2000)

    def test_required_literal(self):
        ns = load(PATTERN_CARD, "Literal prefilter for regex sets")
        rl = ns["required_literal"]
        cases = {"[]abc]xyz": "xyz", "[^]]abcd": "abcd", "(?x) foo \\s+ bar": "", "(?i)hello": "",
                 "foo(bar|baz)qux": "foo", "a|bcd": "", "hello\\d+world": "hello",
                 "[a-z]+error code": "error code", "abc?def": "def", "x{2,3}yyyy": "yyyy"}
        for pat, want in cases.items():
            self.assertEqual(rl(pat), want, pat)
        self.assertEqual(rl("hello world", re.VERBOSE), "")
        self.assertEqual(rl("hello world", re.I), "")
        self.assertEqual(rl("hello world"), "hello world")
        rng = random.Random(7)

        def gen(depth=0):
            kind = rng.choice(["lit", "lit", "lit", "class", "class0", "group", "alt", "quant",
                               "esc", "dot"]) if depth <= 2 else "lit"
            if kind == "lit":
                s = "".join(rng.choice("abcdefgh") for _ in range(rng.randint(1, 5)))
                return s, lambda: s
            if kind == "class":
                members = "".join(sorted(set(rng.choice("abcxyz") for _ in range(3))))
                return "[%s]" % members, lambda: rng.choice(members)
            if kind == "class0":
                members = "]" + "".join(sorted(set(rng.choice("abc") for _ in range(2))))
                if rng.random() < 0.5:
                    return "[^%s]" % members, lambda: rng.choice("xyz")
                return "[%s]" % members, lambda: rng.choice(members)
            if kind == "esc":
                return "\\d", lambda: rng.choice("0123456789")
            if kind == "dot":
                return ".", lambda: rng.choice("qrs")
            if kind == "group":
                parts = [gen(depth + 1) for _ in range(rng.randint(1, 3))]
                return "(" + "".join(p for p, _ in parts) + ")", lambda: "".join(g() for _, g in parts)
            if kind == "alt":
                a, b = gen(depth + 1), gen(depth + 1)
                return "(%s|%s)" % (a[0], b[0]), lambda: rng.choice([a[1], b[1]])()
            a = gen(depth + 1)
            q = rng.choice(["?", "*", "+", "{0,2}", "{1,3}"])
            lo, hi = {"?": (0, 1), "*": (0, 3), "+": (1, 3), "{0,2}": (0, 2), "{1,3}": (1, 3)}[q]
            return "(%s)%s" % (a[0], q), lambda: "".join(a[1]() for _ in range(rng.randint(lo, hi)))

        checked = 0
        for _ in range(5000):
            if checked >= 2000:
                break
            parts = [gen() for _ in range(rng.randint(1, 5))]
            pat = "".join(p for p, _ in parts)
            try:
                cre = re.compile(pat)
            except re.error:
                continue
            lit = rl(pat)
            if not lit:
                continue
            for _ in range(20):
                s = "".join(g() for _, g in parts)
                self.assertTrue(cre.search(s), (pat, s))
                self.assertIn(lit, s, "literal %r is not required by %r" % (lit, pat))
                checked += 1
        self.assertGreaterEqual(checked, 2000)


# ---------------------------------------------------------------- LLM context

LLM_CARD = "cards-llm-context.md"


def pack_ns():
    ns = load(LLM_CARD, "Pack under a token budget")

    def shorten(c):        # test double for the card's stub: keep the first half of a file range
        if c.path.startswith("<"):
            return c
        return Chunk(c.path, c.start, c.start + (c.end - c.start) // 2, c.score)

    ns["shorten"] = shorten
    return ns


def lines_of(chunks):
    out = {}
    for c in chunks:
        out.setdefault(c.path, set()).update(range(c.start, c.end + 1))
    return out


def knapsack_best(pool, cap, tokens):
    best = [0.0] * (cap + 1)
    for c in pool:
        w = tokens(c)
        for b in range(cap, w - 1, -1):
            best[b] = max(best[b], best[b - w] + c.score)
    return best[cap]


def assert_pack_oracle(tc, pack, ns, seed=64):
    """The Pack card's oracle, applied to any pack(must_keep, candidates, B, tokens) with the
    card's merge_ranges, subtract_ranges and shorten (ns) as the reference for the expected
    pool. Returns the (must, cands, B) instances it drew, for agreement checks."""
    tokens = lambda c: c.end - c.start + 1
    # The phase-1 defect: a candidate overlapping a must-keep range was sent twice.
    out, used, note = pack([Chunk("a.py", 1, 10, 0)],
                           [Chunk("a.py", 5, 15, 9), Chunk("b.py", 1, 3, 1)], 100, tokens)
    tc.assertEqual(out, [Chunk("a.py", 1, 10, 0), Chunk("a.py", 11, 15, 9),
                         Chunk("b.py", 1, 3, 1)])
    tc.assertEqual((used, note), (18, ""))
    # Best-single fallback: ratio greedy alone would take the cheap chunk and stop.
    out, used, _ = pack([], [Chunk("a.py", 1, 1, 2), Chunk("b.py", 1, 10, 10)], 12, tokens)
    tc.assertEqual(out, [Chunk("b.py", 1, 10, 10)])
    rng = random.Random(seed)
    ratio_checked, drawn = 0, []
    for case in range(400):
        must = [Chunk(rng.choice(["a.py", "b.py", "c.py"]), s, s + rng.randint(0, 15), 0)
                for s in (rng.randint(1, 60) for _ in range(rng.randint(0, 3)))]
        if rng.random() < 0.5:
            must.append(Chunk("<test command>", 1, 2, 0))
        cands = [Chunk(rng.choice(["a.py", "b.py", "c.py"]), s, s + rng.randint(0, 20),
                       rng.randint(0, 20)) for s in (rng.randint(1, 80) for _ in range(rng.randint(0, 12)))]
        B = rng.randint(5, 200)
        drawn.append((must, cands, B))
        out, used, note = pack(must, cands, B, tokens)
        limit = int(B * 0.9)
        tc.assertEqual(used, sum(tokens(c) for c in out))
        for mk in must:                                  # must-keep always present
            tc.assertTrue(any(c.path == mk.path and c.start <= mk.start <= c.end for c in out))
        seen = {}
        for c in out:                                    # no line of any path twice
            for ln in range(c.start, c.end + 1):
                tc.assertNotIn((c.path, ln), seen, (must, cands, out))
                seen[(c.path, ln)] = True
        tc.assertEqual(out, sorted(out, key=lambda c: (c.path, c.start)))
        shuffled_c, shuffled_m = list(cands), list(must)
        rng.shuffle(shuffled_c)
        rng.shuffle(shuffled_m)
        tc.assertEqual(pack(shuffled_m, shuffled_c, B, tokens), (out, used, note))
        kept = ns["merge_ranges"](must)
        if sum(tokens(c) for c in kept) > limit:
            kept = [ns["shorten"](c) for c in kept]
        if "over budget" in note:
            tc.assertEqual(out, sorted(kept, key=lambda c: (c.path, c.start)))
            continue
        tc.assertLessEqual(used, limit)
        tc.assertLessEqual(used, B)
        room = limit - sum(tokens(c) for c in kept)
        pool = [p for c in ns["merge_ranges"](cands) for p in ns["subtract_ranges"](c, kept)]
        pool = [p for p in pool if tokens(p) <= room]
        picked = [c for c in out if c not in set(kept)]
        opt = knapsack_best(pool, room, tokens)
        if len(cands) <= 12 and opt > 0:
            tc.assertGreaterEqual(sum(c.score for c in picked) + 1e-9, 0.5 * opt,
                                  (must, cands, B, out))
            ratio_checked += 1
    tc.assertGreaterEqual(ratio_checked, 200)
    out, used, note = pack([Chunk("a.py", 1, 400, 0)], [Chunk("b.py", 1, 2, 5)], 100, tokens)
    tc.assertIn("over budget", note)
    tc.assertEqual([c.path for c in out], ["a.py"])
    return drawn


class LlmContextTests(unittest.TestCase):
    def test_names_in_and_exact_hits(self):
        ns = load(LLM_CARD, "Exact-first retrieval")
        task = "Fix paginate_results in svc/api.py; the HttpClient retries too often and fetchPage breaks"
        self.assertEqual(ns["names_in"](task),
                         ["paginate_results", "svc/api.py", "HttpClient", "fetchPage"])
        files = {
            "svc/api.py": "import os\n\ndef paginate_results(q):\n    return fetchPage(q)\n",
            "svc/http.py": "class HttpClient:\n    pass\n",
            "tests/test_api.py": "from svc.api import paginate_results\n\ndef test_p():\n"
                                 "    paginate_results(1)\n",
            "docs/notes.md": "paginate_results_v2 is not paginate results\n",
        }
        hits = ns["exact_hits"](ns["names_in"](task), files)
        self.assertEqual(hits, [
            (0, "svc/api.py", 0, "svc/api.py"),
            (1, "svc/api.py", 3, "paginate_results"),
            (1, "svc/http.py", 1, "HttpClient"),
            (2, "svc/api.py", 4, "fetchPage"),
            (2, "tests/test_api.py", 1, "paginate_results"),
            (2, "tests/test_api.py", 4, "paginate_results"),
        ])

    def test_bm25_vs_direct_scoring(self):
        ns = load(LLM_CARD, "Sparse scoring (BM25) over chunks")
        terms = ns["terms"]
        self.assertEqual(terms("parseHTTPRequest snake_case x2Y"),
                         ["parse", "httprequest", "snake", "case", "x2", "y"])
        rng = random.Random(61)
        vocab = ["alpha", "beta", "gamma", "delta", "parseConfig", "load_file", "cache", "retry",
                 "token", "budget", "graph", "node", "edge", "path", "fetchPage", "http_client",
                 "error", "value", "index", "query", "chunk", "score", "rank", "merge"]
        chunks = [("f%02d.py" % (i // 10), (i % 10) * 100 + 1,
                   " ".join(rng.choice(vocab) for _ in range(rng.randint(1, 40)))) for i in range(500)]
        index = ns["BM25"](chunks)
        tfs = [dict((t, terms(text).count(t)) for t in set(terms(text))) for _, _, text in chunks]
        lens = [len(terms(text)) for _, _, text in chunks]
        avg = sum(lens) / float(len(chunks))
        k1, b = 1.2, 0.75
        for _ in range(100):
            query = " ".join(rng.choice(vocab + ["missing"]) for _ in range(rng.randint(1, 4)))
            qt = set(terms(query))
            scores = {}
            for cid, tf in enumerate(tfs):
                s, hit = 0.0, False
                for t in qt:
                    if t in tf:
                        df = sum(1 for other in tfs if t in other)
                        idf = math.log(1 + (len(chunks) - df + 0.5) / (df + 0.5))
                        n = tf[t]
                        s += idf * n * (k1 + 1) / (n + k1 * (1 - b + b * lens[cid] / avg))
                        hit = True
                if hit:
                    scores[cid] = s
            want = sorted(scores.items(), key=lambda kv: (-kv[1], chunks[kv[0]][:2]))[:10]
            got = index.query(query, k=10)
            self.assertEqual([c for c, _ in got], [c for c, _ in want], query)
            for (_, gs), (_, ws) in zip(got, want):
                self.assertAlmostEqual(gs, ws, delta=1e-9)

    def test_expand_vs_truncated_bfs(self):
        ns = load(LLM_CARD, "Bounded graph expansion")

        def reference(targets, callers, callees, hops, cap, hub):
            order, reason, seen = [], {}, set(targets)
            level, depth = [(t, "target") for t in sorted(targets)], 0
            while level:
                nxt = []
                for node, why in level:
                    order.append(node)
                    reason[node] = why
                    if depth == hops:
                        continue
                    for kind, nbrs in (("caller", callers), ("callee", callees)):
                        for n in sorted(nbrs.get(node, ())):
                            deg = len(callers.get(n, ())) + len(callees.get(n, ()))
                            if n in seen or deg > hub:
                                continue
                            seen.add(n)
                            nxt.append((n, "%s of %s" % (kind, node)))
                level, depth = nxt, depth + 1
            order = order[:cap]
            return order, dict((n, reason[n]) for n in order)

        rng = random.Random(62)
        for _ in range(300):
            V = rng.randint(1, 25)
            callees = {}
            for u in range(V):
                callees["m%02d" % u] = sorted(set("m%02d" % rng.randrange(V)
                                                  for _ in range(rng.randint(0, 3))))
            callers = {}
            for u, vs in callees.items():
                for v in vs:
                    callers.setdefault(v, []).append(u)
            targets = set(rng.sample(sorted(callees), rng.randint(1, min(3, V))))
            hops, cap, hub = rng.randint(0, 2), rng.randint(1, 15), rng.randint(1, 6)
            got = ns["expand"](targets, callers, callees, hops, cap, hub)
            self.assertEqual(got, reference(targets, callers, callees, hops, cap, hub))

    def test_merge_and_subtract_ranges_vs_line_sets(self):
        ns = pack_ns()
        rng = random.Random(63)
        for _ in range(500):
            chunks = [Chunk(rng.choice(["a.py", "b.py"]), s, s + rng.randint(0, 8), rng.randint(0, 9))
                      for s in (rng.randint(1, 40) for _ in range(rng.randint(0, 8)))]
            merged = ns["merge_ranges"](chunks)
            self.assertEqual(lines_of(merged), lines_of(chunks))
            for x, y in zip(merged, merged[1:]):
                if x.path == y.path:
                    self.assertGreater(y.start, x.end + 1)          # disjoint, not adjacent
            for m in merged:
                inside = [c.score for c in chunks if c.path == m.path and m.start <= c.start <= m.end]
                self.assertEqual(m.score, max(inside))
            chosen = ns["merge_ranges"]([Chunk(rng.choice(["a.py", "b.py"]), s, s + rng.randint(0, 6), 0)
                                         for s in (rng.randint(1, 40) for _ in range(rng.randint(0, 3)))])
            for c in chunks:
                parts = ns["subtract_ranges"](c, chosen)
                covered = lines_of([k for k in chosen if k.path == c.path]).get(c.path, set())
                self.assertEqual(lines_of(parts).get(c.path, set()),
                                 set(range(c.start, c.end + 1)) - covered)
                self.assertTrue(all(p.score == c.score and p.path == c.path for p in parts))
                for x, y in zip(parts, parts[1:]):
                    self.assertGreater(y.start, x.end + 1)

    def test_pack_properties(self):
        ns = pack_ns()
        assert_pack_oracle(self, ns["pack"], ns)


# ---------------------------------------------------------------- repeated work

REPEAT_CARD = "cards-repeated-work.md"


class RepeatedWorkTests(unittest.TestCase):
    def test_memoise(self):
        ns = load(REPEAT_CARD, "Memoise pure functions")
        calls = []

        def _parse(text):
            calls.append(text)
            return tuple(text.split())

        ns["_parse"] = _parse
        ns["parse_config"].cache_clear()
        self.assertEqual(ns["parse_config"]("a b"), ("a", "b"))
        self.assertEqual(ns["parse_config"]("a b"), ("a", "b"))
        self.assertEqual(ns["parse_config"]("c"), ("c",))
        self.assertEqual(calls, ["a b", "c"])
        del calls[:]
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = os.path.join(tmp, "one.cfg"), os.path.join(tmp, "two.cfg")
            for p in (p1, p2):
                with open(p, "w") as f:
                    f.write("x = 1")
            self.assertEqual(ns["parse_config_file"](p1), ("x", "=", "1"))
            self.assertEqual(ns["parse_config_file"](p2), ("x", "=", "1"))
            self.assertEqual(len(calls), 1)                  # keyed on content, not path
            with open(p1, "w") as f:
                f.write("x = 2")
            self.assertEqual(ns["parse_config_file"](p1), ("x", "=", "2"))
            self.assertEqual(len(calls), 2)

    def test_process_all_vs_full_recompute(self):
        ns = load(REPEAT_CARD, "Content-hash cache across runs")
        calls = []

        def compute(content):
            calls.append(content)
            return content.decode().upper()

        rng = random.Random(71)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "manifest.json")
            items = dict(("i%02d" % i, ("v%d" % rng.randint(0, 9)).encode()) for i in range(10))
            out, hits = ns["process_all"](items, manifest, compute, "1.0", ["-x"], {"LANG": "C"})
            self.assertEqual((hits, len(calls)), (0, 10))
            for _ in range(30):
                del calls[:]
                changed = set(rng.sample(sorted(items), rng.randint(0, 3)))
                for name in changed:
                    items[name] = (items[name].decode() + "!").encode()
                for name in rng.sample(sorted(items), rng.randint(0, 2)):
                    del items[name]
                    changed.discard(name)
                new = "n%03d" % rng.randint(0, 999)
                if new not in items:
                    items[new] = b"fresh"
                    changed.add(new)
                out, hits = ns["process_all"](items, manifest, compute, "1.0", ["-x"], {"LANG": "C"})
                self.assertEqual(out, dict((k, v.decode().upper()) for k, v in items.items()))
                self.assertEqual(sorted(calls), sorted(items[n] for n in changed))
                self.assertEqual(hits, len(items) - len(changed))
            del calls[:]
            ns["process_all"](items, manifest, compute, "2.0", ["-x"], {"LANG": "C"})
            self.assertEqual(len(calls), len(items))             # tool version is an input
            with open(manifest) as f:
                before = f.read()

            def boom(content):
                raise RuntimeError("compute failed")

            items["zz"] = b"new"
            with self.assertRaises(RuntimeError):
                ns["process_all"](items, manifest, boom, "2.0", ["-x"], {"LANG": "C"})
            with open(manifest) as f:
                self.assertEqual(f.read(), before)               # a raise stores nothing

    def test_incremental_vs_full_recompute(self):
        g, t = graph_ns(), topo_ns()
        ns = load(REPEAT_CARD, "Incremental recompute via dependents",
                  reverse_graph=g["reverse_graph"], impact_set=g["impact_set"],
                  topo_order=t["topo_order"])
        key_of = lambda content: hashlib.sha256(content.encode()).hexdigest()
        rng = random.Random(72)
        for _ in range(60):
            items = dict(("i%02d" % i, "c%d" % rng.randint(0, 5)) for i in range(rng.randint(1, 10)))
            names = sorted(items)
            deps = dict((n, rng.sample(names[:k], min(k, rng.randint(0, 2))))
                        for k, n in enumerate(names))
            calls = []

            def compute(n, dep_results):
                calls.append(n)
                return hashlib.sha256((items[n] + json.dumps(dep_results, sort_keys=True))
                                      .encode()).hexdigest()

            def full():
                res = {}
                for n in t["topo_order"](dict((n, deps.get(n, ())) for n in items)):
                    res[n] = hashlib.sha256((items[n] + json.dumps(
                        dict((d, res[d]) for d in deps.get(n, ())), sort_keys=True)).encode()).hexdigest()
                return res

            manifest, affected = ns["incremental"](items, deps, {}, compute, key_of)
            self.assertEqual(affected, sorted(items))
            for _ in range(8):
                for n in rng.sample(sorted(items), min(len(items), rng.randint(0, 2))):
                    items[n] += "+"
                if len(items) > 1 and rng.random() < 0.4:
                    gone = rng.choice(sorted(items))
                    del items[gone]
                    deps.pop(gone, None)
                    for n in deps:                       # the builder drops edges to missing items
                        deps[n] = [d for d in deps[n] if d != gone]
                if rng.random() < 0.5:
                    new = "j%03d" % rng.randint(0, 999)
                    if new not in items:
                        existing = sorted(items)
                        items[new] = "fresh"
                        deps[new] = rng.sample(existing, min(len(existing), rng.randint(0, 2)))
                del calls[:]
                manifest, affected = ns["incremental"](items, deps, manifest, compute, key_of)
                want = full()
                self.assertEqual(sorted(manifest), sorted(items))
                self.assertEqual(dict((n, manifest[n]["result"]) for n in items), want)
                self.assertEqual(sorted(calls), affected)

    def test_range_index_vs_linear_scan(self):
        ns = load(REPEAT_CARD, "Index once, query many")
        rng = random.Random(73)
        for _ in range(300):
            cuts = sorted(set(rng.randint(0, 100) for _ in range(rng.randint(0, 20))))
            intervals = [(a, b, "t%d" % i) for i, (a, b) in enumerate(zip(cuts, cuts[1:]))
                         if rng.random() < 0.7]
            rng.shuffle(intervals)
            idx = ns["RangeIndex"](intervals)
            for x in list(range(-2, 103)) + [rng.uniform(-1, 101) for _ in range(20)]:
                want = next((tag for s, e, tag in intervals if s <= x < e), None)
                self.assertEqual(idx.lookup(x), want)
            if intervals:
                changed = ns["RangeIndex"](intervals[1:])
                self.assertNotEqual(changed.version, idx.version)

    def test_coalescer_one_fetch_per_key(self):
        ns = load(REPEAT_CARD, "Coalesce identical requests")
        counts, lock = {}, threading.Lock()

        def fetch(key):
            with lock:
                counts[key] = counts.get(key, 0) + 1
            time.sleep(0.2)                      # the in-flight window
            if key == "bad":
                raise KeyError(key)
            return {"key": key}

        c = ns["Coalescer"](fetch)

        def wave(keys):
            barrier = threading.Barrier(len(keys))
            results = [None] * len(keys)

            def worker(i, k):
                barrier.wait()
                try:
                    results[i] = c.get(k)
                except KeyError as e:
                    results[i] = e

            threads = [threading.Thread(target=worker, args=(i, k)) for i, k in enumerate(keys)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            return results

        results = wave(["a"] * 4 + ["b"] * 4)
        self.assertEqual(counts, {"a": 1, "b": 1})
        self.assertEqual(results, [{"key": "a"}] * 4 + [{"key": "b"}] * 4)
        wave(["a"] * 8)
        self.assertEqual(counts["a"], 2)          # nothing is kept past the window
        bad = wave(["bad"] * 8)
        self.assertEqual(counts["bad"], 1)
        self.assertTrue(all(isinstance(r, KeyError) for r in bad))
        wave(["bad"])
        self.assertEqual(counts["bad"], 2)        # a failure is not cached
        self.assertEqual(c.inflight, {})


# ---------------------------------------------------------------- devkit.graph agreement

SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")


def devkit_graph():
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    import devkit.graph
    return devkit.graph


def random_deps(rng, V, density=0.85, max_out=3):
    deps = {}
    for u in range(V):
        if rng.random() < density:
            deps["n%02d" % u] = ["n%02d" % rng.randrange(V) for _ in range(rng.randint(0, max_out))]
    return deps


class DevkitAgreementTests(unittest.TestCase):
    """devkit.graph carries the card algorithms for dev.py; both must give the same answers
    on the same random instances, so a fix in one place cannot silently diverge."""

    @classmethod
    def setUpClass(cls):
        cls.g = devkit_graph()

    def test_reverse_graph_and_impact_set_agree(self):
        ns = graph_ns()
        rng = random.Random(41)
        for _ in range(150):
            deps = random_deps(rng, rng.randint(1, 30))
            rev_card, rev_dev = ns["reverse_graph"](deps), self.g.reverse_graph(deps)
            self.assertEqual(rev_card, rev_dev)
            for x in sorted(rev_card):
                self.assertEqual(ns["impact_set"](rev_card, [x]), self.g.impact_set(rev_dev, [x]))
                parent = self.g.impact_parents(rev_dev, [x])
                self.assertEqual(set(parent), self.g.impact_set(rev_dev, [x]))
                for node in parent:                  # every chain walks real reverse edges
                    c = self.g.chain(parent, node)
                    self.assertEqual(c[-1], x)
                    for a, b in zip(c, c[1:]):
                        self.assertIn(a, rev_dev[b])

    def test_topo_order_agrees_and_names_a_real_cycle(self):
        ns = topo_ns()
        rng = random.Random(42)
        for _ in range(200):
            V = rng.randint(1, 30)
            perm = ["n%02d" % i for i in range(V)]
            rng.shuffle(perm)
            deps = dict((u, rng.sample(perm[:i], min(i, rng.randint(0, 3))))
                        for i, u in enumerate(perm) if rng.random() < 0.8)
            self.assertEqual(ns["topo_order"](deps), self.g.topo_order(deps))
            edges = [(u, d) for u, ds in deps.items() for d in ds]
            if not edges:
                continue
            u, d = rng.choice(edges)
            cyclic = dict((k, list(v)) for k, v in deps.items())
            cyclic.setdefault(d, []).append(u)
            with self.assertRaises(ValueError):
                ns["topo_order"](cyclic)
            with self.assertRaises(self.g.CycleError) as cm:
                self.g.topo_order(cyclic)
            cycle = cm.exception.cycle
            self.assertEqual(cycle[0], cycle[-1])
            self.assertGreaterEqual(len(cycle), 2)
            for a, b in zip(cycle, cycle[1:]):
                self.assertIn(b, cyclic.get(a, ()), (cycle, cyclic))
            self.assertIn(" -> ".join(cycle), str(cm.exception))

    def test_dsu_agrees(self):
        ns = load(GRAPH_CARD, "Union-find for grouping")
        rng = random.Random(43)
        for _ in range(200):
            n = rng.randint(1, 40)
            card, dev = ns["DSU"](n), self.g.DSU(n)
            for _ in range(rng.randint(0, n)):
                a, b = rng.randrange(n), rng.randrange(n)
                card.union(a, b)
                dev.union(a, b)
            self.assertEqual([card.find(i) for i in range(n)], [dev.find(i) for i in range(n)])

    def test_lpt_and_lower_bound_agree(self):
        ns = load(SCHED_CARD, "LPT across workers")
        rng = random.Random(44)
        for _ in range(300):
            m = rng.randint(2, 5)
            jobs = [(float(rng.randint(1, 40)), "j%02d" % i) for i in range(rng.randint(0, 14))]
            pinned = tuple(name for _, name in jobs if rng.random() < 0.2)
            self.assertEqual(ns["lpt"](jobs, m, pinned), self.g.lpt(jobs, m, pinned))
            self.assertEqual(ns["lower_bound"](jobs, m, pinned), self.g.lower_bound(jobs, m, pinned))

    def test_selection_algorithms_agree(self):
        ns = load(CI_CARD, "Coverage under a time budget")
        self.assertEqual(ns["EXACT_SEARCH_MAX"], self.g.EXACT_SEARCH_MAX)
        self.assertEqual(ns["NODE_BUDGET"], self.g.NODE_BUDGET)
        rng = random.Random(45)
        for _ in range(150):
            n = rng.randint(0, 9)
            items = [("t%02d" % i, float(rng.randint(0, 20)), rng.randint(1, 8)) for i in range(n)]
            B = rng.randint(0, 30)
            self.assertEqual(ns["knapsack"](items, B), self.g.knapsack(items, B))
            cov, value = coverage_instance(rng, rng.randint(1, 8))
            B = rng.randint(1, 15)
            self.assertEqual(ns["greedy_submodular"](cov, value, B),
                             self.g.greedy_submodular(cov, value, B))
            self.assertEqual(ns["exact_best"](sorted(cov), value, B),
                             self.g.exact_best(sorted(cov), value, B))


def devkit_context():
    devkit_graph()
    import devkit.context
    return devkit.context


class ContextAgreementTests(unittest.TestCase):
    """devkit.context carries the LLM-context card algorithms for `dev.py context`; it must
    pass the cards' oracles and return the same answers on the same random instances."""

    @classmethod
    def setUpClass(cls):
        cls.c = devkit_context()

    def test_pack_passes_the_card_oracle_and_agrees(self):
        ns = pack_ns()
        dk = lambda m, c, B, t: self.c.pack(m, c, B, t, shorten=ns["shorten"])
        drawn = assert_pack_oracle(self, dk, ns)
        tokens = lambda c: c.end - c.start + 1
        for must, cands, B in drawn:
            self.assertEqual(self.c.pack(must, cands, B, tokens, shorten=ns["shorten"]),
                             ns["pack"](must, cands, B, tokens), (must, cands, B))

    def test_pack_on_disjoint_partitions_without_the_merge(self):
        """`context` passes merge_candidates=False because its candidates partition each
        file; the oracle still holds and adjacent chunks keep their own scores."""
        ns = pack_ns()
        tokens = lambda c: c.end - c.start + 1
        rng = random.Random(71)
        checked = 0
        for _ in range(300):
            cands = []
            for path in ("a.py", "b.py"):
                line = 1
                for _ in range(rng.randint(0, 6)):
                    end = line + rng.randint(0, 9)
                    if rng.random() < 0.8:
                        cands.append(Chunk(path, line, end, rng.randint(0, 20)))
                    line = end + 1
            must = [Chunk(rng.choice(["a.py", "b.py"]), s, s + rng.randint(0, 8), 0)
                    for s in (rng.randint(1, 50) for _ in range(rng.randint(0, 2)))]
            B = rng.randint(5, 120)
            out, used, note = self.c.pack(must, cands, B, tokens, shorten=ns["shorten"],
                                          merge_candidates=False)
            self.assertEqual(used, sum(tokens(c) for c in out))
            seen = set()
            for c in out:
                for ln in range(c.start, c.end + 1):
                    self.assertNotIn((c.path, ln), seen)
                    seen.add((c.path, ln))
            for mk in must:
                self.assertTrue(any(c.path == mk.path and c.start <= mk.start <= c.end for c in out))
            if "over budget" in note:
                continue
            limit = int(B * 0.9)
            self.assertLessEqual(used, limit)
            kept = ns["merge_ranges"](must)
            if sum(tokens(c) for c in kept) > limit:
                kept = [ns["shorten"](c) for c in kept]
            room = limit - sum(tokens(c) for c in kept)
            pool = [p for c in cands for p in ns["subtract_ranges"](c, kept) if tokens(p) <= room]
            opt = knapsack_best(pool, room, tokens)
            if opt > 0:
                picked = [c for c in out if c not in set(kept)]
                self.assertGreaterEqual(sum(c.score for c in picked) + 1e-9, 0.5 * opt)
                checked += 1
        self.assertGreaterEqual(checked, 150)

    def test_merge_and_subtract_agree(self):
        ns = pack_ns()
        rng = random.Random(72)
        for _ in range(300):
            chunks = [Chunk(rng.choice(["a.py", "b.py"]), s, s + rng.randint(0, 8), rng.randint(0, 9))
                      for s in (rng.randint(1, 40) for _ in range(rng.randint(0, 8)))]
            self.assertEqual(self.c.merge_ranges(chunks), ns["merge_ranges"](chunks))
            chosen = ns["merge_ranges"](chunks[:2])
            for c in chunks[2:]:
                self.assertEqual(self.c.subtract_ranges(c, chosen), ns["subtract_ranges"](c, chosen))

    def test_names_hits_bm25_and_expand_agree(self):
        exact = load(LLM_CARD, "Exact-first retrieval")
        task = "Fix paginate_results in svc/api.py; the HttpClient retries too often and fetchPage breaks"
        self.assertEqual(self.c.names_in(task, weak=False), exact["names_in"](task))
        self.assertEqual([n for n in self.c.names_in(task) if self.c.code_like(n)],
                         exact["names_in"](task))
        self.assertIn("retries", self.c.names_in(task))         # weak: plain word, 4+ chars
        files = {"svc/api.py": "import os\n\ndef paginate_results(q):\n    return fetchPage(q)\n",
                 "svc/http.py": "class HttpClient:\n    pass\n",
                 "tests/test_api.py": "from svc.api import paginate_results\n\ndef test_p():\n"
                                      "    paginate_results(1)\n"}
        names = exact["names_in"](task)
        self.assertEqual(self.c.exact_hits(names, files), exact["exact_hits"](names, files))
        sparse = load(LLM_CARD, "Sparse scoring (BM25) over chunks")
        rng = random.Random(73)
        vocab = ["alpha", "beta", "parseConfig", "load_file", "cache", "retry", "token",
                 "graph", "fetchPage", "http_client", "error", "index", "chunk", "score"]
        chunks = [("f%02d.py" % (i // 10), (i % 10) * 100 + 1,
                   " ".join(rng.choice(vocab) for _ in range(rng.randint(1, 30)))) for i in range(300)]
        card, dev = sparse["BM25"](chunks), self.c.BM25(chunks)
        for _ in range(60):
            query = " ".join(rng.choice(vocab + ["missing"]) for _ in range(rng.randint(1, 4)))
            want, got = card.query(query, k=15), dev.query(query, k=15)
            self.assertEqual([i for i, _ in got], [i for i, _ in want], query)
            for (_, g), (_, w) in zip(got, want):
                self.assertAlmostEqual(g, w, delta=1e-9)
            self.assertEqual(self.c.score_direct(chunks, query), dev.query(query, k=None))
        bounded = load(LLM_CARD, "Bounded graph expansion")
        for _ in range(300):
            V = rng.randint(1, 25)
            callees = dict(("m%02d" % u, sorted(set("m%02d" % rng.randrange(V)
                                                   for _ in range(rng.randint(0, 3)))))
                           for u in range(V))
            callers = {}
            for u, vs in callees.items():
                for v in vs:
                    callers.setdefault(v, []).append(u)
            targets = set(rng.sample(sorted(callees), rng.randint(1, min(3, V))))
            args = (targets, callers, callees, rng.randint(0, 2), rng.randint(1, 15), rng.randint(1, 6))
            self.assertEqual(self.c.expand(*args), bounded["expand"](*args))


if __name__ == "__main__":
    unittest.main()
