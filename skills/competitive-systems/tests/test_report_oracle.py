"""`dev.py report` and `dev.py oracle` through the CLI, offline, in temporary directories.

report: fills Before and Verdict from a compare JSON written by `dev.py compare --json`,
names every unfilled field under Not verified, never prints a ratio without the absolute
medians, fills Card facts from the catalog, records the oracle command's exit code.
oracle: the scaffold passes for a correct implementation of `sorted`, fails for a wrong one,
fails the anti-cache test for one that caches by id(), and never overwrites without --force.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
DEV = os.path.join(SCRIPTS, "dev.py")
TIME = re.compile(r"\d+(\.\d+)? (s|ms|us)\b")
RATIO = re.compile(r"\d+\.\d+x (faster|slower)|ratio \d+\.\d+x")


def run_dev(args, cwd=None):
    return subprocess.run([sys.executable, "-I", DEV] + list(args), cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def synthetic_compare(before, after, path, min_effect=0.05):
    """A devkit-compare/1 file built by the same functions `dev.py compare --json` uses."""
    sys.path.insert(0, SCRIPTS)
    try:
        from devkit import compare
    finally:
        sys.path.remove(SCRIPTS)
    result = compare.analyze(before, after, min_effect)
    data = compare.compare_json(result, "min effect 5% (--min-effect)", [],
                                [{"python": "3.x", "platform": "test-os", "cpu_count": 4,
                                  "loadavg": [0.5, 0.5, 0.5], "rev": "abcdef0123", "dirty": False}])
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def field_line(text, field):
    for line in text.splitlines():
        if line.startswith(field + ":"):
            return line
    raise AssertionError("no %s line in:\n%s" % (field, text))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)

    def assert_no_bare_ratio(self, text):
        for line in text.splitlines():
            if RATIO.search(line):
                self.assertTrue(len(TIME.findall(line)) >= 2,
                                "ratio without both absolute medians: %s" % line)

    def test_fills_before_and_verdict_from_cli_compare(self):
        bench = self.path("b.json")
        p = run_dev(["bench", "--arm", "a=%s -c pass" % sys.executable,
                     "--arm", "b=%s -c pass" % sys.executable, "--runs", "3", "--out", bench,
                     "--repo", self.dir])
        self.assertEqual(p.returncode, 0, p.stderr)
        q = run_dev(["compare", bench, "--json", "--repo", self.dir])
        self.assertEqual(q.returncode, 0, q.stderr)
        with open(self.path("c.json"), "w") as f:
            f.write(q.stdout)
        data = json.loads(q.stdout)
        r = run_dev(["report", "--compare", self.path("c.json")])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(field_line(r.stdout, "Before"), data["report_lines"][0] if
                         data["report_lines"][0].endswith(".") else data["report_lines"][0] + ".")
        verdict = field_line(r.stdout, "Verdict")
        self.assertIn("95% CI", verdict)
        self.assertEqual(len(TIME.findall(verdict)), 2, verdict)
        self.assertNotIn("<", verdict)
        not_verified = field_line(r.stdout, "Not verified")
        for f in ("Problem", "Gate 1", "Gate 2", "Gate 3", "Noise floor", "Card", "Invariant",
                  "Oracle", "Class", "Guards", "Kept"):
            self.assertIn(f, not_verified)
        self.assertNotIn("Before,", not_verified)
        self.assertIn("Environment: Python", r.stdout)
        self.assertIn("Protocol: metric wall; runs 3/3", r.stdout)
        self.assert_no_bare_ratio(r.stdout)

    def test_faster_verdict_carries_medians_and_card_facts(self):
        synthetic_compare([1.00, 1.02, 0.98, 1.01, 0.99, 1.03, 0.97, 1.00, 1.01, 0.99],
                          [0.50, 0.51, 0.49, 0.50, 0.52, 0.48, 0.50, 0.51, 0.49, 0.50],
                          self.path("c.json"))
        r = run_dev(["report", "--compare", self.path("c.json"), "--set", "card=lpt-workers",
                     "--set", "precondition=m=8 >= 2, independent jobs: pass",
                     "--set", "Problem=CI takes 40 min on 8 runners.",
                     "--set", "not verified=shared resources between the two longest jobs",
                     "--out", self.path("r.md")])
        self.assertEqual(r.returncode, 0, r.stderr)
        verdict = field_line(r.stdout, "Verdict")
        self.assertTrue(verdict.startswith("Verdict: 2.00x faster (95% CI "), verdict)
        self.assertIn("medians 1.00 s -> 500.0 ms", verdict)
        self.assertEqual(field_line(r.stdout, "Card facts"), "Card facts: (4/3 - 1/(3m)); m >= 2.")
        self.assertEqual(field_line(r.stdout, "Card"),
                         "Card: LPT across workers (cards-scheduling.md). Precondition: m=8 >= 2, "
                         "independent jobs: pass.")
        self.assertEqual(field_line(r.stdout, "Problem"), "Problem: CI takes 40 min on 8 runners.")
        nv = field_line(r.stdout, "Not verified")
        self.assertNotIn("Problem", nv)
        self.assertNotIn("Card,", nv)
        self.assertIn("shared resources between the two longest jobs", nv)
        with open(self.path("r.md")) as f:
            self.assertEqual(f.read(), r.stdout)
        self.assert_no_bare_ratio(r.stdout)

    def test_inconclusive_and_no_change_lines_keep_medians(self):
        for before, after in (([1.0, 1.1, 0.9, 1.05, 0.95, 1.2, 0.8, 1.0, 1.1, 0.9],
                               [1.0, 1.15, 0.85, 1.05, 0.9, 1.2, 0.8, 1.0, 1.1, 0.95]),
                              ([1.000, 1.001, 0.999] * 7, [1.000, 1.001, 0.999] * 7)):
            synthetic_compare(before, after, self.path("c.json"))
            r = run_dev(["report", "--compare", self.path("c.json")])
            self.assertEqual(r.returncode, 0, r.stderr)
            verdict = field_line(r.stdout, "Verdict")
            self.assertRegex(verdict, r"inconclusive|no detectable change")
            self.assertEqual(len(TIME.findall(verdict)), 2, verdict)
            self.assert_no_bare_ratio(r.stdout)

    def test_class_noise_floor_and_oracle_lines(self):
        synthetic_compare([2.0] * 5 + [2.1] * 5, [1.0] * 5 + [1.05] * 5, self.path("c.json"))
        for name, exp, ci in (("sb.json", 2.01, [1.95, 2.07]), ("sa.json", 1.0, [0.97, 1.03])):
            with open(self.path(name), "w") as f:
                json.dump({"schema": "devkit-scale/1", "exponent": exp, "exponent_ci": ci,
                           "sizes": [{"n": 1000}, {"n": 8000}]}, f)
        with open(self.path("aa.json"), "w") as f:
            json.dump({"command": ["python3", "x.py"], "min_effect": 0.04, "ci": [0.98, 1.02],
                       "runs": 20, "timestamp": "2026-01-01T00:00:00+00:00"}, f)
        ok = "%s -c \"print('Ran 300 tests'); print('OK')\"" % sys.executable
        r = run_dev(["report", "--compare", self.path("c.json"), "--scale-before",
                     self.path("sb.json"), "--scale-after", self.path("sa.json"), "--aa",
                     self.path("aa.json"), "--oracle-cmd", ok])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(field_line(r.stdout, "Class"),
                         "Class: exponent 2.01 (95% CI 1.95..2.07 at n=1000..8000) -> 1.00 "
                         "(95% CI 0.97..1.03 at n=1000..8000).")
        self.assertEqual(field_line(r.stdout, "Noise floor"),
                         "Noise floor: A/A ratio CI 0.980..1.020 on 20 runs; min effect 4.0%.")
        oracle = field_line(r.stdout, "Oracle")
        self.assertIn("exit 0 (pass); last line: OK.", oracle)
        bad = "%s -c \"import sys; print('FAILED (failures=1)'); sys.exit(1)\"" % sys.executable
        r = run_dev(["report", "--compare", self.path("c.json"), "--oracle-cmd", bad])
        self.assertEqual(r.returncode, 1)
        self.assertIn("exit 1 (FAIL); last line: FAILED (failures=1).", field_line(r.stdout, "Oracle"))
        self.assertIn("the oracle failed, so the timing is not evidence",
                      field_line(r.stdout, "Not verified"))

    def test_usage_errors_exit_2_without_traceback(self):
        with open(self.path("junk.json"), "w") as f:
            f.write('{"schema": "something-else"}')
        synthetic_compare([1.0] * 10, [0.5] * 10, self.path("c.json"))
        for args in (["--compare", self.path("junk.json")],
                     ["--compare", self.path("missing.json")],
                     ["--compare", self.path("c.json"), "--set", "nosuchfield=x"],
                     ["--compare", self.path("c.json"), "--set", "card=no-such-card"],
                     ["--compare", self.path("c.json"), "--scale-before", self.path("c.json")],
                     []):
            r = run_dev(["report"] + args)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
            self.assertTrue(r.stderr.startswith("dev.py report:"), r.stderr)
            self.assertNotIn("Traceback", r.stderr)


IMPLS = textwrap.dedent('''
    import math

    _cache = {}


    def good(xs):
        out = list(xs)
        out.sort()
        return out


    def wrong(xs):
        return sorted(set(xs))


    def cached(xs):
        key = id(xs)
        if key not in _cache:
            _cache[key] = sorted(xs)
        return _cache[key]


    def close_abs(x):
        return abs(x) * (1 + 1e-12)


    def strip_all(s):
        return s.strip()
''')


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        with open(os.path.join(self.dir, "impls.py"), "w") as f:
            f.write(IMPLS)

    def tearDown(self):
        self.tmp.cleanup()

    def scaffold(self, *args):
        return run_dev(["oracle"] + list(args), cwd=self.dir)

    def run_tests(self, rel):
        # The file runs itself (unittest.main) and puts its root on sys.path, so -I works.
        return subprocess.run([sys.executable, "-I", os.path.join(self.dir, rel), "-v"], cwd=self.dir,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)

    def test_correct_wrong_and_identity_cached_implementations(self):
        for name, want_rc in (("good", 0), ("wrong", 1), ("cached", 1)):
            p = self.scaffold("--ref", "builtins:sorted", "--new", "impls:%s" % name)
            self.assertEqual(p.returncode, 0, p.stderr)
            rel = "tests/test_oracle_%s.py" % name
            self.assertIn("wrote %s" % rel, p.stdout)
            t = self.run_tests(rel)
            self.assertEqual(t.returncode, want_rc, t.stdout)
            if name == "cached":
                # test_matches_reference may fail too: ids freed by the other test get reused
                self.assertIn("FAIL: test_anti_cache_mutated_input", t.stdout)
            if name == "wrong":
                self.assertIn("FAIL: test_matches_reference", t.stdout)

    def test_generated_file_is_deterministic_and_runs_directly(self):
        self.scaffold("--ref", "builtins:sorted", "--new", "impls:good", "--out", "a.py")
        self.scaffold("--ref", "builtins:sorted", "--new", "impls:good", "--out", "b.py")
        with open(os.path.join(self.dir, "a.py")) as f1, open(os.path.join(self.dir, "b.py")) as f2:
            self.assertEqual(f1.read().replace("a.py", ""), f2.read().replace("b.py", ""))
        p = subprocess.run([sys.executable, "-I", os.path.join(self.dir, "a.py")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_refuses_to_overwrite_without_force(self):
        self.assertEqual(self.scaffold("--ref", "builtins:sorted", "--new", "impls:good").returncode, 0)
        target = os.path.join(self.dir, "tests", "test_oracle_good.py")
        with open(target, "a") as f:
            f.write("# hand edit\n")
        p = self.scaffold("--ref", "builtins:sorted", "--new", "impls:good")
        self.assertEqual(p.returncode, 1)
        self.assertIn("exists; pass --force", p.stderr)
        with open(target) as f:
            self.assertTrue(f.read().endswith("# hand edit\n"))
        p = self.scaffold("--ref", "builtins:sorted", "--new", "impls:good", "--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(target) as f:
            self.assertNotIn("# hand edit", f.read())

    def test_float_tolerance_and_string_generator(self):
        self.scaffold("--ref", "builtins:abs", "--new", "impls:close_abs", "--gen", "floats",
                      "--out", "t_exact.py")
        self.scaffold("--ref", "builtins:abs", "--new", "impls:close_abs", "--gen", "floats",
                      "--tol", "1e-9", "--out", "t_tol.py")
        self.assertEqual(self.run_tests("t_exact.py").returncode, 1)
        t = self.run_tests("t_tol.py")
        self.assertEqual(t.returncode, 0, t.stdout)
        self.assertIn("skipped", t.stdout)                  # floats are immutable: no anti-cache
        self.scaffold("--ref", "builtins:str.strip", "--new", "impls:strip_all", "--gen", "strings",
                      "--out", "t_str.py")
        self.assertEqual(self.run_tests("t_str.py").returncode, 0)

    def test_usage_errors(self):
        for args in (["--ref", "nosuchmodule:f", "--new", "impls:good"],
                     ["--ref", "builtins:sorted", "--new", "impls:missing"],
                     ["--ref", "builtins:sorted", "--new", "not-a-spec"],
                     ["--ref", "builtins:sorted", "--new", "impls:good", "--gen", "tuples"],
                     ["--ref", "builtins:sorted", "--new", "impls:good", "--cases", "0"]):
            p = self.scaffold(*args)
            self.assertEqual(p.returncode, 2, (args, p.stderr))
            self.assertTrue(p.stderr.startswith("dev.py oracle:"), p.stderr)
            self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
