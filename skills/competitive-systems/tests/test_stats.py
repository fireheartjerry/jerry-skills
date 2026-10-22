"""Deterministic tests for scripts/devkit/stats.py. Standard library only; no network, no
subprocesses, no timing; every random draw is seeded. Runs in a few seconds."""
import itertools
import math
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from devkit import stats  # noqa: E402


def brute_u(a, b):
    return sum((1.0 if x > y else 0.5 if x == y else 0.0) for x in a for y in b)


class LocationSpreadTests(unittest.TestCase):
    def test_median_and_quartiles_reference_cases(self):
        self.assertEqual(stats.median([3, 1, 2]), 2.0)
        self.assertEqual(stats.median([4, 1, 3, 2]), 2.5)
        self.assertEqual(stats.median([7]), 7.0)
        with self.assertRaises(ValueError):
            stats.median([])
        self.assertEqual(stats.quartiles(list(range(1, 9))), (2.5, 6.5))
        self.assertEqual(stats.quartiles(list(range(1, 10))), (2.5, 7.5))
        self.assertEqual(stats.quartiles([5]), (5.0, 5.0))
        self.assertEqual(stats.quartiles([1, 2]), (1.0, 2.0))
        self.assertEqual(stats.quartiles([1, 2, 3]), (1.0, 3.0))

    def test_mad_and_cv(self):
        self.assertEqual(stats.mad([1, 2, 3, 4, 5]), 1.0)
        self.assertEqual(stats.mad([5, 5, 5]), 0.0)
        self.assertEqual(stats.cv([1, 1, 1]), 0.0)
        self.assertEqual(stats.cv([4]), 0.0)
        # mean 3, sample sd sqrt(2): 47.14%
        self.assertAlmostEqual(stats.cv([2, 4]), 100 * math.sqrt(2) / 3, places=6)

    def test_tukey_counts(self):
        # quartiles 2.5 / 7.5, IQR 5: mild fence 15, severe fence 22.5
        self.assertEqual(stats.tukey_counts([1, 2, 3, 4, 5, 6, 7, 8, 16]), (1, 0))
        self.assertEqual(stats.tukey_counts([1, 2, 3, 4, 5, 6, 7, 8, 100]), (0, 1))
        self.assertEqual(stats.tukey_counts([1, 2, 3, 4, 5, 6, 7, 8]), (0, 0))
        self.assertEqual(stats.tukey_counts([-50, 1, 2, 3, 4, 5, 6, 7, 8, 16]), (1, 1))

    def test_percentile(self):
        self.assertEqual(stats.percentile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertEqual(stats.percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(stats.percentile([1, 2, 3, 4], 0.0), 1)
        self.assertEqual(stats.percentile([1, 2, 3, 4], 1.0), 4)


class RankTests(unittest.TestCase):
    def test_midranks_with_ties(self):
        self.assertEqual(stats.midranks([1, 2, 2, 3, 2, 3, 4, 4]),
                         [1.0, 3.0, 3.0, 5.5, 3.0, 5.5, 7.5, 7.5])

    def test_spearman_monotone_and_flat(self):
        self.assertAlmostEqual(stats.spearman([1, 2, 3, 4, 5], [2, 4, 8, 16, 32]), 1.0)
        self.assertAlmostEqual(stats.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]), -1.0)
        self.assertEqual(stats.spearman([1, 2, 3, 4], [3, 3, 3, 3]), 0.0)
        self.assertEqual(stats.spearman([1], [2]), 0.0)
        rng = random.Random(11)
        noise = [rng.random() for _ in range(200)]
        self.assertLess(abs(stats.spearman(list(range(200)), noise)), 0.2)


