"""Language and tool detection, and the command each repository step will run.

detect() finds languages by marker files at the root and one level down, the JS package
manager by lockfile, and per-language facts (configs, declared tools, scripts). resolve()
turns those facts into the command for a step (lint, typecheck, test, test_targets) using
the first available tool in a fixed order; `which` and `importable` are injectable so the
order is unit-testable without any toolchain. `dev.py detect` prints the result.
"""
import importlib.util
import json
import os
import posixpath
import re
import shlex
import shutil
import sys

from devkit.common import (Parser, config_command, load_config, note, print_json, repo_root,
                           walk_files)

SCHEMA = "devkit-detect/1"
LANG_ORDER = ("python", "typescript", "javascript", "go", "rust")
EXTENSIONS = {
    "python": (".py",),
    "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    "go": (".go",),
    "rust": (".rs",),
}
PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "tox.ini")
LOCKFILES = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"),
             ("bun.lock", "bun"))
STEP_NAMES = ("lint", "typecheck", "test")
PYTHON = "python3"           # shown in commands; run as sys.executable


def language_of(path, languages):
    """The detected language a path belongs to by extension, or None."""
    ext = posixpath.splitext(path)[1]
    for lang in languages:
        if ext in EXTENSIONS.get(lang, ()):
            return lang
    return None


def _shallow(rel):
    return rel.count("/") <= 1


def _read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _section(text, name):
    """True when an INI or TOML text has a [name] section header."""
    return re.search(r"^\s*\[%s\]\s*$" % re.escape(name), text, re.M) is not None


def _declares(text, tool):
    """True when a requirements/pyproject/setup.cfg/Pipfile text names the tool as a
    dependency (a word bounded by quotes, line start, or version specifiers)."""
    return re.search(r"""(?:^|["'\s,\[])%s(?:\[[^\]]*\])?\s*(?:[<>=!~;\s"',\]]|$)""" %
                     re.escape(tool), text, re.M | re.I) is not None


def _pick_root(paths):
    """The directory of the shallowest, then alphabetically first, marker path."""
    if not paths:
        return ""
    best = sorted(paths, key=lambda p: (p.count("/"), p))[0]
    return posixpath.dirname(best)


def detect(root, cfg=None, files=None):
    """Detection facts for root: languages, package manager, per-language root and facts.

    Returns {"languages", "package_manager", "langs": {lang: facts}, "files", "notes"}.
    Facts hold the marker paths and what resolve() needs; nothing is executed.
    """
    cfg = cfg if cfg is not None else load_config(root, warn_unknown=False)
    files = files if files is not None else walk_files(root, cfg["exclude_dirs"])
    notes = []
    shallow = [f for f in files if _shallow(f)]
    base = lambda f: f.rsplit("/", 1)[-1]
    markers = {"python": [], "js": [], "go": [], "rust": []}
    for f in shallow:
        b = base(f)
        if b in PY_MARKERS or re.match(r"requirements.*\.txt$", b):
            markers["python"].append(f)
        elif b == "package.json":
            markers["js"].append(f)
        elif b == "go.mod":
            markers["go"].append(f)
        elif b == "Cargo.toml":
            markers["rust"].append(f)
    py_files = [f for f in files if f.endswith(".py")]
    langs = {}
    if markers["python"] or py_files:
        langs["python"] = python_facts(root, files, markers["python"], py_files, cfg)
    if markers["js"]:
        facts = js_facts(root, files, markers["js"], cfg)
        langs[facts["language"]] = facts
        if not facts["deps_installed"]:
            notes.append("%s declares dependencies but node_modules is absent: steps that "
                         "need them are marked missing, and package scripts that call them "
                         "will fail; run `%s install`" % (posixpath.join(facts["root"],
                                                          "package.json"), facts["package_manager"]))
        others = sorted(set(posixpath.dirname(m) for m in markers["js"]) - {facts["root"]})
        if others:
            notes.append("package.json also in %s; commands run in %s only (set commands in "
                         "tools/dev.json for the others)" % (", ".join(others), facts["root"] or "."))
    if markers["go"]:
        gm = sorted(markers["go"], key=lambda p: (p.count("/"), p))[0]
        from devkit.graph import go_module
        langs["go"] = {"markers": sorted(markers["go"]), "root": posixpath.dirname(gm),
                       "module": go_module(root, gm)}
    if markers["rust"]:
        langs["rust"] = rust_facts(root, markers["rust"])
    order = [l for l in LANG_ORDER if l in langs]
    if cfg.get("languages"):
        wanted = [l for l in cfg["languages"] if l in LANG_ORDER]
        unknown = [l for l in cfg["languages"] if l not in LANG_ORDER]
        if unknown:
            notes.append("tools/dev.json languages: %s not supported; known: %s"
                         % (", ".join(unknown), ", ".join(LANG_ORDER)))
        for l in wanted:
            if l not in langs:
                langs[l] = {"markers": [], "root": ""}
                if l in ("typescript", "javascript"):
                    langs[l].update(js_facts(root, files, [], cfg))
                    langs[l]["language"] = l
                elif l == "python":
                    langs[l] = python_facts(root, files, [], py_files, cfg)
        order = wanted
    pm = cfg.get("package_manager") or (langs.get("typescript") or langs.get("javascript") or
                                        {}).get("package_manager")
    for l in ("typescript", "javascript"):
        if l in langs and cfg.get("package_manager"):
            langs[l]["package_manager"] = cfg["package_manager"]
    return {"languages": order, "package_manager": pm, "langs": langs, "files": files,
            "notes": notes}


