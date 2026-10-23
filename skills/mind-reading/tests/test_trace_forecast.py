"""Trace forecast predictors: ridge generalization and primal/dual agreement, retrieval ties and exclusion, tie-aware
metrics, and the family-level bootstrap. All data is synthetic."""
import unittest

import helpers  # noqa: F401  (puts scripts/ on sys.path)

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

if np is not None:
    from mr import trace_forecast as tf


@unittest.skipIf(np is None, "numpy not installed")
class RidgeTests(unittest.TestCase):
    def test_affine_map_generalizes(self):
        rng = np.random.default_rng(0)
        d = 6
        A = rng.normal(size=(d, d))
        b = rng.normal(size=d)
        X = rng.normal(size=(200, d)) + 3.0
        Xv = rng.normal(size=(40, d)) + 3.0
        model = tf.fit_ridge(X, X @ A + b, alpha=1e-6)
        for arr in model.values():
            if isinstance(arr, np.ndarray):
                self.assertEqual(arr.dtype, np.float32)
        pred = tf.predict_ridge(model, Xv)
        self.assertTrue(np.allclose(pred, tf.normalize(Xv @ A + b), atol=1e-3))
        self.assertTrue(np.allclose(np.linalg.norm(pred, axis=1), 1, atol=1e-5))

    def test_primal_dual_equivalent(self):
        rng = np.random.default_rng(1)
        d, n, alpha = 8, 5, 2.0
        X, Y = rng.normal(size=(n, d)), rng.normal(size=(n, d))
        dual = tf.fit_ridge(X, Y, alpha)  # n < d
        Xc, Yc = X - X.mean(0), Y - Y.mean(0)
        primal = np.linalg.solve(Xc.T @ Xc + alpha * np.eye(d), Xc.T @ Yc)
        self.assertTrue(np.allclose(dual["weight"], primal, atol=1e-5))
        # Same data padded so n >= d takes the primal branch and must give the identical map on duplicated rows.
        big = tf.fit_ridge(np.vstack([X, X]), np.vstack([Y, Y]), alpha * 2)
        self.assertTrue(np.allclose(big["weight"], dual["weight"], atol=1e-5))

    def test_invalid_inputs(self):
        X = np.ones((3, 4))
        for bad in (0.0, -1.0, float("nan")):
            with self.assertRaises(ValueError):
                tf.fit_ridge(X, X, alpha=bad)
        with self.assertRaises(ValueError):
            tf.fit_ridge(X, np.ones((3, 5)))
        with self.assertRaises(ValueError):
            tf.fit_ridge(X[:1], X[:1])
        with self.assertRaises(ValueError):
            tf.fit_ridge(np.full((3, 4), np.inf), X)
        model = tf.fit_ridge(np.eye(4)[:3], np.eye(4)[:3])
        with self.assertRaises(ValueError):
            tf.predict_ridge(model, np.ones((2, 5)))
        with self.assertRaises(ValueError):
            tf.predict_ridge(model, np.full((2, 4), np.nan))


@unittest.skipIf(np is None, "numpy not installed")
class NormalizeAndRankTests(unittest.TestCase):
    def test_zero_norm_and_1d(self):
        out = tf.normalize(np.array([[3.0, 4.0], [0.0, 0.0]]))
        self.assertTrue(np.allclose(out, [[0.6, 0.8], [0, 0]]))
        self.assertTrue(np.allclose(tf.normalize([0.0, 2.0]), [0, 1]))
        with self.assertRaises(ValueError):
            tf.normalize(np.zeros((2, 2, 2)))
        with self.assertRaises(ValueError):
            tf.normalize([[1.0, np.nan]])

    def test_rank_candidates_shapes(self):
        p = np.array([[1.0, 0.0], [0.0, 0.0]])
        shared = tf.rank_candidates(p, np.array([[2.0, 0.0], [0.0, 3.0], [0, 0]]))
        self.assertEqual(shared.shape, (2, 3))
        self.assertTrue(np.allclose(shared[0], [1, 0, 0]))
        self.assertTrue(np.allclose(shared[1], 0))  # zero prediction scores zero, no error
        per_row = tf.rank_candidates(p, np.ones((2, 4, 2)))
        self.assertEqual(per_row.shape, (2, 4))
        with self.assertRaises(ValueError):
            tf.rank_candidates(p, np.ones((3, 4, 2)))
        with self.assertRaises(ValueError):
            tf.rank_candidates(p, np.ones((3, 5)))


