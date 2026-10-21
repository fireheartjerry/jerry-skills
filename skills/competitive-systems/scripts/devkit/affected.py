"""Changed files -> test targets, failing closed.

Rules, in order (tools/dev.json supplies the globs and maps):
1. a changed path matching `sensitive` -> full, except an untracked tools/dev.json (just
   written by `dev.py init`; it changes no code, and selection already reads it from disk);
2. a deleted or renamed-away path -> full (the graph is built from the working tree, so the
   old dependents are unknown); a deleted path that matches `ignore` is dropped instead;
3. paths matching `ignore` are dropped; nothing left -> none;
4. a changed test file selects itself;
5. a source file maps through `test_map` (exact key, then glob keys), else reverse BFS over
   the import graph to test files, else the name heuristic; no mapping -> full;
6. more than `max_changed_files` relevant paths -> full.
Granularity: Python and JS/TS test files, Go package directories (./dir, relative to the
go.mod directory), Rust crates (-p name).
"""
import os
import posixpath

from devkit import graph
from devkit.common import (CONFIG_PATH, Parser, UsageError, changed_files, first_match,
                           glob_match, load_config, note, print_json, rel_to_root, repo_root)
from devkit.detect import EXTENSIONS, LANG_ORDER, detect

SCHEMA = "devkit-affected/1"


def _under(path, root):
    return not root or path == root or path.startswith(root + "/")


def _rel(path, root):
    return path[len(root) + 1:] if root and path.startswith(root + "/") else path


def _is_test(path, lang, cfg, facts):
    if posixpath.splitext(path)[1] not in EXTENSIONS.get(lang, ()):
        return False
    rel = _rel(path, facts.get("root", ""))
    globs = cfg["test_globs"].get(lang, [])
    return any(glob_match(g, rel) or glob_match(g, path) for g in globs)


class LangGraph(object):
    """One language's dependency graph: nodes, reverse edges, test nodes, targets."""

    def __init__(self, lang, deps, tests, node_of, target_of, all_targets=False):
        self.lang = lang
        self.deps = deps
        self.rev = graph.reverse_graph(deps)
        self.tests = tests
        self.node_of = node_of          # repository path -> node, or None
        self.target_of = target_of      # node -> target string
        self.all_targets = all_targets  # every impacted node is a target (Go, Rust)


def build_graph(root, lang, info, cfg):
    """The LangGraph for a detected language, built from the working tree."""
    facts = info["langs"].get(lang, {})
    files = info["files"]
    lroot = facts.get("root", "")
    if lang == "python":
        py = [f for f in files if f.endswith(".py")]
        bases = [""] + ([lroot] if lroot else [])
        roots = graph.python_roots(py, bases)
        fileset = set(py)
        deps = dict((f, graph.python_imports(f, roots, graph.read_text(root, f), fileset))
                    for f in py)
        tests = set(f for f in py if _is_test(f, lang, cfg, facts))
        return LangGraph(lang, deps, tests, lambda p: p if p in fileset else None,
                         lambda n: n)
    if lang in ("typescript", "javascript"):
        exts = graph.JS_EXTS + (".mts", ".cts")
        js = [f for f in files if f.endswith(exts) and _under(f, lroot)]
        fileset = set(files)
        aliases = graph.tsconfig_aliases(root, facts["tsconfig"]) if facts.get("tsconfig") else []
        deps = dict((f, graph.js_imports(f, graph.read_text(root, f), fileset, aliases))
                    for f in js)
        nodes = set(deps) | set(d for ds in deps.values() for d in ds)
        tests = set(f for f in js if _is_test(f, lang, cfg, facts))
        return LangGraph(lang, deps, tests, lambda p: p if p in nodes else None, lambda n: n)
    if lang == "go":
        module = facts.get("module")
        gofiles = [f for f in files if f.endswith(".go") and _under(f, lroot)]
        pkg_dirs = set(posixpath.dirname(f) for f in gofiles)
        deps = dict((d, set()) for d in pkg_dirs)
        for f in gofiles:
            deps[posixpath.dirname(f)].update(
                graph.go_imports(f, graph.read_text(root, f), module, lroot))
        deps = dict((d, sorted(ds)) for d, ds in deps.items())

        def node_of(p):
            d = posixpath.dirname(p)
            return d if d in pkg_dirs else None

        def target_of(d):
            rel = _rel(d, lroot) if d != lroot else ""
            return "./" + rel if rel else "."

        return LangGraph(lang, deps, set(pkg_dirs), node_of, target_of, all_targets=True)
    if lang == "rust":
        crates = graph.rust_crates(root, files, lroot)
        deps = dict((name, c["deps"]) for name, c in crates.items())
        return LangGraph(lang, deps, set(crates), lambda p: graph.crate_of(p, crates),
                         lambda n: n, all_targets=True)
    return None


