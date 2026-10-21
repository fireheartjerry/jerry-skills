"""Import graphs per language and the shared graph and selection algorithms.

Everything here is pure and deterministic (sorted iteration, fixed tie-breaks) except the
thin readers that load a file's text. The algorithms are the card sketches, kept in one
tested place; tests/test_sketches.py asserts they agree with the cards on random instances.

Graph convention: deps maps node -> nodes it depends on (imports). reverse_graph turns that
into node -> nodes that depend on it, which is what impact analysis walks.
"""
import ast
import heapq
import json
import os
import posixpath
import re
from collections import deque

from devkit.common import glob_regex

EXACT_SEARCH_MAX = 18        # candidates; the largest n whose 2^(n+1) nodes fit NODE_BUDGET
NODE_BUDGET = 1_000_000      # deterministic cutoff for exact search

JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


class CycleError(ValueError):
    """topo_order found a cycle; .cycle is one cycle as a node list (first node repeated)."""

    def __init__(self, cycle):
        self.cycle = list(cycle)
        ValueError.__init__(self, "cycle: %s" % " -> ".join(str(n) for n in self.cycle))


# ---------------------------------------------------------------- graph algorithms


def reverse_graph(deps):
    """node -> [nodes that depend on it]; every node appears as a key."""
    rev = {}
    for node, ds in deps.items():
        rev.setdefault(node, [])
        for d in ds:
            rev.setdefault(d, []).append(node)
    return rev


def impact_set(rev, changed):
    """Every node reachable from changed in rev (BFS), including the changed nodes."""
    seen, q = set(changed), deque(changed)
    while q:
        u = q.popleft()
        for v in rev.get(u, ()):
            if v not in seen:
                seen.add(v)
                q.append(v)
    return seen


def impact_parents(rev, changed):
    """Multi-source BFS over rev from sorted(changed): {node: parent toward a changed node}.

    Changed nodes map to None. Neighbours are visited in sorted order, so every chain is a
    shortest one and the result is the same on every run.
    """
    parent = dict((c, None) for c in sorted(changed))
    q = deque(sorted(changed))
    while q:
        u = q.popleft()
        for v in sorted(rev.get(u, ())):
            if v not in parent:
                parent[v] = u
                q.append(v)
    return parent


def chain(parent, node):
    """[node, ..., changed node] following impact_parents."""
    out = [node]
    while parent.get(out[-1]) is not None:
        out.append(parent[out[-1]])
    return out


def _find_cycle(deps, left):
    """One cycle among the leftover nodes of Kahn's algorithm, as [a, b, ..., a]."""
    left = set(left)
    start = min(left)
    path, index = [], {}
    u = start
    while u not in index:
        index[u] = len(path)
        path.append(u)
        u = min(d for d in deps.get(u, ()) if d in left)
    return path[index[u]:] + [u]


def topo_order(deps):
    """Kahn's algorithm, smallest ready node first; CycleError names one cycle."""
    nodes = set(deps)
    for ds in deps.values():
        nodes.update(ds)
    indeg = dict((u, 0) for u in nodes)
    users = dict((u, []) for u in nodes)
    for u, ds in deps.items():
        for d in ds:
            indeg[u] += 1
            users[d].append(u)
    ready = [u for u in nodes if indeg[u] == 0]
    heapq.heapify(ready)
    out = []
    while ready:
        u = heapq.heappop(ready)
        out.append(u)
        for w in sorted(users[u]):
            indeg[w] -= 1
            if indeg[w] == 0:
                heapq.heappush(ready, w)
    if len(out) != len(nodes):
        raise CycleError(_find_cycle(deps, [u for u in nodes if indeg[u] > 0]))
    return out


class DSU:
    """Union-find with path halving and union by size."""

    def __init__(self, n):
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
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


# ---------------------------------------------------------------- scheduling and selection


def lpt(jobs, m, pinned=()):
    """LPT: longest job first to the least loaded worker; pinned names share worker 0.

    jobs: [(duration, name)]. Ties: equal durations by name, equal loads by worker index.
    Returns [(load, [names])] by worker index.
    """
    heap = [(0.0, w, []) for w in range(m)]
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


def lower_bound(jobs, m, pinned=()):
    """Makespan lower bound: max(total/m, longest job, pinned total)."""
    total = sum(d for d, _ in jobs)
    longest = max((d for d, _ in jobs), default=0.0)
    pinned_total = sum(d for d, n in jobs if n in pinned)
    return max(total / m, longest, pinned_total)