@unittest.skipIf(np is None, "numpy not installed")
class RetrieveTests(unittest.TestCase):
    def setUp(self):
        self.tx = np.array([[1.0, 0], [1.0, 0], [0, 1.0], [-1.0, 0]])
        self.ty = np.array([[0, 1.0], [0, -1.0], [1.0, 0], [1.0, 1.0]])

    def test_tied_neighbors_break_by_index(self):
        # Rows 0 and 1 tie exactly; k=1 must choose row 0 every time.
        for _ in range(3):
            out = tf.retrieve_forecast(np.array([[1.0, 0]]), self.tx, self.ty, k=1)
            self.assertTrue(np.allclose(out, [[0, 1]]))
        # k=2 with equal weights: the tied pair cancels to zero, which stays zero.
        out = tf.retrieve_forecast(np.array([[1.0, 0]]), self.tx, self.ty, k=2)
        self.assertTrue(np.allclose(out, 0))

    def test_exclusion_is_explicit(self):
        q = np.array([[1.0, 0], [1.0, 0]])
        out = tf.retrieve_forecast(q, self.tx, self.ty, k=1, exclude=[[0], []])
        self.assertTrue(np.allclose(out, [[0, -1], [0, 1]]))
        out = tf.retrieve_forecast(q, self.tx, self.ty, k=4, exclude=[[0, 1, 3], [2]])
        self.assertTrue(np.allclose(out[0], [1, 0]))  # only row 2 remains
        with self.assertRaises(ValueError):
            tf.retrieve_forecast(q, self.tx, self.ty, exclude=[[0, 1, 2, 3], []])
        with self.assertRaises(ValueError):
            tf.retrieve_forecast(q, self.tx, self.ty, exclude=[[0]])
        with self.assertRaises(ValueError):
            tf.retrieve_forecast(q, self.tx, self.ty, exclude=[[9], []])

    def test_batching_matches_single_queries(self):
        rng = np.random.default_rng(2)
        tx, ty = rng.normal(size=(30, 5)), rng.normal(size=(30, 5))
        q = rng.normal(size=(300, 5))  # spans three batches
        full = tf.retrieve_forecast(q, tx, ty, k=4, temperature=0.3)
        for i in (0, 127, 128, 299):
            one = tf.retrieve_forecast(q[i:i + 1], tx, ty, k=4, temperature=0.3)
            self.assertTrue(np.allclose(full[i], one[0]))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            tf.retrieve_forecast(np.ones((1, 3)), self.tx, self.ty)
        with self.assertRaises(ValueError):
            tf.retrieve_forecast(np.ones((1, 2)), self.tx, self.ty, temperature=0)
        with self.assertRaises(ValueError):
            tf.retrieve_forecast(np.ones((1, 2)), self.tx, self.ty, k=0)