def python_facts(root, files, markers, py_files, cfg=None):
    proot = _pick_root(markers)
    texts = dict((m, _read(root, m)) for m in markers)
    for extra in ("mypy.ini", ".mypy.ini", "pytest.ini", "setup.cfg", "tox.ini"):
        rel = posixpath.join(proot, extra) if proot else extra
        if rel in files and rel not in texts:
            texts[rel] = _read(root, rel)
    joined = re.sub(r"#[^\n]*", "", "\n".join(texts.values()))
    pyproject = texts.get(posixpath.join(proot, "pyproject.toml") if proot else "pyproject.toml", "")

    def has(name):
        return any(p.endswith("/" + name) or p == name for p in texts)

    mypy_configured = (_section(pyproject, "tool.mypy") or has("mypy.ini") or has(".mypy.ini")
                       or any(_section(t, "mypy") for p, t in texts.items()
                              if p.endswith("setup.cfg")))
    pytest_configured = (_section(pyproject, "tool.pytest.ini_options") or has("pytest.ini")
                         or any(_section(t, "tool:pytest") for p, t in texts.items()
                                if p.endswith("setup.cfg"))
                         or any(_section(t, "pytest") for p, t in texts.items()
                                if p.endswith("tox.ini")))
    declared = sorted(t for t in ("ruff", "flake8", "pyflakes", "mypy", "pyright", "pytest")
                      if _declares(joined, t))
    tests_dir = "."
    for cand in ("tests", "test"):
        rel = posixpath.join(proot, cand) if proot else cand
        if any(f.startswith(rel + "/") for f in py_files):
            tests_dir = cand
            break
    from devkit.common import DEFAULT_CONFIG, glob_match
    globs = ((cfg or {}).get("test_globs") or DEFAULT_CONFIG["test_globs"]).get("python", [])
    prefix = proot + "/" if proot else ""
    test_files = sorted(f[len(prefix):] for f in py_files if f.startswith(prefix)
                        and any(glob_match(g, f) or glob_match(g, f[len(prefix):])
                                for g in globs))
    has_tests = bool(test_files) or any(glob_match(g, f) for f in py_files for g in globs)
    init_dirs = sorted(set(posixpath.dirname(f[len(prefix):]) for f in py_files
                           if f.startswith(prefix) and f.endswith("/__init__.py")))
    return {"markers": sorted(markers), "root": proot, "mypy_configured": bool(mypy_configured),
            "pytest_configured": bool(pytest_configured), "declared": declared,
            "tests_dir": tests_dir, "has_tests": has_tests, "test_files": test_files,
            "package_dirs": init_dirs}