def knapsack(items, B):
    """0/1 knapsack by DP. items: [(name, value, int_cost)], additive value.

    Returns (sorted chosen names, best value); ties resolved by the name order.
    """
    items = sorted(items)
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


def greedy_submodular(items, value, B):
    """Better of cost-ratio greedy and the best single item; value(list of items) -> float."""
    chosen, spent, cur, remaining = [], 0, value([]), sorted(items)
    while True:
        scored = [((value(chosen + [it]) - cur) / it[2], it)
                  for it in remaining if spent + it[2] <= B]
        if not scored:
            break
        _, pick = max(scored, key=lambda s: (s[0], s[1][0]))
        chosen.append(pick)
        spent += pick[2]
        remaining.remove(pick)
        cur = value(chosen)
    single = min((it for it in items if it[2] <= B),
                 key=lambda it: (-value([it]), it[0]), default=None)
    if single is not None and value([single]) > cur:
        return [single]
    return chosen


def exact_best(items, value, B):
    """Exact subset search with a node budget: (chosen, value), or None past NODE_BUDGET."""
    best, nodes, stack = ([], value([])), 0, [(0, [], 0)]
    while stack:
        i, chosen, spent = stack.pop()
        nodes += 1
        if nodes > NODE_BUDGET:
            return None
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


# ---------------------------------------------------------------- file helpers


def read_text(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _norm(path):
    path = posixpath.normpath(path)
    return "" if path == "." else path


# ---------------------------------------------------------------- python


class ModuleIndex(dict):
    """dotted module name -> file, with .by_file for the inverse lookup."""

    def __init__(self, *a):
        dict.__init__(self, *a)
        self.by_file = {}


def python_roots(files, bases=("",)):
    """Dotted module name -> repository file, for first-party code.

    For each base directory (the repository root, a Python project root, and their `src/`
    layouts), a top-level directory with `__init__.py` is a package root (every .py file
    below it is indexed, namespace sub-directories included) and a top-level .py file is a
    module. The first base that defines a name wins.
    """
    fileset = set(files)
    roots = ModuleIndex()
    seen_bases = []
    for base in bases:
        for b in (base, posixpath.join(base, "src") if base else "src"):
            b = _norm(b)
            if b not in seen_bases:
                seen_bases.append(b)
    for b in seen_bases:
        prefix = b + "/" if b else ""
        packages = set()
        for f in files:
            if not f.endswith(".py") or not f.startswith(prefix):
                continue
            rest = f[len(prefix):]
            parts = rest.split("/")
            if len(parts) == 1:
                roots.setdefault(parts[0][:-3], f)
            elif prefix + parts[0] + "/__init__.py" in fileset:
                packages.add(parts[0])
        for f in files:
            if not f.endswith(".py") or not f.startswith(prefix):
                continue
            parts = f[len(prefix):][:-3].split("/")
            if len(parts) < 2 or parts[0] not in packages:
                continue
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if all(p.isidentifier() for p in parts):
                roots.setdefault(".".join(parts), f)
    for name in sorted(roots, key=lambda k: (k.count("."), k)):
        roots.by_file.setdefault(roots[name], name)
    roots.fileset = fileset
    return roots


def _module_of(path, roots):
    """The dotted name roots gives to path, or None."""
    by_file = getattr(roots, "by_file", None)
    if by_file is not None:
        return by_file.get(path)
    for name, f in sorted(roots.items()):
        if f == path:
            return name
    return None


def _python_names(text):
    """[(level, module or '', [imported names])] from source text; regex fallback when the
    file does not parse."""
    out = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for m in re.finditer(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+([\w*, ()]+)", text, re.M):
            names = [n.strip() for n in m.group(3).replace("(", "").replace(")", "").split(",")]
            out.append((len(m.group(1)), m.group(2), [n.split()[0] for n in names if n]))
        for m in re.finditer(r"^\s*import\s+([\w., ]+)", text, re.M):
            for part in m.group(1).split(","):
                if part.strip():
                    out.append((0, part.split()[0], []))
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((0, alias.name, []))
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level or 0, node.module or "", [a.name for a in node.names]))
        elif isinstance(node, ast.Call) and node.args:
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            arg = node.args[0]
            if name in ("import_module", "__import__") and isinstance(arg, ast.Constant) \
                    and isinstance(arg.value, str):
                out.append((0, arg.value, []))
    return out


