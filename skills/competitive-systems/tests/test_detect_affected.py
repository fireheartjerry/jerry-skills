"""Detection, command resolution, import graphs and affected-test selection on the fixture
repositories under tests/fixtures/repos. Changed files are passed explicitly, so no git is
needed except in ChangedFilesTests, which builds a real temporary repository (skipped when
git is absent). No toolchain is needed: resolution takes a fake which() and importable().
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
DEV = os.path.join(SCRIPTS, "dev.py")
REPOS = os.path.join(HERE, "fixtures", "repos")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from devkit import graph  # noqa: E402
from devkit.affected import affected, normalize_changed  # noqa: E402
from devkit.common import changed_files, glob_match, load_config  # noqa: E402
from devkit.detect import detect, resolve  # noqa: E402


def nothing(_name):
    return None


def no_module(_name):
    return False


def which_of(*names):
    return lambda n: ("/usr/bin/" + n) if n in names else None


UNIT = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n"
        "        open('ran-%s', 'w').close()\n")


class TempRepo(object):
    """A temporary copy of one fixture repository (or an empty directory)."""

    def __init__(self, name=None):
        self.tmp = tempfile.mkdtemp(prefix="devkit-fixture-")
        self.root = os.path.join(self.tmp, name or "repo")
        if name:
            shutil.copytree(os.path.join(REPOS, name), self.root)
        else:
            os.makedirs(self.root)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def config(self, obj):
        self.write("tools/dev.json", json.dumps(obj))

    def select(self, *paths):
        cfg = load_config(self.root, warn_unknown=False)
        changed = normalize_changed(self.root, [os.path.join(self.root, p) for p in paths])
        return affected(self.root, changed, cfg)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def run_dev(args, cwd=None, env=None):
    """dev.py in a subprocess with an empty PATH (no toolchain, no git) and -I -S (no
    site-packages), so tool resolution is the same on every machine."""
    empty = tempfile.mkdtemp(prefix="devkit-path-")
    e = {"PATH": empty, "HOME": empty, "LC_ALL": "C"}
    if env:
        e.update(env)
    try:
        return subprocess.run([sys.executable, "-I", "-S", DEV] + args, cwd=cwd, env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)
    finally:
        shutil.rmtree(empty, ignore_errors=True)


class FixtureCase(unittest.TestCase):
    repo_name = None

    def setUp(self):
        self.repo = TempRepo(self.repo_name)
        self.root = self.repo.root

    def tearDown(self):
        self.repo.close()


# ---------------------------------------------------------------- detection and resolution


class PythonDetectionTests(FixtureCase):
    repo_name = "py"

    def test_detects_python_and_the_documented_fallbacks(self):
        info = detect(self.root)
        self.assertEqual(info["languages"], ["python"])
        self.assertEqual(info["langs"]["python"]["markers"], ["pyproject.toml"])
        self.assertFalse(info["langs"]["python"]["pytest_configured"])
        step = lambda s: resolve(info, "python", s, which=nothing, importable=no_module)
        self.assertEqual(step("lint")["command"], "python3 -m compileall -q <files>")
        self.assertEqual(step("typecheck")["status"], "skipped")
        self.assertIn("no type checker configured", step("typecheck")["reason"])
        self.assertEqual(step("test")["command"], "python3 -m unittest discover -s tests -p 'test*.py'")
        self.assertEqual(step("test_targets")["command"], "python3 -m unittest <test files>")

    def test_detect_cli_prints_each_step(self):
        p = run_dev(["detect", "--repo", self.root])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("languages: python", p.stdout)
        self.assertIn("lint            python3 -m compileall -q <files>", p.stdout)
        self.assertIn("typecheck       skipped: no type checker configured (mypy config or "
                      "pyright); set commands.python.typecheck in tools/dev.json", p.stdout)
        self.assertIn("test            python3 -m unittest discover -s tests -p 'test*.py'", p.stdout)
        j = json.loads(run_dev(["detect", "--repo", self.root, "--json"]).stdout)
        self.assertEqual(j["schema"], "devkit-detect/1")
        self.assertEqual(j["languages"], ["python"])
        self.assertEqual(j["steps"]["python"]["test"]["tool"], "unittest")
        self.assertEqual(j["steps"]["python"]["typecheck"]["status"], "skipped")

    def test_python_resolution_order(self):
        info = detect(self.root)
        facts = info["langs"]["python"]

        def cmd(step, which=nothing, importable=no_module):
            return resolve(info, "python", step, which=which, importable=importable)

        self.assertEqual(cmd("lint", which_of("ruff", "flake8"))["command"], "ruff check <files>")
        self.assertEqual(cmd("lint", which_of("flake8"))["command"], "flake8 <files>")
        self.assertEqual(cmd("lint", importable=lambda m: m == "pyflakes")["command"],
                         "python3 -m pyflakes <files>")
        self.assertEqual(cmd("typecheck", which_of("pyright"))["command"], "pyright <files>")
        self.assertEqual(cmd("test", importable=lambda m: m == "pytest")["command"],
                         "python3 -m pytest -q")
        self.assertEqual(cmd("test_targets", importable=lambda m: m == "pytest")["command"],
                         "python3 -m pytest -q <test files>")
        self.assertEqual(cmd("test", which_of("pytest"))["command"], "pytest -q")
        facts["mypy_configured"] = True
        self.assertEqual(cmd("typecheck", which_of("mypy", "pyright"))["command"], "mypy <files>")
        missing = cmd("typecheck", which_of("pyright"))
        self.assertEqual((missing["status"], missing["reason"]),
                         ("missing", "mypy is configured but not installed"))
        facts["pytest_configured"] = True
        st = cmd("test")
        self.assertEqual((st["command"], st["status"]), ("python3 -m pytest -q", "missing"))
        facts["declared"] = ["ruff"]
        st = cmd("lint")
        self.assertEqual(st["status"], "missing")
        self.assertIn("declared by the project but not installed", st["reason"])
        facts["has_tests"] = False
        self.assertEqual(cmd("test")["reason"], "no Python test files (test_globs)")

    def test_unittest_full_run_reaches_every_test_file(self):
        """Tests outside one discover tree (plain directories, other roots) must still run
        in a full run, or a change whose tests live there would PASS untested; each runs
        with its own directory importable, so sibling helpers resolve and never collide."""
        helper = "import helpers\n" + UNIT.replace("open(", "helpers.check(); open(")
        for d, name in (("tools/scripts", "tool"), ("tests/plain", "deep")):
            self.repo.write(d + "/helpers.py", "def check():\n    assert __file__.endswith("
                            "%r)\n" % (d + "/helpers.py"))
            self.repo.write("%s/test_%s.py" % (d, name), helper % name)
        self.repo.write("pkg/sub/test_sub.py", UNIT % "sub")
        info = detect(self.root)
        st = resolve(info, "python", "test", which=nothing, importable=no_module)
        self.assertEqual(st["command"], "python3 -m unittest <5 test_globs files, one run per "
                                        "directory: 4>")  # tests, tests/plain, tools/scripts, pkg/sub
        self.assertEqual([p.get("env") for p in st["pieces"]],
                         [{"PYTHONPATH": "pkg/sub"}, {"PYTHONPATH": "tests"},
                          {"PYTHONPATH": "tests/plain"}, {"PYTHONPATH": "tools/scripts"}])
        from devkit.checks import execute, record_plan
        g = execute("tests", st, self.root, None, timeout=60)
        self.assertEqual(g["status"], "pass", "\n".join(g["head"] + g["tail"]))
        for name in ("tool", "sub", "deep"):
            self.assertTrue(os.path.exists(os.path.join(self.root, "ran-" + name)), name)
            os.remove(os.path.join(self.root, "ran-" + name))
        argv, _, kind, report, _, _ = record_plan(st, info, "python", self.repo.tmp, [])
        p = subprocess.run(argv, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(report) as f:
            files = sorted(set(r["file"] for r in json.load(f)))
        self.assertEqual(files, ["pkg/sub/test_sub.py", "tests/plain/test_deep.py",
                                 "tests/test_api.py", "tests/test_core.py",
                                 "tools/scripts/test_tool.py"])
        tt = resolve(info, "python", "test_targets", which=nothing, importable=no_module)
        g = execute("tests", tt, self.root, ["tools/scripts/test_tool.py", "tests/plain/test_deep.py"],
                    timeout=60)
        self.assertEqual(g["status"], "pass", "\n".join(g["head"] + g["tail"]))
        self.assertIn("PYTHONPATH=tools/scripts python3 -m unittest tools/scripts/test_tool.py",
                      g["command"])
        for d in ("tools", "pkg/sub", "tests/plain"):
            shutil.rmtree(os.path.join(self.root, d))
        st = resolve(detect(self.root), "python", "test", which=nothing, importable=no_module)
        self.assertEqual(st["command"], "python3 -m unittest discover -s tests -p 'test*.py'")

    def test_paths_starting_with_a_dash_are_not_options(self):
        from devkit.checks import execute
        from devkit.detect import _piece, _step
        echo = [sys.executable, "-c", "import sys; print(sys.argv[1:])"]
        st = _step("python", "lint", [_piece(echo, files=True)], "echo")
        g = execute("lint", st, self.root, ["-rf.py", "ok.py"])
        self.assertEqual(g["status"], "pass")
        self.assertTrue(g["command"].endswith(" ./-rf.py ok.py"), g["command"])

    def test_declared_tools_ignore_comments(self):
        self.repo.write("pyproject.toml", "[project]\nname = 'x'\n# we may adopt ruff later\n"
                                          "dependencies = ['pytest-cov>=4']\n")
        facts = detect(self.root)["langs"]["python"]
        self.assertEqual(facts["declared"], [])
        self.repo.write("requirements-dev.txt", "ruff==0.4.0\npytest\n")
        self.assertEqual(detect(self.root)["langs"]["python"]["declared"], ["pytest", "ruff"])

    def test_config_strings_win_and_markers_are_detected(self):
        self.repo.write("pyproject.toml", "[project]\nname = 'x'\n\n[tool.mypy]\nstrict = true\n\n"
                                          "[tool.pytest.ini_options]\naddopts = '-q'\n")
        self.repo.config({"schema": "devkit-config/1",
                          "commands": {"python": {"lint": "make lint {files}", "test": None}}})
        cfg = load_config(self.root)
        info = detect(self.root, cfg)
        facts = info["langs"]["python"]
        self.assertTrue(facts["mypy_configured"] and facts["pytest_configured"])
        lint = resolve(info, "python", "lint", cfg, which=nothing, importable=no_module)
        self.assertEqual((lint["source"], lint["command"]), ("config", "make lint {files}"))
        test = resolve(info, "python", "test", cfg, which=nothing, importable=lambda m: True)
        self.assertEqual((test["source"], test["command"]), ("resolved", "python3 -m pytest -q"))


class JsDetectionTests(FixtureCase):
    repo_name = "js"

    def test_pnpm_typescript_and_scripts(self):
        info = detect(self.root)
        self.assertEqual(info["languages"], ["typescript"])
        self.assertEqual(info["package_manager"], "pnpm")
        step = lambda s, w=which_of("pnpm"): resolve(info, "typescript", s, which=w,
                                                     importable=no_module)
        self.assertEqual(step("lint")["command"], "pnpm run lint")
        self.assertEqual(step("typecheck")["command"], "pnpm run typecheck")
        self.assertEqual(step("test")["command"], "pnpm run test")
        self.assertEqual(step("test_targets")["command"], "pnpm exec vitest run <test files>")
        absent = step("lint", nothing)
        self.assertEqual((absent["status"], absent["reason"]), ("missing", "pnpm is not installed"))

    def test_js_resolution_without_scripts_and_lockfiles(self):
        info = detect(self.root)
        facts = info["langs"]["typescript"]
        facts["scripts"] = {}
        w = which_of("pnpm")
        step = lambda s: resolve(info, "typescript", s, which=w, importable=no_module)
        self.assertEqual(step("lint")["status"], "skipped")
        self.assertEqual(step("typecheck")["command"], "pnpm exec tsc --noEmit -p tsconfig.json")
        self.assertEqual(step("test")["command"], "pnpm exec vitest run")
        facts["declared"] = ["eslint", "jest"]
        self.assertEqual(step("lint")["command"], "pnpm exec eslint <files>")
        self.assertEqual(step("test")["command"], "pnpm exec jest")
        facts["declared"] = []
        facts["tsconfig"] = None
        self.assertEqual(step("typecheck")["status"], "skipped")
        self.assertEqual(step("test")["status"], "skipped")
        facts["scripts"] = {"type-check": "tsc", "test": "jest --ci"}
        self.assertEqual(step("typecheck")["command"], "pnpm run type-check")
        self.assertEqual(step("test_targets")["command"], "pnpm exec jest <test files>")
        for lock, pm in (("yarn.lock", "yarn"), ("bun.lockb", "bun")):
            os.remove(os.path.join(self.root, "pnpm-lock.yaml")) if os.path.exists(
                os.path.join(self.root, "pnpm-lock.yaml")) else None
            for other in ("yarn.lock", "bun.lockb"):
                if os.path.exists(os.path.join(self.root, other)):
                    os.remove(os.path.join(self.root, other))
            self.repo.write(lock, "")
            self.assertEqual(detect(self.root)["package_manager"], pm)
        os.remove(os.path.join(self.root, "bun.lockb"))
        self.assertEqual(detect(self.root)["package_manager"], "npm")


    def test_npm_never_installs_and_flags_follow_a_double_dash(self):
        os.remove(os.path.join(self.root, "pnpm-lock.yaml"))
        info = detect(self.root)
        facts = info["langs"]["typescript"]
        self.assertEqual(facts["package_manager"], "npm")
        self.assertIn("node_modules is absent", " ".join(info["notes"]))
        facts["scripts"] = {}
        w = which_of("npm")
        st = resolve(info, "typescript", "typecheck", which=w, importable=no_module)
        argv = st["pieces"][0]["argv"]
        self.assertEqual(argv[:4], ["npm", "exec", "--no", "--"])     # no download, no flag theft
        self.assertEqual(argv[4:], ["tsc", "--noEmit", "-p", "tsconfig.json"])
        self.assertEqual(st["status"], "missing")                      # declared, not installed
        self.assertEqual(st["reason"], "tsc is not in node_modules/.bin; run `npm install`")
        self.assertEqual(resolve(info, "typescript", "test_targets", which=w)["status"], "missing")
        for name in ("tsc", "vitest"):
            self.repo.write("node_modules/.bin/" + name, "")
        info = detect(self.root)
        info["langs"]["typescript"]["scripts"] = {}
        self.assertNotIn("node_modules is absent", " ".join(info["notes"]))
        st = resolve(info, "typescript", "typecheck", which=w, importable=no_module)
        self.assertEqual((st["status"], st["command"]),
                         ("ready", "npm exec --no -- tsc --noEmit -p tsconfig.json"))
        st = resolve(info, "typescript", "test_targets", which=w, importable=no_module)
        self.assertEqual(st["command"], "npm exec --no -- vitest run <test files>")
        from devkit.checks import record_plan
        argv = record_plan(st, info, "typescript", self.root, [])[0]
        self.assertEqual(argv[:5], ["npm", "exec", "--no", "--", "vitest"])
        st = resolve(info, "typescript", "test", which=w, importable=no_module)
        self.assertEqual(st["command"], "npm exec --no -- vitest run")
        os.remove(os.path.join(self.root, "node_modules/.bin/tsc"))  # a global tsc runs directly
        info["langs"]["typescript"]["installed_bins"] = ["vitest"]
        st = resolve(info, "typescript", "typecheck", which=which_of("npm", "tsc"))
        self.assertEqual((st["status"], st["command"]), ("ready", "tsc --noEmit -p tsconfig.json"))


class GoRustDetectionTests(unittest.TestCase):
    def test_go_steps(self):
        repo = TempRepo("go")
        try:
            info = detect(repo.root)
            self.assertEqual(info["languages"], ["go"])
            self.assertEqual(info["langs"]["go"]["module"], "example.com/fixture")
            ok = which_of("go", "gofmt")
            lint = resolve(info, "go", "lint", which=ok, importable=no_module)
            self.assertEqual((lint["command"], lint["status"]),
                             ("gofmt -l <files> && go vet <packages>", "ready"))
            self.assertTrue(resolve(info, "go", "typecheck", which=ok)["covered"])
            self.assertEqual(resolve(info, "go", "test", which=ok)["command"], "go test ./...")
            gone = resolve(info, "go", "test", which=nothing)
            self.assertEqual((gone["status"], gone["reason"]), ("missing", "go is not installed"))
        finally:
            repo.close()

    def test_rust_steps(self):
        repo = TempRepo("rust")
        try:
            info = detect(repo.root)
            self.assertEqual(info["languages"], ["rust"])
            plain = resolve(info, "rust", "lint", which=which_of("cargo"))
            self.assertEqual(plain["command"], "cargo fmt --check && cargo check -q")
            clippy = resolve(info, "rust", "lint", which=which_of("cargo", "cargo-clippy"))
            self.assertEqual(clippy["command"], "cargo fmt --check && cargo clippy -q")
            self.assertEqual(resolve(info, "rust", "test", which=which_of("cargo"))["command"],
                             "cargo test -q")
            self.assertEqual(resolve(info, "rust", "test", which=nothing)["status"], "missing")
        finally:
            repo.close()


# ---------------------------------------------------------------- import graphs


class GraphParsingTests(unittest.TestCase):
    def test_python_imports_absolute_relative_submodule_and_sibling(self):
        files = ["pkg/__init__.py", "pkg/core.py", "pkg/api.py", "pkg/sub/__init__.py",
                 "pkg/sub/deep.py", "src/lib2/__init__.py", "src/lib2/x.py", "tool.py",
                 "tests/helpers.py", "tests/test_a.py"]
        roots = graph.python_roots(files)
        self.assertEqual(roots["pkg.sub.deep"], "pkg/sub/deep.py")
        self.assertEqual(roots["lib2.x"], "src/lib2/x.py")
        self.assertEqual(roots["tool"], "tool.py")
        imp = lambda path, text: graph.python_imports(path, roots, text)
        self.assertEqual(imp("pkg/api.py", "from .core import frobnicate\n"),
                         ["pkg/__init__.py", "pkg/core.py"])
        self.assertEqual(imp("pkg/sub/deep.py", "from .. import core\nfrom . import x\n"),
                         ["pkg/__init__.py", "pkg/core.py", "pkg/sub/__init__.py"])
        self.assertEqual(imp("tests/test_a.py", "import os\nfrom pkg import api\nimport lib2.x\n"
                             "import helpers\nimport tool as t\n"),
                         ["pkg/__init__.py", "pkg/api.py", "src/lib2/__init__.py", "src/lib2/x.py",
                          "tests/helpers.py", "tool.py"])
        self.assertEqual(imp("tests/test_a.py", "import importlib\nm = importlib.import_module('pkg.core')\n"),
                         ["pkg/__init__.py", "pkg/core.py"])
        self.assertEqual(imp("tests/test_a.py", "from pkg.core import (\n  a,\n  b)\ndef broken(:\n"),
                         ["pkg/__init__.py", "pkg/core.py"])

    def test_js_imports_forms_extensions_index_and_alias(self):
        files = {"src/a.ts", "src/b.tsx", "src/c/index.ts", "src/d.js", "src/e.ts",
                 "src/data.json", "src/f.mjs", "lib/util.ts"}
        text = ("import a from './a';\nimport type { B } from \"./b\";\nexport * from './c';\n"
                "import './d.js';\nconst e = require('./e');\nconst f = await import('./f.mjs');\n"
                "import data from './data.json';\nimport {\n  x,\n  y,\n} from '../lib/util';\n"
                "import React from 'react';\nimport missing from './nope';\n")
        self.assertEqual(graph.js_imports("src/main.ts", text, files),
                         sorted(["src/a.ts", "src/b.tsx", "src/c/index.ts", "src/d.js", "src/e.ts",
                                 "src/f.mjs", "src/data.json", "lib/util.ts"]))
        self.assertEqual(graph.js_imports("src/x.ts", "import y from './e.js';", files), ["src/e.ts"])
        aliases = [("@/", True, ["./src/"]), ("~lib", False, ["lib/util"])]
        self.assertEqual(graph.js_imports("t.ts", "import a from '@/c';\nimport u from '~lib';",
                                          files, aliases), ["lib/util.ts", "src/c/index.ts"])

    def test_tsconfig_aliases_from_jsonc(self):
        repo = TempRepo("js")
        try:
            self.assertEqual(graph.tsconfig_aliases(repo.root), [("@/", True, ["./src/"])])
            self.assertEqual(graph.tsconfig_aliases(repo.root, "missing.json"), [])
        finally:
            repo.close()

    def test_go_imports_blocks_and_aliases(self):
        text = ('package b\n\nimport (\n\t"fmt"\n\tx "example.com/m/pkg/a"\n\t_ "example.com/m/internal/c"\n'
                '\t// "example.com/m/commented"\n)\n\nimport "example.com/m/pkg/d"\n')
        self.assertEqual(graph.go_imports("pkg/b/b.go", text, "example.com/m"),
                         ["internal/c", "pkg/a", "pkg/d"])
        self.assertEqual(graph.go_imports("svc/pkg/b/b.go", text, "example.com/m", "svc"),
                         ["svc/internal/c", "svc/pkg/a", "svc/pkg/d"])
        self.assertEqual(graph.go_imports("pkg/b/b.go", text, None), [])

    def test_rust_crates_and_toml_subset(self):
        repo = TempRepo("rust")
        try:
            crates = graph.rust_crates(repo.root, graph_files(repo.root))
            self.assertEqual(crates, {"api": {"dir": "crates/api", "deps": ["core"]},
                                      "core": {"dir": "crates/core", "deps": []}})
            self.assertEqual(graph.crate_of("crates/api/tests/api_test.rs", crates), "api")
            self.assertIsNone(graph.crate_of("README.md", crates))
        finally:
            repo.close()
        repo = TempRepo("rust")
        try:                                   # workspace-inherited path dependencies
            repo.write("Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n\n'
                                     '[workspace.dependencies]\ncore = { path = "crates/core" }\n')
            for deps in ('[dev-dependencies]\ncore.workspace = true\n',
                         '[dependencies.core]\nworkspace = true\n',
                         '[dependencies]\ncore = { workspace = true, features = ["x"] }\n'):
                repo.write("crates/api/Cargo.toml", '[package]\nname = "api"\n\n' + deps)
                crates = graph.rust_crates(repo.root, graph_files(repo.root))
                self.assertEqual(crates["api"]["deps"], ["core"], deps)
        finally:
            repo.close()
        t = graph.parse_toml('[workspace]\nmembers = [\n  "a",  # first\n  "b",\n]\n'
                             '[dependencies.x]\npath = "../x"\n[dependencies]\n'
                             'y = { path = "../y", package = "real-y" }\nz = "1.0"\n')
        self.assertEqual(t["workspace"]["members"], ["a", "b"])
        self.assertEqual(t["dependencies.x"]["path"], "../x")
        self.assertEqual(t["dependencies"]["y"], {"path": "../y", "package": "real-y"})
        self.assertEqual(t["dependencies"]["z"], "1.0")
        self.assertEqual(graph.parse_toml('[package]\nname = "a"\nmeta.x.y = 1\n')["package"],
                         {"name": "a", "meta": {"x": {"y": 1}}})

    def test_globs_are_gitignore_style(self):
        self.assertTrue(glob_match("conftest.py", "a/b/conftest.py"))
        self.assertTrue(glob_match("**/test_*.py", "test_x.py"))
        self.assertTrue(glob_match("**/test_*.py", "tests/unit/test_x.py"))
        self.assertTrue(glob_match("docs/**", "docs/a/b.md"))
        self.assertTrue(glob_match(".github/workflows/**", ".github/workflows/ci.yml"))
        self.assertFalse(glob_match("tests/*.py", "tests/a/b.py"))
        self.assertTrue(glob_match("requirements*.txt", "requirements-dev.txt"))
        self.assertFalse(glob_match("/setup.py", "sub/setup.py"))
        self.assertTrue(glob_match("[Ll]icense*", "License.txt"))


def graph_files(root):
    from devkit.common import DEFAULT_CONFIG, walk_files
    return walk_files(root, DEFAULT_CONFIG["exclude_dirs"])


# ---------------------------------------------------------------- affected: python fixture


class PythonAffectedTests(FixtureCase):
    repo_name = "py"

    def test_core_change_selects_both_tests_transitively_with_chains(self):
        res = self.repo.select("pkg/core.py")
        self.assertEqual(res["mode"], "selected")
        self.assertEqual(res["targets"], {"python": ["tests/test_api.py", "tests/test_core.py"]})
        self.assertEqual(res["chains"]["python"]["tests/test_api.py"],
                         ["tests/test_api.py", "pkg/api.py", "pkg/core.py (changed)"])
        self.assertEqual(res["chains"]["python"]["tests/test_core.py"],
                         ["tests/test_core.py", "pkg/core.py (changed)"])
        self.assertEqual(self.repo.select("pkg/api.py")["targets"],
                         {"python": ["tests/test_api.py"]})

    def test_explain_cli_prints_the_chain(self):
        p = run_dev(["affected", "--repo", self.root, "pkg/core.py", "--explain"], cwd=self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines(), [
            "tests/test_api.py tests/test_core.py",
            "  tests/test_api.py <- pkg/api.py <- pkg/core.py (changed)",
            "  tests/test_core.py <- pkg/core.py (changed)"])
        j = json.loads(run_dev(["affected", "--repo", self.root, "pkg/core.py", "--json"],
                               cwd=self.root).stdout)
        self.assertEqual(j["schema"], "devkit-affected/1")
        self.assertEqual(j["mode"], "selected")
        self.assertEqual(j["args"]["python"], ["tests/test_api.py", "tests/test_core.py"])

    def test_sensitive_files_force_a_full_run(self):
        for path in ("conftest.py", "requirements.lock", "pyproject.toml"):
            res = self.repo.select(path, "pkg/core.py")
            self.assertEqual((res["mode"], res["reason"]), ("full", "sensitive file: %s" % path))
            self.assertEqual(res["targets"], {})
        p = run_dev(["affected", "--repo", self.root, "conftest.py"], cwd=self.root)
        self.assertEqual(p.stdout.strip(), "full: sensitive file: conftest.py")

    def test_deleted_file_forces_full_unless_ignored(self):
        os.remove(os.path.join(self.root, "pkg", "api.py"))
        res = self.repo.select("pkg/api.py")
        self.assertEqual((res["mode"], res["reason"]), ("full", "deleted or renamed: pkg/api.py"))
        os.remove(os.path.join(self.root, "README.md"))
        res = self.repo.select("README.md")
        self.assertEqual((res["mode"], res["reason"]), ("none", "only ignored paths changed"))

    def test_ignored_only_change_is_none(self):
        res = self.repo.select("README.md")
        self.assertEqual((res["mode"], res["reason"], res["ignored"]),
                         ("none", "only ignored paths changed", ["README.md"]))
        res = self.repo.select("README.md", "pkg/api.py")
        self.assertEqual(res["targets"], {"python": ["tests/test_api.py"]})

    def test_changed_test_file_selects_itself(self):
        res = self.repo.select("tests/test_core.py")
        self.assertEqual(res["targets"], {"python": ["tests/test_core.py"]})
        self.assertEqual(res["chains"]["python"]["tests/test_core.py"], ["tests/test_core.py (changed)"])

    def test_test_map_overrides_the_graph(self):
        self.repo.config({"test_map": {"pkg/core.py": ["tests/test_core.py"],
                                       "data/**": ["tests/test_api.py"],
                                       "_doc": "comments are skipped"}})
        res = self.repo.select("pkg/core.py")
        self.assertEqual(res["targets"], {"python": ["tests/test_core.py"]})
        self.assertEqual(res["chains"]["python"]["tests/test_core.py"],
                         ["tests/test_core.py", "pkg/core.py (changed; test_map)"])
        self.repo.write("data/rows.csv", "a,b\n")
        self.assertEqual(self.repo.select("data/rows.csv")["targets"],
                         {"python": ["tests/test_api.py"]})

    def test_unmapped_sources_fail_closed_and_name_match_maps(self):
        self.repo.write("pkg/orphan.py", "X = 1\n")
        res = self.repo.select("pkg/orphan.py")
        self.assertEqual((res["mode"], res["reason"]), ("full", "unmapped source file: pkg/orphan.py"))
        self.repo.write("data/rows.csv", "a,b\n")
        res = self.repo.select("data/rows.csv")
        self.assertEqual((res["mode"], res["reason"]), ("full", "unmapped source file: data/rows.csv"))
        self.repo.write("pkg/util.py", "Y = 2\n")
        self.repo.write("tests/test_util.py", "import importlib\nimport unittest\n")
        res = self.repo.select("pkg/util.py")
        self.assertEqual(res["targets"], {"python": ["tests/test_util.py"]})
        self.assertEqual(res["chains"]["python"]["tests/test_util.py"],
                         ["tests/test_util.py", "pkg/util.py (changed; name match)"])

    def test_too_many_changed_files_is_full(self):
        self.repo.config({"max_changed_files": 1})
        res = self.repo.select("pkg/core.py", "pkg/api.py")
        self.assertEqual((res["mode"], res["reason"]),
                         ("full", "2 changed files exceed max_changed_files (1)"))
        self.assertEqual(self.repo.select("pkg/core.py")["mode"], "selected")

    def test_config_errors_and_warnings(self):
        self.repo.config({"schema": "devkit-config/1", "mystery": 1, "commands": {"python": {"lnt": None}}})
        p = run_dev(["affected", "--repo", self.root, "pkg/core.py"], cwd=self.root)
        self.assertEqual(p.returncode, 0)
        self.assertIn("unknown key 'mystery' ignored", p.stderr)
        self.assertIn("unknown step commands.python.lnt ignored", p.stderr)
        self.repo.config({"sensitive": "conftest.py"})
        p = run_dev(["affected", "--repo", self.root, "pkg/core.py"], cwd=self.root)
        self.assertEqual(p.returncode, 2)
        self.assertIn("dev.py affected: tools/dev.json: sensitive must be a list", p.stderr)
        self.repo.write("tools/dev.json", "{not json")
        p = run_dev(["affected", "--repo", self.root, "pkg/core.py"], cwd=self.root)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)

    def test_no_git_and_no_files_is_a_usage_error(self):
        p = run_dev(["affected", "--repo", self.root], cwd=self.root)
        self.assertEqual(p.returncode, 2)
        self.assertIn("dev.py affected: no git: pass files explicitly", p.stderr)
        p = run_dev(["affected", "--repo", self.root, "/"], cwd=self.root)
        self.assertEqual(p.returncode, 2)
        self.assertIn("outside the repository root", p.stderr)


# ---------------------------------------------------------------- affected: js, go, rust


class OtherLanguageAffectedTests(unittest.TestCase):
    def test_js_resolves_through_index_and_alias(self):
        repo = TempRepo("js")
        try:
            res = repo.select("src/paginate.ts")
            self.assertEqual(res["targets"], {"typescript": ["src/paginate.test.ts"]})
            self.assertEqual(res["chains"]["typescript"]["src/paginate.test.ts"],
                             ["src/paginate.test.ts", "src/index.ts", "src/paginate.ts (changed)"])
            self.assertEqual(repo.select("README.md")["mode"], "none")
            self.assertEqual(repo.select("package.json")["mode"], "full")
            self.assertEqual(repo.select("tsconfig.json")["reason"], "sensitive file: tsconfig.json")
        finally:
            repo.close()

    def test_go_selects_package_directories(self):
        repo = TempRepo("go")
        try:
            res = repo.select("pkg/a/a.go")
            self.assertEqual(res["targets"], {"go": ["./pkg/a", "./pkg/b"]})
            self.assertEqual(res["chains"]["go"]["./pkg/b"],
                             ["./pkg/b", "./pkg/a", "pkg/a/a.go (changed)"])
            self.assertEqual(repo.select("pkg/b/b_test.go")["targets"], {"go": ["./pkg/b"]})
            self.assertEqual(repo.select("go.mod")["mode"], "full")
            p = run_dev(["affected", "--repo", repo.root, "pkg/a/a.go"], cwd=repo.root)
            self.assertEqual(p.stdout.strip(), "./pkg/a ./pkg/b")
        finally:
            repo.close()

    def test_rust_selects_crates(self):
        repo = TempRepo("rust")
        try:
            res = repo.select("crates/core/src/lib.rs")
            self.assertEqual(res["targets"], {"rust": ["api", "core"]})
            self.assertEqual(res["args"], {"rust": ["-p", "api", "-p", "core"]})
            self.assertEqual(res["chains"]["rust"]["api"],
                             ["api", "core", "crates/core/src/lib.rs (changed)"])
            self.assertEqual(repo.select("crates/api/tests/api_test.rs")["targets"], {"rust": ["api"]})
            self.assertEqual(repo.select("crates/core/Cargo.toml")["mode"], "full")
            p = run_dev(["affected", "--repo", repo.root, "crates/core/src/lib.rs"], cwd=repo.root)
            self.assertEqual(p.stdout.strip(), "-p api -p core")
        finally:
            repo.close()

    def test_js_package_one_level_down(self):
        repo = TempRepo("py")
        try:
            shutil.copytree(os.path.join(REPOS, "js"), os.path.join(repo.root, "web"))
            info = detect(repo.root)
            self.assertEqual(info["languages"], ["python", "typescript"])
            ts = info["langs"]["typescript"]
            self.assertEqual((ts["root"], ts["tsconfig"], ts["package_manager"]),
                             ("web", "web/tsconfig.json", "pnpm"))
            info["langs"]["typescript"]["scripts"] = {}
            st = resolve(info, "typescript", "typecheck", which=which_of("pnpm"))
            self.assertEqual(st["command"], "pnpm exec tsc --noEmit -p tsconfig.json")
            res = repo.select("web/src/paginate.ts", "pkg/api.py")
            self.assertEqual(res["targets"], {"python": ["tests/test_api.py"],
                                              "typescript": ["web/src/paginate.test.ts"]})
            self.assertEqual(res["chains"]["typescript"]["web/src/paginate.test.ts"],
                             ["web/src/paginate.test.ts", "web/src/index.ts",
                              "web/src/paginate.ts (changed)"])
        finally:
            repo.close()

    def test_several_languages_print_one_line_each(self):
        repo = TempRepo("py")
        try:
            shutil.copytree(os.path.join(REPOS, "go", "pkg"), os.path.join(repo.root, "pkg", "gosrc"))
            shutil.copy(os.path.join(REPOS, "go", "go.mod"), repo.root)
            with open(os.path.join(repo.root, "pkg/gosrc/b/b.go")) as f:
                text = f.read().replace("example.com/fixture/pkg/a", "example.com/fixture/pkg/gosrc/a")
            repo.write("pkg/gosrc/b/b.go", text)
            p = run_dev(["affected", "--repo", repo.root, "pkg/core.py", "pkg/gosrc/a/a.go"],
                        cwd=repo.root)
            self.assertEqual(p.stdout.splitlines(), [
                "python: tests/test_api.py tests/test_core.py",
                "go: ./pkg/gosrc/a ./pkg/gosrc/b"])
        finally:
            repo.close()


# ---------------------------------------------------------------- changed_files with real git


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class ChangedFilesTests(FixtureCase):
    repo_name = "py"

    def git(self, *args):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith("GIT_"):
                del env[k]
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "HOME": self.repo.tmp,
                    "GIT_AUTHOR_NAME": "devkit", "GIT_AUTHOR_EMAIL": "devkit@example.invalid",
                    "GIT_COMMITTER_NAME": "devkit", "GIT_COMMITTER_EMAIL": "devkit@example.invalid"})
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                               "-c", "init.defaultBranch=main"] + list(args),
                              cwd=self.root, env=env, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True).stdout

    def test_rename_reports_both_paths_and_untracked_files(self):
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.assertEqual(changed_files(self.root), [])
        self.git("mv", "pkg/api.py", "pkg/public.py")
        with open(os.path.join(self.root, "pkg", "core.py"), "a") as f:
            f.write("\n# edited\n")
        self.repo.write("pkg/new_module.py", "Z = 3\n")
        got = changed_files(self.root)
        self.assertIn(("R", "pkg/public.py", "pkg/api.py"), got)
        self.assertIn(("D", "pkg/api.py", None), got)
        self.assertIn(("M", "pkg/core.py", None), got)
        self.assertIn(("?", "pkg/new_module.py", None), got)
        res = affected(self.root, got, load_config(self.root))
        self.assertEqual((res["mode"], res["reason"]), ("full", "deleted or renamed: pkg/api.py"))

    def test_base_ref_diff_includes_committed_renames(self):
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.git("branch", "base")
        self.git("mv", "pkg/api.py", "pkg/public.py")
        self.git("commit", "-q", "-m", "rename")
        self.assertEqual(changed_files(self.root), [])
        got = changed_files(self.root, base="base")
        self.assertEqual(sorted(got, key=lambda t: t[1]),
                         [("D", "pkg/api.py", None), ("R", "pkg/public.py", "pkg/api.py")])
        p = run_dev(["affected", "--repo", self.root, "--base", "no-such-ref"], cwd=self.root,
                    env={"PATH": os.path.dirname(shutil.which("git"))})
        self.assertEqual(p.returncode, 2)
        self.assertIn("is it a valid ref?", p.stderr)

    def test_base_falls_back_to_origin_and_fresh_config_is_not_sensitive(self):
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.git("update-ref", "refs/remotes/origin/trunk", "HEAD")    # only origin/trunk
        with open(os.path.join(self.root, "pkg", "core.py"), "a") as f:
            f.write("\n# edited\n")
        p = run_dev(["affected", "--repo", self.root, "--base", "trunk"], cwd=self.root,
                    env={"PATH": os.path.dirname(shutil.which("git"))})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("using origin/trunk", p.stderr)
        self.assertEqual(p.stdout.split(), ["tests/test_api.py", "tests/test_core.py"])
        self.repo.config({"budget_seconds": 60})                        # what init leaves
        got = changed_files(self.root)
        self.assertIn(("?", "tools/dev.json", None), got)
        res = affected(self.root, got, load_config(self.root))
        self.assertEqual(res["mode"], "selected")
        self.assertIn("tools/dev.json is new and untracked", res["notes"][0])
        self.git("add", "tools/dev.json")                               # tracked: sensitive again
        res = affected(self.root, changed_files(self.root), load_config(self.root))
        self.assertEqual((res["mode"], res["reason"]), ("full", "sensitive file: tools/dev.json"))

    def test_unmerged_files_are_reported_as_unmerged(self):
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.git("checkout", "-q", "-b", "other")
        self.repo.write("pkg/core.py", "X = 'other'\n")
        self.git("commit", "-q", "-am", "other")
        self.git("checkout", "-q", "main")
        self.repo.write("pkg/core.py", "X = 'main'\n")
        self.git("commit", "-q", "-am", "main")
        try:
            self.git("merge", "-q", "other")
        except subprocess.CalledProcessError:
            pass
        self.assertIn(("U", "pkg/core.py", None), changed_files(self.root))

    def test_no_repository_gives_an_empty_list(self):
        self.assertEqual(changed_files(self.root), [])


if __name__ == "__main__":
    unittest.main()
