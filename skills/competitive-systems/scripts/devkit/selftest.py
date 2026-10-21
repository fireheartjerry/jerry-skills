"""`dev.py selftest`: an offline end-to-end check a stranger can run to confirm the tool works
on their machine. It materialises four embedded fixture repositories (Python with a passing
unittest suite, TypeScript with echo scripts and a pnpm lockfile, a Go module with two
packages, a Rust workspace of two crates) in a temporary directory and drives every command
through the real CLI, asserting known answers. No network, no git, no toolchain: each step
runs `dev.py` as a child process with PATH holding only a `python3` link to this interpreter,
so Node, Go, Cargo, linters and git are absent by construction and the result is the same on
every machine. The fixtures live in this file, so it works when installed without tests/.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from devkit.common import Parser, print_json

DEV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dev.py")
STEP_TIMEOUT = 120

PY_TEST_API = '''import unittest

from pkg import api


class ApiTest(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(api.paginate([5, 1, 3], 0), [2, 6])

    def test_last_page(self):
        self.assertEqual(api.paginate([5, 1, 3], 1), [10])
'''

PY_TEST_CORE = '''import unittest

from pkg.core import frobnicate


class CoreTest(unittest.TestCase):
    def test_frobnicate_dedups_and_doubles(self):
        self.assertEqual(frobnicate([3, 1, 3, 2]), [2, 4, 6])

    def test_empty(self):
        self.assertEqual(frobnicate([]), [])
'''

FIXTURES = {
    "python": {
        "pyproject.toml": '[project]\nname = "fixture-pkg"\nversion = "0.1.0"\n'
                          'requires-python = ">=3.8"\n',
        "conftest.py": "# Shared test setup; a change here forces a full test run.\n",
        "pkg/__init__.py": '"""fixture-pkg: frobnicate and a paginated view of it."""\n',
        "pkg/core.py": '"""Core logic."""\n\n\ndef frobnicate(values):\n'
                       '    """Sorted unique values, doubled."""\n'
                       '    return [2 * v for v in sorted(set(values))]\n',
        "pkg/api.py": '"""Public API: pages over frobnicate\'s output."""\n'
                      'from .core import frobnicate\n\n\ndef paginate(values, page, size=2):\n'
                      '    """One page (0-based) of frobnicate(values)."""\n'
                      '    out = frobnicate(values)\n'
                      '    return out[page * size:(page + 1) * size]\n',
        "tests/test_api.py": PY_TEST_API,
        "tests/test_core.py": PY_TEST_CORE,
        "sortmod.py": '"""Reference and candidate for the oracle step."""\n\n\ndef ref(xs):\n'
                      '    return sorted(xs)\n\n\ndef new(xs):\n    out = []\n'
                      '    for x in xs:\n        i = len(out)\n'
                      '        while i > 0 and x < out[i - 1]:\n            i -= 1\n'
                      '        out.insert(i, x)\n    return out\n',
    },
    "typescript": {
        "package.json": json.dumps({
            "name": "fixture-js", "private": True, "type": "module",
            "scripts": {"lint": "echo lint ok", "typecheck": "echo typecheck ok",
                        "test": "echo test ok"},
            "devDependencies": {"typescript": "5.4.5", "vitest": "1.6.0"}}, indent=2) + "\n",
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "tsconfig.json": '{\n  // JSON with comments, as TypeScript allows.\n  "compilerOptions": {\n'
                         '    "strict": true,\n    "baseUrl": ".",\n'
                         '    "paths": {"@/*": ["src/*"],},\n  },\n  "include": ["src"]\n}\n',
        "src/index.ts": "export { paginate } from './paginate';\n",
        "src/paginate.ts": "export function paginate<T>(items: T[], page: number, size = 2): T[] {\n"
                           "  return items.slice(page * size, (page + 1) * size);\n}\n",
        "src/paginate.test.ts": "import { describe, expect, it } from 'vitest';\n"
                                "import { paginate } from '@/index';\n\n"
                                "describe('paginate', () => {\n"
                                "  it('returns the second page', () => {\n"
                                "    expect(paginate([1, 2, 3, 4, 5], 1)).toEqual([3, 4]);\n"
                                "  });\n});\n",
    },
    "go": {
        "go.mod": "module example.com/fixture\n\ngo 1.21\n",
        "pkg/a/a.go": "// Package a doubles numbers.\npackage a\n\n// Double returns 2x.\n"
                      "func Double(x int) int { return 2 * x }\n",
        "pkg/b/b.go": '// Package b builds on package a.\npackage b\n\nimport (\n\t"fmt"\n\n'
                      '\t"example.com/fixture/pkg/a"\n)\n\n// Quad returns 4x as text.\n'
                      'func Quad(x int) string { return fmt.Sprint(a.Double(a.Double(x))) }\n',
        "pkg/b/b_test.go": 'package b\n\nimport "testing"\n\nfunc TestQuad(t *testing.T) {\n'
                           '\tif got := Quad(3); got != "12" {\n'
                           '\t\tt.Fatalf("Quad(3) = %s, want 12", got)\n\t}\n}\n',
    },
    "rust": {
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\nresolver = "2"\n',
        "crates/core/Cargo.toml": '[package]\nname = "core"\nversion = "0.1.0"\n'
                                  'edition = "2021"\n',
        "crates/core/src/lib.rs": "pub fn double(x: i64) -> i64 {\n    2 * x\n}\n",
        "crates/api/Cargo.toml": '[package]\nname = "api"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                 '[dependencies]\n'
                                 'fixture_core = { path = "../core", package = "core" }\n',
        "crates/api/src/lib.rs": "pub fn quad(x: i64) -> i64 {\n"
                                 "    fixture_core::double(fixture_core::double(x))\n}\n",
        "crates/api/tests/api_test.rs": "#[test]\nfn quad_of_three() {\n"
                                        "    assert_eq!(api::quad(3), 12);\n}\n",
    },
}