def python_imports(path, roots, text, fileset=None):
    """Repository files that `path` imports, resolved through roots (see python_roots).

    `import a.b` depends on a/__init__.py, a/b/__init__.py or a/b.py; `from a import b`
    on a/b.py when it exists, else on a/__init__.py; relative imports resolve from the
    package of path. A name that roots does not know is tried next to the importing file
    (a sibling helper module, as test runners put the test directory on sys.path; needs
    fileset). Third-party and standard-library names resolve to nothing.
    """
    deps = set()
    own = _module_of(path, roots)
    is_pkg = path.endswith("/__init__.py")
    fileset = fileset if fileset is not None else getattr(roots, "fileset", None)
    here = posixpath.dirname(path)

    def sibling(dotted):
        if not fileset:
            return None
        rel = dotted.replace(".", "/")
        for cand in (rel + ".py", rel + "/__init__.py"):
            f = posixpath.join(here, cand) if here else cand
            if f in fileset:
                return f
        return None

    def add_module(dotted):
        """Add the file for dotted and every parent package's __init__; True if found."""
        parts = dotted.split(".")
        found = False
        for i in range(1, len(parts) + 1):
            f = roots.get(".".join(parts[:i]))
            if f is not None:
                deps.add(f)
                found = found or i == len(parts)
        if not found and roots.get(parts[0]) is None:
            f = sibling(dotted)
            if f is not None:
                deps.add(f)
                found = True
        return found

    for level, module, names in _python_names(text):
        if level:
            if own is None:
                continue
            pkg = own.split(".") if is_pkg else own.split(".")[:-1]
            if level - 1 > len(pkg):
                continue
            pkg = pkg[:len(pkg) - (level - 1)]
            module = ".".join(pkg + ([module] if module else []))
            if not module:
                continue
        if not add_module(module):
            continue
        for name in names:
            if name != "*":
                add_module(module + "." + name)
    deps.discard(path)
    return sorted(deps)


# ---------------------------------------------------------------- javascript / typescript

_JS_SPEC = re.compile(
    r"""(?:\bimport\s+(?:type\s+)?[\w$*{}\s,]*?\s*from\s*|\bexport\s+(?:type\s+)?[\w$*{}\s,]*?\s*from\s*"""
    r"""|\bimport\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)(['"])([^'"\n]+)\1""")


