"""CLI smoke tests for scripts/dev.py (bench, compare, aa, scale, bound, changepoint) through
subprocess. Standard library only; no network; commands under test are `true` and tiny
python3 one-liners so the file stays well under ten seconds."""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
DEV = os.path.join(SCRIPTS, "dev.py")
PY = sys.executable


def run_dev(*args, **kw):
    env = dict(os.environ)
    env.update(kw.pop("env", {}))
    return subprocess.run([PY, "-I", DEV] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, env=env, **kw)


def bench_json(before, after, path, meta=None, command=("true",), cpu=None):
    """Write a devkit-bench/1 file with two arms whose wall times are given."""
    def arm(vals):
        return {"command": list(command), "shell": False, "wall": vals,
                "cpu": [cpu if cpu is not None else v * 0.9 for v in vals],
                "rss": [1 << 20] * len(vals), "exit": [0] * len(vals),
                "stdout_sha256": [None] * len(vals)}
    data = {"schema": "devkit-bench/1", "arms": {"before": arm(before), "after": arm(after)},
            "order": ["before", "after"] * len(before), "runs": len(before), "warmup": 0,
            "meta": meta or {"python": "3.8.0", "platform": "test", "cpu_count": 8,
                             "loadavg": [1.0, 1.0, 1.0], "rev": "abc", "dirty": False}}
    with open(path, "w") as f:
        json.dump(data, f)
    return path


WIDE_BEFORE = [1.00, 1.30, 0.80, 1.10, 0.90, 1.20, 0.95]
WIDE_AFTER = [0.95, 1.25, 0.85, 1.05, 1.00, 1.15, 0.90]
TEN_BEFORE = [2.00, 2.10, 1.90, 2.05, 2.20, 1.95, 2.00, 2.10, 2.00, 1.98]
TEN_AFTER = [1.00, 1.10, 0.90, 1.05, 1.20, 0.95, 1.00, 1.10, 1.00, 0.98]
TIGHT = [1.000, 1.001, 0.999, 1.002, 0.998, 1.000, 1.001, 0.999, 1.000, 1.000]


class DispatcherTests(unittest.TestCase):
    def test_help_and_unknown_command(self):
        for args in (["--help"], ["help"]):
            p = run_dev(*args)
            self.assertEqual(p.returncode, 0, p.stderr)
            for name in ("bench", "compare", "aa", "scale", "bound", "changepoint"):
                self.assertIn(name, p.stdout)
        self.assertEqual(run_dev().returncode, 2)
        p = run_dev("nosuchcmd")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown command", p.stderr)
        self.assertEqual(run_dev("--version").stdout.strip(), "3.0.0")
        for cmd in ("bench", "compare", "aa", "scale", "bound", "changepoint"):
            self.assertEqual(run_dev(cmd, "--help").returncode, 0, cmd)

    def test_argparse_errors_are_one_prefixed_line(self):
        cases = ((["bench", "--runs", "abc"], "dev.py bench:"),
                 (["compare", "--alpha", "z", "x.json"], "dev.py compare:"),
                 (["bound", "amdahl", "--p", "x", "--s", "2"], "dev.py bound:"),
                 (["scale", "--sizes", "1,2", "--nosuch", "--", "true", "{n}"], "dev.py scale:"),
                 (["changepoint"], "dev.py changepoint:"))
        for args, prefix in cases:
            p = run_dev(*args)
            self.assertEqual(p.returncode, 2, args)
            self.assertTrue(p.stderr.startswith(prefix), (args, p.stderr))
            self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
            self.assertIn("--help", p.stderr)
            self.assertNotIn("Traceback", p.stderr)