@unittest.skipIf(np is None, "numpy not installed")
class MetricTests(unittest.TestCase):
    def test_basic_values(self):
        s = np.array([[0.9, 0.1, 0.0], [0.1, 0.5, 0.9]])
        m = tf.metrics(s, [0, 1])
        self.assertAlmostEqual(m["top1"], 0.5)
        self.assertAlmostEqual(m["top3"], 1.0)
        self.assertAlmostEqual(m["mrr"], (1 + 0.5) / 2)
        e = np.exp(s)
        expect = -np.mean([np.log(e[0, 0] / e[0].sum()), np.log(e[1, 1] / e[1].sum())])
        self.assertAlmostEqual(m["logloss"], expect)
        self.assertNotIn("family_macro_top1", m)

    def test_ties_use_average_rank_and_expected_credit(self):
        m = tf.metrics(np.zeros((1, 4)), [3])  # four-way tie
        self.assertAlmostEqual(m["top1"], 0.25)
        self.assertAlmostEqual(m["top3"], 0.75)
        self.assertAlmostEqual(m["mrr"], 1 / 2.5)
        self.assertAlmostEqual(m["logloss"], np.log(4))
        # One strictly better column, gold tied with another for ranks 2-3.
        m = tf.metrics(np.array([[1.0, 0.0, 0.0, -1.0]]), [2])
        self.assertAlmostEqual(m["top1"], 0.0)
        self.assertAlmostEqual(m["top3"], 1.0)
        self.assertAlmostEqual(m["mrr"], 1 / 2.5)

    def test_column_permutation_invariance(self):
        rng = np.random.default_rng(3)
        s = np.round(rng.normal(size=(20, 6)), 1)  # rounding creates ties
        gold = rng.integers(0, 6, size=20)
        base = tf.metrics(s, gold)
        perm = rng.permutation(6)
        inv = np.argsort(perm)
        moved = tf.metrics(s[:, perm], inv[gold])
        for key in ("top1", "top3", "mrr", "logloss"):
            self.assertAlmostEqual(base[key], moved[key])

    def test_stable_softmax_and_validation(self):
        m = tf.metrics(np.array([[1000.0, 0.0]]), [1])
        self.assertTrue(np.isfinite(m["logloss"]))
        self.assertAlmostEqual(m["logloss"], 1000.0)
        with self.assertRaises(ValueError):
            tf.metrics(np.zeros((2, 3)), [0, 3])
        with self.assertRaises(ValueError):
            tf.metrics(np.zeros((2, 3)), [0])
        with self.assertRaises(ValueError):
            tf.metrics(np.array([[np.nan, 0.0]]), [0])
        with self.assertRaises(ValueError):
            tf.metrics(np.zeros((2, 3)), [0, 1], families=["a"])

    def test_family_macro(self):
        s = np.array([[1.0, 0], [1.0, 0], [1.0, 0], [0, 1.0]])
        m = tf.metrics(s, [0, 0, 0, 0], families=["a", "a", "a", "b"])
        self.assertAlmostEqual(m["top1"], 0.75)
        self.assertAlmostEqual(m["family_macro_top1"], 0.5)
        self.assertAlmostEqual(m["family_macro_mrr"], (1 + 0.5) / 2)


@unittest.skipIf(np is None, "numpy not installed")
class BootstrapTests(unittest.TestCase):
    def test_duplicated_examples_keep_macro_mean(self):
        a = np.array([1.0, 0.0, 0.5, 0.9])
        b = np.array([0.0, 0.0, 0.5, 0.1])
        fam = ["x", "x", "y", "z"]
        base = tf.paired_bootstrap(a, b, fam, draws=500)
        # Duplicate every example in family x three times; the macro estimand must not move.
        a2, b2 = np.concatenate([a, a[:2], a[:2]]), np.concatenate([b, b[:2], b[:2]])
        dup = tf.paired_bootstrap(a2, b2, fam + ["x"] * 4, draws=500)
        self.assertAlmostEqual(base["mean_diff"], dup["mean_diff"])
        self.assertEqual(base["ci_low"], dup["ci_low"])
        self.assertEqual(base["ci_high"], dup["ci_high"])
        self.assertAlmostEqual(base["mean_diff"], np.mean([0.5, 0.0, 0.8]))
        self.assertEqual(base["n_families"], 3)

    def test_deterministic_and_interval_brackets_mean(self):
        rng = np.random.default_rng(4)
        a, b = rng.normal(size=60) + 0.5, rng.normal(size=60)
        fam = [i % 12 for i in range(60)]
        r1 = tf.paired_bootstrap(a, b, fam, seed=7)
        r2 = tf.paired_bootstrap(a, b, fam, seed=7)
        self.assertEqual(r1, r2)
        self.assertLess(r1["ci_low"], r1["mean_diff"])
        self.assertGreater(r1["ci_high"], r1["mean_diff"])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            tf.paired_bootstrap([1.0, 2.0], [1.0], ["a", "b"])
        with self.assertRaises(ValueError):
            tf.paired_bootstrap([1.0, 2.0], [1.0, 2.0], ["a"])
        with self.assertRaises(ValueError):
            tf.paired_bootstrap([1.0, 2.0], [1.0, 2.0], ["a", "a"])
        with self.assertRaises(ValueError):
            tf.paired_bootstrap([1.0, np.nan], [1.0, 2.0], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
