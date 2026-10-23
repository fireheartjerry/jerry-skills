"""Optional local learning tests never download weights or call a service."""
import unittest
import helpers
from mr.trace_learning import hard_candidates, temperature

try:
    import numpy as np
except ImportError:
    np = None
try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(np is None, "optional NumPy is not installed")
class CandidateTests(unittest.TestCase):
    def test_candidates_use_seed_only_and_separate_families(self):
        ex = [{"id": str(i), "family": str(i//2), "source": "fiction", "project": "p",
               "target": {"text": "unique next turn %d" % i}} for i in range(12)]
        v = np.random.default_rng(0).normal(size=(12, 4))
        rows, gold, strata = hard_candidates(ex, v, count=4)
        for i, row in enumerate(rows):
            self.assertEqual(row[gold[i]], i)
            for j in row:
                if j != i:
                    self.assertNotEqual(ex[j]["family"], ex[i]["family"])
        # Future content changes cannot determine which situations supply negatives.
        for i, e in enumerate(ex): e["target"]["text"] = "different unique future %d" % i
        self.assertTrue(np.array_equal(rows, hard_candidates(ex, v, 4)[0]))

    def test_calibration_uses_given_validation_scores(self):
        self.assertEqual(temperature(np.array([[1., 0.], [0., 1.]]), [0, 1]), 0.03)
        self.assertEqual(temperature(np.array([[1., 0.], [0., 1.]]), [1, 0]), 1.0)


@unittest.skipIf(np is None or torch is None, "optional local training dependencies are not installed")
class AdapterTests(unittest.TestCase):
    def test_learns_nonidentity_continuation_without_changing_encoder(self):
        from mr.trace_learning import fit_adapter, predict_adapter
        from mr.trace_forecast import normalize
        torch.set_num_threads(2)
        x = normalize(np.random.default_rng(7).normal(size=(20, 4)).astype("float32"))
        y = x[:, [1, 0, 3, 2]]
        model = fit_adapter(x, y, list(map(str, range(20))), rank=4, steps=120, learning_rate=0.03)
        predicted = predict_adapter(model, x)
        self.assertGreater(float(np.mean(np.sum(predicted*y, axis=1))), float(np.mean(np.sum(x*y, axis=1))) + 0.4)
        self.assertTrue(np.allclose(np.linalg.norm(predicted, axis=1), 1, atol=1e-5))
        self.assertLess(model["loss_last"], model["loss_first"])
        self.assertEqual(model["parameter_count"], 40)


if __name__ == "__main__": unittest.main()
