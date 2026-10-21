"""`dev.py size-audit`: line counts per source file of every detected language, and per
Python function (via `ast`), against `size_audit` in tools/dev.json (defaults 450 and 70).
Test files (test_globs) are skipped unless include_tests: suites grow by cases, and the
limits target the code an agent edits. Exit 1 when anything exceeds its limit."""
import ast
import os

from devkit.common import (Parser, UsageError, glob_match, load_config, note, print_json,
                           repo_root)

SCHEMA = "devkit-size-audit/1"


def line_count(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def python_functions(text):
    """[(qualified name, def line, length in lines)] for every function, nested ones too.
    The length runs from the `def` line to the last line of the body."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(child, "end_lineno", None) or child.lineno
                out.append((prefix + child.name, child.lineno, end - child.lineno + 1))
                visit(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def limits(cfg):
    sa = cfg.get("size_audit") or {}
    out = {}
    for key, default in (("file_lines", 450), ("function_lines", 70)):
        v = sa.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise UsageError("tools/dev.json: size_audit.%s must be a positive integer" % key)
        out[key] = v
    return out


def audit(root, cfg=None, info=None, include_tests=False):
    from devkit import repomap
    from devkit.detect import detect
    from devkit.graph import read_text
    cfg = cfg if cfg is not None else load_config(root, warn_unknown=False)
    info = info if info is not None else detect(root, cfg)
    lim = limits(cfg)
    files, functions, skipped = [], [], 0
    for f in repomap.mapped_files(info):
        lang = repomap.lang_of_file(f)
        if not include_tests and any(glob_match(g, f)
                                     for g in cfg["test_globs"].get(lang, [])):
            skipped += 1
            continue
        text = read_text(root, f)
        if "\x00" in text:
            continue
        files.append({"path": f, "lang": repomap.lang_of_file(f), "lines": line_count(text)})
        if f.endswith(".py"):
            for name, line, n in python_functions(text):
                functions.append({"path": f, "name": name, "line": line, "lines": n})
    over_files = sorted((x for x in files if x["lines"] > lim["file_lines"]),
                        key=lambda x: (-x["lines"], x["path"]))
    over_funcs = sorted((x for x in functions if x["lines"] > lim["function_lines"]),
                        key=lambda x: (-x["lines"], x["path"], x["line"]))
    largest_file = max(files, key=lambda x: (x["lines"], x["path"]), default=None)
    largest_func = max(functions, key=lambda x: (x["lines"], x["path"], -x["line"]), default=None)
    return {"schema": SCHEMA, "limits": lim, "files": len(files), "test_files_skipped": skipped,
            "functions": len(functions), "languages": info["languages"],
            "file_violations": over_files, "function_violations": over_funcs,
            "largest_file": largest_file, "largest_function": largest_func,
            "status": "fail" if (over_files or over_funcs) else "pass"}


def cmd_size_audit(args):
    p = Parser(prog="dev.py size-audit", description="Source files and Python functions longer "
               "than the size_audit limits in tools/dev.json (defaults 450 and 70 lines).")
    p.add_argument("--include-tests", action="store_true",
                   help="audit test files (test_globs) too; skipped by default")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    res = audit(root, load_config(root), include_tests=a.include_tests)
    if a.json:
        print_json(res)
        return 1 if res["status"] == "fail" else 0
    lim = res["limits"]
    print("size-audit: %d file(s) (%s), %d Python function(s); limits %d lines per file, %d per "
          "function%s" % (res["files"], ", ".join(res["languages"]) or "no languages detected",
                          res["functions"], lim["file_lines"], lim["function_lines"],
                          ("; %d test file(s) skipped (--include-tests)" % res["test_files_skipped"])
                          if res["test_files_skipped"] else ""))
    for x in res["file_violations"]:
        print("over  file      %s: %d lines (limit %d)" % (x["path"], x["lines"], lim["file_lines"]))
    for x in res["function_violations"]:
        print("over  function  %s:%d %s: %d lines (limit %d)"
              % (x["path"], x["line"], x["name"], x["lines"], lim["function_lines"]))
    if res["status"] == "fail":
        print("size-audit: FAIL (%d file(s), %d function(s) over the limits; split them or raise "
              "size_audit in tools/dev.json)" % (len(res["file_violations"]),
                                                 len(res["function_violations"])))
        return 1
    parts = []
    if res["largest_file"]:
        parts.append("largest file %s (%d lines)" % (res["largest_file"]["path"],
                                                    res["largest_file"]["lines"]))
    if res["largest_function"]:
        f = res["largest_function"]
        parts.append("largest function %s:%d %s (%d lines)" % (f["path"], f["line"], f["name"],
                                                              f["lines"]))
    print("size-audit: PASS" + ("; " + "; ".join(parts) if parts else ""))
    return 0
