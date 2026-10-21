"""Repository map and navigation: `map`, `outline FILE`, `where SYMBOL`.

Definitions come from `ast` for Python and from line regexes for JS/TS, Go and Rust.
Imports and importers are file-level, from devkit.graph (Python and JS/TS through
devkit.affected.build_graph; Go by package directory; Rust by `mod` declarations and path
dependencies); `tests` per file is the devkit.affected selection for a change to that file.
Only `.devkit/repo-map.json` is ever written; source files are read, never modified.
"""
import ast
import hashlib
import json
import os
import posixpath
import re

from devkit.common import (DevError, Parser, UsageError, devkit_dir, glob_match, load_config,
                           note, print_json, rel_to_root, repo_root, to_posix)

SCHEMA = "devkit-map/1"
MAP_FILE = "repo-map.json"
SIG_MAX = 160
MAX_BYTES = 1000000           # larger files are mapped without definitions
LANG_BY_EXT = {".py": "python", ".ts": "typescript", ".tsx": "typescript",
               ".mts": "typescript", ".cts": "typescript", ".js": "javascript",
               ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
               ".go": "go", ".rs": "rust"}
SECTION = re.compile(r"^\s*(?:#|//)\s*[=\-~*#]{4,}\s*(.*?)\s*[=\-~*#]*\s*$")


def lang_of_file(path):
    return LANG_BY_EXT.get(posixpath.splitext(path)[1])


def _sig(text):
    """One-line signature: whitespace collapsed, a trailing `{` or one-line body cut."""
    s = " ".join(text.split())
    s = re.sub(r"\s*\{\s*$", "", s)
    m = re.search(r"\s\{\s.*\}\s*;?$", s)
    if m:
        s = s[:m.start()]
    s = s.rstrip(":; ")
    return s if len(s) <= SIG_MAX else s[:SIG_MAX - 3] + "..."


def _defn(name, kind, line, signature, summary="", depth=0, exported=False, start=None):
    return {"name": name, "kind": kind, "line": line, "start": start or line, "end": line,
            "signature": signature, "summary": summary, "depth": depth, "exported": exported}


def _comment_text(line):
    s = line.strip()
    for mark in ("///", "//!", "//", "/**", "/*", "*/", "*", "#"):
        if s.startswith(mark):
            s = s[len(mark):]
            break
    return s.replace("*/", "").strip()


def _leading(lines, line, comment_marks, attr_marks):
    """(first line of the comment/attribute block directly above `line` (1-based) at the
    same indentation, its first comment text). A blank line ends the block."""
    target = lines[line - 1] if 0 < line <= len(lines) else ""
    indent = target[:len(target) - len(target.lstrip())]
    i, summary = line - 1, ""
    while i >= 1:
        raw = lines[i - 1]
        s = raw.strip()
        if raw[:len(raw) - len(raw.lstrip())] != indent:
            break
        if s.startswith(comment_marks):
            text = _comment_text(s)
            if text:
                summary = text
            i -= 1
        elif attr_marks and s.startswith(attr_marks):
            i -= 1
        else:
            break
    return i + 1, summary


# ---------------------------------------------------------------- python


def _expr(node):
    if hasattr(ast, "unparse"):
        return ast.unparse(node)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _expr(node.value) + "." + node.attr
    return "..."


def _py_args(args):
    if hasattr(ast, "unparse"):
        return ast.unparse(args)
    names = []                           # Python 3.8: argument names only
    posonly = getattr(args, "posonlyargs", [])
    names += [a.arg for a in posonly] + (["/"] if posonly else [])
    names += [a.arg for a in args.args]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        names.append("*")
    names += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return ", ".join(names)