def _language_for(path, langs, graphs):
    """The detected language that owns a changed path, or None."""
    ext = posixpath.splitext(path)[1]
    for lang in langs:
        if ext in EXTENSIONS.get(lang, ()) and lang in ("python", "typescript", "javascript"):
            if graphs[lang].node_of(path) is not None:
                return lang
    for lang in langs:
        g = graphs.get(lang)
        if g is not None and g.node_of(path) is not None:
            return lang
    return None


def _target_language(target, langs, graphs):
    """The language a test_map target belongs to."""
    for lang in langs:
        g = graphs.get(lang)
        if g is None:
            continue
        if lang in ("python", "typescript", "javascript"):
            if posixpath.splitext(target)[1] in EXTENSIONS[lang]:
                return lang, target
        elif lang == "go" and (target.startswith("./") or target == "."):
            return lang, target
        elif lang == "rust":
            name = target[3:] if target.startswith("-p ") else target
            if name in g.deps:
                return lang, name
    return None, target


def _heuristic(path, lang, g):
    stem = posixpath.splitext(posixpath.basename(path))[0]
    if lang == "python":
        names = ("test_%s.py" % stem, "%s_test.py" % stem)
        return sorted(t for t in g.tests if posixpath.basename(t) in names)
    if lang in ("typescript", "javascript"):
        out = []
        for t in g.tests:
            b = posixpath.basename(t)
            if b.startswith(stem + ".test.") or b.startswith(stem + ".spec."):
                out.append(t)
        return sorted(out)
    return []


def normalize_changed(root, items):
    """[(status, path, old)] from git tuples or user paths (relative to the current
    directory or absolute). A user path that does not exist counts as deleted."""
    out = []
    for item in items:
        if isinstance(item, (tuple, list)):
            out.append((item[0], item[1], item[2] if len(item) > 2 else None))
            continue
        rel = rel_to_root(root, item)
        full = os.path.join(root, rel)
        if os.path.isdir(full):
            raise UsageError("%s is a directory; pass files" % item)
        out.append(("M" if os.path.exists(full) else "D", rel, None))
    return out