class MannWhitneyTests(unittest.TestCase):
    A, B = [1, 2, 2, 3], [2, 3, 4, 4]

    def test_u_and_exact_p_with_ties_match_enumeration(self):
        r = stats.mann_whitney(self.A, self.B, "less")
        self.assertEqual(r["u"], 2.5)
        self.assertEqual(r["method"], "exact")
        pooled = self.A + self.B
        u_obs = brute_u(self.A, self.B)
        n_le = n_ge = total = 0
        for idx in itertools.combinations(range(len(pooled)), len(self.A)):
            a = [pooled[i] for i in idx]
            b = [pooled[i] for i in range(len(pooled)) if i not in idx]
            u = brute_u(a, b)
            total += 1
            n_le += u <= u_obs + 1e-9
            n_ge += u >= u_obs - 1e-9
        self.assertAlmostEqual(r["p_less"], n_le / total, places=9)
        self.assertAlmostEqual(r["p_greater"], n_ge / total, places=9)
        self.assertAlmostEqual(r["p_less"], 0.1, places=9)
        self.assertAlmostEqual(r["p_two_sided"], min(1.0, 2 * min(r["p_less"], r["p_greater"])))

    def test_normal_approximation_tie_and_continuity_correction(self):
        r = stats.mann_whitney(self.A, self.B, "less", exact_limit=0)
        self.assertEqual(r["method"], "normal")
        # ties: 2 x3, 3 x2, 4 x2 -> sum(t^3 - t) = 24 + 6 + 6 = 36; n = 8
        var = 4 * 4 / 12.0 * (9 - 36 / (8 * 7.0))
        z = (2.5 + 0.5 - 8.0) / math.sqrt(var)
        self.assertAlmostEqual(r["z"], z, places=9)
        self.assertAlmostEqual(r["p_less"], stats.norm_cdf(z), places=9)

    def test_exact_and_normal_agree_at_8_plus_8(self):
        rng = random.Random(1)
        for _ in range(5):
            a = [rng.gauss(0, 1) for _ in range(8)]
            b = [rng.gauss(0.5, 1) for _ in range(8)]
            exact = stats.mann_whitney(a, b)
            approx = stats.mann_whitney(a, b, exact_limit=0)
            self.assertEqual(exact["method"], "exact")
            self.assertLess(abs(exact["p_two_sided"] - approx["p_two_sided"]), 0.02)
            self.assertLess(abs(exact["p_less"] - approx["p_less"]), 0.02)

    def test_directions_and_errors(self):
        low, high = [1, 2, 3, 4, 5], [6, 7, 8, 9, 10]
        self.assertLess(stats.mann_whitney(low, high, "less")["p"], 0.01)
        self.assertGreater(stats.mann_whitney(low, high, "greater")["p"], 0.99)
        self.assertEqual(stats.mann_whitney(low, high, "less")["p"],
                         1.0 / math.comb(10, 5))
        with self.assertRaises(ValueError):
            stats.mann_whitney([], [1])
        with self.assertRaises(ValueError):
            stats.mann_whitney([1], [2], "sideways")


class EffectTests(unittest.TestCase):
    def test_hodges_lehmann_known_shift(self):
        before = [10, 11, 12, 13, 14]
        after = [b - 3 for b in before]
        self.assertEqual(stats.hodges_lehmann(before, after), 3.0)
        self.assertEqual(stats.hodges_lehmann(after, before), -3.0)
        self.assertEqual(stats.hodges_lehmann([1, 2, 3], [1, 2, 3]), 0.0)

    def test_bootstrap_is_reproducible_for_a_seed(self):
        before = [2.0, 2.1, 1.9, 2.05, 2.2, 1.95, 2.0, 2.1, 2.0, 1.98]
        after = [1.0, 1.1, 0.9, 1.05, 1.2, 0.95, 1.0, 1.1, 1.0, 0.98]
        r1 = stats.bootstrap_ratio_ci(before, after, resamples=500, seed=3)
        r2 = stats.bootstrap_ratio_ci(before, after, resamples=500, seed=3)
        self.assertEqual((r1["lo"], r1["hi"]), (r2["lo"], r2["hi"]))
        self.assertAlmostEqual(r1["ratio"], 2.0, places=6)
        self.assertLess(r1["lo"], 2.0)
        self.assertGreater(r1["hi"], 2.0)
        r3 = stats.bootstrap_ratio_ci(before, after, resamples=500, seed=4)
        self.assertNotEqual((r1["lo"], r1["hi"]), (r3["lo"], r3["hi"]))

    def test_bootstrap_covers_the_true_ratio(self):
        hits = 0
        for i in range(200):
            rng = random.Random(5000 + i)
            before = [1.5 * math.exp(rng.gauss(0, 0.1)) for _ in range(10)]
            after = [math.exp(rng.gauss(0, 0.1)) for _ in range(10)]
            ci = stats.bootstrap_ratio_ci(before, after, resamples=300, seed=i)
            hits += ci["lo"] <= 1.5 <= ci["hi"]
        self.assertGreaterEqual(hits / 200.0, 0.90, "coverage %d/200" % hits)