def unittest_top(test_file, package_dirs):
    """The directory above test_file's packages: its own directory, or above it while
    directories hold __init__.py. Package-style imports (`from tests.helpers import x`)
    resolve from there."""
    d = posixpath.dirname(test_file)
    while d and d in package_dirs:
        d = posixpath.dirname(d)
    return d or "."


def unittest_pieces(test_files, package_dirs):
    """One `python3 -m unittest FILE...` process per test directory, with that directory
    (and, when it is a package, the directory above its packages) on PYTHONPATH, as
    `discover -s DIR` would put DIR on sys.path. Sibling imports (`import helpers`) and
    package imports both resolve, and same-named helpers in different directories never
    share a process."""
    groups = {}
    for f in test_files:
        groups.setdefault(posixpath.dirname(f) or ".", []).append(f)
    pieces = []
    for d in sorted(groups):
        files = sorted(groups[d])
        piece = _piece([PYTHON, "-m", "unittest"] + [safe_path(f) for f in files])
        path = []
        for p in (d, unittest_top(files[0], package_dirs)):
            if p != "." and p not in path:
                path.append(p)
        if path:
            piece["env"] = {"PYTHONPATH": os.pathsep.join(path)}
        pieces.append(piece)
    return pieces


def discover_reaches(test_file, tests_dir, package_dirs):
    """True when `unittest discover -s tests_dir -p 'test*.py'` imports test_file: it sits
    in tests_dir itself, or below it with an __init__.py in every directory on the way
    (discover does not descend into plain directories)."""
    base = "" if tests_dir in ("", ".") else tests_dir + "/"
    if not test_file.startswith(base) or not posixpath.basename(test_file).startswith("test"):
        return False
    parts = test_file[len(base):].split("/")[:-1]
    for i in range(1, len(parts) + 1):
        if base + "/".join(parts[:i]) not in package_dirs:
            return False
    return True


def js_facts(root, files, markers, cfg):
    pkg_path = sorted(markers, key=lambda p: (p.count("/"), p))[0] if markers else "package.json"
    jroot = posixpath.dirname(pkg_path)
    try:
        pkg = json.loads(_read(root, pkg_path) or "{}")
    except ValueError:
        pkg = {}
    if not isinstance(pkg, dict):
        pkg = {}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    deps = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    prefix = jroot + "/" if jroot else ""
    tsconfigs = sorted(f for f in files if f.startswith(prefix) and "/" not in f[len(prefix):]
                       and re.match(r"tsconfig.*\.json$", f[len(prefix):]))
    if not tsconfigs and jroot:
        tsconfigs = sorted(f for f in files if "/" not in f and re.match(r"tsconfig.*\.json$", f))
    pm = "npm"
    for lock, name in LOCKFILES:
        if (prefix + lock) in files or lock in files:
            pm = name
            break
    typescript = bool(tsconfigs) or "typescript" in deps
    main_ts = (prefix + "tsconfig.json") if (prefix + "tsconfig.json") in tsconfigs else (
        tsconfigs[0] if tsconfigs else None)
    # node_modules is excluded from `files`, so installed binaries are read from disk: the
    # package's own node_modules/.bin, then the repository root's (hoisted workspaces).
    bins = set()
    for d in sorted(set([jroot, ""])):
        bin_dir = os.path.join(root, d, "node_modules", ".bin")
        try:
            bins.update(os.listdir(bin_dir))
        except OSError:
            pass
    pnp = any(os.path.isfile(os.path.join(root, d, name)) for d in set([jroot, ""])
              for name in (".pnp.cjs", ".pnp.js"))
    modules = pnp or any(os.path.isdir(os.path.join(root, d, "node_modules"))
                         for d in set([jroot, ""]))
    return {"language": "typescript" if typescript else "javascript", "markers": sorted(markers),
            "root": jroot, "package_manager": pm, "scripts": dict(scripts),
            "declared": sorted(t for t in ("eslint", "vitest", "jest", "typescript") if t in deps),
            "tsconfig": main_ts, "installed_bins": sorted(bins), "pnp": pnp,
            "deps_installed": modules or not deps}