def affected(root, changed, cfg=None, info=None, graphs=None):
    """The selection for changed [(status, path, old)]: a dict with mode (selected | full |
    none), reason, targets {lang: [...]}, args {lang: argv tokens}, chains {lang: {target:
    [target, ..., changed path]}}, changed and ignored lists."""
    cfg = cfg if cfg is not None else load_config(root)
    info = info if info is not None else detect(root, cfg)
    langs = list(info["languages"])
    result = {"schema": SCHEMA, "mode": "none", "reason": "", "languages": langs,
              "changed": [{"status": s, "path": p, "old_path": o} for s, p, o in changed],
              "ignored": [], "targets": {}, "args": {}, "chains": {}, "notes": []}

    def full(reason):
        result.update(mode="full", reason=reason, targets={}, args={}, chains={})
        return result

    if not changed:
        result["reason"] = "no changed files"
        return result
    fresh_config = [(s, p, o) for s, p, o in changed if s == "?" and p == CONFIG_PATH]
    if fresh_config:
        result["notes"].append("%s is new and untracked, so it does not force a full run; "
                               "commit it" % CONFIG_PATH)
        result["ignored"].append(CONFIG_PATH)
        changed = [c for c in changed if c not in fresh_config]
        if not changed:
            result["reason"] = "only ignored paths changed"
            return result
    for status, path, _ in changed:
        if first_match(cfg["sensitive"], path):
            return full("sensitive file: %s" % path)
    relevant = []
    for status, path, _ in changed:
        if first_match(cfg["ignore"], path):
            result["ignored"].append(path)
            continue
        if status == "D":
            return full("deleted or renamed: %s" % path)
        relevant.append(path)
    if not relevant:
        result["reason"] = "only ignored paths changed"
        return result
    graphs = graphs if graphs is not None else dict(
        (lang, build_graph(root, lang, info, cfg)) for lang in langs)
    graphs = dict((k, v) for k, v in graphs.items() if v is not None)
    chains = {}             # lang -> target -> chain

    def select(lang, target, path_chain):
        best = chains.setdefault(lang, {}).get(target)
        if best is None or (len(path_chain), path_chain) < (len(best), best):
            chains[lang][target] = path_chain

    test_map = dict((k, v) for k, v in cfg.get("test_map", {}).items() if not k.startswith("_doc"))
    for path in sorted(set(relevant)):
        mapped = test_map.get(path)
        if mapped is None:
            hits = [k for k in sorted(test_map) if k != path and glob_match(k, path)]
            if hits:
                mapped = sorted(set(t for k in hits for t in test_map[k]))
        if mapped is not None:
            for target in mapped:
                lang, t = _target_language(target, langs, graphs)
                if lang is None:
                    return full("test_map target %s for %s matches no detected language"
                                % (target, path))
                select(lang, t, [t, path + " (changed; test_map)"])
            continue
        lang = _language_for(path, langs, graphs)
        if lang is None:
            return full("unmapped source file: %s" % path)
        g = graphs[lang]
        node = g.node_of(path)
        if g.all_targets:
            parent = graph.impact_parents(g.rev, [node])
            for n in parent:
                c = [g.target_of(x) if lang == "go" else x for x in graph.chain(parent, n)]
                select(lang, g.target_of(n), c + [path + " (changed)"])
            continue
        parent = graph.impact_parents(g.rev, [node])
        reached = sorted(n for n in parent if n in g.tests)
        if reached:
            for t in reached:
                c = graph.chain(parent, t)
                select(lang, g.target_of(t), c[:-1] + [c[-1] + " (changed)"])
            continue
        named = _heuristic(path, lang, g)
        if named:
            for t in named:
                select(lang, g.target_of(t), [t, path + " (changed; name match)"])
            continue
        return full("unmapped source file: %s" % path)
    limit = int(cfg.get("max_changed_files", 25))
    if len(set(relevant)) > limit:
        return full("%d changed files exceed max_changed_files (%d)" % (len(set(relevant)), limit))
    result["mode"] = "selected"
    result["reason"] = "%d target(s) from %d changed file(s)" % (
        sum(len(v) for v in chains.values()), len(set(relevant)))
    for lang in [l for l in LANG_ORDER if l in chains]:
        targets = sorted(chains[lang])
        result["targets"][lang] = targets
        result["chains"][lang] = dict((t, chains[lang][t]) for t in targets)
        if lang == "rust":
            result["args"][lang] = [tok for t in targets for tok in ("-p", t)]
        else:
            result["args"][lang] = list(targets)
    return result


def target_line(lang, res):
    return " ".join(("-p " + t) if lang == "rust" else t for t in res["targets"][lang])


def print_result(res, explain=False):
    if res["mode"] != "selected":
        print("%s: %s" % (res["mode"], res["reason"]))
        if explain:
            for c in res["changed"]:
                tag = " (ignored)" if c["path"] in res["ignored"] else ""
                print("  %s %s%s" % (c["status"], c["path"], tag))
        return
    langs = list(res["targets"])
    for lang in langs:
        line = target_line(lang, res)
        print(("%s: %s" % (lang, line)) if len(langs) > 1 else line)
    if explain:
        for lang in langs:
            for t, c in sorted(res["chains"][lang].items()):
                print("  " + " <- ".join(c))
        if res["ignored"]:
            print("  ignored: %s" % " ".join(res["ignored"]))


def gather_changed(root, files, base):
    """Changed entries from explicit files or git; UsageError when neither is available."""
    if files:
        return normalize_changed(root, files)
    rc_git = changed_files(root, base)
    if not rc_git:
        from devkit.common import run_quiet
        rc, out = run_quiet(["git", "-C", root, "rev-parse", "--is-inside-work-tree"])
        if rc != 0 or out.strip() != "true":
            raise UsageError("no git: pass files explicitly")
    return rc_git


def cmd_affected(args):
    p = Parser(prog="dev.py affected",
               description="Changed files to test targets; fails closed to a full run.")
    p.add_argument("files", nargs="*", help="changed files (default: git diff against HEAD "
                                            "plus untracked files)")
    p.add_argument("--base", default=None, help="diff REF...HEAD plus the working tree")
    p.add_argument("--explain", action="store_true", help="print the chain behind each target")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    if a.files and a.base:
        raise UsageError("pass FILES or --base, not both")
    changed = gather_changed(root, a.files, a.base)
    res = affected(root, changed)
    if a.json:
        print_json(res)
    else:
        print_result(res, a.explain)
        for n in res["notes"]:
            note(n)
    return 0
