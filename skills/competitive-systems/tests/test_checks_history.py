"""dev.py check, lint, typecheck and test on temporary copies of the fixture repositories,
the report parsers behind `test --record`, and the commands that read the recorded history:
slowest, flakes (and the quarantine file), smart (budgeted knapsack selection, checked
against brute force), shards (LPT within Graham's bound of the brute-force optimum) and perf
with changepoint --perf.

Every CLI call runs with an empty PATH and `python3 -I -S`, so no linter, type checker,
test runner, node, go, cargo or git is found and the Python fallbacks (compileall,
unittest) are what runs: the same on every machine. Python steps run as sys.executable.
"""
import contextlib
import datetime
import io
import itertools
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

from devkit import checks, history, perf  # noqa: E402

GATE_KEYS = {"name", "language", "command", "status", "seconds", "head", "tail", "omitted",
             "fix_hint", "reason"}
RECORD_KEYS = {"run_id", "nodeid", "target", "outcome", "duration_seconds", "timestamp"}


class Repo(object):
    def __init__(self, name):
        self.tmp = tempfile.mkdtemp(prefix="devkit-check-")
        self.root = os.path.join(self.tmp, name)
        shutil.copytree(os.path.join(REPOS, name), self.root)
        self.empty = os.path.join(self.tmp, "empty-path")
        os.makedirs(self.empty)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def replace(self, rel, old, new):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            text = f.read()
        assert old in text, (rel, old)
        self.write(rel, text.replace(old, new))

    def dev(self, *args):
        env = {"PATH": self.empty, "HOME": self.tmp, "LC_ALL": "C",
               "PYTHONDONTWRITEBYTECODE": "1"}
        argv = [args[0], "--repo", self.root] + list(args[1:])
        return subprocess.run([sys.executable, "-I", "-S", DEV] + argv,
                              cwd=self.root, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=120)

    def runs(self):
        d = os.path.join(self.root, ".devkit", "runs")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def records(self, name):
        with open(os.path.join(self.root, ".devkit", "runs", name), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def run_dev_in(root, args):
    env = {"PATH": os.path.join(root, "no-path"), "HOME": root, "LC_ALL": "C",
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-I", "-S", DEV, args[0], "--repo", root] + args[1:],
                          cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=120)


class RepoCase(unittest.TestCase):
    name = "py"

    def setUp(self):
        self.repo = Repo(self.name)

    def tearDown(self):
        self.repo.close()


# ---------------------------------------------------------------- check


class CheckTests(RepoCase):
    def test_check_passes_with_compileall_skipped_typecheck_and_unittest(self):
        p = self.repo.dev("check", "pkg/core.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        self.assertRegex(lines[0], r"^lint \(python\) +pass +\S+ m?s +python3 -m compileall -q pkg/core\.py$")
        self.assertEqual(lines[1], "typecheck (python)     skipped  no type checker configured (mypy "
                                   "config or pyright); set commands.python.typecheck in tools/dev.json")
        self.assertRegex(lines[2], r"^tests \(python\) +pass +.* PYTHONPATH=tests python3 -m "
                                   r"unittest tests/test_api\.py tests/test_core\.py$")
        self.assertEqual(lines[-1], "check: PASS (1 file(s), 2 gate(s) ran, 1 skipped; --strict "
                                    "fails on a skipped gate)")
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse(any("__pycache__" in d for d, _, _ in os.walk(self.repo.root)),
                         "compileall must not write bytecode into the repository")

    def test_check_json_gate_records(self):
        p = self.repo.dev("check", "pkg/core.py", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        j = json.loads(p.stdout)
        self.assertEqual((j["schema"], j["status"], j["failed_gate"]), ("devkit-check/1", "pass", None))
        self.assertEqual([(g["name"], g["status"]) for g in j["gates"]],
                         [("lint", "pass"), ("typecheck", "skipped"), ("tests", "pass")])
        for g in j["gates"]:
            self.assertEqual(set(g), GATE_KEYS)
            self.assertEqual(g["language"], "python")
        self.assertIn("commands.python.typecheck", j["gates"][1]["fix_hint"])
        self.assertEqual(j["affected"]["targets"], {"python": ["tests/test_api.py", "tests/test_core.py"]})
        self.assertEqual(j["skipped"], 1)

    def test_failing_test_fails_at_tests(self):
        self.repo.replace("tests/test_core.py", "[2, 4, 6]", "[2, 4, 7]")
        p = self.repo.dev("check", "pkg/core.py")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(p.stdout.splitlines()[-1], "check: FAIL at tests (python)")
        self.assertIn("FAIL: test_frobnicate_dedups_and_doubles", p.stdout)
        self.assertIn("fix: rerun the targets alone: PYTHONPATH=tests python3 -m unittest "
                      "tests/test_api.py tests/test_core.py", p.stdout)
        j = json.loads(self.repo.dev("check", "pkg/core.py", "--json").stdout)
        self.assertEqual((j["status"], j["failed_gate"]), ("fail", "tests (python)"))
        g = j["gates"][-1]
        self.assertLessEqual(len(g["head"]), 25)
        self.assertLessEqual(len(g["tail"]), 15)
        self.assertTrue(any(line.startswith("FAILED") for line in g["head"] + g["tail"]))

    def test_syntax_error_fails_at_lint_and_stops(self):
        self.repo.write("pkg/core.py", "def frobnicate(values:\n    return values\n")
        j = json.loads(self.repo.dev("check", "pkg/core.py", "--json").stdout)
        self.assertEqual(j["failed_gate"], "lint (python)")
        self.assertEqual([g["name"] for g in j["gates"]], ["lint"])
        self.assertEqual(j["gates"][0]["fix_hint"], "fix the syntax error above")
        self.assertTrue(any("SyntaxError" in line or "invalid syntax" in line or "never closed" in line
                            for line in j["gates"][0]["head"]), j["gates"][0]["head"])

    def test_strict_no_tests_ignored_and_sensitive(self):
        p = self.repo.dev("check", "pkg/core.py", "--strict")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.splitlines()[-1],
                         "check: FAIL at typecheck (python) (skipped, --strict)")
        p = self.repo.dev("check", "pkg/core.py", "--no-tests")
        self.assertEqual(p.returncode, 0)
        self.assertIn("note: tests not run (--no-tests)", p.stdout)
        p = self.repo.dev("check", "README.md")
        self.assertEqual(p.returncode, 0)
        self.assertIn("note: tests: none affected (only ignored paths changed)", p.stdout)
        self.assertEqual(p.stdout.splitlines()[-1], "check: PASS (1 file(s), 0 gate(s) ran, 0 skipped)")
        p = self.repo.dev("check", "conftest.py")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("note: tests: full run (sensitive file: conftest.py)", p.stdout)
        self.assertRegex(p.stdout, r"tests \(python\) +pass .*python3 -m unittest discover -s tests")

    def test_config_commands_and_placeholders(self):
        py = sys.executable
        self.repo.write("tools/dev.json", json.dumps({"commands": {"python": {
            "lint": "%s -c \"import sys; print('linted', sys.argv[1:])\" {files}" % py,
            "typecheck": "%s -c \"print('typed')\"" % py,
            "test": "%s -m unittest {targets}" % py}}}))
        j = json.loads(self.repo.dev("check", "pkg/api.py", "--json").stdout)
        self.assertEqual(j["status"], "pass", j)
        lint, typed, tests = j["gates"]
        self.assertTrue(lint["command"].endswith("pkg/api.py"))
        self.assertEqual(typed["status"], "pass")
        self.assertTrue(tests["command"].endswith("-m unittest tests/test_api.py"))

    def test_config_command_quotes_files_with_spaces(self):
        py = sys.executable
        self.repo.write("pkg/my mod.py", "X = 1\n")
        self.repo.write("tools/dev.json", json.dumps({"commands": {"python": {
            "lint": "%s -c \"import sys; print('argv', len(sys.argv) - 1)\" {files}" % py}}}))
        j = json.loads(self.repo.dev("check", "pkg/my mod.py", "--no-tests", "--json").stdout)
        lint = j["gates"][0]
        self.assertEqual(lint["status"], "pass", lint)
        self.assertIn("'pkg/my mod.py'", lint["command"])

    def test_no_files_and_no_git(self):
        p = self.repo.dev("check")
        self.assertEqual(p.returncode, 2)
        self.assertIn("dev.py check: no git: pass files explicitly", p.stderr)

    def test_lint_and_typecheck_commands(self):
        p = self.repo.dev("lint", "pkg/core.py", "pkg/api.py")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("python3 -m compileall -q pkg/core.py pkg/api.py", p.stdout)
        p = self.repo.dev("lint", "--all", "--json")
        j = json.loads(p.stdout)
        self.assertEqual((j["schema"], j["status"]), ("devkit-lint/1", "pass"))
        self.assertIn("tests/test_core.py", j["gates"][0]["command"])
        p = self.repo.dev("typecheck", "pkg/core.py")
        self.assertEqual(p.returncode, 0)
        self.assertIn("skipped  no type checker configured", p.stdout)
        self.assertEqual(self.repo.dev("typecheck", "pkg/core.py", "--strict").returncode, 1)
        self.repo.write("pkg/core.py", "def broken(:\n")
        self.assertEqual(self.repo.dev("lint", "pkg/core.py").returncode, 1)


class ToolchainAbsentTests(unittest.TestCase):
    """JS, Go and Rust fixtures: targets resolve and every step reports skipped, so the check
    fails with "nothing was checked" instead of passing having run no gate."""

    def check(self, name, path, targets, skipped_reason):
        repo = Repo(name)
        try:
            p = repo.dev("check", path, "--json")
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            j = json.loads(p.stdout)
            self.assertEqual((j["executed"], j["status"]), (0, "fail"))
            self.assertTrue(j["failed_gate"].startswith("nothing was checked"), j["failed_gate"])
            lang = j["languages"][0]
            self.assertEqual(j["affected"]["targets"], {lang: targets})
            self.assertTrue(j["gates"])
            self.assertEqual(set(g["status"] for g in j["gates"]), {"skipped"}, j["gates"])
            self.assertIn(skipped_reason, j["gates"][0]["reason"])
            self.assertEqual(repo.dev("check", path, "--strict").returncode, 1)
            text = repo.dev("check", path).stdout
            self.assertIn("check: FAIL at nothing was checked: 1 relevant changed file(s) and no "
                          "gate ran", text)
        finally:
            repo.close()

    def test_mixed_repo_runs_python_and_skips_typescript(self):
        repo = Repo("py")
        try:
            shutil.copytree(os.path.join(REPOS, "js"), os.path.join(repo.root, "web"))
            j = json.loads(repo.dev("check", "pkg/core.py", "web/src/paginate.ts", "--json").stdout)
            self.assertEqual(j["status"], "pass", j)
            got = [(g["name"], g["language"], g["status"]) for g in j["gates"]]
            self.assertEqual(got, [("lint", "python", "pass"), ("lint", "typescript", "skipped"),
                                   ("typecheck", "python", "skipped"),
                                   ("typecheck", "typescript", "skipped"),
                                   ("tests", "python", "pass"), ("tests", "typescript", "skipped")])
            self.assertTrue(j["gates"][0]["command"].endswith("compileall -q pkg/core.py"))
        finally:
            repo.close()

    def test_unsupported_language_fails_and_says_so(self):
        tmp = tempfile.mkdtemp(prefix="devkit-maven-")
        try:
            os.makedirs(os.path.join(tmp, "core", "src"))
            with open(os.path.join(tmp, "pom.xml"), "w") as f:
                f.write("<project/>\n")
            with open(os.path.join(tmp, "core", "src", "Money.java"), "w") as f:
                f.write("class Money {}\n")
            for cmd in (["check", "core/src/Money.java", "--strict"],
                        ["check", "core/src/Money.java"], ["lint", "core/src/Money.java"]):
                p = run_dev_in(tmp, cmd)
                self.assertEqual(p.returncode, 1, (cmd, p.stdout, p.stderr))
                self.assertIn("no language dev.py supports was detected", p.stdout)
                self.assertNotIn("check: PASS", p.stdout)
                self.assertNotIn("tests: full run (", p.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_js_without_pnpm(self):
        self.check("js", "src/paginate.ts", ["src/paginate.test.ts"], "pnpm is not installed")

    def test_go_without_go(self):
        self.check("go", "pkg/a/a.go", ["./pkg/a", "./pkg/b"], "is not installed")

    def test_rust_without_cargo(self):
        self.check("rust", "crates/core/src/lib.rs", ["api", "core"], "cargo is not installed")


# ---------------------------------------------------------------- test and --record


class TestCommandTests(RepoCase):
    def test_record_writes_per_target_records(self):
        p = self.repo.dev("test", "--record")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        runs = self.repo.runs()
        self.assertEqual(len(runs), 1)
        self.assertRegex(runs[0], r"^\d{8}T\d{6}Z-python\.jsonl$")
        recs = self.repo.records(runs[0])
        self.assertEqual(len(recs), 4)
        for r in recs:
            self.assertEqual(set(r), RECORD_KEYS)
            self.assertEqual(r["run_id"], runs[0][:-len(".jsonl")])
            self.assertEqual(r["outcome"], "passed")
            self.assertGreaterEqual(r["duration_seconds"], 0.0)
        self.assertEqual(sorted(set(r["target"] for r in recs)),
                         ["tests/test_api.py", "tests/test_core.py"])
        self.assertIn("tests/test_core.py::CoreTest::test_empty", [r["nodeid"] for r in recs])
        self.assertIn("recorded .devkit/runs/%s (4 record(s): 4 passed)" % runs[0], p.stdout)
        with open(os.path.join(self.repo.root, ".devkit", ".gitignore")) as f:
            self.assertEqual(f.read(), "*\n")

    def test_record_is_the_default_and_failures_are_recorded(self):
        self.repo.replace("tests/test_api.py", "[10]", "[11]")
        p = self.repo.dev("test", "--json")
        self.assertEqual(p.returncode, 1)
        j = json.loads(p.stdout)
        self.assertEqual((j["schema"], j["status"]), ("devkit-test/1", "fail"))
        self.assertEqual(j["results"][0]["outcomes"], {"failed": 1, "passed": 3})
        recs = self.repo.records(self.repo.runs()[0])
        failed = [r["nodeid"] for r in recs if r["outcome"] == "failed"]
        self.assertEqual(failed, ["tests/test_api.py::ApiTest::test_last_page"])

    def test_no_record_and_runner_args(self):
        p = self.repo.dev("test", "--no-record", "--", "tests/test_core.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.repo.runs(), [])
        self.assertIn("Ran 2 tests", p.stdout)
        p = self.repo.dev("test", "--", "tests/test_core.py")
        recs = self.repo.records(self.repo.runs()[0])
        self.assertEqual(set(r["target"] for r in recs), {"tests/test_core.py"})

    def test_config_command_without_report_records_one_target(self):
        cmd = "%s -m unittest discover -s tests" % sys.executable
        self.repo.write("tools/dev.json", json.dumps({"commands": {"python": {"test": cmd}}}))
        p = self.repo.dev("test", "--record")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        recs = self.repo.records(self.repo.runs()[0])
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["target"], recs[0]["outcome"]), (cmd, "passed"))
        self.repo.dev("test")                                   # no report: not recorded by default
        self.assertEqual(len(self.repo.runs()), 1)

    def test_skipped_language_and_usage_errors(self):
        repo = Repo("rust")
        try:
            p = repo.dev("test")
            self.assertEqual(p.returncode, 0)
            self.assertIn("test (rust): skipped: cargo is not installed", p.stdout)
            self.assertEqual(repo.dev("test", "--strict").returncode, 1)
        finally:
            repo.close()
        p = self.repo.dev("test", "--lang", "go")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--lang go is not a detected language", p.stderr)


class GateRecordTests(unittest.TestCase):
    def test_head_tail_and_omitted(self):
        out = "\n".join("line %d" % i for i in range(1, 61))
        g = checks.gate("tests", "python", "fail", command="x", output=out)
        self.assertEqual(g["head"], ["line %d" % i for i in range(1, 26)])
        self.assertEqual(g["tail"], ["line %d" % i for i in range(46, 61)])
        self.assertEqual(g["omitted"], 20)
        short = checks.gate("lint", "python", "fail", output="a\nb")
        self.assertEqual((short["head"], short["tail"], short["omitted"]), (["a", "b"], [], 0))
        mid = checks.gate("lint", "python", "fail", output="\n".join(str(i) for i in range(30)))
        self.assertEqual((len(mid["head"]), mid["tail"], mid["omitted"]),
                         (25, ["25", "26", "27", "28", "29"], 0))


# ---------------------------------------------------------------- report parsers


class ReportParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="devkit-report-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_junit_xunit1_and_xunit2(self):
        self.write("tests/test_api.py", "")
        xml = self.write("r.xml", """<?xml version="1.0"?>
<testsuites><testsuite name="pytest">
 <testcase classname="tests.test_api.ApiTest" name="test_a" file="tests/test_api.py" time="0.25"/>
 <testcase classname="tests.test_api" name="test_b" time="0.5"><failure message="x"/></testcase>
 <testcase classname="tests.test_api" name="test_c" time="0.0"><skipped/></testcase>
 <testcase classname="tests.test_api" name="test_d" time="1.5"><error message="boom"/></testcase>
</testsuite></testsuites>""")
        recs = checks.parse_junit(xml, self.tmp, "svc")
        self.assertEqual([(r["nodeid"], r["target"], r["outcome"], r["duration_seconds"]) for r in recs], [
            ("svc/tests/test_api.py::ApiTest::test_a", "svc/tests/test_api.py", "passed", 0.25),
            ("svc/tests/test_api.py::test_b", "svc/tests/test_api.py", "failed", 0.5),
            ("svc/tests/test_api.py::test_c", "svc/tests/test_api.py", "skipped", 0.0),
            ("svc/tests/test_api.py::test_d", "svc/tests/test_api.py", "error", 1.5)])

    def test_vitest_and_jest_json(self):
        abs_file = os.path.join(self.tmp, "src", "a.test.ts")
        report = self.write("v.json", json.dumps({"testResults": [
            {"name": abs_file, "status": "passed", "assertionResults": [
                {"ancestorTitles": ["paginate"], "title": "page 2", "status": "passed", "duration": 12},
                {"ancestorTitles": [], "title": "todo", "status": "pending", "duration": None},
                {"ancestorTitles": ["x"], "title": "bad", "status": "failed", "duration": 3.5}]},
            {"name": "src/broken.test.ts", "status": "failed", "assertionResults": [],
             "perfStats": {"start": 1000, "end": 1750}}]}))
        recs = checks.parse_js_json(report, self.tmp, "")
        self.assertEqual([(r["nodeid"], r["outcome"], r["duration_seconds"]) for r in recs], [
            ("src/a.test.ts::paginate > page 2", "passed", 0.012),
            ("src/a.test.ts::todo", "skipped", 0.0),
            ("src/a.test.ts::x > bad", "failed", 0.0035),
            ("src/broken.test.ts", "error", 0.75)])

    def test_go_json(self):
        lines = [
            {"Action": "run", "Package": "example.com/m/pkg/b", "Test": "TestQuad"},
            {"Action": "output", "Package": "example.com/m/pkg/b", "Output": "ok\n"},
            {"Action": "pass", "Package": "example.com/m/pkg/b", "Test": "TestQuad/sub", "Elapsed": 0.1},
            {"Action": "pass", "Package": "example.com/m/pkg/b", "Test": "TestQuad", "Elapsed": 0.2},
            {"Action": "fail", "Package": "example.com/m/pkg/b", "Test": "TestOther", "Elapsed": 0.3},
            {"Action": "skip", "Package": "example.com/m/pkg/b", "Test": "TestSkip", "Elapsed": 0},
            {"Action": "fail", "Package": "example.com/m/pkg/b", "Elapsed": 0.6},
            {"Action": "fail", "Package": "example.com/m/pkg/c", "Elapsed": 0.4},
            {"Action": "skip", "Package": "example.com/m", "Elapsed": 0}]
        text = "\n".join(json.dumps(l) for l in lines) + "\nnot json\n"
        recs = checks.parse_go_json(text, "example.com/m", "")
        self.assertEqual([(r["nodeid"], r["target"], r["outcome"]) for r in recs], [
            ("./pkg/b::TestQuad", "./pkg/b", "passed"),
            ("./pkg/b::TestOther", "./pkg/b", "failed"),
            ("./pkg/b::TestSkip", "./pkg/b", "skipped"),
            ("./pkg/c", "./pkg/c", "error")])
        self.assertEqual(checks._go_filter(json.dumps(lines[1]) + "\n"), "ok\n")
        self.assertIsNone(checks._go_filter(json.dumps(lines[0]) + "\n"))

    def test_unittest_reporter_records_subtests_and_class_errors(self):
        self.write("tests/test_mix.py", """import unittest

class Mix(unittest.TestCase):
    def test_ok(self):
        pass
    def test_sub(self):
        for i in range(3):
            with self.subTest(i=i):
                self.assertNotEqual(i, 1)
    @unittest.skip("later")
    def test_skip(self):
        pass
    def test_err(self):
        raise RuntimeError("boom")

class Setup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("no fixture")
    def test_never(self):
        pass
""")
        out = os.path.join(self.tmp, "u.json")
        p = subprocess.run([sys.executable, "-c", checks.UNITTEST_REPORTER, out, "discover", "tests",
                            "test*.py"], cwd=self.tmp, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 1)
        recs = dict((r["nodeid"], r["outcome"]) for r in checks.parse_unittest(out, ""))
        self.assertEqual(recs["tests/test_mix.py::Mix::test_ok"], "passed")
        self.assertEqual(recs["tests/test_mix.py::Mix::test_sub"], "failed")
        self.assertEqual(recs["tests/test_mix.py::Mix::test_skip"], "skipped")
        self.assertEqual(recs["tests/test_mix.py::Mix::test_err"], "error")
        setup = [k for k in recs if "setUpClass" in k]
        self.assertEqual(len(setup), 1)
        self.assertTrue(setup[0].startswith("tests/test_mix.py::"), setup)
        self.assertEqual(recs[setup[0]], "error")



# ---------------------------------------------------------------- test history


def write_runs(root, plan, days_ago=None, lang="python", extra_lines=()):
    """Synthetic .devkit/runs: plan maps target -> [(outcome, seconds) per run, oldest first];
    every run holds two records per target (the seconds split between them). days_ago[i] is
    how long before now run i happened (default: one run per day ending today)."""
    runs_dir = os.path.join(root, ".devkit", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    n = max(len(v) for v in plan.values())
    days_ago = days_ago or [n - 1 - i for i in range(n)]
    now = datetime.datetime.now(datetime.timezone.utc)
    for i in range(n):
        when = now - datetime.timedelta(days=days_ago[i], minutes=1)
        rid = when.strftime("%Y%m%dT%H%M%SZ") + "-" + lang
        lines = []
        for target in sorted(plan):
            if i >= len(plan[target]):
                continue
            outcome, secs = plan[target][i]
            for half, o in (("a", outcome), ("b", "passed" if outcome != "skipped" else "skipped")):
                lines.append(json.dumps({"run_id": rid, "nodeid": "%s::T::%s" % (target, half),
                                         "target": target, "outcome": o,
                                         "duration_seconds": secs / 2.0,
                                         "timestamp": when.isoformat(timespec="seconds")}))
        if i == 0:
            lines.extend(extra_lines)
        with open(os.path.join(runs_dir, rid + ".jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    for target in plan:
        path = os.path.join(root, target)
        if "/" in target and not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("import unittest\n")


def ok(*secs):
    return [("passed", s) for s in secs]


def brute_best(items, budget):
    """Best total value of a subset of [(name, value, cost)] within budget."""
    best = 0.0
    for k in range(len(items) + 1):
        for combo in itertools.combinations(items, k):
            if sum(c for _, _, c in combo) <= budget:
                best = max(best, sum(v for _, v, _ in combo))
    return best


def brute_makespan(durations, m):
    best = None
    for assign in itertools.product(range(m), repeat=len(durations)):
        loads = [0.0] * m
        for d, w in zip(durations, assign):
            loads[w] += d
        best = max(loads) if best is None else min(best, max(loads))
    return best


class HistoryRepo(Repo):
    """The py fixture with `python3` on PATH (a link to this interpreter), for perf."""

    def __init__(self):
        Repo.__init__(self, "py")
        os.symlink(sys.executable, os.path.join(self.empty, "python3"))

    def run(self, *args):
        args = list(args)
        cut = args.index("--") if "--" in args else len(args)
        args[cut:cut] = ["--repo", self.root]
        env = {"PATH": self.empty, "HOME": self.tmp, "LC_ALL": "C"}
        return subprocess.run([sys.executable, "-I", "-S", DEV] + args, cwd=self.root, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=120)

    def json(self, *args):
        p = self.run(*(list(args) + ["--json"]))
        assert p.returncode == 0, (args, p.returncode, p.stderr)
        assert "Traceback" not in p.stderr, p.stderr
        return json.loads(p.stdout)


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.repo = HistoryRepo()
        self.root = self.repo.root

    def tearDown(self):
        self.repo.close()


class AggregateTests(HistoryCase):
    def test_outcome_rules_dedupe_and_durations(self):
        write_runs(self.root, {
            "tests/test_a.py": [("failed", 2.0), ("error", 4.0), ("passed", 6.0)],
            "tests/test_s.py": [("skipped", 0.0), ("skipped", 0.0), ("passed", 3.0)]})
        # a duplicated record (same run_id and nodeid) must not count twice
        name = sorted(os.listdir(os.path.join(self.root, ".devkit", "runs")))[0]
        path = os.path.join(self.root, ".devkit", "runs", name)
        with open(path, encoding="utf-8") as f:
            first = f.readline()
        with open(path, "a", encoding="utf-8") as f:
            f.write(first + "not json\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            stats, info = history.load_stats(self.root)
        self.assertIn("1 unreadable line(s) in .devkit/runs skipped", err.getvalue())
        a, s = stats["tests/test_a.py"], stats["tests/test_s.py"]
        self.assertEqual((a["runs"], a["passes"], a["failures"], a["skips"]), (3, 1, 2, 0))
        self.assertAlmostEqual(a["failure_rate"], 2 / 3.0, places=5)
        self.assertEqual((a["p50_seconds"], a["max_seconds"], a["total_seconds"]), (4.0, 6.0, 12.0))
        self.assertEqual(a["language"], "python")
        self.assertIsNotNone(a["last_failed"])
        self.assertEqual((s["passes"], s["skips"], s["failure_rate"]), (1, 2, 0.0))
        self.assertEqual(s["p50_seconds"], 3.0)       # skip-only runs excluded from p50
        self.assertEqual(info["bad_lines"], 1)
        self.assertEqual(info["runs"], 3)

    def test_unknown_duration_defaults(self):
        self.assertEqual(history.default_seconds([]), history.UNKNOWN_SECONDS)
        self.assertEqual(history.default_seconds([1.0, 9.0, 4.0]), 4.0)
        recs = [{"run_id": "r1", "target": "t_known", "outcome": "passed", "duration": 2.0,
                 "ts": None, "language": None},
                {"run_id": "r1", "target": "t_none", "outcome": "passed", "duration": None,
                 "ts": None, "language": None}]
        stats = history.aggregate(recs)
        self.assertEqual(stats["t_none"]["p50_seconds"], 2.0)
        self.assertTrue(stats["t_none"]["estimated"])
        self.assertFalse(stats["t_known"]["estimated"])
        only = history.aggregate(recs[1:])
        self.assertEqual(only["t_none"]["p50_seconds"], 5.0)

    def test_days_window(self):
        write_runs(self.root, {"tests/test_a.py": ok(1.0, 1.0, 9.0)}, days_ago=[30, 20, 1])
        recent, info = history.load_stats(self.root, days=7)
        self.assertEqual(recent["tests/test_a.py"]["runs"], 1)
        self.assertEqual(info["outside_window"], 4)
        everything, _ = history.load_stats(self.root)
        self.assertEqual(everything["tests/test_a.py"]["runs"], 3)


class SlowestFlakesTests(HistoryCase):
    def test_slowest_orders_by_p50_not_mean(self):
        write_runs(self.root, {
            "tests/test_spiky.py": ok(1.0, 1.0, 1.0, 1.0, 100.0),     # mean 20.8, p50 1
            "tests/test_steady.py": ok(5.0, 5.0, 5.0, 5.0, 5.0),      # mean 5, p50 5
            "tests/test_tiny.py": ok(0.1, 0.1, 0.1, 0.1, 0.1)})
        d = self.repo.json("slowest")
        self.assertEqual(d["schema"], "devkit-slowest/1")
        self.assertEqual([r["target"] for r in d["rows"]],
                         ["tests/test_steady.py", "tests/test_spiky.py", "tests/test_tiny.py"])
        spiky = d["rows"][1]
        self.assertEqual((spiky["p50_seconds"], spiky["max_seconds"], spiky["runs"]), (1.0, 100.0, 5))
        self.assertEqual(len(self.repo.json("slowest", "--limit", "1")["rows"]), 1)
        p = self.repo.run("slowest")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("by p50", p.stdout)
        self.assertLess(p.stdout.index("test_steady"), p.stdout.index("test_spiky"))

    def test_no_history_is_a_hint_not_an_error(self):
        for cmd in ("slowest", "flakes"):
            p = self.repo.run(cmd)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("no test history", p.stdout)

    def test_flakes_mixed_within_rate_and_quarantine(self):
        P, F = ("passed", 1.0), ("failed", 1.0)
        write_runs(self.root, {
            "tests/test_flaky.py": [P, F, P, P, F, P],        # 2/6: flaky
            "tests/test_mostly_bad.py": [F, F, P, F, F, P],   # 4/6 > 0.5: not flaky
            "tests/test_broken.py": [F] * 6,                  # never passes
            "tests/test_fine.py": [P] * 6,                    # never fails
            "tests/test_short.py": [P, F]})                   # 2 executed runs < 3
        d = self.repo.json("flakes")
        self.assertEqual([r["target"] for r in d["flakes"]], ["tests/test_flaky.py"])
        self.assertIsNone(d["quarantine"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".devkit", "quarantine.json")))
        wider = self.repo.json("flakes", "--max-rate", "0.7", "--min-runs", "2")
        self.assertEqual([r["target"] for r in wider["flakes"]],
                         ["tests/test_mostly_bad.py", "tests/test_short.py",
                          "tests/test_flaky.py"])
        p = self.repo.run("flakes", "--write-quarantine")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("wrote .devkit/quarantine.json (1 target(s))", p.stdout)
        with open(os.path.join(self.root, ".devkit", "quarantine.json"), encoding="utf-8") as f:
            q = json.load(f)
        self.assertEqual(q["schema"], "devkit-quarantine/1")
        self.assertEqual(q["policy"], {"min_runs": 3, "max_rate": 0.5})
        self.assertEqual([t["target"] for t in q["targets"]], ["tests/test_flaky.py"])
        self.assertIn("generated_at", q)
        for bad in (["--min-runs", "1"], ["--max-rate", "0"], ["--days", "0"]):
            p = self.repo.run("flakes", *bad)
            self.assertEqual(p.returncode, 2, bad)
            self.assertTrue(p.stderr.startswith("dev.py flakes:"), p.stderr)


class SmartTests(HistoryCase):
    REQUIRED = ["tests/test_api.py", "tests/test_core.py"]

    def history(self):
        """Required targets cost 4 + 3 s; six candidates that failed in the last run (1 day
        ago) or 3 days ago; test_old failed only 40 days ago."""
        P = lambda s: ("passed", s)         # noqa: E731
        F = lambda s: ("failed", s)         # noqa: E731
        write_runs(self.root, {
            "tests/test_api.py": ok(3.2, 3.2, 3.2, 3.2),
            "tests/test_core.py": ok(2.1, 2.1, 2.1, 2.1),
            "tests/test_c1.py": [P(9.5), F(9.5), P(9.5), F(9.5)],   # cost 10, rate .5
            "tests/test_c2.py": [F(6.0), F(6.0), F(6.0), F(6.0)],   # cost 6, rate 1
            "tests/test_c3.py": [P(4.2), P(4.2), P(4.2), F(4.2)],   # cost 5, rate .25
            "tests/test_c4.py": [P(7.0), F(7.0), P(7.0), F(7.0)],   # cost 7, rate .5
            "tests/test_c5.py": [P(2.0), P(2.0), F(2.0), F(2.0)],   # cost 2, rate .5
            "tests/test_c6.py": [F(12.0), P(12.0), P(12.0), P(12.0)],  # cost 12
            "tests/test_old.py": [F(1.0), P(1.0), P(1.0), P(1.0)]},   # last failed 40 d ago
            days_ago=[40, 30, 20, 1])
        # test_c6 also failed 3 days ago (a run of its own), so it is a candidate while
        # test_old, whose only failure is 40 days old, is not
        write_runs(self.root, {"tests/test_c6.py": [("failed", 12.0)]}, days_ago=[3])

    def candidates(self, stats, required):
        recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)
        out = []
        for name, s in sorted(stats.items()):
            if name in required or s["last_failed"] is None or s["last_failed"] < recent:
                continue
            out.append((name, 2.0 + s["failure_rate"], history.cost_of(s["p50_seconds"])))
        return out

    def test_budget_keeps_required_and_matches_brute_force(self):
        self.history()
        stats, _ = history.load_stats(self.root)
        cands = self.candidates(stats, self.REQUIRED)
        names = [c[0] for c in cands]
        self.assertNotIn("tests/test_old.py", names)      # failed more than 14 days ago
        self.assertIn("tests/test_c6.py", names)
        for budget in (7, 10, 20, 30, 60, 200):
            d = self.repo.json("smart", "pkg/core.py", "--budget", str(budget))
            self.assertEqual(d["schema"], "devkit-smart/1")
            self.assertEqual([r["target"] for r in d["required"]], self.REQUIRED)
            self.assertEqual([r["reason"] for r in d["required"]], ["affected", "affected"])
            self.assertEqual(d["required_seconds"], 7)
            left = budget - 7
            chosen = [c for c in cands if c[0] in set(r["target"] for r in d["optional"])]
            self.assertLessEqual(sum(c[2] for c in chosen), left)
            self.assertAlmostEqual(sum(c[1] for c in chosen), brute_best(cands, left), places=6,
                                   msg="budget %d" % budget)
            self.assertEqual(d["mode"], "selected")
            self.assertEqual(d["estimated_seconds"], 7 + sum(c[2] for c in chosen))
            self.assertTrue(all(r["reason"] == "knapsack" for r in d["optional"]))
            listed = sorted(r["target"] for r in d["optional"] + d["not_selected"])
            self.assertEqual(listed, names)                # every candidate has a reason
            self.assertEqual(d["run"]["python"], sorted(self.REQUIRED + [c[0] for c in chosen]))
        p = self.repo.run("smart", "pkg/core.py", "--budget", "60")
        self.assertEqual(p.returncode, 0, p.stderr)
        for text in ("smart: selected; estimated", "budget of 60 s", "required (7 s",
                     "optional (knapsack over 6", "knapsack: value", "run:",
                     "python: tests/test_api.py"):
            self.assertIn(text, p.stdout)

    def test_over_budget_drops_nothing(self):
        self.history()
        d = self.repo.json("smart", "pkg/core.py", "--budget", "5")
        self.assertEqual(d["mode"], "over_budget")
        self.assertEqual(d["message"], "required set exceeds budget by 2 s")
        self.assertEqual([r["target"] for r in d["required"]], self.REQUIRED)
        self.assertEqual(d["optional"], [])
        self.assertEqual(d["run"]["python"], self.REQUIRED)
        p = self.repo.run("smart", "pkg/core.py", "--budget", "5")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("required set exceeds budget by 2 s", p.stdout)

    def test_quarantine_always_run_config_budget_and_full(self):
        self.history()
        with open(os.path.join(self.root, ".devkit", "quarantine.json"), "w") as f:
            json.dump({"schema": "devkit-quarantine/1",
                       "targets": [{"target": "tests/test_c2.py"}, {"target": "tests/test_api.py"}]}, f)
        self.repo.write("tools/dev.json", json.dumps({"budget_seconds": 60,
                                                      "always_run": ["tests/test_c5.py"]}))
        d = self.repo.json("smart", "pkg/core.py")
        self.assertEqual(d["budget_seconds"], 60)
        self.assertEqual(d["budget_source"], "tools/dev.json")
        req = dict((r["target"], r["reason"]) for r in d["required"])
        self.assertEqual(req, {"tests/test_api.py": "affected", "tests/test_core.py": "affected",
                               "tests/test_c5.py": "always_run"})   # quarantine never drops required
        opt = [r["target"] for r in d["optional"] + d["not_selected"]]
        self.assertNotIn("tests/test_c2.py", opt)
        self.assertNotIn("tests/test_c5.py", opt)
        self.assertEqual(d["quarantined"], ["tests/test_api.py", "tests/test_c2.py"])
        full = self.repo.json("smart", "conftest.py")
        self.assertEqual(full["mode"], "full")
        self.assertEqual([r["target"] for r in full["required"]], [history.FULL_SUITE])
        self.assertIn("sensitive file: conftest.py", full["message"])
        self.assertEqual(full["optional"], [])

    def test_no_history_estimates_and_usage_errors(self):
        d = self.repo.json("smart", "pkg/core.py", "--budget", "100")
        self.assertEqual([r["seconds"] for r in d["required"]], [5, 5])
        self.assertTrue(all(r["estimated"] for r in d["required"]))
        self.assertIn("no test history", d["estimate_note"])
        for bad in (["--budget", "-1", "pkg/core.py"], ["--base", "HEAD", "pkg/core.py"], []):
            p = self.repo.run("smart", *bad)
            self.assertEqual(p.returncode, 2, (bad, p.stderr))
            self.assertTrue(p.stderr.startswith("dev.py smart:"), p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    def test_plan_smart_equals_brute_force_on_random_instances(self):
        rng = random.Random(7)
        now = datetime.datetime.now(datetime.timezone.utc)
        for trial in range(60):
            stats = {}
            for i in range(rng.randint(0, 9)):
                p50 = rng.choice([0.4, 1.0, 2.5, 3.0, 7.9, 12.0])
                rate = rng.choice([0.1, 0.25, 0.5, 1.0])
                stats["t%d" % i] = {"p50_seconds": p50, "estimated": False, "failure_rate": rate,
                                    "language": "python",
                                    "last_failed": now - datetime.timedelta(days=rng.choice([1, 5, 20]))}
            req = [("r", "python", "affected")]
            stats["r"] = {"p50_seconds": 2.0, "estimated": False, "failure_rate": 0.0,
                          "language": "python", "last_failed": None}
            budget = rng.randint(0, 30)
            sel = history.plan_smart(req, stats, set(), budget, now=now)
            if budget < 2:
                self.assertEqual(sel["mode"], "over_budget")
                continue
            items = [(n, 2.0 + s["failure_rate"], history.cost_of(s["p50_seconds"]))
                     for n, s in sorted(stats.items()) if n != "r" and s["last_failed"]
                     and s["last_failed"] >= now - datetime.timedelta(days=14)]
            got = sum(c["value"] for c in sel["optional"])
            self.assertAlmostEqual(got, brute_best(items, budget - 2), places=5, msg=trial)
            self.assertLessEqual(sel["estimated_seconds"], budget)


class ShardsTests(HistoryCase):
    DURATIONS = {"tests/test_a.py": 7.0, "tests/test_b.py": 7.0, "tests/test_c.py": 6.0,
                 "tests/test_d.py": 6.0, "tests/test_e.py": 5.0, "tests/test_f.py": 5.0,
                 "tests/test_g.py": 4.0, "tests/test_h.py": 4.0}

    def setUp(self):
        HistoryCase.setUp(self)
        write_runs(self.root, dict((t, ok(d, d, d)) for t, d in self.DURATIONS.items()))

    def test_shards_lb_and_lpt_bound_against_brute_force(self):
        d = self.repo.json("shards", "3")
        total, longest = sum(self.DURATIONS.values()), max(self.DURATIONS.values())
        self.assertAlmostEqual(d["lb"], max(total / 3, longest), places=5)
        opt = brute_makespan(list(self.DURATIONS.values()), 3)
        self.assertLessEqual(d["makespan"], (4 / 3.0 - 1 / 9.0) * opt + 1e-9)
        self.assertGreaterEqual(d["makespan"], opt - 1e-9)
        self.assertAlmostEqual(d["makespan_over_lb"], d["makespan"] / d["lb"], places=5)
        placed = sorted(t for s in d["plan"] for t in s["targets"])
        self.assertEqual(placed, sorted(self.DURATIONS))
        for s in d["plan"]:
            self.assertAlmostEqual(s["estimated_seconds"],
                                   sum(self.DURATIONS[t] for t in s["targets"]))
        self.assertEqual(self.repo.json("shards", "3"), d)       # deterministic
        p = self.repo.run("shards", "3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("LB = max(sum/N, longest)", p.stdout)
        self.assertIn("makespan/LB", p.stdout)

    def test_matrix_explicit_targets_and_errors(self):
        p = self.repo.run("shards", "10", "--matrix")
        self.assertEqual(p.returncode, 0, p.stderr)
        m = json.loads(p.stdout)
        self.assertEqual(list(m), ["include"])
        self.assertEqual(len(m["include"]), 10)
        self.assertEqual(sum(1 for s in m["include"] if s["has_targets"]), 8)
        self.assertEqual(set(m["include"][0]), {"shard", "estimated_seconds", "targets",
                                                "has_targets"})
        self.assertIsInstance(m["include"][0]["targets"], str)
        d = self.repo.json("shards", "2", "tests/test_a.py", "tests/new_test.py")
        self.assertEqual(d["estimated"], ["tests/new_test.py"])
        self.assertEqual(d["targets"], 2)
        empty = HistoryRepo()
        try:
            p = empty.run("shards", "2")
            self.assertEqual(p.returncode, 2)
            self.assertIn("no targets", p.stderr)
        finally:
            empty.close()
        p = self.repo.run("shards", "0")
        self.assertEqual(p.returncode, 2)

    def test_plan_shards_bound_on_random_instances(self):
        rng = random.Random(3)
        for trial in range(40):
            n = rng.randint(1, 7)
            durs = dict(("t%d" % i, float(rng.randint(1, 20))) for i in range(n))
            m = rng.randint(1, 3)
            res = history.plan_shards(durs, m)
            opt = brute_makespan(list(durs.values()), m)
            self.assertLessEqual(res["makespan"], (4 / 3.0 - 1 / (3.0 * m)) * opt + 1e-9, trial)
            self.assertLessEqual(res["lb"], opt + 1e-9)


class PerfTests(HistoryCase):
    def config(self, benchmarks):
        self.repo.write("tools/dev.json", json.dumps({"benchmarks": benchmarks}))

    def lines(self, name):
        with open(os.path.join(self.root, ".devkit", "perf", name + ".jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_perf_run_history_and_changepoint(self):
        self.config([{"name": "noop", "command": "python3 -c pass", "runs": 3, "warmup": 0}])
        d = self.repo.json("perf", "run")
        self.assertEqual(d["results"][0]["file"], ".devkit/perf/noop.jsonl")
        rows = self.lines("noop")
        self.assertEqual(len(rows), 1)
        for key in ("ts", "commit", "dirty", "median", "iqr", "cpu_median", "runs"):
            self.assertIn(key, rows[0])
        self.assertEqual(rows[0]["runs"], 3)
        self.assertGreater(rows[0]["median"], 0)
        p = self.repo.run("perf", "run", "--runs", "2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("noop: median", p.stdout)
        self.assertEqual([r["runs"] for r in self.lines("noop")], [3, 2])
        h = self.repo.json("perf", "history", "noop")
        self.assertEqual(h["points"], 2)
        p = self.repo.run("perf", "history", "noop")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("noop: 2 point(s)", p.stdout)
        p = self.repo.run("changepoint", "--perf", "noop")
        self.assertEqual(p.returncode, 2)                  # 2 points < 2 x min-size
        self.assertIn("need at least", p.stderr)
        # a synthetic 24-point series with a 50% step at index 12; labels are short commits
        with open(os.path.join(self.root, ".devkit", "perf", "noop.jsonl"), "w") as f:
            for i in range(24):
                f.write(json.dumps({"ts": "t%d" % i, "commit": "%07x" % (i + 1) + "0" * 33,
                                    "dirty": i == 23, "median": (1.0 if i < 12 else 1.5)
                                    + (i % 3) * 0.01, "iqr": 0.0, "cpu_median": None,
                                    "runs": 7}) + "\n")
        c = self.repo.json("changepoint", "--perf", "noop")
        self.assertEqual(c["source"], "perf:noop")
        self.assertEqual(c["n"], 24)
        self.assertEqual([pt["index"] for pt in c["points"]], [12])
        self.assertEqual(c["points"][0]["label"], "%07x" % 13)
        self.assertGreater(c["points"][0]["percent_change"], 0)
        self.assertEqual(perf.label_of({"commit": "abcdef0123", "dirty": True}), "abcdef0-dirty")

    def test_perf_errors(self):
        p = self.repo.run("perf", "run")
        self.assertEqual(p.returncode, 2)
        self.assertIn("no benchmarks", p.stderr)
        self.config([{"name": "bad name", "command": "x"}])
        self.assertEqual(self.repo.run("perf", "run").returncode, 2)
        self.config([{"name": "fails", "command": "python3 -c \"raise SystemExit(3)\"", "runs": 1}])
        p = self.repo.run("perf", "run")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.config([{"name": "gone", "command": "no-such-binary-xyz", "runs": 1}])
        self.assertEqual(self.repo.run("perf", "run").returncode, 3)
        self.config([{"name": "a", "command": "python3 -c pass"}])
        self.assertEqual(self.repo.run("perf", "run", "--name", "b").returncode, 2)
        p = self.repo.run("perf", "history", "nosuch")
        self.assertEqual(p.returncode, 2)
        self.assertIn("no perf history", p.stderr)
        self.assertEqual(self.repo.run("perf", "nosuch").returncode, 2)
        self.assertEqual(self.repo.run("changepoint").returncode, 2)
        series = os.path.join(self.repo.tmp, "s.csv")
        with open(series, "w") as f:
            f.write("\n".join(["1"] * 20))
        self.assertEqual(self.repo.run("changepoint", series, "--perf", "a").returncode, 2)


if __name__ == "__main__":
    unittest.main()