def strip_jsonc(text):
    """JSON with // and /* */ comments and trailing commas -> plain JSON text."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def tsconfig_aliases(root, rel="tsconfig.json"):
    """[(key, wildcard, [targets])] from compilerOptions.paths, targets relative to the
    repository. Supports the `"@/*": ["src/*"]` form (one trailing `*`) and exact names;
    `baseUrl` defaults to the tsconfig's directory. Unreadable or invalid files give [].
    Longest keys first, so the most specific alias wins."""
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            data = json.loads(strip_jsonc(f.read()))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    opts = data.get("compilerOptions") or {}
    paths = opts.get("paths") or {}
    base = posixpath.join(posixpath.dirname(rel), opts.get("baseUrl") or ".")
    out = []
    for key in sorted(paths, key=lambda k: (-len(k), k)):
        targets = [t for t in (paths[key] if isinstance(paths[key], list) else [])
                   if isinstance(t, str)]
        if key.endswith("*"):
            out.append((key[:-1], True, [posixpath.join(base, t[:-1]) for t in targets
                                         if t.endswith("*")]))
        elif "*" not in key:
            out.append((key, False, [posixpath.join(base, t) for t in targets]))
    return out


def _resolve_js(candidate, fileset):
    candidate = _norm(candidate)
    if candidate in fileset:
        return candidate
    stem, ext = posixpath.splitext(candidate)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):            # ESM style './a.js' for a.ts
        for alt in (".ts", ".tsx", ".mts", ".cts"):
            if stem + alt in fileset:
                return stem + alt
    for e in JS_EXTS:
        if candidate + e in fileset:
            return candidate + e
    for e in JS_EXTS:
        f = posixpath.join(candidate, "index" + e) if candidate else "index" + e
        if f in fileset:
            return f
    return None


def js_specifiers(text):
    """Every module specifier in import/export-from, bare import, require() and import()."""
    return [m.group(2) for m in _JS_SPEC.finditer(text)]


def js_imports(path, text, fileset, aliases=()):
    """Repository files that `path` imports. Relative specifiers resolve with the JS/TS
    extensions and /index files; alias prefixes (tsconfig paths) map to repository
    directories; bare package specifiers are ignored."""
    deps = set()
    here = posixpath.dirname(path)
    for spec in js_specifiers(text):
        spec = spec.split("?", 1)[0]
        if spec.startswith("./") or spec.startswith("../") or spec in (".", ".."):
            hit = _resolve_js(posixpath.join(here, spec), fileset)
            if hit:
                deps.add(hit)
            continue
        for key, wildcard, targets in aliases:
            if not (spec.startswith(key) if wildcard else spec == key):
                continue
            rest = spec[len(key):] if wildcard else ""
            for t in targets:
                hit = _resolve_js(t + rest, fileset)
                if hit:
                    deps.add(hit)
                    break
            break
    deps.discard(path)
    return sorted(deps)


# ---------------------------------------------------------------- go

_GO_BLOCK = re.compile(r"^\s*import\s*\((.*?)\)", re.S | re.M)
_GO_SINGLE = re.compile(r'^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_GO_LINE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)


def go_module(root, rel="go.mod"):
    """The module path declared in go.mod, or None."""
    text = read_text(root, rel)
    m = re.search(r"^\s*module\s+(\S+)", text, re.M)
    return m.group(1).strip('"') if m else None


def go_import_paths(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    paths = [m.group(1) for m in _GO_SINGLE.finditer(text)]
    for block in _GO_BLOCK.finditer(text):
        paths.extend(m.group(1) for m in _GO_LINE.finditer(block.group(1)))
    return paths


def go_imports(path, text, module_path, module_dir=""):
    """Package directories (repository-relative) that the file at path imports from its own
    module. module_dir is where go.mod lives ('' for the root)."""
    if not module_path:
        return []
    deps = set()
    own = posixpath.dirname(path)
    for imp in go_import_paths(text):
        if imp == module_path or imp.startswith(module_path + "/"):
            sub = imp[len(module_path):].lstrip("/")
            d = _norm(posixpath.join(module_dir, sub)) if module_dir else _norm(sub)
            if d != own:
                deps.add(d)
    return sorted(deps)


# ---------------------------------------------------------------- rust and a tiny TOML subset


def _toml_value(text, i):
    """Parse one value at text[i:]; returns (value, next index). Strings, numbers,
    booleans, arrays (multi-line) and inline tables; enough for Cargo.toml dependency and
    workspace tables."""
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if i >= n:
        return None, i
    c = text[i]
    if text.startswith('"""', i) or text.startswith("'''", i):
        q = text[i:i + 3]
        j = text.find(q, i + 3)
        j = n if j == -1 else j
        return text[i + 3:j], j + 3
    if c in "\"'":
        j = i + 1
        buf = []
        while j < n and text[j] != c:
            if c == '"' and text[j] == "\\" and j + 1 < n:
                buf.append(text[j + 1])
                j += 2
                continue
            buf.append(text[j])
            j += 1
        return "".join(buf), j + 1
    if c == "[":
        items, j = [], i + 1
        while j < n:
            while j < n and text[j] in " \t\r\n,":
                j += 1
            if j < n and text[j] == "#":
                j = text.find("\n", j)
                j = n if j == -1 else j
                continue
            if j < n and text[j] == "]":
                return items, j + 1
            v, j = _toml_value(text, j)
            items.append(v)
        return items, j
    if c == "{":
        table, j = {}, i + 1
        while j < n:
            while j < n and text[j] in " \t,":
                j += 1
            if j < n and text[j] == "}":
                return table, j + 1
            m = re.compile(r'\s*("[^"]*"|[\w.-]+)\s*=').match(text, j)
            if not m:
                return table, n
            key = m.group(1).strip('"')
            v, j = _toml_value(text, m.end())
            table[key] = v
        return table, j
    m = re.compile(r"[^\s,\]}#]+").match(text, i)
    tok = m.group(0) if m else ""
    if tok in ("true", "false"):
        return tok == "true", i + len(tok)
    try:
        return (float(tok) if "." in tok else int(tok)), i + len(tok)
    except ValueError:
        return tok, i + len(tok)


