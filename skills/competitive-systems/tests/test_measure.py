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


def bench_json(before, after, path, meta=None):
    """Write a devkit-bench/1 file with two arms whose wall times are given."""
    def arm(vals):
        return {"command": ["true"], "shell": False, "wall": vals, "cpu": [v * 0.9 for v in vals],
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

    def test_check_output_warns_when_outputs_differ(self):
        p = run_dev("bench", "--arm", "a=%s -c print(1)" % PY, "--arm", "b=%s -c print(2)" % PY,
                    "--runs", "2", "--warmup", "0", "--check-output", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertTrue(any("outputs differ" in w for w in data["warnings"]), data["warnings"])
        self.assertIn("outputs differ", p.stderr)
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

    def test_aa_needs_a_command(self):
        p = run_dev("aa", "--runs", "2")
        self.assertEqual(p.returncode, 2)
        self.assertIn("dev.py aa:", p.stderr)


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
        self.assertEqual(run_dev("bound").returncode, 0)

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
        cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"] + list(args)
        return subprocess.run(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, check=True).stdout.strip()

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
            self.assertEqual(self.changed_files(os.path.realpath(plain)), [])


if __name__ == "__main__":
    unittest.main()