# Per fixture: the changed file, the expected affected targets, the expected lint status
# without toolchains, and a symbol with its defining file for outline, where and context.
EXPECT = {
    "python": {"change": "pkg/core.py", "targets": ["tests/test_api.py", "tests/test_core.py"],
               "lint": "pass", "symbol": "frobnicate", "query": "frobnicate core values"},
    "typescript": {"change": "src/paginate.ts", "targets": ["src/paginate.test.ts"],
                   "lint": "skipped", "symbol": "paginate", "query": "paginate page size"},
    "go": {"change": "pkg/a/a.go", "targets": ["./pkg/a", "./pkg/b"], "lint": "skipped",
           "symbol": "Double", "query": "Double numbers"},
    "rust": {"change": "crates/core/src/lib.rs", "targets": ["api", "core"], "lint": "skipped",
             "symbol": "double", "query": "double crate"},
}

# 60 points with a 20% step at index 30 and a small deterministic wobble.
SERIES = [round((100.0 if i < 30 else 120.0) + ((i * 7) % 5 - 2) * 0.5, 3) for i in range(60)]

# Synthetic test history: target -> (p50 seconds, outcomes over six runs, oldest first).
HISTORY = {
    "tests/test_api.py": (3.0, "pppppp"),
    "tests/test_core.py": (2.0, "pppppp"),
    "tests/test_flaky.py": (8.0, "pfppfp"),
    "tests/test_broken.py": (4.0, "ffffff"),
    "tests/test_slow.py": (30.0, "pppppp"),
}


class StepFailed(Exception):
    pass