class BenchTests(unittest.TestCase):
    def test_one_arm_json_keys_and_run_count(self):
        p = run_dev("bench", "--arm", "a=true", "--runs", "4", "--warmup", "0", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["schema"], "devkit-bench/1")
        self.assertEqual(list(data["arms"]), ["a"])
        arm = data["arms"]["a"]
        for key in ("command", "wall", "cpu", "rss", "exit", "stdout_sha256"):
            self.assertEqual(len(arm[key]) if key != "command" else 4, 4, key)
        self.assertEqual(arm["exit"], [0, 0, 0, 0])
        self.assertEqual(data["order"], ["a"] * 4)
        for key in ("python", "platform", "cpu_count", "timestamp", "seed", "cpu_accounting",
                    "inputs"):
            self.assertIn(key, data["meta"])
        self.assertEqual(data["meta"]["seed"], 0)

    def test_one_arm_human_output(self):
        p = run_dev("bench", "--arm", "a=true", "--runs", "2", "--warmup", "0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("runs    2", p.stdout)
        self.assertIn("Gate 1: CPU", p.stdout)

    def test_differing_outputs_make_the_verdict_invalid(self):
        p = run_dev("bench", "--arm", "a=%s -c print(1)" % PY, "--arm", "b=%s -c print(2)" % PY,
                    "--runs", "2", "--warmup", "0", "--check-output", "--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        data = json.loads(p.stdout)
        self.assertTrue(any("outputs differ" in w for w in data["warnings"]), data["warnings"])
        self.assertIn("outputs differ", p.stderr)
        self.assertEqual(data["outputs_differ"], [["a", "b"]])
        self.assertEqual(data["compare"]["verdict"], "INVALID")
        self.assertIn("no timing claim", data["compare"]["report_lines"][1])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            with open(path, "w") as f:
                json.dump(data, f)
            c = run_dev("compare", path)
            self.assertEqual(c.returncode, 1, c.stderr)
            self.assertIn("verdict: INVALID", c.stdout)
            self.assertNotIn("Paste into the report", c.stdout)
            t = run_dev("bench", "--arm", "a=%s -c print(1)" % PY, "--arm",
                        "b=%s -c print(2)" % PY, "--runs", "2", "--warmup", "0",
                        "--check-output")
            self.assertEqual(t.returncode, 1)
            self.assertNotIn("Paste into the report", t.stdout)
        e = run_dev("bench", "--arm", "a=%s -c pass" % PY, "--arm", "b=%s -c pass" % PY,
                    "--runs", "2", "--warmup", "0", "--check-output", "--json")
        self.assertEqual(e.returncode, 0, e.stderr)
        self.assertIn("compared empty outputs", e.stderr)
        hashes = data["arms"]["a"]["stdout_sha256"]
        self.assertEqual(len(set(hashes)), 1)
        self.assertNotEqual(hashes[0], data["arms"]["b"]["stdout_sha256"][0])

    def test_order_random_is_seeded_and_blocked_is_grouped(self):
        p1 = run_dev("bench", "--arm", "a=true", "--arm", "b=true", "--runs", "3", "--warmup", "0",
                     "--order", "random", "--seed", "5", "--json")
        p2 = run_dev("bench", "--arm", "a=true", "--arm", "b=true", "--runs", "3", "--warmup", "0",
                     "--order", "random", "--seed", "5", "--json")
        self.assertEqual(json.loads(p1.stdout)["order"], json.loads(p2.stdout)["order"])
        p3 = run_dev("bench", "--arm", "a=true", "--arm", "b=true", "--runs", "2", "--warmup", "0",
                     "--order", "blocked", "--json")
        self.assertEqual(json.loads(p3.stdout)["order"], ["a", "a", "b", "b"])

    def test_compare_on_identical_arms_is_never_faster(self):
        verdicts = []
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "b.json")
            for _ in range(2):  # retry once: a loaded machine can produce one odd sample
                p = run_dev("bench", "--arm", "a=true", "--arm", "b=true", "--runs", "6",
                            "--warmup", "0", "--out", out)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("verdict:", p.stdout)
                q = run_dev("compare", out, "--json", "--resamples", "500")
                self.assertEqual(q.returncode, 0, q.stderr)
                data = json.loads(q.stdout)
                verdicts.append(data["verdict"])
                if data["verdict"] in ("NO DETECTABLE CHANGE", "INCONCLUSIVE"):
                    break
            self.assertTrue(set(verdicts) & {"NO DETECTABLE CHANGE", "INCONCLUSIVE"}, verdicts)
            self.assertEqual(data["schema"], "devkit-compare/1")
            self.assertEqual(data["arms"], ["a", "b"])
            self.assertIn("ci_lo", data["effect"])
            self.assertEqual(len(data["report_lines"]), 2)
            self.assertTrue(data["report_lines"][0].startswith("Before: median"))
            self.assertTrue(data["report_lines"][1].startswith("Verdict:"))

    def test_usage_errors(self):
        p = run_dev("bench", "--runs", "2")
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("dev.py bench:"), p.stderr)
        p = run_dev("bench", "--arm", "noequals", "--runs", "1")
        self.assertEqual(p.returncode, 2)
        self.assertIn("NAME=COMMAND", p.stderr)
        p = run_dev("bench", "--arm", "a=true", "--arm", "a=true", "--runs", "1")
        self.assertEqual(p.returncode, 2)
        self.assertIn("distinct", p.stderr)
        p = run_dev("bench", "--arm", "a=true", "--runs", "0")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--runs", p.stderr)

    def test_failing_command_exits_1_with_stderr_tail(self):
        p = run_dev("bench", "--arm", "a=%s -c \"import sys; sys.stderr.write('boom\\\\nbang\\\\n'); "
                    "sys.exit(4)\"" % PY, "--runs", "1", "--warmup", "0")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("dev.py bench: measured command failed with exit 4", p.stderr)
        self.assertIn("boom", p.stderr)
        self.assertIn("bang", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_missing_executable_exits_3(self):
        p = run_dev("bench", "--arm", "a=dev_no_such_command_xyz", "--runs", "1", "--warmup", "0")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("dev.py bench: cannot run dev_no_such_command_xyz", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_timeout_is_a_failure_not_a_crash(self):
        p = run_dev("bench", "--arm", "a=%s -c \"import time; time.sleep(5)\"" % PY, "--runs", "1",
                    "--warmup", "0", "--timeout", "0.3")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("timed out", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


    @unittest.skipIf(os.name != "posix", "process groups are POSIX")
    def test_timeout_kills_everything_the_shell_started(self):
        import time
        tmp = tempfile.mkdtemp()
        try:
            marks = [os.path.join(tmp, m) for m in ("bench", "check")]
            t0 = time.perf_counter()
            p = run_dev("bench", "--shell", "--arm", "a=(sleep 2; touch %s) & wait" % marks[0],
                        "--runs", "1", "--warmup", "0", "--timeout", "0.2")
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertIn("timed out after 0.2 s", p.stderr)
            self.assertLess(time.perf_counter() - t0, 1.5)      # did not wait for the sleep
            if SCRIPTS not in sys.path:
                sys.path.insert(0, SCRIPTS)
            from devkit.checks import run_process
            t1 = time.perf_counter()
            rc, out, _ = run_process("(sleep 2; touch %s) & wait" % marks[1], tmp, shell=True,
                                     timeout=0.2)
            self.assertEqual(rc, 124, out)
            self.assertLess(time.perf_counter() - t1, 1.5)
            time.sleep(max(0.0, t1 + 2.4 - time.perf_counter()))
            self.assertEqual([m for m in marks if os.path.exists(m)], [])
        finally:
            shutil.rmtree(tmp)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_faster_verdict_lines_and_fail_on(self):
        path = bench_json(TEN_BEFORE, TEN_AFTER, os.path.join(self.tmp, "b.json"))
        p = run_dev("compare", path, "--resamples", "500", "--fail-on", "slower,inconclusive")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("verdict: FASTER", p.stdout)
        self.assertIn("Before: median 2.00 s (IQR 120.0 ms, runs=10); After: median 1.00 s "
                      "(IQR 120.0 ms, runs=10).", p.stdout)
        self.assertIn("Verdict: 2.00x faster (95% CI ", p.stdout)
        self.assertIn(", p=", p.stdout)
        self.assertIn("min effect 5% (default; run dev.py aa", p.stdout)
        self.assertNotIn("only 10 runs", p.stderr)
        # the mirror is SLOWER and --fail-on slower exits 1
        path2 = bench_json(TEN_AFTER, TEN_BEFORE, os.path.join(self.tmp, "c.json"))
        q = run_dev("compare", path2, "--resamples", "500", "--fail-on", "slower", "--json")
        self.assertEqual(q.returncode, 1, q.stderr)
        data = json.loads(q.stdout)
        self.assertEqual(data["verdict"], "SLOWER")
        self.assertTrue(data["report_lines"][1].startswith("Verdict: 2.00x slower"))
        self.assertLess(data["effect"]["p_slower"], 0.001)
        self.assertAlmostEqual(data["effect"]["hodges_lehmann_shift"], -1.0, delta=0.05)

    def test_inconclusive_with_fail_on_exits_1(self):
        path = bench_json(WIDE_BEFORE, WIDE_AFTER, os.path.join(self.tmp, "b.json"))
        p = run_dev("compare", path, "--resamples", "500")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("verdict: INCONCLUSIVE", p.stdout)
        self.assertIn("add runs", p.stdout)
        self.assertIn("try 28 runs per arm", p.stdout)  # half-width 0.23 > 2 x 0.05: quadruple
        self.assertIn("only 7 runs", p.stderr)
        q = run_dev("compare", path, "--resamples", "500", "--fail-on", "inconclusive", "--json")
        self.assertEqual(q.returncode, 1, q.stderr)
        data = json.loads(q.stdout)
        self.assertEqual(data["verdict"], "INCONCLUSIVE")
        self.assertEqual(data["suggested_runs"], 28)
        self.assertIsNotNone(data["effect"]["ci_hi"])

    def test_no_detectable_change_with_min_effect(self):
        after = [1.001, 1.000, 0.999, 1.002, 0.999, 1.000, 1.001, 0.998, 1.000, 1.001]
        path = bench_json(TIGHT, after, os.path.join(self.tmp, "b.json"))
        p = run_dev("compare", path, "--resamples", "500", "--min-effect", "0.02", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["verdict"], "NO DETECTABLE CHANGE")
        self.assertEqual(data["min_effect"], 0.02)
        self.assertIn("--min-effect", data["min_effect_source"])
        self.assertIn("no detectable change", data["report_lines"][1])
        self.assertIn("min effect 2%", data["report_lines"][1])

    def test_two_files_named_arms_and_metadata_mismatch(self):
        a = bench_json(TEN_BEFORE, TIGHT, os.path.join(self.tmp, "a.json"))
        meta = {"python": "3.9.0", "platform": "other", "cpu_count": 8, "loadavg": [7.0, 1, 1],
                "rev": "def"}
        b = bench_json(TEN_BEFORE, TIGHT, os.path.join(self.tmp, "b.json"), meta)
        p = run_dev("compare", a, b, "--before", "after", "--after", "after", "--resamples", "500",
                    "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["arms"], ["after", "after"])
        self.assertEqual(data["verdict"], "NO DETECTABLE CHANGE")
        joined = " ".join(data["warnings"])
        self.assertIn("metadata differs", joined)
        self.assertIn("load average 7.0", joined)
        q = run_dev("compare", a, "--metric", "rss", "--resamples", "500", "--json")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(json.loads(q.stdout)["metric"], "rss")

    def test_compare_usage_errors(self):
        self.assertEqual(run_dev("compare").returncode, 2)
        bad = os.path.join(self.tmp, "x.json")
        with open(bad, "w") as f:
            f.write("{}")
        p = run_dev("compare", bad)
        self.assertEqual(p.returncode, 2)
        self.assertIn("devkit-bench/1", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        path = bench_json(WIDE_BEFORE, WIDE_AFTER, os.path.join(self.tmp, "b.json"))
        p = run_dev("compare", path, "--before", "zzz")
        self.assertEqual(p.returncode, 2)
        self.assertIn("not in", p.stderr)
        p = run_dev("compare", path, "--fail-on", "faster")
        self.assertEqual(p.returncode, 2)
        p = run_dev("compare", os.path.join(self.tmp, "missing.json"))
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)


class AATests(unittest.TestCase):
    def test_aa_save_then_compare_uses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DEVKIT_REPO": tmp}
            p = run_dev("aa", "--runs", "3", "--warmup", "0", "--save", "--resamples", "300",
                        "--", "true", cwd=tmp, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("A/A noise floor: ratio CI", p.stdout)
            self.assertIn("minimum effect", p.stdout)
            saved = os.path.join(tmp, ".devkit", "aa.json")
            self.assertTrue(os.path.isfile(saved))
            with open(saved) as f:
                data = json.load(f)
            for key in ("command", "min_effect", "ci", "runs", "timestamp"):
                self.assertIn(key, data)
            self.assertGreaterEqual(data["min_effect"], 0.02)
            self.assertEqual(data["runs"], 3)
            path = bench_json(TEN_BEFORE, TEN_AFTER, os.path.join(tmp, "b.json"))
            q = run_dev("compare", path, "--resamples", "300", cwd=tmp, env=env)
            self.assertEqual(q.returncode, 0, q.stderr)
            self.assertIn("min effect %.1f%% (from .devkit/aa.json" % (data["min_effect"] * 100),
                          q.stdout)
            r = run_dev("aa", "--runs", "2", "--warmup", "0", "--json", "--resamples", "300",
                        "--", "true", cwd=tmp, env=env)
            self.assertEqual(json.loads(r.stdout)["schema"], "devkit-aa/1")

    def write_aa(self, tmp, command, min_effect, runs=20):
        import datetime
        os.makedirs(os.path.join(tmp, ".devkit"), exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(os.path.join(tmp, ".devkit", "aa.json"), "w") as f:
            json.dump({"command": command, "min_effect": min_effect, "ci": [0.9, 1.1],
                       "runs": runs, "timestamp": stamp}, f)

    def test_saved_floor_applies_only_to_its_own_command_and_never_below(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DEVKIT_REPO": tmp}
            self.write_aa(tmp, ["python3", "run_old.py", "4000"], 0.12)
            other = bench_json(TEN_BEFORE, TEN_AFTER, os.path.join(tmp, "o.json"),
                               command=("sleep", "1"))
            q = run_dev("compare", other, "--resamples", "300", cwd=tmp, env=env)
            self.assertEqual(q.returncode, 0, q.stderr)
            self.assertIn("min effect 5% (default", q.stdout)
            self.assertIn("not on these arms", q.stderr)
            same = bench_json(TEN_BEFORE, TEN_AFTER, os.path.join(tmp, "s.json"),
                              command=("python3", "run_old.py", "20000"))
            q = run_dev("compare", same, "--resamples", "300", cwd=tmp, env=env)
            self.assertIn("min effect 12.0% (from .devkit/aa.json on `python3 run_old.py`, "
                          "20 runs", q.stdout)
            q = run_dev("compare", same, "--resamples", "300", "--min-effect", "0.01",
                        cwd=tmp, env=env)
            self.assertIn("min effect 12.0% (the A/A noise floor", q.stdout)
            q = run_dev("compare", same, "--resamples", "300", "--min-effect", "0.2",
                        cwd=tmp, env=env)
            self.assertIn("min effect 20% (--min-effect)", q.stdout)

    def test_save_refuses_a_noisier_floor_and_shell_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DEVKIT_REPO": tmp}
            self.write_aa(tmp, ["true"], 0.05, runs=20)
            p = run_dev("aa", "--runs", "3", "--warmup", "0", "--save", "--", "true",
                        cwd=tmp, env=env)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("pass --force", p.stderr)
            p = run_dev("aa", "--runs", "3", "--warmup", "0", "--save", "--force",
                        "--resamples", "300", "--", "true", cwd=tmp, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("runs the command 6 times", p.stderr)
            self.assertIn("under 10", p.stderr)
            p = run_dev("aa", "--shell", "--runs", "2", "--warmup", "0", "--resamples", "300",
                        "--", PY, "-c", "print(1)", cwd=tmp, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_aa_needs_a_command(self):
        p = run_dev("aa", "--runs", "2")
        self.assertEqual(p.returncode, 2)
        self.assertIn("dev.py aa:", p.stderr)


class RobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="devkit-robust-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bench_rejects_typos_before_the_timed_runs(self):
        slow = "a=%s -c \"import time; time.sleep(0.5)\"" % PY
        for extra in (["--input", "/nonexistent/file"], ["--repo", "/nonexistent"],
                      ["--out", "/nonexistent/dir/b.json"], ["--min-effect", "-1"]):
            args = ["bench", "--arm", slow, "--runs", "3", "--warmup", "0"] + extra
            if extra[0] == "--min-effect":
                args += ["--arm", slow.replace("a=", "b=", 1)]
            t0 = time.time()
            p = run_dev(*args, cwd=self.tmp)
            self.assertEqual(p.returncode, 2, (extra, p.stderr))
            self.assertLess(time.time() - t0, 1.2, extra)
            self.assertNotIn("Traceback", p.stderr)

    def test_unbalanced_quotes_and_zero_medians_are_usage_errors(self):
        for args in (["bench", "--arm", "a=%s -c 'print(1)" % PY],
                     ["scale", "--sizes", "10,20", "--baseline", "%s -c 'x" % PY, "--",
                      PY, "-c", "print({n})"]):
            p = run_dev(*args, cwd=self.tmp)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("check the quotes", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
        path = bench_json(TEN_BEFORE, TEN_AFTER, os.path.join(self.tmp, "z.json"), cpu=0.0)
        p = run_dev("compare", path, "--metric", "cpu", cwd=self.tmp)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("zero median", p.stderr)
        p = run_dev("scale", "--sizes", "10,20", "--", PY, "-c", "pass", cwd=self.tmp)
        self.assertEqual(p.returncode, 2)
        self.assertIn("quote it as '{n}'", p.stderr)

    def test_compare_rss_uses_bytes_and_skips_wall_checks(self):
        path = os.path.join(self.tmp, "r.json")
        bench_json(TEN_BEFORE, TEN_AFTER, path, cpu=0.1)
        with open(path) as f:
            data = json.load(f)
        data["arms"]["before"]["rss"] = [8691712 + i * 4096 for i in range(10)]
        data["arms"]["after"]["rss"] = [8675328 + i * 4096 for i in range(10)]
        with open(path, "w") as f:
            json.dump(data, f)
        p = run_dev("compare", path, "--metric", "rss", "--min-effect", "0.05", cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("appears to be waiting", p.stderr)
        before_line = [ln for ln in p.stdout.splitlines() if ln.startswith("Before:")][0]
        self.assertNotRegex(before_line, r"\d s\b")
        self.assertRegex(before_line, r"MiB|KiB|MB|KB|B\b")

    def test_changepoint_rejects_non_finite_values(self):
        path = os.path.join(self.tmp, "s.csv")
        vals = ["100" if i < 30 else "120" for i in range(60)]
        vals[10] = "nan"
        with open(path, "w") as f:
            f.write("\n".join(vals) + "\n")
        p = run_dev("changepoint", path, cwd=self.tmp)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("value 10", p.stderr)
        sys.path.insert(0, SCRIPTS)
        try:
            from devkit import stats
        finally:
            sys.path.remove(SCRIPTS)
        series = [100.0 + (i % 3) * 0.1 for i in range(30)] + \
                 [120.0 + (i % 3) * 0.1 for i in range(30)]
        self.assertEqual([pt["index"] for pt in stats.changepoint(series, 5, 0.01)], [30])
        series[10] = float("nan")
        with self.assertRaises(ValueError):
            stats.changepoint(series, 5, 0.01)

    def test_internal_error_exit_code_and_legacy_zero_speedup(self):
        sys.path.insert(0, SCRIPTS)
        try:
            import importlib
            import types
            dev = importlib.import_module("dev")
            fake = types.ModuleType("devkit_fake_cmd")

            def cmd_bench(_args):
                raise RuntimeError("boom")
            fake.cmd_bench = cmd_bench
            sys.modules["devkit_fake_cmd"] = fake
            real = dev.find
            dev.find = lambda name: "devkit_fake_cmd"
            import contextlib
            import io
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    rc = dev.main(["bench"])
            finally:
                dev.find = real
                del sys.modules["devkit_fake_cmd"]
            self.assertEqual(rc, dev.INTERNAL_ERROR)
            self.assertIn("internal error: RuntimeError: boom", err.getvalue())
            from devkit import measure
            before = measure.summarize([0.001] * 7)
            after = measure.summarize([0.4] * 7)
            name, speedup, _ = measure.verdict(before, after)
            self.assertEqual(name, "slower")
            self.assertIn("slower", measure.report_lines(before, after, name, speedup)[1])
        finally:
            sys.path.remove(SCRIPTS)

    def test_scale_classifies_the_unrounded_exponent_and_reports_progress(self):
        sys.path.insert(0, SCRIPTS)
        try:
            from devkit import measure
        finally:
            sys.path.remove(SCRIPTS)
        real = measure.time_many
        measure.time_many = lambda cmd, runs, warmup, **kw: (
            [1e-7 * float(cmd[-1]) ** 1.696] * runs, [None] * runs)
        try:
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                res = measure.scale_core(["prog", "{n}"], [10000, 20000, 40000, 80000], 3, 0,
                                         None, True, progress=True)
        finally:
            measure.time_many = real
        self.assertEqual(res["exponent"], 1.7)
        self.assertAlmostEqual(res["exponent_raw"], 1.696, places=3)
        self.assertEqual(res["class"], "between linear and quadratic")
        self.assertIn("scale: n=10000 median", err.getvalue())
        self.assertIn("if time grows quadratically", err.getvalue())

    def test_time_once_with_and_without_waitid(self):
        sys.path.insert(0, SCRIPTS)
        try:
            from devkit import measure
        finally:
            sys.path.remove(SCRIPTS)
        real = measure.wait_unreaped
        try:
            for waited in (True, False):
                if not waited:
                    measure.wait_unreaped = lambda proc: False
                r = measure.time_once([PY, "-c", "print(1)"], capture_stdout=True)
                self.assertEqual(r["exit"], 0)
                self.assertGreater(r["wall"], 0)
                self.assertIsNotNone(r["stdout_sha256"])
                self.assertFalse(r["leftover"])
        finally:
            measure.wait_unreaped = real

    @unittest.skipUnless(os.name == "posix", "process groups and signals are POSIX")
    def test_sigterm_and_leftovers_kill_the_measured_process_group(self):
        import signal
        pidfile = os.path.join(self.tmp, "pid")
        proc = subprocess.Popen([PY, "-I", DEV, "bench", "--shell", "--runs", "1", "--warmup",
                                 "0", "--arm", "a=echo $$ > %s; exec sleep 30" % pidfile],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        for _ in range(100):
            if os.path.exists(pidfile) and open(pidfile).read().strip():
                break
            time.sleep(0.05)
        child = int(open(pidfile).read())
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        time.sleep(0.1)
        with self.assertRaises(OSError):
            os.kill(child, 0)
        bg = os.path.join(self.tmp, "bg")
        p = run_dev("bench", "--shell", "--runs", "1", "--warmup", "0",
                    "--arm", "a=sleep 30 & echo $! > %s" % bg, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("left processes running", p.stderr)
        time.sleep(0.1)
        with self.assertRaises(OSError):
            os.kill(int(open(bg).read()), 0)


class ScaleTests(unittest.TestCase):
    def test_rejects_baseline_with_no_baseline(self):
        p = run_dev("scale", "--sizes", "10,20", "--baseline", "true", "--no-baseline", "--",
                    "true", "{n}")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("dev.py scale: --baseline and --no-baseline", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_warns_floor_and_size_count_and_reports_ci(self):
        p = run_dev("scale", "--sizes", "10,20", "--runs", "2", "--warmup", "0", "--no-baseline",
                    "--json", "--", "true", "{n}")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["schema"], "devkit-scale/1")
        self.assertTrue(any("at least 0.5 s" in w for w in data["warnings"]), data["warnings"])
        self.assertTrue(any("only 2 sizes" in w for w in data["warnings"]), data["warnings"])
        self.assertIn("exponent_ci", data)
        self.assertEqual([r["n"] for r in data["sizes"]], [10, 20])
        self.assertEqual(len(data["sizes"][0]["times"]), 2)
        q = run_dev("scale", "--sizes", "10,20", "--runs", "1", "--warmup", "0", "--no-baseline",
                    "--", "true", "{n}")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn("Exponent:", q.stdout)
        self.assertIn("at least 0.5 s", q.stderr)

    def test_scale_usage_errors(self):
        p = run_dev("scale", "--sizes", "10,20", "--", "true")
        self.assertEqual(p.returncode, 2)
        self.assertIn("{n}", p.stderr)
        p = run_dev("scale", "--sizes", "0,10", "--", "true", "{n}")
        self.assertEqual(p.returncode, 2)
        p = run_dev("scale", "--sizes", "10,20")
        self.assertEqual(p.returncode, 2)
        self.assertIn("after --", p.stderr)


class BoundTests(unittest.TestCase):
    def test_amdahl(self):
        p = run_dev("bound", "amdahl", "--p", "0.12", "--s", "100")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("1.13x", p.stdout)
        self.assertIn("1.14x", p.stdout)
        q = run_dev("bound", "amdahl", "--p", "0.12", "--s", "100", "--json")
        data = json.loads(q.stdout)
        self.assertAlmostEqual(data["overall"], 1.0 / (0.88 + 0.0012), places=9)
        self.assertAlmostEqual(data["cap"], 1.0 / 0.88, places=9)
        self.assertEqual(run_dev("bound", "amdahl", "--p", "1.5", "--s", "2").returncode, 2)
        self.assertEqual(run_dev("bound", "nosuch").returncode, 2)
        self.assertEqual(run_dev("bound").returncode, 2)
        self.assertEqual(run_dev("bound", "--help").returncode, 0)

    def test_makespan_graham_tight_instance(self):
        # Graham's tight instance for m = 4: LPT gives 15 against the optimum 12 = 4/3 - 1/12.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.json")
            with open(path, "w") as f:
                json.dump({"j1": 7, "j2": 7, "j3": 6, "j4": 6, "j5": 5, "j6": 5, "j7": 4,
                           "j8": 4, "j9": 4}, f)
            p = run_dev("bound", "makespan", "--durations", path, "--workers", "4", "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
            self.assertEqual(data["lb"], 12.0)
            self.assertEqual(data["lb_binding"], "total/workers")
            self.assertEqual(data["lpt_makespan"], 15.0)
            self.assertAlmostEqual(data["lpt_over_lb"], 1.25)
            self.assertAlmostEqual(data["graham_bound"], 1.25)
            self.assertEqual(data["longest_binds_from_m"], 7)
            jobs = sorted(j for w in data["plan"] for j in w["jobs"])
            self.assertEqual(jobs, ["j%d" % i for i in range(1, 10)])
            q = run_dev("bound", "makespan", "--durations", path, "--workers", "4",
                        "--current", "alphabetical")
            self.assertIn("LB 12.00 s (total/workers binds", q.stdout)
            self.assertIn("LPT plan: makespan 15.00 s = 1.25x LB", q.stdout)
            self.assertIn("current plan (alphabetical): makespan", q.stdout)
            r = run_dev("bound", "makespan", "--durations", "7,7,6,6,5,5,4,4,4", "--workers", "4",
                        "--pinned", "job1", "job2", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            rd = json.loads(r.stdout)
            self.assertEqual(rd["lb"], 14.0)
            self.assertEqual(rd["lb_binding"], "pinned jobs on one worker")

    def test_makespan_replay_alphabetical_is_contiguous_slices(self):
        # 7 jobs on 3 workers: sorted names cut into slices of 3, 2, 2 (cards-scheduling.md
        # "alphabetical contiguous slices"); roundrobin deals in file order.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.json")
            with open(path, "w") as f:
                f.write('{"g": 1, "a": 2, "b": 3, "c": 4, "d": 5, "e": 6, "f": 7}')
            p = run_dev("bound", "makespan", "--durations", path, "--workers", "3",
                        "--current", "alphabetical", "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            cur = json.loads(p.stdout)["current"]
            self.assertEqual([w["jobs"] for w in cur["assignment"]],
                             [["a", "b", "c"], ["d", "e"], ["f", "g"]])
            self.assertEqual(cur["makespan"], 11.0)
            q = run_dev("bound", "makespan", "--durations", path, "--workers", "3",
                        "--current", "roundrobin", "--json")
            cur = json.loads(q.stdout)["current"]
            self.assertEqual([w["jobs"] for w in cur["assignment"]],
                             [["g", "c", "f"], ["a", "d"], ["b", "e"]])

    def test_makespan_validation_replaces_bad_durations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.json")
            with open(path, "w") as f:
                json.dump({"jobs": {"a": 10, "b": 0, "c": None, "d": 20, "e": "x"}, "runners": 2}, f)
            p = run_dev("bound", "makespan", "--durations", path, "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
            self.assertEqual(len(data["replaced"]), 3)
            self.assertEqual(data["workers"], 2)
            self.assertEqual(data["total"], 10 + 20 + 3 * 15)
            self.assertEqual(run_dev("bound", "makespan", "--durations", path, "--pinned",
                                     "zz").returncode, 2)
            self.assertEqual(run_dev("bound", "makespan", "--durations", "1,2").returncode, 2)

    def test_ops_stop_and_flip_size(self):
        p = run_dev("bound", "ops", "--n", "2000", "--ops", "40*n", "--path", "batch")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("stop", p.stdout)
        self.assertIn("250000", p.stdout)
        q = run_dev("bound", "ops", "--n", "2000", "--ops", "40*n", "--path", "batch", "--json")
        data = json.loads(q.stdout)
        self.assertEqual(data["decision"], "stop")
        self.assertEqual(data["flip_size"], 250000)
        self.assertAlmostEqual(data["time_at_10n"], 0.08)
        r = run_dev("bound", "ops", "--n", "2000", "--ops", "n*n*log2(n)", "--json")
        rd = json.loads(r.stdout)
        self.assertEqual(rd["decision"], "proceed")
        s = run_dev("bound", "ops", "--n", "1000", "--time-at-n", "0.002", "--exponent", "2",
                    "--json")
        sd = json.loads(s.stdout)
        self.assertEqual(sd["decision"], "proceed")      # 0.2 s at 10n on the request path
        self.assertAlmostEqual(sd["flip_size"], 7072, delta=1)
        for bad in (["--n", "10", "--ops", "__import__('os')"], ["--n", "10", "--ops", "n +"],
                    ["--n", "10"], ["--n", "10", "--ops", "n", "--time-at-n", "1", "--exponent", "1"]):
            t = run_dev("bound", "ops", *bad)
            self.assertEqual(t.returncode, 2, bad)
            self.assertIn("dev.py bound:", t.stderr)
            self.assertNotIn("Traceback", t.stderr)


class BoundEdgeTests(unittest.TestCase):
    def test_exponential_models_and_huge_exponents_never_hang_or_crash(self):
        for args in (["--n", "20", "--ops", "2**n"], ["--n", "5", "--ops", "n**n"],
                     ["--n", "5", "--ops", "9**9**9"],
                     ["--n", "1000", "--time-at-n", "0.01", "--exponent", "50"]):
            t0 = time.time()
            p = run_dev("bound", "ops", *args, timeout=20)
            self.assertEqual(p.returncode, 0, (args, p.stderr))
            self.assertLess(time.time() - t0, 5, args)
            self.assertNotIn("Traceback", p.stderr)
            self.assertIn("proceed", p.stdout)
        p = run_dev("bound", "ops", "--n", "20", "--ops", "2**n", "--json")
        self.assertEqual(json.loads(p.stdout)["flip_size"], 20)

    def test_caret_rates_and_the_estimate_label(self):
        p = run_dev("bound", "ops", "--n", "5000", "--ops", "n^2")
        self.assertEqual(p.returncode, 2)
        self.assertIn("use ** for powers", p.stderr)
        self.assertNotIn("BinOp", p.stderr)
        p = run_dev("bound", "ops", "--n", "5000", "--ops", "foo*n")
        self.assertIn("the only name is n", p.stderr)
        j = lambda *a: json.loads(run_dev("bound", "ops", "--json", *a).stdout)
        slow = j("--n", "20000", "--ops", "n*n/2", "--path", "batch")
        fast = j("--n", "20000", "--ops", "n*n/2", "--path", "batch", "--runtime", "c-builtin")
        mine = j("--n", "20000", "--ops", "n*n/2", "--path", "batch", "--rate", "1e8")
        self.assertAlmostEqual(slow["time_at_n"], 20.0)
        self.assertAlmostEqual(fast["time_at_n"], 1.0)
        self.assertAlmostEqual(mine["time_at_n"], 2.0)
        self.assertEqual((mine["runtime"], slow["estimate"]), ("--rate", True))
        self.assertGreater(fast["flip_size"], slow["flip_size"])
        text = run_dev("bound", "ops", "--n", "1200000", "--ops", "0.0085*n**2",
                       "--path", "batch").stdout
        self.assertIn("122400.00 s (34.0 h)", text)
        self.assertIn("one timed run", text)
        meas = j("--n", "1000", "--time-at-n", "0.23", "--exponent", "2")
        self.assertFalse(meas["estimate"])

    def test_duplicate_job_names_keep_their_durations(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "jobs.json")
            with open(path, "w") as f:
                json.dump([["a", 5], ["a", 7], ["b", 3]], f)
            out = json.loads(run_dev("bound", "makespan", "--durations", path, "--workers",
                                     "2", "--json").stdout)
            self.assertEqual(out["longest"], {"name": "a#2", "seconds": 7.0})
            self.assertAlmostEqual(out["lb"], 7.5)
            with open(path, "w") as f:
                json.dump([["Foo#a", 1], ["Foo#a", 2], ["Foo#b", 3], ["x", 4]], f)
            out = json.loads(run_dev("bound", "makespan", "--durations", path, "--workers",
                                     "2", "--json").stdout)
            names = sorted(n for w in out["plan"] for n in w["jobs"])
            self.assertEqual(names, ["Foo#a", "Foo#a#2", "Foo#b", "x"])
            text = run_dev("bound", "makespan", "--durations", "1,2,3,4,5,6,7", "--workers",
                           "2", "--brief").stdout
            self.assertIn("... (", text)


class ChangepointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        rng = random.Random(7)
        self.values = ([1.0 + rng.gauss(0, 0.05) for _ in range(30)]
                       + [1.5 + rng.gauss(0, 0.05) for _ in range(30)])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_csv_one_number_per_line(self):
        path = self.write("s.csv", "\n".join("%r" % v for v in self.values) + "\n")
        p = run_dev("changepoint", path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("change point at index 30", p.stdout)
        self.assertIn("Triage before acting", p.stdout)

    def test_csv_label_value_with_header(self):
        lines = ["commit,seconds"] + ["c%02d,%r" % (i, v) for i, v in enumerate(self.values)]
        path = self.write("s.csv", "\n".join(lines) + "\n")
        p = run_dev("changepoint", path, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["schema"], "devkit-changepoint/1")
        self.assertEqual(len(data["points"]), 1)
        self.assertEqual(data["points"][0]["index"], 30)
        self.assertEqual(data["points"][0]["label"], "c30")
        self.assertAlmostEqual(data["points"][0]["percent_change"], 50.0, delta=8.0)

    def test_json_inputs(self):
        path = self.write("s.json", json.dumps(self.values))
        p = run_dev("changepoint", path, "--json")
        self.assertEqual(json.loads(p.stdout)["points"][0]["index"], 30)
        path = self.write("t.json", json.dumps([{"label": "r%d" % i, "value": v}
                                               for i, v in enumerate(self.values)]))
        p = run_dev("changepoint", path, "--json")
        pt = json.loads(p.stdout)["points"][0]
        self.assertEqual((pt["index"], pt["label"]), (30, "r30"))

    def test_flat_series_and_errors(self):
        path = self.write("f.csv", "\n".join(["1.0", "1.1"] * 10))
        p = run_dev("changepoint", path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no change point", p.stdout)
        p = run_dev("changepoint", self.write("short.csv", "1\n2\n3\n"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("dev.py changepoint:", p.stderr)
        p = run_dev("changepoint", self.write("bad.csv", "1\nabc\n3\n"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("line 2", p.stderr)
        p = run_dev("changepoint", os.path.join(self.tmp, "missing.csv"))
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)
        for text in ('[1, "abc", 3]', '[{"label": "x"}]', '{"other": 1}', '[1, null]'):
            p = run_dev("changepoint", self.write("bad.json", text))
            self.assertEqual(p.returncode, 2, text)
            self.assertTrue(p.stderr.startswith("dev.py changepoint:"), p.stderr)
            self.assertNotIn("Traceback", p.stderr)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class ChangedFilesTests(unittest.TestCase):
    """devkit.common.changed_files against a real temporary repository (no network)."""

    def git(self, *args):
        # Isolated from the developer's git config: a global core.hooksPath (husky,
        # pre-commit, gitleaks templates) or signing setup must not break the fixture.
        env = dict((k, v) for k, v in os.environ.items() if not k.startswith("GIT_"))
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "HOME": self.tmp})
        cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
               "-c", "core.hooksPath=" + os.devnull] + list(args)
        return subprocess.run(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, check=True, env=env).stdout.strip()

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w") as f:
            f.write(text)

    def setUp(self):
        sys.path.insert(0, SCRIPTS)
        from devkit.common import changed_files
        self.changed_files = changed_files
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(self.tmp)
        self.git("init", "-q")
        self.write("a.txt", "a\n")
        self.write("b.txt", "b\n")
        self.git("add", "a.txt", "b.txt")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.tmp)
        sys.path.remove(SCRIPTS)

    def test_working_tree_statuses_including_renames(self):
        self.write("a.txt", "changed\n")        # M, unstaged
        self.write("c.txt", "new\n")            # ?, untracked
        self.git("mv", "b.txt", "d.txt")        # staged rename: R d.txt (from b.txt) and D b.txt
        got = self.changed_files(self.root)
        self.assertEqual(sorted(got), [("?", "c.txt", None), ("D", "b.txt", None),
                                       ("M", "a.txt", None), ("R", "d.txt", "b.txt")])

    def test_base_ref_adds_committed_changes(self):
        self.write("a.txt", "changed\n")
        self.write("e.txt", "e\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "work")
        self.assertEqual(self.changed_files(self.root), [])
        got = self.changed_files(self.root, base=self.base)
        self.assertEqual(sorted(got), [("A", "e.txt", None), ("M", "a.txt", None)])
        self.write("f.txt", "f\n")
        got = self.changed_files(self.root, base=self.base)
        self.assertIn(("?", "f.txt", None), got)
        from devkit.common import UsageError
        with self.assertRaises(UsageError):
            self.changed_files(self.root, base="no-such-ref-zzz")

    def test_outside_a_repository_is_empty(self):
        with tempfile.TemporaryDirectory() as plain:
            plain = os.path.realpath(plain)
            old = os.environ.get("GIT_CEILING_DIRECTORIES")
            os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(plain)  # even when TMPDIR
            try:                                                           # sits in a repo
                self.assertEqual(self.changed_files(plain), [])
            finally:
                if old is None:
                    del os.environ["GIT_CEILING_DIRECTORIES"]
                else:
                    os.environ["GIT_CEILING_DIRECTORIES"] = old


class CostAndSlopeTests(unittest.TestCase):
    """--max-total stops a run before it gets expensive; pairwise slopes flag growth that one
    fitted exponent cannot describe."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, SCRIPTS)
        try:
            from devkit import measure
            from devkit.common import CommandFailed
        finally:
            sys.path.remove(SCRIPTS)
        cls.m, cls.CommandFailed = measure, CommandFailed

    def stub_scale(self, fn, sizes, **kw):
        import contextlib
        import io
        real = self.m.time_many
        self.m.time_many = lambda cmd, runs, warmup, **k: ([fn(float(cmd[-1]))] * runs,
                                                           [None] * runs)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                return self.m.scale_core(["prog", "{n}"], sizes, 3, 0, None, True,
                                         progress=True, **kw), err.getvalue()
        finally:
            self.m.time_many = real

    def test_pair_slopes_and_rising_rule(self):
        self.assertEqual([round(x, 6) for x in self.m.pair_slopes([1, 2, 4, 8], [1, 4, 16, 64])],
                         [2.0, 2.0, 2.0])
        self.assertFalse(self.m.slopes_rising([2.0, 2.0, 2.0]))
        self.assertFalse(self.m.slopes_rising([1.37, 1.86, 1.91]))     # startup noise settling
        self.assertFalse(self.m.slopes_rising([1.2, 1.5, 1.8]))        # a*n + b*n^2
        self.assertTrue(self.m.slopes_rising([1.0, 2.0, 4.0]))         # 2^(n/c)
        self.assertEqual(self.m.pair_slopes([1, 2], [0.5, 1.0], baseline=0.6), [None])

    def test_exponential_growth_is_flagged_not_fitted_as_a_power(self):
        res, err = self.stub_scale(lambda n: 2 ** (n / 10.0) * 1e-3, [10, 20, 40, 80])
        self.assertTrue(res["slopes_rising"])
        self.assertTrue(any("growth looks exponential" in w for w in res["warnings"]))
        res, _ = self.stub_scale(lambda n: 1e-6 * n ** 2, [1000, 2000, 4000, 8000])
        self.assertFalse(res["slopes_rising"])
        self.assertEqual(res["warnings"], [])

    def test_mixed_linear_quadratic_in_the_middle_band_says_what_to_do(self):
        res, _ = self.stub_scale(lambda n: 1e-3 * n + 1e-6 * n * n, [250, 500, 1000, 2000])
        self.assertTrue(1.3 <= res["exponent_raw"] < 1.7, res["exponent_raw"])
        self.assertTrue(any("a mix of terms" in w and "production n" in w
                            for w in res["warnings"]), res["warnings"])

    def test_scale_stops_before_a_size_past_the_cap_using_the_fitted_exponent(self):
        cubic = lambda n: (n / 1000.0) ** 3            # 1 s, 8 s, then 64 s per run
        with self.assertRaises(self.CommandFailed) as cm:
            self.stub_scale(cubic, [1000, 2000, 4000, 8000], max_total=100)
        msg = cm.exception.message
        self.assertIn("n=4000 would take about", msg)
        self.assertIn("exponent 3.00", msg)
        self.assertIn("largest size that fits is about n=", msg)
        self.assertIn("--runs 3", msg)
        res, err = self.stub_scale(cubic, [1000, 2000, 4000, 8000], max_total=0)
        self.assertEqual(len(res["sizes"]), 4)
        self.assertIn("at the exponent 3.00 fitted so far", err)
        self.assertIn("will run `prog {n}` 12 times", err)

    def test_bench_and_aa_stop_when_the_first_runs_project_past_the_cap(self):
        sleep = "%s -c \"import time; time.sleep(0.15)\"" % PY
        p = run_dev("bench", "--arm", "a=" + sleep, "--arm", "b=" + sleep, "--runs", "10",
                    "--max-total", "1")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("--max-total", p.stderr)
        self.assertIn("bench will run each of 2 arm(s) 11 times", p.stderr)
        self.assertIn("side effects", p.stderr)
        p = run_dev("aa", "--runs", "10", "--max-total", "1", "--", PY, "-c",
                    "import time; time.sleep(0.15)")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("aa would take about", p.stderr)
        self.assertEqual(run_dev("bench", "--arm", "a=true", "--max-total", "-1").returncode, 2)


class ExternalSamplesTests(unittest.TestCase):
    """compare --samples: external timings get the same CI and verdict rules."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, values, header=""):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(header + "\n".join(str(v) for v in values) + "\n")
        return path

    def test_samples_give_a_verdict_and_say_where_they_came_from(self):
        b = self.write("before.txt", [x * 100 for x in TEN_BEFORE])
        a = self.write("after.txt", [x * 100 for x in TEN_AFTER], header="# ms per op\n")
        p = run_dev("compare", "--samples", "before=" + b, "--samples", "after=" + a,
                    "--unit", "ms", "--min-effect", "0.05", "--json", cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["verdict"], "FASTER")
        self.assertEqual(data["arms"], ["before", "after"])
        self.assertAlmostEqual(data["before"]["median"], 0.200, places=6)    # ms -> s
        self.assertIn("external samples", p.stderr)
        self.assertNotIn("process startup", p.stderr)          # per-op times are not launches

    def test_samples_usage_errors(self):
        b = self.write("b.txt", TEN_BEFORE)
        for args in (["--samples", "x=" + b],
                     ["--samples", "x=" + b, "--samples", "x=" + b],
                     ["--samples", "x=" + b, "--samples", "y=" + b, "--metric", "cpu"],
                     ["--samples", "nofile"]):
            p = run_dev("compare", *args, cwd=self.tmp)
            self.assertEqual(p.returncode, 2, (args, p.stderr))
            self.assertNotIn("Traceback", p.stderr)
        bad = self.write("zero.txt", [0.0] * 10)
        p = run_dev("compare", "--samples", "x=" + bad, "--samples", "y=" + b, cwd=self.tmp)
        self.assertEqual(p.returncode, 2)
        self.assertIn("positive", p.stderr)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class BisectTests(unittest.TestCase):
    """dev.py bisect on a temporary repository: finds the commit that slowed the command and
    leaves the user's tree, branch and worktree list as they were."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.root)
        self.env = dict((k, v) for k, v in os.environ.items() if not k.startswith("GIT_"))
        self.env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                         "HOME": self.tmp, "DEVKIT_REPO": self.root})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def git(self, *args):
        cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
               "-c", "core.hooksPath=" + os.devnull] + list(args)
        return subprocess.run(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, check=True, env=self.env).stdout.strip()

    def commit_series(self, slow_from, total=9, broken=(), fast_delay=0.01):
        self.git("init", "-q")
        for i in range(1, total + 1):
            delay = 0.3 if i >= slow_from else fast_delay  # well above interpreter start-up noise on slow CI runners
            body = "raise SystemExit(3)\n" if i in broken else ""
            with open(os.path.join(self.root, "step.py"), "w") as f:
                f.write("import time\n%stime.sleep(%s)\nprint('v%d')\n" % (body, delay, i))
            self.git("add", "step.py")
            self.git("commit", "-qm", "commit %d" % i)
        return self.git("rev-list", "--reverse", "HEAD").splitlines()

    def bisect(self, *extra):
        commits = self.git("rev-list", "--reverse", "HEAD").splitlines()
        return run_dev("bisect", "--good", commits[0], "--bad", "HEAD", "--runs", "3",
                       "--warmup", "0", "--json", *extra + ("--", PY, "step.py"),
                       cwd=self.root, env=self.env)

    def test_finds_the_first_slower_commit_without_touching_the_tree(self):
        commits = self.commit_series(slow_from=6)
        head = self.git("rev-parse", "HEAD")
        p = self.bisect()
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["schema"], "devkit-bisect/1")
        self.assertEqual(data["first_bad"], commits[5])
        self.assertIn("will run", p.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_skips_a_commit_that_fails_and_still_finds_the_step(self):
        commits = self.commit_series(slow_from=7, broken=(5,))
        p = self.bisect()
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["first_bad"], commits[6])
        self.assertIn(commits[4], data["skipped"])

    def test_an_unbuildable_commit_next_to_the_step_leaves_a_range(self):
        commits = self.commit_series(slow_from=6, broken=(5,))
        p = self.bisect()
        self.assertEqual(p.returncode, 1, p.stderr)
        data = json.loads(p.stdout)
        self.assertFalse(data["found"])
        self.assertEqual(data["range"], [commits[3], commits[5]])

    def test_no_slowdown_refuses_and_bad_refs_are_usage_errors(self):
        # identical commits that each sleep 0.3 s: start-up jitter on a loaded runner cannot double the median
        self.commit_series(slow_from=99, total=4, fast_delay=0.3)
        p = self.bisect("--min-effect", "0.5")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("nothing to bisect", p.stderr)
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)
        p = run_dev("bisect", "--good", "nosuchref", "--bad", "HEAD", "--", PY, "step.py",
                    cwd=self.root, env=self.env)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)

    def test_search_order_and_skips_are_pure_functions(self):
        sys.path.insert(0, SCRIPTS)
        try:
            from devkit.perfbisect import order_to_try, search
        finally:
            sys.path.remove(SCRIPTS)
        self.assertEqual(order_to_try(-1, 5, 2), [2, 1, 3, 0, 4])
        for first in range(10):
            lo, hi, skipped = search(list(range(10)), lambda i: i >= first)
            self.assertEqual((hi, hi - lo), (first, 1))
        lo, hi, skipped = search(list(range(6)), lambda i: None if i < 5 else True)
        self.assertEqual((lo, hi), (-1, 5))
        self.assertEqual(skipped, [0, 1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