def _py_def(node, lines, depth, prefix):
    if isinstance(node, ast.ClassDef):
        bases = [_expr(b) for b in node.bases]
        sig = "class %s(%s)" % (node.name, ", ".join(bases)) if bases else "class " + node.name
        kind = "class"
    else:
        sig = "%sdef %s(%s)" % ("async " if isinstance(node, ast.AsyncFunctionDef) else "",
                                node.name, _py_args(node.args))
        if node.returns is not None and hasattr(ast, "unparse"):
            sig += " -> " + _expr(node.returns)
        kind = "method" if depth else "function"
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    start, comment = _leading(lines, start, ("#",), ())
    doc = ast.get_docstring(node)
    summary = doc.strip().splitlines()[0].strip() if doc and doc.strip() else comment
    d = _defn(prefix + node.name, kind, node.lineno, _sig(sig), summary, depth,
              start=start)
    d["end"] = getattr(node, "end_lineno", None) or node.lineno
    return d


def _py_all(tree):
    names = None
    for node in tree.body:
        value, add = None, False
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__"
                                                for t in node.targets):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "__all__":
            value = node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "__all__" and isinstance(node.op, ast.Add):
            value, add = node.value, True
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        found = [e.value for e in value.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        names = (names or []) + found if add else found
    return names


def python_symbols(text):
    """(definitions, exports, module summary) for Python source."""
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        defs = _regex_defs(lines, "python")
        return defs, [d for d in defs if d["depth"] == 0 and not d["name"].startswith("_")], ""
    defs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append(_py_def(node, lines, 0, ""))
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defs.append(_py_def(child, lines, 1, node.name + "."))
    doc = ast.get_docstring(tree)
    module_summary = doc.strip().splitlines()[0].strip() if doc and doc.strip() else ""
    top = [d for d in defs if d["depth"] == 0]
    all_names = _py_all(tree)
    if all_names is None:
        exports = [d for d in top if not d["name"].startswith("_")]
    else:
        by_name = dict((d["name"], d) for d in top)
        others = {}
        for node in tree.body:
            src = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        others.setdefault(t.id, ("variable", node.lineno, src))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                others.setdefault(node.target.id, ("variable", node.lineno, src))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    name = a.asname or a.name.split(".")[0]
                    others.setdefault(name, ("reexport", node.lineno, src))
        exports, seen = [], set()
        for name in all_names:
            if name in seen:
                continue
            seen.add(name)
            if name in by_name:
                exports.append(by_name[name])
            elif name in others:
                kind, line, src = others[name]
                exports.append(_defn(name, kind, line, _sig(src)))
    for d in exports:
        d["exported"] = True
    exports = sorted(exports, key=lambda d: (d["line"], d["name"]))
    return defs, exports, module_summary


# ---------------------------------------------------------------- regex languages

PY_DEF = re.compile(r"^(?P<indent>\s*)(?P<async>async\s+)?(?P<kind>def|class)\s+(?P<name>[A-Za-z_]\w*)")
JS_DEF = re.compile(
    r"^(?P<export>export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?(?P<async>async\s+)?"
    r"(?P<kind>(?:function|class|interface|type|const\s+enum|enum|const|let|var|namespace)\b)"
    r"\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)(?P<rest>.*)")
JS_DEFAULT = re.compile(r"^export\s+default\s+(?:async\s+)?(?P<kind>function|class)\b\s*\*?\s*[({]")
JS_FUNC_VALUE = re.compile(r"^\s*(?::[^=]+)?=\s*(?:async\b\s*)?(?:function\b|class\b|\([^)]*\)\s*"
                           r"(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)")
JS_DEFAULT_EXPR = re.compile(r"^export\s+default\s+(?!(?:async\s+)?(?:function|class)\b)\S")
JS_CJS = re.compile(r"^(?:module\.)?exports\.(?P<name>[A-Za-z_$][\w$]*)\s*=")
JS_BRACES = re.compile(r"^export\s+(?:type\s+)?\{(?P<names>[^}]*)\}\s*(?:from\s*['\"](?P<src>[^'\"]+)['\"])?",
                       re.M)
JS_STAR = re.compile(r"^export\s+\*\s+(?:as\s+(?P<name>[A-Za-z_$][\w$]*)\s+)?from\b", re.M)
GO_FUNC = re.compile(r"^func\s+(?:\(\s*(?:[A-Za-z_]\w*\s+)?\*?\s*(?P<recv>[A-Za-z_]\w*)"
                     r"(?:\[[^\]]*\])?\s*\)\s*)?(?P<name>[A-Za-z_]\w*)")
GO_TYPE = re.compile(r"^type\s+(?P<name>[A-Za-z_]\w*)\b")
GO_VAR = re.compile(r"^(?P<kind>var|const)\s+(?P<name>[A-Za-z_]\w*)\b")
RS_QUAL = r"(?:(?:async|unsafe|default|extern(?:\s+\"[^\"]*\")?)\s+)*"
RS_ITEM = re.compile(r"^(?P<pub>pub(?:\s*\((?P<scope>[^)]*)\))?\s+)?" + RS_QUAL +
                     r"(?:const\s+(?=fn\b))?(?P<kind>fn|struct|enum|trait|type|mod|union|const|static)"
                     r"\s+(?:mut\s+)?(?P<name>[A-Za-z_]\w*)")
RS_MACRO = re.compile(r"^macro_rules!\s*(?P<name>[A-Za-z_]\w*)")
RS_IMPL = re.compile(r"^(?:unsafe\s+)?impl\b(?:\s*<[^{]*?>)?\s+(?:(?P<trait>[^{]+?)\s+for\s+)?"
                     r"(?P<ty>[A-Za-z_][\w:]*)")
RS_METHOD = re.compile(r"^\s+(?P<pub>pub(?:\s*\((?P<scope>[^)]*)\))?\s+)?" + RS_QUAL +
                       r"(?:const\s+)?fn\s+(?P<name>[A-Za-z_]\w*)")
COMMENTS = {"python": ("#",), "go": ("//", "/*", "*"), "rust": ("//", "/*", "*"),
            "typescript": ("//", "/*", "*"), "javascript": ("//", "/*", "*")}
ATTRS = {"python": ("@",), "go": (), "rust": ("#[",), "typescript": ("@",),
         "javascript": ("@",)}


def _regex_defs(lines, lang):
    """Definitions by line regexes; top level at column 0, plus Python class methods and
    Rust impl methods at depth 1."""
    defs, owner = [], None
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        found = None
        if lang == "python":
            m = PY_DEF.match(line)
            if m and not m.group("indent"):
                kind = "class" if m.group("kind") == "class" else "function"
                found = (m.group("name"), kind, 0, not m.group("name").startswith("_"))
                owner = m.group("name") if kind == "class" else None
            elif m and owner and m.group("kind") == "def":
                found = (owner + "." + m.group("name"), "method", 1, False)
            elif not line[0].isspace() and not line.lstrip().startswith(("#", "@")):
                owner = None
        elif lang in ("typescript", "javascript"):
            m = JS_DEF.match(line)
            if m:
                kind = m.group("kind").split()[-1]
                kind = "function" if kind == "function" else kind
                exported = bool(m.group("export"))
                if kind in ("const", "let", "var") and not exported:
                    if not JS_FUNC_VALUE.match(m.group("rest")):
                        continue
                    kind = "function"
                found = (m.group("name"), kind, 0, exported)
            elif JS_DEFAULT.match(line):
                found = ("default", JS_DEFAULT.match(line).group("kind"), 0, True)
            elif JS_DEFAULT_EXPR.match(line):
                found = ("default", "default", 0, True)
            elif JS_CJS.match(line):
                found = (JS_CJS.match(line).group("name"), "const", 0, True)
        elif lang == "go":
            m = GO_FUNC.match(line)
            if m:
                name = m.group("name")
                if m.group("recv"):
                    found = (m.group("recv") + "." + name, "method", 0, name[0].isupper())
                else:
                    found = (name, "func", 0, name[0].isupper())
            elif GO_TYPE.match(line):
                name = GO_TYPE.match(line).group("name")
                found = (name, "type", 0, name[0].isupper())
            elif GO_VAR.match(line):
                m = GO_VAR.match(line)
                found = (m.group("name"), m.group("kind"), 0, m.group("name")[0].isupper())
        elif lang == "rust":
            m = RS_ITEM.match(line)
            if m:
                public = bool(m.group("pub")) and m.group("scope") is None
                found = (m.group("name"), m.group("kind"), 0, public)
                owner = None
            elif RS_MACRO.match(line):
                found = (RS_MACRO.match(line).group("name"), "macro", 0, False)
                owner = None
            elif RS_IMPL.match(line):
                m = RS_IMPL.match(line)
                ty = m.group("ty").split("::")[-1]
                label = "impl %s for %s" % (" ".join(m.group("trait").split()), ty) \
                    if m.group("trait") else "impl " + ty
                found = (label, "impl", 0, False)
                owner = ty
            elif owner and RS_METHOD.match(line):
                m = RS_METHOD.match(line)
                public = bool(m.group("pub")) and m.group("scope") is None
                found = (owner + "::" + m.group("name"), "method", 1, public)
            elif not line[0].isspace() and not line.startswith(("}", "//", "#", "/*", "*")):
                owner = None
        if found is None:
            continue
        name, kind, depth, exported = found
        start, summary = _leading(lines, i, COMMENTS[lang], ATTRS[lang])
        defs.append(_defn(name, kind, i, _sig(line), summary, depth, exported, start))
    top = [d for d in defs if d["depth"] == 0]
    for a, b in zip(top, top[1:] + [None]):
        end = (b["start"] - 1) if b else len(lines)
        while end > a["line"] and not lines[end - 1].strip():
            end -= 1
        a["end"] = end
    return defs


def _js_reexports(text, defs):
    """Exports declared by `export { a, b as c } [from '...']` and `export * from`."""
    out = []
    local = dict((d["name"], d) for d in defs if d["depth"] == 0)
    for m in JS_BRACES.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        src_line = text[m.start():].split("\n", 1)[0]
        for part in m.group("names").split(","):
            bits = part.split()
            if not bits or bits[0] == "type" and len(bits) == 1:
                continue
            if bits[0] == "type":
                bits = bits[1:]
            name = bits[-1] if len(bits) == 3 and bits[1] == "as" else bits[0]
            if m.group("src") is None and name == bits[0] and bits[0] in local:
                local[bits[0]]["exported"] = True
                continue
            out.append(_defn(name, "reexport", line, _sig(src_line), exported=True))
    for m in JS_STAR.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        src_line = text[m.start():].split("\n", 1)[0]
        out.append(_defn(m.group("name") or "*", "reexport", line, _sig(src_line),
                         exported=True))
    return out


def symbols(path, text, lang=None):
    """(definitions, exports, module summary) for a file. Definitions carry name, kind,
    line, start (leading comments and decorators included), end, signature, summary,
    depth and exported; exports are the public subset (map schema fields)."""
    lang = lang or lang_of_file(path)
    if lang is None or len(text) > MAX_BYTES or "\x00" in text:
        return [], [], ""
    if lang == "python":
        return python_symbols(text)
    lines = text.splitlines()
    defs = _regex_defs(lines, lang)
    extra = _js_reexports(text, defs) if lang in ("typescript", "javascript") else []
    exports = [d for d in defs if d["exported"]] + extra
    exports.sort(key=lambda d: (d["line"], d["name"]))
    summary = ""
    if lang in ("go", "rust", "typescript", "javascript"):
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if s.startswith(("//", "/*", "*")):
                summary = _comment_text(s)
            break
    return defs, exports, summary


def export_record(d):
    return {"name": d["name"], "kind": d["kind"], "line": d["line"],
            "signature": d["signature"], "summary": d["summary"]}


def sections(lines):
    """[(line, title)] for `# ====` / `// ----` style section markers."""
    out = []
    for i, line in enumerate(lines, 1):
        m = SECTION.match(line)
        if m:
            out.append((i, m.group(1)))
    return out


# ---------------------------------------------------------------- imports


def _rust_imports(root, files, texts, crates):
    """File-level Rust edges: `mod name;` to the module file, and `dep::` / `use dep` to the
    root file of a path-dependency crate (an integration test using its own crate counts)."""
    from devkit import graph
    fileset = set(files)
    out = {}
    roots = {}
    for name, c in crates.items():
        for cand in ("src/lib.rs", "src/main.rs"):
            f = posixpath.join(c["dir"], cand) if c["dir"] else cand
            if f in fileset:
                roots[name] = f
                break
    by_dir = dict((c["dir"], name) for name, c in crates.items())
    local_names = {}
    for name, c in crates.items():
        found = graph._crate_at(root, c["dir"])
        keys = {}
        if found:
            for key, rel in found[1].items():
                dep = by_dir.get(graph._norm(posixpath.join(c["dir"], rel)))
                if dep and dep != name:
                    keys[key.replace("-", "_")] = dep
        for dep in c.get("deps", []):
            keys.setdefault(dep.replace("-", "_"), dep)
        local_names[name] = keys
    for f in files:
        if not f.endswith(".rs"):
            continue
        text = texts.get(f, "")
        deps = set()
        here, stem = posixpath.dirname(f), posixpath.splitext(posixpath.basename(f))[0]
        base = here if stem in ("lib", "main", "mod") else posixpath.join(here, stem)
        for m in re.finditer(r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)\s*;", text, re.M):
            for cand in (m.group(1) + ".rs", m.group(1) + "/mod.rs"):
                p = graph._norm(posixpath.join(base, cand))
                if p in fileset:
                    deps.add(p)
                    break
        crate = graph.crate_of(f, crates)
        if crate is not None:
            names = dict(local_names.get(crate, {}))
            own_root = roots.get(crate)
            if own_root and f != own_root and "/src/" not in "/" + f[len(crates[crate]["dir"]):]:
                names.setdefault(crate.replace("-", "_"), crate)
            for local, dep in sorted(names.items()):
                if dep in roots and re.search(r"(?<![\w:])(?:%s::|use\s+%s\b|extern\s+crate\s+%s\b)"
                                              % ((re.escape(local),) * 3), text):
                    deps.add(roots[dep])
        deps.discard(f)
        out[f] = sorted(deps)
    return out


def file_imports(root, info, cfg, graphs, files, texts):
    """{file: [imported repository files]} across the detected languages."""
    from devkit import graph
    out = dict((f, []) for f in files)
    langs = info["languages"]
    for lang in ("python", "typescript", "javascript"):
        g = graphs.get(lang)
        if g is None:
            continue
        for f, ds in g.deps.items():
            if f in out:
                out[f] = sorted(set(out[f]) | set(d for d in ds if d in out))
    if "go" in langs:
        facts = info["langs"]["go"]
        gofiles = [f for f in files if f.endswith(".go")]
        by_dir = {}
        for f in gofiles:
            if not f.endswith("_test.go"):
                by_dir.setdefault(posixpath.dirname(f), []).append(f)
        for f in gofiles:
            dirs = graph.go_imports(f, texts.get(f, ""), facts.get("module"), facts.get("root", ""))
            out[f] = sorted(set(p for d in dirs for p in by_dir.get(d, ())))
    if "rust" in langs:
        crates = graph.rust_crates(root, info["files"], info["langs"]["rust"].get("root", ""))
        out.update(_rust_imports(root, files, texts, crates))
    return out


def language_graphs(root, cfg, info):
    from devkit.affected import build_graph
    graphs = dict((lang, build_graph(root, lang, info, cfg)) for lang in info["languages"])
    return dict((k, v) for k, v in graphs.items() if v is not None)


def edges(root, cfg, info, files, texts, graphs=None):
    """({file: [imports]}, {file: [importers]}) restricted to `files`, both sorted."""
    graphs = graphs if graphs is not None else language_graphs(root, cfg, info)
    imports = file_imports(root, info, cfg, graphs, files, texts)
    fileset = set(files)
    imports = dict((f, [d for d in imports.get(f, []) if d in fileset]) for f in files)
    importers = {}
    for f in files:
        for d in imports[f]:
            importers.setdefault(d, []).append(f)
    return imports, dict((f, sorted(v)) for f, v in importers.items())


def _target_paths(lang, target, info, cfg, files, crates):
    """A devkit.affected target as test file paths."""
    if lang in ("python", "typescript", "javascript"):
        return [target]
    globs = cfg["test_globs"].get(lang, [])
    lroot = info["langs"].get(lang, {}).get("root", "")
    if lang == "go":
        d = posixpath.normpath(posixpath.join(lroot, target)) if lroot else posixpath.normpath(target)
        d = "" if d == "." else d
        return [f for f in files if posixpath.dirname(f) == d and f.endswith("_test.go")]
    if lang == "rust" and target in crates:
        d = crates[target]["dir"]
        return [f for f in files if (not d or f.startswith(d + "/")) and f.endswith(".rs")
                and any(glob_match(g, f[len(lroot) + 1:] if lroot else f) for g in globs)]
    return []


# ---------------------------------------------------------------- build, store, drift


def mapped_files(info):
    langs = set(info["languages"])
    if langs & {"typescript", "javascript"}:
        langs |= {"typescript", "javascript"}
    return [f for f in info["files"] if lang_of_file(f) in langs]


def root_id(files):
    return hashlib.sha1("\n".join(sorted(files)).encode("utf-8")).hexdigest()


def read_texts(root, files):
    from devkit.graph import read_text
    return dict((f, read_text(root, f)) for f in files)


def build(root, cfg=None, info=None, tests=True, texts=None):
    """The repository map as a dict (schema devkit-map/1). `tests=False` skips the
    per-file affected selection (navigation that needs only symbols and edges)."""
    from devkit import graph
    from devkit.affected import affected
    from devkit.detect import detect
    cfg = cfg if cfg is not None else load_config(root, warn_unknown=False)
    info = info if info is not None else detect(root, cfg)
    files = mapped_files(info)
    texts = texts if texts is not None else read_texts(root, files)
    graphs = language_graphs(root, cfg, info)
    imports, importers = edges(root, cfg, info, files, texts, graphs)
    crates = graph.rust_crates(root, info["files"], info["langs"]["rust"].get("root", "")) \
        if "rust" in info["languages"] else {}
    entries = {}
    for f in files:
        _, exports, _ = symbols(f, texts[f])
        entry = {"lang": lang_of_file(f), "exports": [export_record(d) for d in exports],
                 "imports": imports.get(f, []), "tests": [],
                 "importers": importers.get(f, [])}
        if tests:
            res = affected(root, [("M", f, None)], cfg, info, graphs)
            if res["mode"] == "selected":
                found = set()
                for lang, targets in res["targets"].items():
                    for t in targets:
                        found.update(_target_paths(lang, t, info, cfg, files, crates))
                entry["tests"] = sorted(found)
        entries[f] = entry
    return {"schema": SCHEMA, "root_id": root_id(files), "files": entries}


def map_path(root):
    return os.path.join(devkit_dir(root), MAP_FILE)


def dumps(m):
    return json.dumps(m, indent=1, sort_keys=True) + "\n"


def load_stored(root):
    """The stored map, or None when absent; DevError when unreadable."""
    path = map_path(root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise DevError("cannot read %s: %s; run `dev.py map` to rewrite it"
                       % (to_posix(os.path.relpath(path, root)), e))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise DevError(".devkit/%s is not a %s map; run `dev.py map` to rewrite it"
                       % (MAP_FILE, SCHEMA))
    return data


def diff(old, new):
    """Sorted paths added, removed or changed between two maps."""
    a, b = old.get("files", {}), new.get("files", {})
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def drift(root):
    """Changed paths between .devkit/repo-map.json and a fresh build ([] when current).
    Used by `check` as the map gate."""
    stored = load_stored(root)
    fresh = build(root)
    if stored is None:
        return sorted(fresh["files"])
    return diff(stored, fresh)


def cmd_map(args):
    p = Parser(prog="dev.py map", description="Write .devkit/repo-map.json: exports with line "
               "numbers, imports, importers and covering tests per source file.")
    p.add_argument("--check", action="store_true",
                   help="rebuild in memory and exit 1 when the stored map differs")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true",
                   help="print the map (or, with --check, the drift) as one JSON object")
    a = p.parse_args(args)
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    cfg = load_config(root)
    m = build(root, cfg)
    rel = ".devkit/" + MAP_FILE
    if a.check:
        stored = load_stored(root)
        changed = sorted(m["files"]) if stored is None else diff(stored, m)
        if a.json:
            print_json({"schema": SCHEMA, "mode": "check", "stored": stored is not None,
                        "changed": changed})
        elif stored is None:
            print("map: no %s; run `dev.py map` to write it" % rel)
        elif changed:
            print("map: %s is stale in %d file(s); run `dev.py map`" % (rel, len(changed)))
            for path in changed:
                print("changed: %s" % path)
        else:
            print("map: %s is current (%d files)" % (rel, len(m["files"])))
        return 1 if (stored is None or changed) else 0
    devkit_dir(root, create=True)
    with open(map_path(root), "w", encoding="utf-8") as f:
        f.write(dumps(m))
    if a.json:
        print_json(m)
        return 0
    n_exports = sum(len(e["exports"]) for e in m["files"].values())
    n_edges = sum(len(e["imports"]) for e in m["files"].values())
    by_lang = {}
    for e in m["files"].values():
        by_lang[e["lang"]] = by_lang.get(e["lang"], 0) + 1
    langs = ", ".join("%s %d" % kv for kv in sorted(by_lang.items())) or "no source files"
    print("map: wrote %s: %d files (%s), %d exports, %d import edges"
          % (rel, len(m["files"]), langs, n_exports, n_edges))
    return 0


# ---------------------------------------------------------------- outline and where


def cmd_outline(args):
    p = Parser(prog="dev.py outline", description="Definitions of one file with line numbers, "
               "their docstring or leading comment, and section markers.")
    p.add_argument("file", help="source file (.py, .ts/.tsx/.js/.jsx/.mjs/.cjs, .go, .rs)")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if not os.path.isfile(a.file):
        raise UsageError("%s: no such file" % a.file)
    lang = lang_of_file(a.file)
    if lang is None:
        raise UsageError("%s: unsupported file type; outline reads %s"
                         % (a.file, " ".join(sorted(LANG_BY_EXT))))
    root, _ = repo_root(a.repo)
    try:
        shown = rel_to_root(root, a.file)
    except UsageError:
        shown = to_posix(a.file)
    from devkit.graph import read_text
    text = read_text(os.getcwd(), a.file)
    defs, exports, summary = symbols(a.file, text, lang)
    lines = text.splitlines()
    exported = set((d["name"], d["line"]) for d in exports)
    items = [{"line": d["line"], "kind": d["kind"], "name": d["name"], "depth": d["depth"],
              "exported": (d["name"], d["line"]) in exported, "signature": d["signature"],
              "summary": d["summary"]} for d in defs]
    items += [{"line": d["line"], "kind": d["kind"], "name": d["name"], "depth": 0,
               "exported": True, "signature": d["signature"], "summary": d["summary"]}
              for d in exports if d["kind"] in ("reexport", "variable")]
    items += [{"line": ln, "kind": "section", "name": title, "depth": 0, "exported": False,
               "signature": "", "summary": ""} for ln, title in sections(lines)]
    items.sort(key=lambda it: (it["line"], it["kind"] != "section", it["name"]))
    if a.json:
        print_json({"schema": "devkit-outline/1", "path": shown, "lang": lang,
                    "lines": len(lines), "summary": summary, "items": items})
        return 0
    print("%s (%s, %d lines)%s" % (shown, lang, len(lines), (": " + summary) if summary else ""))
    width = len(str(len(lines) or 1))
    for it in items:
        if it["kind"] == "section":
            print("%*d  ---- %s" % (width, it["line"], it["name"] or ""))
            continue
        mark = "" if it["exported"] or it["depth"] else "  (private)"
        if it["kind"] in ("reexport", "variable"):
            mark = "  (%s)" % it["kind"]
        text_ = "  " * it["depth"] + it["signature"] + mark
        if it["summary"]:
            text_ += "  # " + it["summary"]
        print("%*d  %s" % (width, it["line"], text_))
    return 0


def _norm_name(s):
    return s.replace("::", ".")


def name_matches(name, symbol, path):
    n, s = _norm_name(name), _norm_name(symbol)
    if n == s or n.endswith("." + s):
        return True
    if "." in s and s.endswith("." + n):
        qual = s[:-len(n) - 1]
        module = posixpath.splitext(path)[0].replace("/", ".")
        return module == qual or module.endswith("." + qual)
    return False


def find_definitions(m, symbol, root=None, texts=None):
    """[(path, definition dict, from_map)] sorted: real definitions before re-exports and
    variables, then path and line. Searches the map's exports first, then every
    definition (private names and methods) of the mapped files."""
    hits = []
    for path in sorted(m["files"]):
        for ex in m["files"][path]["exports"]:
            if name_matches(ex["name"], symbol, path):
                hits.append((path, ex, True))
    if not hits and root is not None:
        from devkit.graph import read_text
        for path in sorted(m["files"]):
            text = texts[path] if texts and path in texts else read_text(root, path)
            defs, _, _ = symbols(path, text)
            for d in defs:
                if name_matches(d["name"], symbol, path):
                    hits.append((path, export_record(d), False))
    rank = {"reexport": 2, "variable": 1}
    hits.sort(key=lambda h: (rank.get(h[1]["kind"], 0), h[0], h[1]["line"]))
    return hits


def _stale(root, m, hits):
    from devkit.graph import read_text
    for path, ex, _ in hits:
        lines = read_text(root, path).splitlines()
        last = re.split(r"\.|::", ex["name"])[-1]
        if ex["line"] > len(lines) or last not in lines[ex["line"] - 1]:
            return "%s:%d no longer defines %s" % (path, ex["line"], ex["name"])
    return None


def cmd_where(args):
    p = Parser(prog="dev.py where", description="Where a symbol is defined, and the files that "
               "import the defining file, from the repository map.")
    p.add_argument("symbol", help="name, Class.method, Type::method or module.name")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    from devkit.detect import detect
    cfg = load_config(root, warn_unknown=False)
    info = detect(root, cfg)
    m, source = load_stored(root), ".devkit/" + MAP_FILE
    if m is None:
        note("no .devkit/%s; built the map in memory (run `dev.py map` to store it)" % MAP_FILE)
        m, source = build(root, cfg, info), "memory"
    elif m.get("root_id") != root_id(mapped_files(info)):
        note("the stored map is stale (the file set changed); built the map in memory "
             "(run `dev.py map` to refresh it)")
        m, source = build(root, cfg, info), "memory"
    hits = find_definitions(m, a.symbol, root)
    if source != "memory":
        reason = _stale(root, m, hits)
        if reason:
            note("the stored map is stale (%s); built the map in memory (run `dev.py map` to "
                 "refresh it)" % reason)
            m, source = build(root, cfg, info), "memory"
            hits = find_definitions(m, a.symbol, root)
    out = []
    for path, ex, exported in hits:
        e = m["files"][path]
        out.append({"path": path, "line": ex["line"], "name": ex["name"], "kind": ex["kind"],
                    "exported": exported, "signature": ex["signature"],
                    "summary": ex["summary"], "importers": e["importers"],
                    "tests": e["tests"]})
    if a.json:
        print_json({"schema": "devkit-where/1", "symbol": a.symbol, "map": source,
                    "definitions": out})
        return 0 if out else 1
    if not out:
        import sys
        sys.stderr.write("dev.py where: no definition of %r in %d mapped file(s); try "
                         "`dev.py context %s` for uses\n" % (a.symbol, len(m["files"]), a.symbol))
        return 1
    for d in out:
        label = d["kind"] + ("" if d["exported"] else ", not exported")
        print("%s:%d  %s  (%s)" % (d["path"], d["line"], d["signature"], label))
        if d["summary"]:
            print("  %s" % d["summary"])
        print("  importers: %s" % (", ".join(d["importers"]) or "none"))
        print("  tests: %s" % (", ".join(d["tests"]) or "none mapped"))
    return 0