class Env(object):
    def __init__(self, keep):
        self.keep = keep
        self.tmp = tempfile.mkdtemp(prefix="devkit-selftest-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.tmp, "home"))
        link = os.path.join(self.bin, "python3")
        try:
            os.symlink(sys.executable, link)
        except (OSError, NotImplementedError, AttributeError):
            with open(link, "w") as f:
                f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
            os.chmod(link, 0o755)
        self.env = {"PATH": self.bin, "HOME": os.path.join(self.tmp, "home"), "LC_ALL": "C",
                    "TMPDIR": self.tmp, "PYTHONDONTWRITEBYTECODE": "1"}
        self.last = None

    def close(self):
        if not self.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def materialise(self, name, files):
        root = os.path.join(self.tmp, name)
        for rel, text in sorted(files.items()):
            write(os.path.join(root, rel), text)
        return root

    def run(self, argv, cwd):
        self.last = " ".join(argv)
        try:
            return subprocess.run(argv, cwd=cwd, env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, universal_newlines=True,
                                  timeout=STEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise StepFailed("timed out after %d s: %s" % (STEP_TIMEOUT, self.last))

    def dev(self, root, *args, **kw):
        """Run dev.py (with --repo root unless repo=False) from root. Returns the parsed
        JSON when --json is among the arguments, else the completed process. Raises
        StepFailed on an unexpected exit code, a traceback or output that is not JSON."""
        expect = kw.get("rc", 0)
        args = list(args)
        if kw.get("repo", True):
            cut = args.index("--") if "--" in args else len(args)
            args[cut:cut] = ["--repo", root]
        argv = [sys.executable, "-I", DEV] + args
        p = self.run(argv, kw.get("cwd", root))
        shown = "dev.py " + " ".join(args)
        if p.returncode != expect:
            tail = (p.stderr.strip().splitlines() or p.stdout.strip().splitlines() or [""])[-6:]
            raise StepFailed("`%s` exited %d (expected %d): %s" % (
                shown, p.returncode, expect, " | ".join(tail)))
        if "Traceback" in p.stderr:
            raise StepFailed("`%s` printed a traceback" % shown)
        if "--json" in args and expect == 0:
            try:
                return json.loads(p.stdout)
            except ValueError:
                raise StepFailed("`%s` did not print one JSON object: %r" % (
                    shown, p.stdout[:200]))
        return p


def write(path, text):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def check(cond, message):
    if not cond:
        raise StepFailed(message)


# ---------------------------------------------------------------- per-fixture steps


def fixture_steps(env, lang, root):
    ex = EXPECT[lang]

    def init():
        env.dev(root, "init")
        for rel in ("tools/dev.py", "tools/dev.json", ".devkit/.gitignore"):
            check(os.path.isfile(os.path.join(root, rel)), "init did not write %s" % rel)
        return "shim, tools/dev.json, .devkit/"

    def init_check():
        env.dev(root, "init", "--check")
        return "shim resolves"

    def detect():
        d = env.dev(root, "detect", "--json")
        check(d.get("languages") == [lang], "detected %r, expected [%r]" % (
            d.get("languages"), lang))
        return lang

    def affected():
        d = env.dev(root, "affected", ex["change"], "--json")
        got = d.get("targets", {}).get(lang)
        check(d.get("mode") == "selected" and got == ex["targets"],
              "affected %s: mode %s targets %r, expected %r" % (
                  ex["change"], d.get("mode"), got, ex["targets"]))
        return "%s -> %s" % (ex["change"], " ".join(got))

    def check_no_tests():
        d = env.dev(root, "check", "--no-tests", ex["change"], "--json")
        lint = [g for g in d.get("gates", []) if g.get("name") == "lint"]
        check(d.get("status") == "pass" and lint and lint[0]["status"] == ex["lint"],
              "check: status %s, lint %s (expected pass, lint %s)" % (
                  d.get("status"), lint[0]["status"] if lint else None, ex["lint"]))
        return "lint %s (%s)" % (lint[0]["status"], lint[0].get("command") or "-")

    def repo_map():
        env.dev(root, "map")
        path = os.path.join(root, ".devkit", "repo-map.json")
        check(os.path.isfile(path), "map did not write .devkit/repo-map.json")
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        names = [e["name"] for e in m["files"].get(ex["change"], {}).get("exports", [])]
        check(ex["symbol"] in names, "map: %s exports %r, expected %s" % (
            ex["change"], names, ex["symbol"]))
        env.dev(root, "map", "--check")
        return "%d file(s); --check clean" % len(m["files"])

    def outline():
        d = env.dev(root, "outline", ex["change"], "--json")
        text = json.dumps(d)
        check(ex["symbol"] in text, "outline %s does not list %s" % (ex["change"], ex["symbol"]))
        return "%s lists %s" % (ex["change"], ex["symbol"])

    def where():
        p = env.dev(root, "where", ex["symbol"])
        check(ex["change"] + ":" in p.stdout, "where %s: %r lacks %s:<line>" % (
            ex["symbol"], p.stdout.strip()[:200], ex["change"]))
        return "%s in %s" % (ex["symbol"], ex["change"])

    def context():
        d = env.dev(root, "context", ex["query"], "--budget", "2000", "--json")
        check(ex["change"] in d.get("direct_targets", []),
              "context %r: direct targets %r lack %s" % (ex["query"], d.get("direct_targets"),
                                                         ex["change"]))
        check(d.get("used", 0) <= 2000, "context used %s tokens of 2000" % d.get("used"))
        return "%s direct; %d tokens" % (ex["change"], d.get("used", 0))

    return [("init", init), ("init --check", init_check), ("detect", detect),
            ("affected", affected), ("check --no-tests", check_no_tests), ("map", repo_map),
            ("outline", outline), ("where", where), ("context", context)]


# ---------------------------------------------------------------- evidence and history steps


NOOP = "python3 -c pass"


def evidence_steps(env, root):
    """Steps run in the Python fixture after its repository steps."""
    hist = history_steps(env, root)
    return (hist[:1] + measurement_steps(env, root) + tool_steps(env, root) + hist[1:])


def measurement_steps(env, root):
    """aa, bench, compare, scale and bound."""

    def aa_bench_compare():
        env.dev(root, "aa", "--runs", "6", "--warmup", "1", "--save", "--", "python3", "-c",
                "pass")
        check(os.path.isfile(os.path.join(root, ".devkit", "aa.json")), "aa --save wrote nothing")
        out = os.path.join(root, ".devkit", "bench.json")
        verdict = None
        for attempt in range(2):      # identical arms: a false FASTER is possible, retry once
            env.dev(root, "bench", "--arm", "a=" + NOOP, "--arm", "b=" + NOOP, "--runs", "5",
                    "--warmup", "1", "--out", out, "--json")
            d = env.dev(root, "compare", out, "--json")
            verdict = d.get("verdict")
            if verdict != "FASTER":
                break
        check(verdict is not None and verdict != "FASTER",
              "compare on identical arms said %r twice" % verdict)
        check("aa.json" in (d.get("min_effect_source") or ""),
              "compare ignored the saved A/A noise floor: %r" % d.get("min_effect_source"))
        return "identical arms: %s (min effect from aa --save)" % verdict

    def scale():
        d = env.dev(root, "scale", "--sizes", "10,20", "--no-baseline", "--runs", "3",
                    "--warmup", "0", "--json", "--", "python3", "-c",
                    "import sys; sum(range(int(sys.argv[1])))", "{n}", repo=False)
        check(d.get("exponent") is not None, "scale produced no exponent")
        return "exponent %.2f (startup dominated, as expected)" % d["exponent"]

    def bound():
        a = env.dev(root, "bound", "amdahl", "--p", "0.12", "--s", "100", "--json", repo=False)
        check(abs(a["cap"] - 1 / 0.88) < 1e-9 and abs(a["overall"] - 1.1348161) < 1e-6,
              "amdahl: %r" % a)
        path = os.path.join(root, ".devkit", "durations.json")
        write(path, json.dumps(dict(zip("abcdefghi", [7, 7, 6, 6, 5, 5, 4, 4, 4]))))
        m = env.dev(root, "bound", "makespan", "--durations", path, "--workers", "4", "--json",
                    repo=False)
        check(m["lb"] == 12 and m["lpt_makespan"] == 15 and abs(m["graham_bound"] - 1.25) < 1e-9,
              "makespan: lb %s, LPT %s, bound %s (expected 12, 15, 1.25)" % (
                  m["lb"], m["lpt_makespan"], m["graham_bound"]))
        o = env.dev(root, "bound", "ops", "--n", "2000", "--ops", "40*n", "--path", "batch",
                    "--json", repo=False)
        check(o["decision"] == "stop" and o["flip_size"] == 250000, "ops: %r" % o)
        return "amdahl cap 1.14x; Graham instance LB 12, LPT 15; ops stop, flip 250000"

    return [('aa/bench/compare', aa_bench_compare), ('scale', scale), ('bound', bound)]


def tool_steps(env, root):
    """changepoint, oracle and catalog."""

    def changepoint():
        path = os.path.join(root, ".devkit", "series.json")
        write(path, json.dumps(SERIES))
        d = env.dev(root, "changepoint", path, "--json", repo=False)
        idx = [pt["index"] for pt in d["points"]]
        check(len(idx) == 1 and abs(idx[0] - 30) <= 1, "change points %r, expected one at 30"
              % idx)
        return "one step at index %d of 60" % idx[0]

    def oracle():
        out = "tests/test_oracle_new.py"
        env.dev(root, "oracle", "--ref", "sortmod:ref", "--new", "sortmod:new", "--cases", "60",
                "--out", out, repo=False)
        p = env.run([sys.executable, "-m", "unittest", "-q", out], cwd=root)
        check(p.returncode == 0, "the generated oracle test failed: %s" % " | ".join(
            p.stderr.strip().splitlines()[-4:]))
        os.remove(os.path.join(root, out))
        return "insertion sort matches sorted() on 60 cases + edges, anti-cache check run"

    def catalog():
        d = env.dev(root, "catalog", "--symptom", "dedup", "--json", repo=False)
        ids = [m["id"] for m in d.get("matches", [])]
        check(ids and ids[0] == "hash-key-dedup", "catalog --symptom dedup ranked %r" % ids[:3])
        env.dev(root, "catalog", "--symptom", "zzzz", rc=2, repo=False)
        return "hash-key-dedup first; no match exits 2"

    return [('changepoint', changepoint), ('oracle', oracle), ('catalog', catalog)]


def history_steps(env, root):
    """test --record first, then flakes/smart/shards and perf."""

    def test_record():
        d = env.dev(root, "test", "--json")
        res = (d.get("results") or [{}])[0]
        check(d.get("status") == "pass" and res.get("outcomes") == {"passed": 4},
              "test: status %s, outcomes %r (expected 4 passed)" % (
                  d.get("status"), res.get("outcomes")))
        check(res.get("run_file"), "test did not write .devkit/runs")
        s = env.dev(root, "slowest", "--json")
        targets = sorted(r["target"] for r in s["rows"])
        check(targets == ["tests/test_api.py", "tests/test_core.py"],
              "slowest after test lists %r" % targets)
        return "4 tests passed, recorded in %s; slowest reads them" % res["run_file"]

    def history():
        write_history(root)
        f = env.dev(root, "flakes", "--write-quarantine", "--json")
        names = [r["target"] for r in f["flakes"]]
        check(names == ["tests/test_flaky.py"], "flakes %r, expected tests/test_flaky.py" % names)
        s = env.dev(root, "smart", "pkg/core.py", "--budget", "20", "--json")
        req = [r["target"] for r in s["required"]]
        opt = [r["target"] for r in s["optional"]]
        check(s["mode"] == "selected" and req == ["tests/test_api.py", "tests/test_core.py"]
              and opt == ["tests/test_broken.py"] and s["quarantined"] == ["tests/test_flaky.py"]
              and s["estimated_seconds"] == 9,
              "smart --budget 20: mode %s required %r optional %r quarantined %r estimate %s"
              % (s["mode"], req, opt, s["quarantined"], s["estimated_seconds"]))
        o = env.dev(root, "smart", "pkg/core.py", "--budget", "3", "--json")
        check(o["mode"] == "over_budget" and len(o["required"]) == 2
              and o["message"] == "required set exceeds budget by 2 s",
              "smart --budget 3: %s, %r" % (o["mode"], o["message"]))
        h = env.dev(root, "shards", "2", "--json")
        check(h["lb"] == 30 and h["makespan"] == 30 and h["plan"][0]["targets"] ==
              ["tests/test_slow.py"], "shards 2: LB %s makespan %s plan %r" % (
                  h["lb"], h["makespan"], h["plan"]))
        return "flakes, quarantine, smart (selected and over_budget), shards LB 30 = makespan"

    def perf():
        cfg_path = os.path.join(root, "tools", "dev.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["benchmarks"] = [{"name": "noop", "command": NOOP, "runs": 3, "warmup": 0}]
        write(cfg_path, json.dumps(cfg, indent=2) + "\n")
        r = env.dev(root, "perf", "run", "--json")
        check(len(r["results"]) == 1 and r["results"][0]["median"] > 0, "perf run: %r" % r)
        h = env.dev(root, "perf", "history", "noop", "--json")
        check(h["points"] == 1, "perf history noop has %s point(s)" % h["points"])
        synthetic = os.path.join(root, ".devkit", "perf", "synthetic.jsonl")
        with open(synthetic, "w", encoding="utf-8") as f:
            for i, v in enumerate(SERIES[10:50]):
                f.write(json.dumps({"ts": "2026-01-01T00:00:%02d+00:00" % i,
                                    "commit": "%07x" % (i + 1), "dirty": False, "median": v,
                                    "iqr": 0.1, "cpu_median": v, "runs": 7}) + "\n")
        c = env.dev(root, "changepoint", "--perf", "synthetic", "--json")
        labels = [pt["label"] for pt in c["points"]]
        check(len(labels) == 1 and labels[0] in ("0000014", "0000015", "0000016"),
              "changepoint --perf synthetic: %r" % labels)
        return "perf run appended 1 point; changepoint --perf finds the step at %s" % labels[0]

    return [('test (records history)', test_record), ('history', history), ('perf', perf)]


def write_history(root):
    """Six synthetic runs, one per day ending now, in .devkit/runs (after the real ones)."""
    import datetime
    runs = os.path.join(root, ".devkit", "runs")
    for name in os.listdir(runs):
        os.remove(os.path.join(runs, name))
    now = datetime.datetime.now(datetime.timezone.utc)
    for i in range(6):
        when = now - datetime.timedelta(days=5 - i, minutes=1)
        rid = when.strftime("%Y%m%dT%H%M%SZ") + "-python"
        lines = []
        for target, (secs, outcomes) in sorted(HISTORY.items()):
            outcome = "passed" if outcomes[i] == "p" else "failed"
            lines.append(json.dumps({"run_id": rid, "nodeid": target + "::T::t", "target": target,
                                     "outcome": outcome, "duration_seconds": secs,
                                     "timestamp": when.isoformat(timespec="seconds")}))
        write(os.path.join(runs, rid + ".jsonl"), "\n".join(lines) + "\n")
    for target in HISTORY:
        path = os.path.join(root, target)
        if not os.path.exists(path):
            write(path, "import unittest\n")


# ---------------------------------------------------------------- driver


def run_all(env, emit):
    """Run every step in order; emit(record) per step. Returns the records (the last one
    failed when the run failed)."""
    records = []
    plan = []
    for lang in ("python", "typescript", "go", "rust"):
        root = env.materialise(lang, FIXTURES[lang])
        plan.extend(("%s %s" % (lang, n), fn) for n, fn in fixture_steps(env, lang, root))
        if lang == "python":
            py_root = root
    plan.extend(("python " + n, fn) for n, fn in evidence_steps(env, py_root))
    for name, fn in plan:
        start = time.perf_counter()
        try:
            detail = fn()
            rec = {"step": name, "status": "ok", "seconds": round(time.perf_counter() - start, 3),
                   "detail": detail}
        except StepFailed as e:
            rec = {"step": name, "status": "fail", "seconds":
                   round(time.perf_counter() - start, 3), "detail": str(e)}
        records.append(rec)
        emit(rec)
        if rec["status"] == "fail":
            break
    return records


def cmd_selftest(args):
    p = Parser(prog="dev.py selftest", description="Offline end-to-end check on embedded "
               "fixture repositories (Python, TypeScript, Go, Rust); no network, no toolchains.")
    p.add_argument("--keep", action="store_true", help="keep the temporary directory")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    env = Env(a.keep)
    start = time.perf_counter()

    def emit(rec):
        if not a.json:
            print("%-4s %-34s %6.2f s  %s" % (rec["status"], rec["step"], rec["seconds"],
                                             rec["detail"]))
            sys.stdout.flush()

    try:
        records = run_all(env, emit)
    finally:
        env.close()
    total = round(time.perf_counter() - start, 2)
    failed = [r for r in records if r["status"] == "fail"]
    if a.json:
        print_json({"schema": "devkit-selftest/1", "status": "fail" if failed else "pass",
                    "seconds": total, "steps": records, "python": sys.version.split()[0],
                    "kept": env.tmp if a.keep else None})
    elif failed:
        print("selftest: FAIL at %s (%.1f s)" % (failed[0]["step"], total))
    else:
        print("selftest: PASS (%d steps, %.1f s)" % (len(records), total))
    if a.keep and not a.json:
        print("kept %s" % env.tmp)
    return 1 if failed else 0