def rust_facts(root, markers):
    from devkit.graph import parse_toml
    ws = None
    for m in sorted(markers, key=lambda p: (p.count("/"), p)):
        t = parse_toml(_read(root, m))
        if "workspace" in t or "package" in t:
            ws = m
            break
    ws = ws or sorted(markers)[0]
    return {"markers": sorted(markers), "root": posixpath.dirname(ws)}


# ---------------------------------------------------------------- resolution


def module_importable(name):
    """True when this interpreter can import the module (no import is executed)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _step(lang, step, pieces=None, display=None, status="ready", reason=None, source="resolved",
          tool=None, report=None, runner=None, covered=False):
    """A resolved step. pieces: [{"argv": [...], "files": bool, "fail_on_output": bool}] run
    in sequence; argv starting with python3 runs as sys.executable. A config string is one
    piece with "shell": text (placeholders {files}/{targets} substituted)."""
    return {"language": lang, "step": step, "pieces": pieces or [], "command": display,
            "status": status, "reason": reason, "source": source, "tool": tool,
            "report": report, "runner": runner, "covered": covered}


def safe_path(path):
    """A relative path that no tool can parse as an option."""
    return "./" + path if path.startswith("-") else path


def _piece(argv, files=False, fail_on_output=False):
    return {"argv": list(argv), "files": files, "fail_on_output": fail_on_output}


def display_of(pieces, arg_hint=None):
    parts = []
    for p in pieces:
        if "shell" in p:
            parts.append(p["shell"])
            continue
        text = " ".join(shlex.quote(a) for a in p["argv"])
        if p.get("env"):
            text = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in sorted(p["env"].items())) \
                + " " + text
        if p.get("files") and arg_hint:
            text += " " + arg_hint
        parts.append(text)
    return " && ".join(parts)


def skip_hint(lang, step):
    return "set commands.%s.%s in tools/dev.json" % (lang, "test" if step == "test_targets"
                                                      else step)


def resolve(info, lang, step, cfg=None, which=None, importable=None):
    """The step for a language: config string first, else the first available tool.

    step: lint | typecheck | test | test_targets. `which(name) -> path or None` and
    `importable(module) -> bool` default to shutil.which and module_importable.
    """
    which = which or shutil.which
    importable = importable or module_importable
    cfg = cfg or {"commands": {}}
    facts = info["langs"].get(lang, {})
    cfg_step = "test" if step == "test_targets" else step
    configured = config_command(cfg, lang, cfg_step)
    if configured is None and lang in ("typescript", "javascript"):
        configured = config_command(cfg, "javascript" if lang == "typescript" else "typescript",
                                    cfg_step)
    if configured is not None:
        placeholder = "{targets}" if cfg_step == "test" else "{files}"
        report = config_command(cfg, lang, "test_report") if cfg_step == "test" else None
        return _step(lang, step, [{"shell": configured, "placeholder": placeholder}],
                     display=configured, source="config", report=report)
    fn = {"python": _python, "typescript": _js, "javascript": _js, "go": _go,
          "rust": _rust}.get(lang)
    if fn is None:
        return _step(lang, step, status="skipped", reason="no built-in commands for %s" % lang)
    return fn(facts, lang, step, which, importable)


def _python(facts, lang, step, which, importable):
    declared = set(facts.get("declared", ()))

    def avail(tool, module=None):
        """'bin' (on PATH), 'module' (importable), 'declared' (named by the project) or None."""
        if which(tool):
            return "bin"
        if module and importable(module):
            return "module"
        if tool in declared:
            return "declared"
        return None

    def tool_step(tool, args, module, files=True, status_note=None):
        how = avail(tool, module)
        if how == "bin":
            pieces = [_piece([tool] + args, files=files)]
        else:
            pieces = [_piece([PYTHON, "-m", module or tool] + args, files=files)]
        st = _step(lang, step, pieces, display_of(pieces, "<files>" if files else None), tool=tool)
        if how == "declared" and not (module and importable(module)):
            st["status"] = "missing"
            st["reason"] = "%s is declared by the project but not installed" % tool
        return st

    if step == "lint":
        for tool, args in (("ruff", ["check"]), ("flake8", []), ("pyflakes", [])):
            if avail(tool, tool):
                return tool_step(tool, args, tool)
        pieces = [_piece([PYTHON, "-m", "compileall", "-q"], files=True)]
        return _step(lang, step, pieces, display_of(pieces, "<files>"), tool="compileall")
    if step == "typecheck":
        if facts.get("mypy_configured"):
            if avail("mypy", "mypy") in ("bin", "module"):
                return tool_step("mypy", [], "mypy")
            return _step(lang, step, [_piece(["mypy"], files=True)], "mypy <files>",
                         status="missing", tool="mypy",
                         reason="mypy is configured but not installed")
        if avail("pyright", "pyright"):
            return tool_step("pyright", [], "pyright")
        return _step(lang, step, status="skipped", tool=None,
                     reason="no type checker configured (mypy config or pyright)")
    tests_dir = facts.get("tests_dir", ".")
    if not facts.get("has_tests", True):
        return _step(lang, step, status="skipped", reason="no Python test files (test_globs)")
    pytest_how = avail("pytest", "pytest")
    if facts.get("pytest_configured") or pytest_how:
        in_python = importable("pytest")
        base = ["pytest", "-q"] if (pytest_how == "bin" and not in_python) else \
            [PYTHON, "-m", "pytest", "-q"]
        if step == "test":
            pieces = [_piece(base)]
            st = _step(lang, step, pieces, display_of(pieces), tool="pytest", runner="pytest",
                       report="junit")
        else:
            pieces = [_piece(base, files=True)]
            st = _step(lang, step, pieces, display_of(pieces, "<test files>"), tool="pytest",
                       runner="pytest", report="junit")
        if not in_python and pytest_how != "bin":
            st["status"] = "missing"
            st["reason"] = "pytest is configured or declared but not installed"
        return st
    if step == "test":
        # The full run must reach every file test_globs calls a test, or a change whose
        # tests live elsewhere would PASS without running them. discover only finds files in
        # one directory tree of packages; when some test file lies outside it, list them all.
        test_files = facts.get("test_files") or []
        pkgs = set(facts.get("package_dirs") or ())
        if all(discover_reaches(f, tests_dir, pkgs) for f in test_files):
            pieces = [_piece([PYTHON, "-m", "unittest", "discover", "-s", tests_dir, "-p",
                              "test*.py"])]
            display = display_of(pieces)
        else:
            pieces = unittest_pieces(test_files, pkgs)
            display = ("python3 -m unittest <%d test_globs files, one run per directory: %d>"
                       % (len(test_files), len(pieces)))
        st = _step(lang, step, pieces, display, tool="unittest", runner="unittest",
                   report="unittest")
        st["package_dirs"] = sorted(pkgs)
        return st
    piece = _piece([PYTHON, "-m", "unittest"], files=True)
    piece["unittest_files"] = True          # execute() splits the files with unittest_pieces
    st = _step(lang, step, [piece], display_of([piece], "<test files>"), tool="unittest",
               runner="unittest", report="unittest")
    st["package_dirs"] = sorted(facts.get("package_dirs") or ())
    return st


JS_PACKAGE_OF = {"tsc": "typescript"}     # binary -> the npm package that provides it


def js_exec(pm, tool):
    """argv prefix that runs a local package binary and never installs one: `npm exec`
    downloads a missing package unless given --no, and takes flags after the binary as its
    own unless they follow `--`."""
    return [pm, "exec", "--no", "--", tool] if pm == "npm" else [pm, "exec", tool]


def _js(facts, lang, step, which, importable):
    pm = facts.get("package_manager") or "npm"
    scripts = facts.get("scripts") or {}
    declared = set(facts.get("declared", ()))
    local = set(facts.get("installed_bins", ()))

    def has_tool(tool):
        """Named by the project or runnable: decides which runner the project uses."""
        return (JS_PACKAGE_OF.get(tool, tool) in declared or tool in local
                or bool(which(tool)))

    def pm_step(pieces, hint=None, **kw):
        st = _step(lang, step, pieces, display_of(pieces, hint), **kw)
        if not which(pm):
            st["status"] = "missing"
            st["reason"] = "%s is not installed" % pm
        return st

    def exec_step(tool, args, files=False, hint=None, **kw):
        """Run a package binary without ever letting the package manager install one:
        `npm exec --no --` refuses to download, and the other managers' exec only runs
        local binaries. A binary absent from node_modules/.bin (and from PATH) is missing
        even when package.json declares it, so the gate says "install", not "type errors"."""
        if tool in local or (facts.get("pnp") and JS_PACKAGE_OF.get(tool, tool) in declared):
            argv = js_exec(pm, tool) + args
            return pm_step([_piece(argv, files=files)], hint, tool=tool, **kw)
        if which(tool):
            pieces = [_piece([tool] + args, files=files)]
            return _step(lang, step, pieces, display_of(pieces, hint), tool=tool, **kw)
        pieces = [_piece(js_exec(pm, tool) + args, files=files)]
        return _step(lang, step, pieces, display_of(pieces, hint), tool=tool, status="missing",
                     reason="%s is not in node_modules/.bin; run `%s install`" % (tool, pm),
                     **kw)

    runner = None
    test_script = scripts.get("test", "") if isinstance(scripts.get("test"), str) else ""
    if "vitest" in test_script or (not re.search(r"\bjest\b", test_script) and has_tool("vitest")):
        runner = "vitest" if (has_tool("vitest") or "vitest" in test_script) else None
    if runner is None and (re.search(r"\bjest\b", test_script) or has_tool("jest")):
        runner = "jest"
    if step == "lint":
        if "lint" in scripts:
            return pm_step([_piece([pm, "run", "lint"])], tool="script:lint")
        if has_tool("eslint"):
            return exec_step("eslint", [], files=True, hint="<files>")
        return _step(lang, step, status="skipped", reason="no lint script and no eslint")
    if step == "typecheck":
        for name in ("typecheck", "type-check"):
            if name in scripts:
                return pm_step([_piece([pm, "run", name])], tool="script:" + name)
        if facts.get("tsconfig"):
            rel = posixpath.relpath(facts["tsconfig"], facts.get("root") or ".")
            return exec_step("tsc", ["--noEmit", "-p", rel])
        return _step(lang, step, status="skipped", reason="no typecheck script and no tsconfig")
    if step == "test":
        if "test" in scripts:
            return pm_step([_piece([pm, "run", "test"])], tool="script:test", runner=runner,
                           report=runner)
        if runner == "vitest":
            return exec_step("vitest", ["run"], runner="vitest", report="vitest")
        if runner == "jest":
            return exec_step("jest", [], runner="jest", report="jest")
        return _step(lang, step, status="skipped", reason="no test script, vitest or jest")
    if runner == "vitest":
        return exec_step("vitest", ["run"], files=True, hint="<test files>", runner="vitest",
                         report="vitest")
    if runner == "jest":
        return exec_step("jest", [], files=True, hint="<test files>", runner="jest",
                         report="jest")
    if "test" in scripts:
        argv = [pm, "run", "test"] + (["--"] if pm == "npm" else [])
        return pm_step([_piece(argv, files=True)], "<test files>", tool="script:test")
    return _step(lang, step, status="skipped", reason="no test script, vitest or jest")


def _missing(st, tools, which):
    for t in tools:
        if not which(t):
            st["status"] = "missing"
            st["reason"] = "%s is not installed" % t
            break
    return st


def _go(facts, lang, step, which, importable):
    if step == "lint":
        pieces = [_piece(["gofmt", "-l"], files=True, fail_on_output=True),
                  _piece(["go", "vet"], files=True)]
        st = _step(lang, step, pieces, "gofmt -l <files> && go vet <packages>", tool="go vet")
        st["packages_for"] = 1          # the second piece takes package dirs, not files
        return _missing(st, ("gofmt", "go"), which)
    if step == "typecheck":
        return _step(lang, step, status="covered", reason="covered by go vet in lint",
                     covered=True)
    if step == "test":
        st = _step(lang, step, [_piece(["go", "test", "./..."])], "go test ./...", tool="go test",
                   runner="go", report="go")
    else:
        st = _step(lang, step, [_piece(["go", "test"], files=True)], "go test <packages>",
                   tool="go test", runner="go", report="go")
    return _missing(st, ("go",), which)


def _rust(facts, lang, step, which, importable):
    if step == "lint":
        second = ["cargo", "clippy", "-q"] if which("cargo-clippy") else ["cargo", "check", "-q"]
        pieces = [_piece(["cargo", "fmt", "--check"]), _piece(second)]
        st = _step(lang, step, pieces, display_of(pieces), tool=second[1])
        return _missing(st, ("cargo",), which)
    if step == "typecheck":
        return _step(lang, step, status="covered", reason="covered by cargo %s in lint" % (
            "clippy" if which("cargo-clippy") else "check"), covered=True)
    if step == "test":
        st = _step(lang, step, [_piece(["cargo", "test", "-q"])], "cargo test -q",
                   tool="cargo test", runner="cargo")
    else:
        st = _step(lang, step, [_piece(["cargo", "test", "-q"], files=True)],
                   "cargo test -q -p <crate> ...", tool="cargo test", runner="cargo")
    return _missing(st, ("cargo",), which)


def resolve_all(info, cfg, which=None, importable=None):
    """{lang: {step: resolved step}} for every detected language and step."""
    out = {}
    for lang in info["languages"]:
        out[lang] = dict((s, resolve(info, lang, s, cfg, which, importable))
                         for s in ("lint", "typecheck", "test", "test_targets"))
    return out


# ---------------------------------------------------------------- dev.py detect


def describe(st):
    if st["status"] == "ready":
        return st["command"]
    if st["status"] == "covered":
        return "(%s)" % st["reason"]
    if st["status"] == "missing":
        return "%s  [not runnable: %s; %s]" % (st["command"] or "", st["reason"],
                                               skip_hint(st["language"], st["step"]))
    return "skipped: %s; %s" % (st["reason"], skip_hint(st["language"], st["step"]))


def cmd_detect(args):
    p = Parser(prog="dev.py detect",
               description="Languages, package manager and the command each step will run.")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    cfg = load_config(root)
    info = detect(root, cfg)
    steps = resolve_all(info, cfg)
    if a.json:
        out = {"schema": SCHEMA, "root": root.replace(os.sep, "/"), "config": cfg["path"],
               "languages": info["languages"], "package_manager": info["package_manager"],
               "notes": info["notes"], "steps": {}}
        for lang in info["languages"]:
            facts = dict((k, v) for k, v in info["langs"][lang].items())
            out["steps"][lang] = dict(
                (s, {"command": st["command"], "status": st["status"], "reason": st["reason"],
                     "source": st["source"], "tool": st["tool"], "report": st["report"]})
                for s, st in steps[lang].items())
            out["steps"][lang]["root"] = facts.get("root") or "."
            out["steps"][lang]["markers"] = facts.get("markers", [])
        print_json(out)
        return 0
    if not info["languages"]:
        print("languages: none detected (no pyproject.toml, package.json, go.mod, Cargo.toml "
              "or .py files); set languages in tools/dev.json")
        return 0
    print("languages: %s" % ", ".join(info["languages"]))
    if info["package_manager"]:
        print("package manager: %s" % info["package_manager"])
    print("config: %s" % (cfg["path"] or "none (defaults; dev.py init writes tools/dev.json)"))
    for lang in info["languages"]:
        facts = info["langs"][lang]
        print("%s (root %s; markers: %s)" % (lang, facts.get("root") or ".",
                                             ", ".join(facts.get("markers", [])) or "source files"))
        for s in ("lint", "typecheck", "test", "test_targets"):
            label = "test (targets)" if s == "test_targets" else s
            print("  %-15s %s" % (label, describe(steps[lang][s])))
    for n in info["notes"]:
        note(n)
    return 0