class VerdictTests(unittest.TestCase):
    def test_one_case_per_branch(self):
        self.assertEqual(stats.decide_verdict(1.10, 1.30, 0.001, 0.999, 0.05)[0], "FASTER")
        self.assertEqual(stats.decide_verdict(0.70, 0.85, 0.999, 0.001, 0.05)[0], "SLOWER")
        self.assertEqual(stats.decide_verdict(0.97, 1.03, 0.4, 0.6, 0.05)[0],
                         "NO DETECTABLE CHANGE")
        self.assertEqual(stats.decide_verdict(0.90, 1.20, 0.3, 0.7, 0.05)[0], "INCONCLUSIVE")
        # a CI past the band but a p above alpha is not a verdict
        self.assertEqual(stats.decide_verdict(1.06, 1.30, 0.2, 0.8, 0.05)[0], "INCONCLUSIVE")
        # the band edges are 1/(1+e) and 1+e
        name, reason = stats.decide_verdict(1.0 / 1.05, 1.05, 0.5, 0.5, 0.05)
        self.assertEqual(name, "NO DETECTABLE CHANGE")
        self.assertIn("0.952x..1.050x", reason)

    def test_suggest_runs(self):
        self.assertEqual(stats.suggest_runs(10, 0.05, 0.05), 20)
        self.assertEqual(stats.suggest_runs(10, 0.09, 0.05), 20)
        self.assertEqual(stats.suggest_runs(10, 0.20, 0.05), 40)

    def test_aa_false_positive_rate_at_n10(self):
        bad = 0
        trials = 500
        for i in range(trials):
            rng = random.Random(1000 + i)
            before = [math.exp(rng.gauss(0, 0.05)) for _ in range(10)]
            after = [math.exp(rng.gauss(0, 0.05)) for _ in range(10)]
            ci = stats.bootstrap_ratio_ci(before, after, resamples=400, seed=i)
            faster = stats.mann_whitney(after, before, "less")["p"]
            slower = stats.mann_whitney(after, before, "greater")["p"]
            name, _ = stats.decide_verdict(ci["lo"], ci["hi"], faster, slower, 0.02, 0.05)
            bad += name in ("FASTER", "SLOWER")
        self.assertLessEqual(bad / float(trials), 0.08, "false verdicts %d/%d" % (bad, trials))


