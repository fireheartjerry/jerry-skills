"""Navigation commands on the fixture repositories: `map` (exports, imports, importers,
tests, --check), `outline`, `where`, `context` (direct targets, budget, determinism) and
`size-audit`. Every test works on a temporary copy; no git, no toolchain, no network.
"""
import ast
import hashlib
import json
import os
import random
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

from devkit import context, repomap  # noqa: E402


def clean_env(**extra):
    env = dict((k, v) for k, v in os.environ.items() if not k.startswith("DEVKIT_"))
    env.update(extra)
    return env


def run_dev(args, cwd=None, env=None):
    return subprocess.run([sys.executable, "-I", DEV] + list(args), cwd=cwd,
                          env=env or clean_env(), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=60)


class Fixture(object):
    def __init__(self, name):
        self.tmp = tempfile.mkdtemp(prefix="devkit-nav-")
        self.root = os.path.join(self.tmp, name)
        shutil.copytree(os.path.join(REPOS, name), self.root)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, text):
        full = self.path(rel)
        if not os.path.isdir(os.path.dirname(full)):
            os.makedirs(os.path.dirname(full))
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()

    def dev(self, *args, **kw):
        return run_dev(list(args) + ["--repo", self.root], cwd=kw.pop("cwd", self.root), **kw)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def tree_hashes(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".devkit"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


EXPECTED = {
    "py": {"export": ("pkg/core.py", "frobnicate", "function", 4),
           "edge": ("pkg/api.py", "pkg/core.py"),
           "tests": ("pkg/core.py", ["tests/test_api.py", "tests/test_core.py"])},
    "js": {"export": ("src/paginate.ts", "paginate", "function", 1),
           "edge": ("src/index.ts", "src/paginate.ts"),
           "tests": ("src/paginate.ts", ["src/paginate.test.ts"])},
    "go": {"export": ("pkg/a/a.go", "Double", "func", 5),
           "edge": ("pkg/b/b.go", "pkg/a/a.go"),
           "tests": ("pkg/a/a.go", ["pkg/b/b_test.go"])},
    "rust": {"export": ("crates/core/src/lib.rs", "double", "fn", 1),
             "edge": ("crates/api/src/lib.rs", "crates/core/src/lib.rs"),
             "tests": ("crates/core/src/lib.rs", ["crates/api/tests/api_test.rs"])},
}


class MapTests(unittest.TestCase):
    def test_help_for_every_navigation_and_setup_command(self):
        for cmd in ("map", "outline", "where", "context", "init", "size-audit"):
            p = run_dev([cmd, "--help"])
            self.assertEqual(p.returncode, 0, (cmd, p.stderr))
            self.assertIn("usage: dev.py %s" % cmd, p.stdout)
        p = run_dev(["help"])
        for cmd in ("map", "outline", "where", "context", "init", "size-audit"):
            self.assertIn("  %s " % cmd, p.stdout)

    def test_map_on_every_fixture(self):
        for name, want in sorted(EXPECTED.items()):
            fx = Fixture(name)
            try:
                before = tree_hashes(fx.root)
                p = fx.dev("map", "--json")
                self.assertEqual(p.returncode, 0, p.stderr)
                m = json.loads(p.stdout)
                self.assertEqual(json.loads(fx.read(".devkit/repo-map.json")), m)
                self.assertEqual(fx.read(".devkit/.gitignore"), "*\n")
                self.assertEqual(tree_hashes(fx.root), before, "map modified a source file")
                self.assertEqual(m["schema"], "devkit-map/1")
                self.assertEqual(m["root_id"], hashlib.sha1(
                    "\n".join(sorted(m["files"])).encode()).hexdigest())
                path, sym, kind, line = want["export"]
                ex = [e for e in m["files"][path]["exports"] if e["name"] == sym]
                self.assertEqual([(e["kind"], e["line"]) for e in ex], [(kind, line)], name)
                self.assertEqual(sorted(ex[0]), ["kind", "line", "name", "signature", "summary"])
                src, dst = want["edge"]
                self.assertIn(dst, m["files"][src]["imports"], name)
                self.assertIn(src, m["files"][dst]["importers"], name)
                f, tests = want["tests"]
                self.assertEqual(m["files"][f]["tests"], tests, name)
                for entry in m["files"].values():
                    self.assertEqual(sorted(entry), ["exports", "importers", "imports", "lang",
                                                     "tests"])
            finally:
                fx.close()

    def test_check_detects_an_edit_and_never_writes(self):
        fx = Fixture("py")
        try:
            self.assertEqual(fx.dev("map").returncode, 0)
            p = fx.dev("map", "--check")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("is current", p.stdout)
            stored = fx.read(".devkit/repo-map.json")
            fx.write("pkg/core.py", fx.read("pkg/core.py") + "\n\ndef triple(x):\n    return 3 * x\n")
            p = fx.dev("map", "--check")
            self.assertEqual(p.returncode, 1)
            self.assertIn("changed: pkg/core.py", p.stdout)
            self.assertEqual(fx.read(".devkit/repo-map.json"), stored)
            self.assertEqual(repomap.drift(fx.root), ["pkg/core.py"])
            fx.write("pkg/extra.py", "def extra():\n    pass\n")
            j = json.loads(fx.dev("map", "--check", "--json").stdout)
            self.assertEqual(j["changed"], ["pkg/core.py", "pkg/extra.py"])
            self.assertEqual(fx.dev("map").returncode, 0)
            self.assertEqual(fx.dev("map", "--check").returncode, 0)
            self.assertEqual(repomap.drift(fx.root), [])
            shutil.rmtree(fx.path(".devkit"))
            p = fx.dev("map", "--check")
            self.assertEqual(p.returncode, 1)
            self.assertIn("no .devkit/repo-map.json", p.stdout)
        finally:
            fx.close()


PY_SRC = '''"""Module doc."""
import os
from x import helper

__all__ = ["Visible", "go", "LIMIT", "helper"]
LIMIT = 3


# a comment that is not a summary because the docstring wins
@decorator
async def go(a, b=2, *rest, key, **kw) -> int:
    """Run it.

    More text."""
    return a


def hidden():
    pass


class Visible(Base):
    # leading comment
    def method(self, x):
        return x
'''


class SymbolTests(unittest.TestCase):
    def test_python_all_signatures_and_methods(self):
        defs, exports, summary = repomap.symbols("m.py", PY_SRC)
        self.assertEqual(summary, "Module doc.")
        self.assertEqual([(e["name"], e["kind"], e["line"]) for e in exports],
                         [("helper", "reexport", 3), ("LIMIT", "variable", 6),
                          ("go", "function", 11), ("Visible", "class", 22)])
        go = [d for d in defs if d["name"] == "go"][0]
        self.assertEqual(go["start"], 9)                     # comment and decorator included
        self.assertEqual(go["summary"], "Run it.")
        self.assertEqual(go["signature"], "async def go(a, b=2, *rest, key, **kw) -> int")
        method = [d for d in defs if d["name"] == "Visible.method"][0]
        self.assertEqual((method["depth"], method["kind"], method["line"]), (1, "method", 24))
        self.assertEqual(method["summary"], "leading comment")
        self.assertNotIn("hidden", [e["name"] for e in exports])
        defs2, exports2, _ = repomap.symbols("n.py", "def a():\n    pass\n\ndef _b():\n    pass\n")
        self.assertEqual([e["name"] for e in exports2], ["a"])
        self.assertEqual([d["name"] for d in defs2], ["a", "_b"])

    def test_leading_comments_stop_at_another_indentation(self):
        src = "def a():\n    x = 1\n    # trailing note of a\ndef b():\n    pass\n"
        defs, _, _ = repomap.symbols("m.py", src)
        b = [d for d in defs if d["name"] == "b"][0]
        self.assertEqual((b["start"], b["summary"]), (4, ""))
        go = "// One.\nfunc One() {}\n\t// inner\nfunc Two() {}\n"
        defs, _, _ = repomap.symbols("m.go", go)
        self.assertEqual([(d["name"], d["start"], d["summary"]) for d in defs],
                         [("One", 1, "One."), ("Two", 4, "")])

    def test_python_38_fallback_signature(self):
        saved = getattr(repomap.ast, "unparse", None)
        if saved is not None:
            del repomap.ast.unparse
        try:
            defs, _, _ = repomap.symbols("m.py", PY_SRC)
            go = [d for d in defs if d["name"] == "go"][0]
            self.assertEqual(go["signature"], "async def go(a, b, *rest, key, **kw)")
            self.assertEqual([d for d in defs if d["name"] == "Visible"][0]["signature"],
                             "class Visible(Base)")
        finally:
            if saved is not None:
                repomap.ast.unparse = saved

    def test_python_syntax_error_falls_back_to_regex(self):
        defs, exports, _ = repomap.symbols("bad.py", "def ok(x):\n    return (\n\nclass K:\n    def m(self):\n")
        self.assertEqual([(d["name"], d["depth"]) for d in defs], [("ok", 0), ("K", 0), ("K.m", 1)])
        self.assertEqual([e["name"] for e in exports], ["ok", "K"])

    def test_typescript(self):
        src = ("// Paging helpers.\n"
               "export async function load(id: string): Promise<T> {\n}\n"
               "export const add = (a: number) => a + 1;\n"
               "export default function () {}\n"
               "export interface Shape { w: number }\n"
               "export type Id = string;\n"
               "export enum Color { Red }\n"
               "function local() {}\n"
               "const notAFunction = 3;\n"
               "const arrow = async (x) => x;\n"
               "export { local as renamed, arrow };\n"
               "export * from './other';\n"
               "class Hidden {}\n"
               "export default config;\n")
        defs, exports, summary = repomap.symbols("a.ts", src)
        self.assertEqual(summary, "Paging helpers.")
        self.assertEqual([(e["name"], e["kind"], e["line"]) for e in exports],
                         [("load", "function", 2), ("add", "const", 4), ("default", "function", 5),
                          ("Shape", "interface", 6), ("Id", "type", 7), ("Color", "enum", 8),
                          ("arrow", "function", 11), ("renamed", "reexport", 12),
                          ("*", "reexport", 13), ("default", "default", 15)])
        self.assertEqual(exports[0]["signature"], "export async function load(id: string): Promise<T>")
        self.assertEqual(exports[0]["summary"], "Paging helpers.")
        names = [d["name"] for d in defs]
        self.assertIn("local", names)
        self.assertIn("Hidden", names)
        self.assertNotIn("notAFunction", names)

    def test_go(self):
        src = ("package p\n\n// Box holds one value.\ntype Box[T any] struct{ v T }\n\n"
               "// Get returns it.\nfunc (b *Box[T]) Get() T { return b.v }\n"
               "func helper() {}\nvar Limit = 3\nconst small = 1\n")
        defs, exports, _ = repomap.symbols("p.go", src)
        self.assertEqual([(e["name"], e["kind"], e["line"], e["summary"]) for e in exports],
                         [("Box", "type", 4, "Box holds one value."),
                          ("Box.Get", "method", 7, "Get returns it."), ("Limit", "var", 9, "")])
        self.assertEqual(exports[1]["signature"], "func (b *Box[T]) Get() T")
        self.assertIn("helper", [d["name"] for d in defs])

    def test_rust(self):
        src = ("/// A point.\n#[derive(Debug)]\npub struct Point { x: i64 }\n\n"
               "impl Point {\n    pub fn new() -> Self { Point { x: 0 } }\n    fn private(&self) {}\n}\n\n"
               "pub(crate) fn internal() {}\npub const fn zero() -> i64 { 0 }\nmod tests;\n"
               "pub static mut COUNT: u32 = 0;\n")
        defs, exports, _ = repomap.symbols("lib.rs", src)
        self.assertEqual([(e["name"], e["kind"], e["line"]) for e in exports],
                         [("Point", "struct", 3), ("Point::new", "method", 6), ("zero", "fn", 11),
                          ("COUNT", "static", 13)])
        point = [d for d in defs if d["name"] == "Point"][0]
        self.assertEqual((point["start"], point["summary"]), (1, "A point."))
        names = [d["name"] for d in defs]
        for n in ("impl Point", "Point::private", "internal", "tests"):
            self.assertIn(n, names)

    def test_rust_mod_files_and_crate_edges(self):
        fx = Fixture("rust")
        try:
            fx.write("crates/core/src/lib.rs", "pub mod util;\npub fn double(x: i64) -> i64 {\n"
                     "    util::twice(x)\n}\n")
            fx.write("crates/core/src/util.rs", "pub fn twice(x: i64) -> i64 { 2 * x }\n")
            m = repomap.build(fx.root)
            self.assertEqual(m["files"]["crates/core/src/lib.rs"]["imports"],
                             ["crates/core/src/util.rs"])
            self.assertEqual(m["files"]["crates/core/src/util.rs"]["tests"],
                             ["crates/api/tests/api_test.rs"])
        finally:
            fx.close()


class OutlineWhereTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture("py")

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_outline(self):
        fx = self.fx
        p = fx.dev("outline", "pkg/core.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines(),
                         ["pkg/core.py (python, 6 lines): Core logic.",
                          "4  def frobnicate(values)  # Sorted unique values, doubled."])
        fx.write("tools/sections.py", "# ==== Setup ====\nimport os\n\n\ndef _a():\n    pass\n"
                 "# ---------------------------------------------------------------- io\n"
                 "class B:\n    def c(self):\n        pass\n")
        j = json.loads(fx.dev("outline", "tools/sections.py", "--json").stdout)
        self.assertEqual(j["schema"], "devkit-outline/1")
        self.assertEqual([(i["line"], i["kind"], i["name"]) for i in j["items"]],
                         [(1, "section", "Setup"), (5, "function", "_a"), (7, "section", "io"),
                          (8, "class", "B"), (9, "method", "B.c")])
        text = fx.dev("outline", "tools/sections.py").stdout
        self.assertIn(" 1  ---- Setup", text)
        self.assertIn("(private)", text)
        os.remove(fx.path("tools/sections.py"))
        fx.write("notes.txt", "x\n")
        for args in (["outline", "notes.txt"], ["outline", "missing.py"]):
            p = fx.dev(*args)
            self.assertEqual(p.returncode, 2, args)
            self.assertTrue(p.stderr.startswith("dev.py outline:"), p.stderr)
        os.remove(fx.path("notes.txt"))

    def test_where_from_memory_then_stored_then_stale(self):
        fx = self.fx
        shutil.rmtree(fx.path(".devkit"), ignore_errors=True)
        p = fx.dev("where", "frobnicate")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("built the map in memory", p.stderr)
        lines = p.stdout.splitlines()
        self.assertTrue(lines[0].startswith("pkg/core.py:4  def frobnicate(values)"), lines)
        self.assertIn("importers: pkg/api.py, tests/test_core.py", p.stdout)
        self.assertEqual(fx.dev("map").returncode, 0)
        j = json.loads(fx.dev("where", "frobnicate", "--json").stdout)
        self.assertEqual(j["map"], ".devkit/repo-map.json")
        d = j["definitions"][0]
        self.assertEqual((d["path"], d["line"]), ("pkg/core.py", 4))
        self.assertIn("pkg/api.py", d["importers"])
        original = fx.read("pkg/core.py")
        try:
            fx.write("pkg/core.py", "\n\n" + original)
            p = fx.dev("where", "frobnicate")
            self.assertIn("stale", p.stderr)
            self.assertTrue(p.stdout.startswith("pkg/core.py:6 "), p.stdout)
        finally:
            fx.write("pkg/core.py", original)
            fx.dev("map")
        p = fx.dev("where", "CoreTest.test_empty")
        self.assertTrue(p.stdout.startswith("tests/test_core.py:10 "), p.stdout)
        self.assertIn("not exported", p.stdout)
        p = fx.dev("where", "pkg.api.paginate")
        self.assertTrue(p.stdout.startswith("pkg/api.py:5 "), p.stdout)
        p = fx.dev("where", "no_such_symbol")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stderr.startswith("dev.py where: no definition of 'no_such_symbol'"))

    def test_where_runs_from_a_subdirectory(self):
        fx = self.fx
        p = run_dev(["where", "paginate"], cwd=fx.path("pkg"),
                    env=clean_env(DEVKIT_REPO=fx.root))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("pkg/api.py:5 "), p.stdout)


class ContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture("py")

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def packet(self, *args):
        p = self.fx.dev("context", *(list(args) + ["--json"]))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_direct_target_first_and_must_keep(self):
        j = self.packet("frobnicate core", "--budget", "2000")
        self.assertEqual(j["schema"], "devkit-context/1")
        self.assertEqual(j["direct_targets"][0], "pkg/core.py")
        self.assertEqual(j["direct_reasons"]["pkg/core.py"], ["path matches core", "defines frobnicate"])
        core = [c for c in j["chunks"] if c["path"] == "pkg/core.py"]
        self.assertEqual([(c["start"], c["end"], c["must_keep"]) for c in core], [(1, 6, True)])
        self.assertEqual(j["chunks"], sorted(j["chunks"], key=lambda c: (c["path"], c["start"])))
        self.assertEqual(j["token_estimate"], "chars/4")
        self.assertEqual(j["used"], sum(c["tokens"] for c in j["chunks"]))
        self.assertIn("pkg/api.py", [e["path"] for e in j["expanded"]])
        seen = set()
        for c in j["chunks"]:
            for ln in range(c["start"], c["end"] + 1):
                self.assertNotIn((c["path"], ln), seen)
                seen.add((c["path"], ln))

    def test_budget_is_respected(self):
        for budget in (2000, 200, 60, 40, 30, 10, 1):
            j = self.packet("frobnicate core", "--budget", str(budget))
            self.assertEqual(j["limit"], int(budget * 0.9))
            self.assertIn("pkg/core.py", [c["path"] for c in j["chunks"] if c["must_keep"]])
            if "over budget" in j["note"]:
                self.assertTrue(all(c["must_keep"] for c in j["chunks"]), budget)
                continue
            self.assertLessEqual(j["used"], j["limit"], budget)
        j = self.packet("frobnicate core", "--budget", "30")
        self.assertEqual(j["note"], "must-keep set exceeded budget; bodies trimmed")
        core = [c for c in j["chunks"] if c["path"] == "pkg/core.py"][0]
        self.assertEqual(core["lines"], [[1, 1], [4, 4]])        # docstring and def line

    def test_byte_identical_across_runs_and_hash_seeds(self):
        outs = set()
        for seed in ("0", "1", "12345"):
            env = clean_env(PYTHONHASHSEED=seed)
            for extra in ([], ["--json"], ["--emit"]):
                p = subprocess.run([sys.executable, DEV, "context", "frobnicate core", "--budget",
                                    "2000", "--repo", self.fx.root] + extra, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                outs.add((tuple(extra), p.stdout))
        self.assertEqual(len(outs), 3)

    def test_must_keep_and_errors(self):
        j = self.packet("frobnicate", "--budget", "2000", "--must-keep", "pkg/api.py:5-8",
                        "--must-keep", "README.md")
        kept = [(c["path"], c["start"], c["end"]) for c in j["chunks"] if c["must_keep"]]
        self.assertIn(("pkg/api.py", 5, 8), kept)
        self.assertIn("README.md", [k[0] for k in kept])
        for args in (["--must-keep", "nope.py"], ["--must-keep", "pkg/api.py:5-99"],
                     ["--budget", "0"]):
            p = self.fx.dev("context", "frobnicate", *args)
            self.assertEqual(p.returncode, 2, args)
            self.assertTrue(p.stderr.startswith("dev.py context:"), p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    def test_human_output_lists_every_chunk(self):
        p = self.fx.dev("context", "frobnicate core", "--budget", "2000")
        out = p.stdout
        self.assertIn("direct targets: pkg/core.py (path matches core, defines frobnicate)", out)
        self.assertIn("tokens are chars/4 estimates", out)
        self.assertIn("must-keep: path matches core; defines frobnicate", out)
        self.assertRegex(out, r"used \d+ of 1800 tokens \(budget 2000; estimate\)")
        emitted = self.fx.dev("context", "frobnicate core", "--budget", "2000", "--emit").stdout
        self.assertIn("==> pkg/core.py:1-6 <==", emitted)
        self.assertIn("return [2 * v for v in sorted(set(values))]", emitted)

    def test_common_names_do_not_become_direct_targets(self):
        fx = Fixture("py")
        try:
            for i in range(7):
                fx.write("mods/m%d.py" % i, "def process(x):\n    return x\n")
            j = json.loads(fx.dev("context", "process frobnicate", "--json").stdout)
            self.assertEqual(j["direct_targets"], ["pkg/core.py"])
            self.assertTrue(any("'process' is defined in or names 7 files" in n for n in j["notes"]))
        finally:
            fx.close()

    def test_bm25_index_path_on_a_larger_repo(self):
        fx = Fixture("py")
        try:
            rng = random.Random(5)
            words = ["cache", "parse", "token", "budget", "graph", "retry", "merge", "index"]
            for i in range(60):
                body = "".join("def %s_%d_%d(x):\n    return %s(x)\n\n\n" % (
                    rng.choice(words), i, k, rng.choice(words)) for k in range(4))
                fx.write("gen/mod%02d.py" % i, body)
            outs = [fx.dev("context", "merge_3_1 retry budget", "--budget", "800", "--json").stdout
                    for _ in range(2)]
            self.assertEqual(outs[0], outs[1])
            j = json.loads(outs[0])
            self.assertTrue(j["retrieval"].startswith("bm25 index over"), j["retrieval"])
            self.assertGreater(j["chunks_total"], context.BM25_MIN_CHUNKS)
            self.assertLessEqual(j["used"], j["limit"])
        finally:
            fx.close()

    def test_emitted_packet_with_headers_fits_the_budget(self):
        fx = Fixture("py")
        try:
            for i in range(40):
                fx.write("gen/m%02d.py" % i, "".join(
                    "def retry_%d_%d(x):\n    return x\n\n\n" % (i, k) for k in range(6)))
            budget = 1500
            p = fx.dev("context", "retry budget parse", "--budget", str(budget), "--emit",
                       "--min-score", "0", "--must-keep-text",
                       "python3 -m unittest tests/test_core.py")
            self.assertEqual(p.returncode, 0, p.stderr)
            body = p.stdout[p.stdout.index("==>"):]
            self.assertLessEqual((len(body) + 3) // 4, int(budget * 0.9))
            self.assertIn("==> <text> <==\npython3 -m unittest tests/test_core.py", p.stdout)
            self.assertRegex(p.stdout, r"emitted \d+ tokens")
            j = json.loads(fx.dev("context", "retry budget parse", "--budget", str(budget),
                                  "--min-score", "0", "--must-keep-text", "make test",
                                  "--json").stdout)
            self.assertEqual(j["chunks"][0]["path"], "<text>")
            self.assertEqual(j["used"], sum(c["tokens"] for c in j["chunks"]))
            self.assertLessEqual(j["used"], j["limit"])
        finally:
            fx.close()

    def test_min_score_floor_and_instruction_words(self):
        self.assertEqual(context.names_in("fix paginate after the empty status line; run "
                                          "python -m unittest test_httplib"),
                         ["paginate", "test_httplib"])
        loose = self.packet("frobnicate core", "--budget", "20000", "--min-score", "0")
        tight = self.packet("frobnicate core", "--budget", "20000", "--min-score", "0.9")
        self.assertLessEqual(len(tight["chunks"]), len(loose["chunks"]))
        self.assertTrue(all(c["score"] >= 0.9 or c["must_keep"] for c in tight["chunks"]))
        if tight["dropped_below_min_score"]:
            self.assertTrue(any("under --min-score" in n for n in tight["notes"]))

    def test_adjacent_chunks_emit_as_one_block(self):
        blocks = context.emit_blocks([
            {"path": "a.py", "start": 1, "end": 3, "lines": None, "text": "x\ny\nz"},
            {"path": "a.py", "start": 4, "end": 5, "lines": None, "text": "u\nv"},
            {"path": "a.py", "start": 9, "end": 9, "lines": None, "text": "w"}])
        self.assertEqual([h for h, _ in blocks], ["==> a.py:1-5 <==", "==> a.py:9-9 <=="])

    def test_chunk_ranges_partition_and_split(self):
        lines = ["x = 1"] * 3 + ["def f():"] + ["    pass"] * 300 + ["", "def g():", "    pass"]
        defs = [{"name": "f", "depth": 0, "start": 4, "line": 4},
                {"name": "g", "depth": 0, "start": 306, "line": 306}]
        got = context.chunk_ranges(lines, defs)
        covered = [ln for a, b, _ in got for ln in range(a, b + 1)]
        self.assertEqual(covered, list(range(1, len(lines) + 1)))
        self.assertTrue(all(b - a + 1 <= context.MAX_CHUNK_LINES for a, b, _ in got))
        self.assertEqual(got[0], (1, 3, "module header"))
        self.assertEqual(got[-1], (306, 307, "g"))


BIG = "def huge(x):\n" + "".join("    x += %d\n" % i for i in range(80)) + "    return x\n"


class SizeAuditTests(unittest.TestCase):
    def test_oversized_function_and_file(self):
        fx = Fixture("py")
        try:
            p = fx.dev("size-audit")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("size-audit: PASS", p.stdout)
            fx.write("pkg/big.py", BIG)
            p = fx.dev("size-audit")
            self.assertEqual(p.returncode, 1)
            self.assertIn("over  function  pkg/big.py:1 huge: 82 lines (limit 70)", p.stdout)
            j = json.loads(fx.dev("size-audit", "--json").stdout)
            self.assertEqual(j["schema"], "devkit-size-audit/1")
            self.assertEqual([(v["path"], v["name"], v["lines"]) for v in j["function_violations"]],
                             [("pkg/big.py", "huge", 82)])
            fx.write("tools/dev.json", json.dumps({"size_audit": {"file_lines": 50,
                                                                  "function_lines": 100}}))
            j = json.loads(fx.dev("size-audit", "--json").stdout)
            self.assertEqual(j["function_violations"], [])
            self.assertEqual([v["path"] for v in j["file_violations"]], ["pkg/big.py"])
            fx.write("tests/test_big.py", BIG)                  # test files: only on request
            j = json.loads(fx.dev("size-audit", "--json").stdout)
            self.assertNotIn("tests/test_big.py", [v["path"] for v in j["file_violations"]])
            self.assertGreaterEqual(j["test_files_skipped"], 1)
            j = json.loads(fx.dev("size-audit", "--include-tests", "--json").stdout)
            self.assertIn("tests/test_big.py", [v["path"] for v in j["file_violations"]])
            fx.write("tools/dev.json", json.dumps({"size_audit": {"file_lines": "many"}}))
            p = fx.dev("size-audit")
            self.assertEqual(p.returncode, 2)
            self.assertTrue(p.stderr.startswith("dev.py size-audit:"), p.stderr)
        finally:
            fx.close()

    def test_nested_and_method_names(self):
        from devkit.sizeaudit import python_functions
        src = "class A:\n    def m(self):\n        def inner():\n            pass\n        return 1\n"
        self.assertEqual(python_functions(src), [("A.m", 2, 4), ("A.m.inner", 3, 2)])
        self.assertEqual(python_functions("def broken(:\n"), [])


if __name__ == "__main__":
    unittest.main()
