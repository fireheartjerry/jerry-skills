#!/usr/bin/env python3
"""Deterministic tests for scripts/bench.py. Standard library only; no network."""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
BENCH = os.path.join(SCRIPTS, "bench.py")
sys.path.insert(0, SCRIPTS)

import bench  # noqa: E402


class StatisticsTests(unittest.TestCase):
    def test_median_odd_and_even(self):
        self.assertEqual(bench.median([3, 1, 2]), 2.0)
        self.assertEqual(bench.median([4, 1, 3, 2]), 2.5)
        self.assertEqual(bench.median([7]), 7.0)
        with self.assertRaises(ValueError):
            bench.median([])

    def test_quartiles_median_of_halves(self):
        self.assertEqual(bench.quartiles(list(range(1, 9))), (2.5, 6.5))
        self.assertEqual(bench.quartiles(list(range(1, 10))), (2.5, 7.5))
        self.assertEqual(bench.quartiles([5]), (5.0, 5.0))
        self.assertEqual(bench.quartiles([1, 2]), (1.0, 2.0))
        self.assertEqual(bench.quartiles([1, 2, 3]), (1.0, 3.0))

    def test_summarize(self):
        s = bench.summarize([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        self.assertEqual(s["runs"], 7)
        self.assertEqual(s["median"], 4.0)
        self.assertEqual(s["q1"], 2.0)
        self.assertEqual(s["q3"], 6.0)
        self.assertEqual(s["iqr"], 4.0)
        self.assertEqual(s["min"], 1.0)
        self.assertEqual(s["max"], 7.0)

    def test_with_cpu_flags_waiting(self):
        cpu_bound = bench.with_cpu(bench.summarize([1.0, 1.0, 1.0]), [0.98, 0.99, 0.97])
        self.assertAlmostEqual(cpu_bound["cpu_median"], 0.98)
        self.assertEqual(cpu_bound["wall_over_cpu"], 1.02)
        self.assertFalse(cpu_bound["waiting"])
        waiting = bench.with_cpu(bench.summarize([1.0, 1.0, 1.0]), [0.1, 0.1, 0.1])
        self.assertTrue(waiting["waiting"])
        self.assertIn("appears to be waiting", bench.gate1_line(waiting))
        self.assertIn("CPU-bound", bench.gate1_line(cpu_bound))
        # under the floor the ratio is startup noise: never flagged as waiting
        tiny = bench.with_cpu(bench.summarize([0.01, 0.01, 0.01]), [0.001, 0.001, 0.001])
        self.assertFalse(tiny["waiting"])
        self.assertIn("unreliable", bench.gate1_line(tiny))
        none = bench.with_cpu(bench.summarize([1.0]), [None])
        self.assertIsNone(none["cpu_median"])
        self.assertIn("unavailable", bench.gate1_line(none))


class VerdictTests(unittest.TestCase):
    before = [10, 10.1, 10.2, 9.9, 10.05, 10.15, 9.95]
    after = [5, 5.1, 4.9, 5.05, 5.2, 4.95, 5]

    def test_faster(self):
        name, speedup, reason = bench.verdict(bench.summarize(self.before),
                                              bench.summarize(self.after))
        self.assertEqual(name, "faster")
        self.assertAlmostEqual(speedup, 2.0, places=1)
        self.assertEqual(reason, "")

    def test_slower_is_the_mirror(self):
        name, speedup, _ = bench.verdict(bench.summarize(self.after),
                                         bench.summarize(self.before))
        self.assertEqual(name, "slower")
        self.assertAlmostEqual(speedup, 0.5, places=1)

    def test_inconclusive_when_iqrs_overlap(self):
        tight = [3.2, 3.21, 3.19, 3.2, 3.22, 3.18, 3.2]
        noisy = [2.95, 3.3, 3.1, 3.0, 3.25, 3.15, 3.05]
        name, speedup, reason = bench.verdict(bench.summarize(tight), bench.summarize(noisy))
        self.assertEqual(name, "inconclusive")
        self.assertIsNone(speedup)
        self.assertIn("IQR", reason)

    def test_inconclusive_when_gap_is_small(self):
        # medians 1.00 vs 0.90, IQRs 0.06 each and non-overlapping, gap 0.10 < 2 * 0.06
        before = [0.97, 0.98, 1.0, 1.0, 1.0, 1.03, 1.04]
        after = [0.87, 0.88, 0.9, 0.9, 0.9, 0.93, 0.94]
        name, speedup, reason = bench.verdict(bench.summarize(before), bench.summarize(after))
        self.assertEqual(name, "inconclusive")
        self.assertIsNone(speedup)
        self.assertIn("twice the larger IQR", reason)

    def test_inconclusive_when_medians_equal(self):
        same = [1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0]
        name, speedup, reason = bench.verdict(bench.summarize(same), bench.summarize(same))
        self.assertEqual(name, "inconclusive")
        self.assertIsNone(speedup)
        self.assertEqual(reason, "medians equal")

    def test_report_lines(self):
        b, a = bench.summarize(self.before), bench.summarize(self.after)
        lines = bench.report_lines(b, a, "faster", 2.01)
        self.assertEqual(lines[0], "Before: median 10.05 s (IQR 200.0 ms, runs=7); "
                                   "After: median 5.00 s (IQR 150.0 ms, runs=7).")
        self.assertEqual(lines[1], "Verdict: 2.01x faster")
        self.assertEqual(bench.report_lines(b, a, "inconclusive", None)[1],
                         "Verdict: no measurable change")

    def test_compare_warnings_by_tier(self):
        b, a = bench.summarize([1.0] * 7), bench.summarize([0.9] * 7)
        w = bench.compare_warnings(b, a)
        self.assertTrue(any("30 runs" in x for x in w), w)
        self.assertFalse(any("at least 7" in x for x in w), w)
        b, a = bench.summarize([1.0] * 7), bench.summarize([0.6] * 7)
        self.assertTrue(any("15 runs" in x for x in bench.compare_warnings(b, a)))
        b, a = bench.summarize([1.0] * 15), bench.summarize([0.6] * 15)
        self.assertEqual(bench.compare_warnings(b, a), [])
        b, a = bench.summarize([1.0] * 3), bench.summarize([0.4] * 3)
        self.assertTrue(any("at least 7" in x for x in bench.compare_warnings(b, a)))
        b, a = bench.summarize([0.010] * 7), bench.summarize([0.002] * 7)
        self.assertTrue(any("startup" in x for x in bench.compare_warnings(b, a)))


class ExponentTests(unittest.TestCase):
    def test_fit_exponent(self):
        self.assertAlmostEqual(bench.fit_exponent([1000, 2000, 4000], [1, 4, 16]), 2.0, delta=0.01)
        self.assertAlmostEqual(bench.fit_exponent([1000, 2000], [1, 2]), 1.0, delta=0.01)
        self.assertAlmostEqual(bench.fit_exponent([10, 20, 40], [1, 8, 64]), 3.0, delta=0.01)
        with self.assertRaises(ValueError):
            bench.fit_exponent([1000, 1000], [1, 2])
        with self.assertRaises(ValueError):
            bench.fit_exponent([0, 10], [1, 2])

    def test_scale_exponent_subtracts_startup_baseline(self):
        # quadratic work plus a 25 ms fixed startup: raw fit reads well under 2
        sizes = [300, 600, 1200]
        startup, k = 0.025, 2e-7
        medians = [startup + k * n * n for n in sizes]
        raw = bench.fit_exponent(sizes, medians)
        self.assertLess(raw, 1.7)
        fitted, warnings = bench.scale_exponent(sizes, medians, startup)
        self.assertAlmostEqual(fitted, 2.0, delta=0.02)
        self.assertTrue(any("5x the startup baseline" in w for w in warnings), warnings)

    def test_scale_exponent_without_baseline_is_the_raw_fit(self):
        fitted, warnings = bench.scale_exponent([1000, 2000, 4000], [1.0, 4.0, 16.0], 0.0)
        self.assertAlmostEqual(fitted, 2.0, delta=0.01)
        self.assertEqual(warnings, [])
        fitted, warnings = bench.scale_exponent([10, 20], [0.05, 0.06], 0.0)
        self.assertTrue(any("under 500.0 ms" in w and "at least 0.5 s" in w for w in warnings),
                        warnings)

    def test_scale_exponent_within_baseline_is_not_fitted(self):
        fitted, warnings = bench.scale_exponent([10, 20], [0.020, 0.021], 0.021)
        self.assertIsNone(fitted)
        self.assertTrue(any("not fitted" in w for w in warnings), warnings)

    def test_classify_exponent_boundaries(self):
        self.assertEqual(bench.classify_exponent(1.29), "about linear")
        self.assertEqual(bench.classify_exponent(1.3), "between linear and quadratic")
        self.assertEqual(bench.classify_exponent(1.7), "about quadratic")
        self.assertEqual(bench.classify_exponent(2.49), "about quadratic")
        self.assertEqual(bench.classify_exponent(2.5), "about cubic")
        self.assertEqual(bench.classify_exponent(3.5), "worse than cubic")

    def test_default_baseline(self):
        self.assertEqual(bench.default_baseline(["python3", "x.py"]), ["python3", "-c", "pass"])
        self.assertEqual(bench.default_baseline(["/usr/bin/python3.11", "x.py"]),
                         ["/usr/bin/python3.11", "-c", "pass"])
        self.assertEqual(bench.default_baseline(["node", "x.js"]), ["node", "-e", "0"])
        self.assertIsNone(bench.default_baseline(["./a.out", "{n}"]))

    def test_fmt_time(self):
        self.assertEqual(bench.fmt_time(8.1), "8.10 s")
        self.assertEqual(bench.fmt_time(0.0903), "90.3 ms")
        self.assertEqual(bench.fmt_time(0.0000500), "50 us")


def run_bench(*args):
    return subprocess.run([sys.executable, BENCH] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)


class SubprocessSmokeTests(unittest.TestCase):
    def test_run_json(self):
        p = run_bench("run", "--runs", "3", "--warmup", "0", "--json", "--",
                      sys.executable, "-c", "pass")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        for key in ("median", "iqr", "runs", "q1", "q3", "min", "max", "cpu_median",
                    "wall_over_cpu", "waiting", "gate1", "cpu_times"):
            self.assertIn(key, data)
        self.assertEqual(data["runs"], 3)
        self.assertEqual(len(data["cpu_times"]), 3)
        self.assertTrue(data["gate1"].startswith("CPU "))

    def test_run_human_output(self):
        p = run_bench("run", "--runs", "2", "--warmup", "0", "--", sys.executable, "-c", "pass")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("runs    2", p.stdout)
        self.assertIn("median", p.stdout)
        self.assertIn("Gate 1: CPU", p.stdout)

    def test_scale_json_without_baseline_reports_raw_exponent(self):
        p = run_bench("scale", "--sizes", "10,20", "--runs", "2", "--warmup", "0", "--json",
                      "--no-baseline", "--", sys.executable, "-c", "pass # {n}")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertIsInstance(data["exponent"], float)
        self.assertIsNone(data["baseline"])
        self.assertEqual([r["n"] for r in data["sizes"]], [10, 20])
        self.assertTrue(any("raise the sizes" in w for w in data["warnings"]), data)

    def test_scale_json_times_a_python_baseline(self):
        p = run_bench("scale", "--sizes", "10,20", "--runs", "2", "--warmup", "0", "--json",
                      "--", sys.executable, "-c", "pass # {n}")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["baseline"]["command"], [sys.executable, "-c", "pass"])
        self.assertIn("median", data["baseline"])
        self.assertIn("exponent", data)  # None or a float: the medians sit inside the baseline
        self.assertTrue(data["warnings"], data)
        self.assertIn("gate1", data)

    def test_scale_warns_when_no_baseline_is_known(self):
        # `true` ignores its arguments and finishes in a few milliseconds everywhere.
        p = run_bench("scale", "--sizes", "10,20", "--runs", "2", "--warmup", "0", "--json",
                      "--", "true", "{n}")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertTrue(any("no startup baseline" in w for w in data["warnings"]), data)
        self.assertTrue(any("raise the sizes" in w for w in data["warnings"]), data)

    def test_scale_explicit_baseline(self):
        p = run_bench("scale", "--sizes", "10,20", "--runs", "2", "--warmup", "0", "--json",
                      "--baseline", "true", "--", "true", "{n}")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["baseline"]["command"], ["true"])
        # --baseline together with --no-baseline is a usage error, not a silent precedence
        p = run_bench("scale", "--sizes", "10,20", "--baseline", "true", "--no-baseline",
                      "--", "true", "{n}")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("--no-baseline", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_scale_requires_placeholder(self):
        p = run_bench("scale", "--sizes", "10,20", "--", "true")
        self.assertEqual(p.returncode, 2)
        self.assertIn("{n}", p.stderr)

    def test_scale_rejects_nonpositive_sizes(self):
        p = run_bench("scale", "--sizes", "0,10", "--", "true", "{n}")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("positive", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        p = run_bench("scale", "--sizes", "-5,10", "--", "true", "{n}")
        self.assertEqual(p.returncode, 2, p.stderr)

    def test_compare_equivalent_commands_is_inconclusive(self):
        verdicts = []
        for _ in range(2):  # retry once: a loaded machine can produce one odd sample
            p = run_bench("compare", "--runs", "3", "--warmup", "0", "--json",
                          "--before", "true", "--after", "true")
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
            verdicts.append(data["verdict"])
            if data["verdict"] == "inconclusive":
                break
        self.assertIn("inconclusive", verdicts, verdicts)
        self.assertEqual(data["before"]["runs"], 3)
        self.assertEqual(data["after"]["runs"], 3)
        self.assertTrue(data["interleaved"])
        self.assertEqual(len(data["report_lines"]), 2)
        self.assertIn("runs=3", data["report_lines"][0])
        self.assertTrue(any("at least 7" in w for w in data["warnings"]), data["warnings"])

    def test_compare_human_output_has_cpu_column(self):
        p = run_bench("compare", "--runs", "2", "--warmup", "0",
                      "--before", "true", "--after", "true")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("wall/CPU", p.stdout)
        self.assertIn("warning: only 2 runs", p.stdout)

    def test_failed_command_exits_3_and_shows_stderr(self):
        p = run_bench("run", "--runs", "1", "--warmup", "0", "--", sys.executable, "-c",
                      "import sys; sys.stderr.write('boom\\nbang\\n'); sys.exit(4)")
        self.assertEqual(p.returncode, 3)
        self.assertIn("failed with exit 4", p.stderr)
        self.assertIn("boom", p.stderr)
        self.assertIn("bang", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_missing_executable_exits_3(self):
        p = run_bench("run", "--runs", "1", "--warmup", "0", "--", "bench_no_such_command_xyz")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("cannot run bench_no_such_command_xyz", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_runs_zero_exits_2(self):
        p = run_bench("run", "--runs", "0", "--", "true")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--runs", p.stderr)

    def test_missing_command_exits_2(self):
        p = run_bench("run")
        self.assertEqual(p.returncode, 2)

    def test_no_subcommand_exits_2(self):
        p = run_bench()
        self.assertEqual(p.returncode, 2)

    def test_help_exits_0(self):
        p = run_bench("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("compare", p.stdout)


if __name__ == "__main__":
    unittest.main()