def parse_toml(text):
    """A small TOML subset: {table name: {key: value}}; top-level keys under ''. Dotted
    table headers stay as one string key ('dependencies.core'). Unparseable lines are
    skipped, so callers must treat a missing key as unknown."""
    out = {"": {}}
    cur = out[""]
    i, n = 0, len(text)
    header = re.compile(r"[ \t]*\[\[?([^\]\n]+)\]\]?[ \t]*(?:#[^\n]*)?(?:\n|$)")
    keyval = re.compile(r'[ \t]*("[^"\n]*"|[\w.-]+)[ \t]*=')
    while i < n:
        m = header.match(text, i)
        if m:
            name = ".".join(p.strip().strip('"') for p in m.group(1).split("."))
            cur = out.setdefault(name, {})
            i = m.end()
            continue
        m = keyval.match(text, i)
        if m:
            v, i = _toml_value(text, m.end())
            key = m.group(1)
            if key.startswith('"'):
                cur[key.strip('"')] = v
            else:                       # dotted keys nest: core.workspace = true
                parts = key.split(".")
                node = cur
                for part in parts[:-1]:
                    node = node.setdefault(part, {}) if isinstance(node.get(part, {}), dict) else {}
                node[parts[-1]] = v
        j = text.find("\n", i)
        i = n if j == -1 else j + 1
    return out


def _crate_at(root, rel_dir):
    """(name, {dependency key: path}, [keys inherited with `workspace = true`]) for the
    package manifest at rel_dir, or None."""
    manifest = posixpath.join(rel_dir, "Cargo.toml") if rel_dir else "Cargo.toml"
    if not os.path.isfile(os.path.join(root, manifest)):
        return None
    t = parse_toml(read_text(root, manifest))
    name = (t.get("package") or {}).get("name")
    if not isinstance(name, str):
        return None
    deps, inherited = {}, []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        specs = list((t.get(section) or {}).items())
        specs += [(table[len(section) + 1:], body) for table, body in t.items()
                  if table.startswith(section + ".")]
        for key, spec in specs:
            if not isinstance(spec, dict):
                continue
            if isinstance(spec.get("path"), str):
                deps[key] = spec["path"]
            elif spec.get("workspace") is True:
                inherited.append(key)
    return name, deps, inherited


def rust_crates(root, files=None, ws_dir=""):
    """{crate name: {"dir": relative dir, "deps": sorted crate names}} for a Cargo
    workspace (members globs, `*` within one segment) and/or a root package, with path
    dependencies as edges. Dependencies outside the workspace are ignored."""
    manifest = posixpath.join(ws_dir, "Cargo.toml") if ws_dir else "Cargo.toml"
    t = parse_toml(read_text(root, manifest))
    dirs = []
    if _crate_at(root, ws_dir):
        dirs.append(ws_dir)
    ws = t.get("workspace") or {}
    excluded = set(_norm(posixpath.join(ws_dir, e)) for e in ws.get("exclude") or []
                   if isinstance(e, str))
    manifests = None
    for pattern in ws.get("members") or []:
        if not isinstance(pattern, str):
            continue
        full = _norm(posixpath.join(ws_dir, pattern))
        if any(ch in full for ch in "*?["):
            if manifests is None:
                if files is None:
                    from devkit.common import DEFAULT_CONFIG, walk_files
                    files = walk_files(root, DEFAULT_CONFIG["exclude_dirs"])
                manifests = sorted(set(posixpath.dirname(f) for f in files
                                       if f.endswith("/Cargo.toml")))
            rx = glob_regex(full)
            dirs.extend(d for d in manifests if rx.match(d))
        else:
            dirs.append(full)
    crates = {}
    by_dir = {}
    for d in dirs:
        rel = _norm(d)
        if rel in by_dir or rel in excluded:
            continue
        found = _crate_at(root, rel)
        if found is None:
            continue
        name, deps, inherited = found
        by_dir[rel] = name
        paths = dict((k, _norm(posixpath.join(rel, v))) for k, v in deps.items())
        shared = t.get("workspace.dependencies") or {}
        for key in inherited:
            spec = shared.get(key)
            if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                paths[key] = _norm(posixpath.join(ws_dir, spec["path"]))
        crates[name] = {"dir": rel, "deps_paths": paths}
    for name, info in crates.items():
        info["deps"] = sorted(set(by_dir[p] for p in info.pop("deps_paths").values()
                                  if p in by_dir and by_dir[p] != name))
    return crates


def crate_of(path, crates):
    """The crate whose directory contains path (longest match), or None."""
    best, best_len = None, -1
    for name, info in crates.items():
        d = info["dir"]
        if (not d or path == d or path.startswith(d + "/")) and len(d) > best_len:
            best, best_len = name, len(d)
    return best