class TDistributionTests(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(stats.t_two_sided(2.0, 10), 0.0734, delta=1e-3)
        self.assertAlmostEqual(stats.t_sf(2.0, 10), 0.0367, delta=5e-4)
        self.assertAlmostEqual(stats.t_two_sided(0.0, 10), 1.0, places=9)
        self.assertAlmostEqual(stats.t_sf(0.0, 5), 0.5, places=9)
        self.assertAlmostEqual(stats.t_sf(-2.0, 10), 1 - 0.0367, delta=5e-4)
        self.assertAlmostEqual(stats.t_two_sided(2.228, 10), 0.05, delta=1e-3)
        self.assertAlmostEqual(stats.t_two_sided(1.96, 1e6), 0.05, delta=1e-3)
        self.assertEqual(stats.t_two_sided(math.inf, 4), 0.0)
        self.assertLess(stats.t_two_sided(50.0, 8), 1e-9)

    def test_betainc_and_norm_cdf(self):
        self.assertAlmostEqual(stats.betainc(1, 1, 0.3), 0.3, places=9)   # uniform CDF
        self.assertAlmostEqual(stats.betainc(2, 3, 0.5), 0.6875, places=9)
        self.assertEqual(stats.betainc(2, 3, 0.0), 0.0)
        self.assertEqual(stats.betainc(2, 3, 1.0), 1.0)
        self.assertAlmostEqual(stats.norm_cdf(0.0), 0.5, places=12)
        self.assertAlmostEqual(stats.norm_cdf(1.959964), 0.975, places=5)

    def test_welch_t(self):
        t, df = stats.welch_t([1, 2, 3, 4, 5], [3, 4, 5, 6, 7])
        self.assertAlmostEqual(t, -2.0, places=9)
        self.assertAlmostEqual(df, 8.0, places=9)
        self.assertEqual(stats.welch_t([1, 1, 1], [2, 2, 2]), (-math.inf, 1.0))
        self.assertEqual(stats.welch_t([2, 2, 2], [2, 2, 2]), (0.0, 1.0))
        with self.assertRaises(ValueError):
            stats.welch_t([1], [2, 3])


class ExponentTests(unittest.TestCase):
    def test_fit_exponent_recovers_1_2_3(self):
        self.assertAlmostEqual(stats.fit_exponent([1000, 2000], [1, 2]), 1.0, delta=0.01)
        self.assertAlmostEqual(stats.fit_exponent([1000, 2000, 4000], [1, 4, 16]), 2.0, delta=0.01)
        self.assertAlmostEqual(stats.fit_exponent([10, 20, 40], [1, 8, 64]), 3.0, delta=0.01)
        with self.assertRaises(ValueError):
            stats.fit_exponent([1000, 1000], [1, 2])
        with self.assertRaises(ValueError):
            stats.fit_exponent([0, 10], [1, 2])

    def test_exponent_ci_contains_the_true_slope(self):
        sizes = [1000, 2000, 4000, 8000]
        for seed, slope in ((3, 2.0), (4, 1.0), (5, 3.0)):
            rng = random.Random(seed)
            runs = [[(n / 1000.0) ** slope * math.exp(rng.gauss(0, 0.05)) for _ in range(7)]
                    for n in sizes]
            ci = stats.exponent_ci(sizes, runs, resamples=500, seed=seed)
            self.assertIsNotNone(ci)
            self.assertLessEqual(ci[0], slope + 0.02, (slope, ci))
            self.assertGreaterEqual(ci[1], slope - 0.02, (slope, ci))
            self.assertLess(ci[1] - ci[0], 0.5)
        self.assertEqual(stats.exponent_ci(sizes, [[0.01], [0.01], [0.01], [0.01]], 0.02), None)

    def test_scale_exponent_and_floor_text(self):
        fitted, warnings = stats.scale_exponent([10, 20], [0.05, 0.06], 0.0)
        self.assertTrue(any("at least 0.5 s" in w for w in warnings), warnings)
        self.assertEqual(stats.SCALE_FLOOR, 0.5)
        fitted, warnings = stats.scale_exponent([1000, 2000, 4000], [1.0, 4.0, 16.0], 0.0)
        self.assertAlmostEqual(fitted, 2.0, delta=0.01)
        self.assertEqual(warnings, [])

    def test_classify_exponent_bands(self):
        self.assertEqual(stats.classify_exponent(1.29), "about linear")
        self.assertEqual(stats.classify_exponent(1.3), "between linear and quadratic")
        self.assertEqual(stats.classify_exponent(1.7), "about quadratic")
        self.assertEqual(stats.classify_exponent(2.5), "about cubic")
        self.assertEqual(stats.classify_exponent(3.5), "worse than cubic")


class ChangepointTests(unittest.TestCase):
    def test_finds_an_injected_step_at_30_of_60(self):
        for seed in range(5):
            rng = random.Random(7 + seed)
            series = ([1.0 + rng.gauss(0, 0.05) for _ in range(30)]
                      + [1.5 + rng.gauss(0, 0.05) for _ in range(30)])
            points = stats.changepoint(series, min_size=5, alpha=0.01)
            self.assertEqual(len(points), 1, points)
            self.assertLessEqual(abs(points[0]["index"] - 30), 1)
            self.assertLess(points[0]["p"], 1e-6)
            self.assertAlmostEqual(points[0]["percent_change"], 50.0, delta=8.0)

    def test_two_steps(self):
        rng = random.Random(9)
        series = ([1.0 + rng.gauss(0, 0.02) for _ in range(20)]
                  + [2.0 + rng.gauss(0, 0.02) for _ in range(20)]
                  + [1.0 + rng.gauss(0, 0.02) for _ in range(20)])
        idx = [p["index"] for p in stats.changepoint(series)]
        self.assertEqual(len(idx), 2, idx)
        self.assertLessEqual(abs(idx[0] - 20), 1)
        self.assertLessEqual(abs(idx[1] - 40), 1)

    def test_flat_series_rarely_flags(self):
        flagged = 0
        for seed in range(20):
            rng = random.Random(100 + seed)
            flat = [1.0 + rng.gauss(0, 0.05) for _ in range(60)]
            flagged += bool(stats.changepoint(flat, min_size=5, alpha=0.01))
        self.assertLessEqual(flagged, 1, "flagged %d of 20 flat series" % flagged)

    def test_short_series_and_exact_step(self):
        self.assertEqual(stats.changepoint([1.0] * 9, min_size=5), [])
        points = stats.changepoint([1.0] * 10 + [2.0] * 10, min_size=5)
        self.assertEqual([p["index"] for p in points], [10])
        self.assertEqual(points[0]["p"], 0.0)


if __name__ == "__main__":
    unittest.main()
