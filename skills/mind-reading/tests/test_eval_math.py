import unittest

import helpers  # noqa: F401
from mr import evaluate as ev

ARMS = ["none", "full"]


def row(sid, n, none, full):
    return {"sid": sid, "n": n, "arms": {
        "none": {"full": none[0], "partial": none[1], "none": n - none[0] - none[1], "contra": none[2], "contra_texts": ["c"] * none[2]},
        "full": {"full": full[0], "partial": full[1], "none": n - full[0] - full[1], "contra": full[2], "contra_texts": ["c"] * full[2]},
    }}


ROWS = [row("s00", 10, (2, 4, 1), (5, 2, 0)), row("s01", 4, (1, 1, 2), (1, 2, 1)), row("s02", 6, (3, 0, 0), (3, 0, 0))]


class EvalMathTests(unittest.TestCase):
    def test_pooled_rates(self):
        # none: (2+1+3) + 0.5*(4+1+0) = 8.5 of 20; full: (5+1+3) + 0.5*(2+2+0) = 11 of 20
        self.assertAlmostEqual(ev.pooled_rate(ROWS, "none", 0.5), 8.5 / 20)
        self.assertAlmostEqual(ev.pooled_rate(ROWS, "full", 0.5), 11 / 20)
        self.assertAlmostEqual(ev.pooled_rate(ROWS, "none", 0.0), 6 / 20)
        self.assertAlmostEqual(ev.pooled_rate(ROWS, "full", 0.0), 9 / 20)

    def test_per_session_and_paired(self):
        rates = ev.per_session_rates(ROWS, "full", 0.5)
        self.assertAlmostEqual(rates["s00"], 6 / 10)
        self.assertAlmostEqual(rates["s01"], 2 / 4)
        diffs = ev.paired_differences(ROWS, "full", "none", 0.5)
        self.assertAlmostEqual(diffs["s00"], 0.6 - 0.4)
        self.assertAlmostEqual(diffs["s01"], 0.5 - 0.375)
        self.assertAlmostEqual(diffs["s02"], 0.0)

    def test_win_tie_loss_and_contradictions(self):
        self.assertEqual(ev.win_tie_loss(ROWS, "full", "none", 0.5), {"win": 2, "tie": 1, "loss": 0})
        self.assertEqual(ev.win_tie_loss(ROWS, "none", "full", 0.5), {"win": 0, "tie": 1, "loss": 2})
        rate, count = ev.contradiction_rate(ROWS, "none")
        self.assertEqual(count, 3)
        self.assertAlmostEqual(rate, 3 / 20)

    def test_bootstrap_deterministic_and_zero_width_when_equal(self):
        a1, p1 = ev.bootstrap(ROWS, ARMS, 0.5, 500, seed=0)
        a2, p2 = ev.bootstrap(ROWS, ARMS, 0.5, 500, seed=0)
        self.assertEqual(a1, a2)
        self.assertEqual(p1, p2)
        same = [row("s%d" % k, 4, (1, 2, 0), (2, 2, 0)) for k in range(5)]
        arms, pairs = ev.bootstrap(same, ARMS, 0.5, 200, seed=0)
        self.assertEqual(arms["none"], (0.5, 0.5))
        self.assertEqual(arms["full"], (0.75, 0.75))
        self.assertEqual(pairs["none-full"], (-0.25, -0.25))

    def test_bootstrap_two_session_hand_check(self):
        # pooled full-arm rates: both s00 -> 0.6; both s01 -> 0.5; mixed -> 8/14 = 0.5714. Interval spans [0.5, 0.6].
        two = [ROWS[0], ROWS[1]]
        arms, _ = ev.bootstrap(two, ["full"], 0.5, 2000, seed=0)
        lo, hi = arms["full"]
        self.assertAlmostEqual(lo, 0.5, places=6)
        self.assertAlmostEqual(hi, 0.6, places=6)

    def test_report_fields(self):
        cfg = {"partial_weight": 0.5, "bootstrap_resamples": 100, "bootstrap_seed": 0}
        rep = ev.build_report(ROWS, ARMS, cfg, {"model_hash": "abc", "split_id": "s", "forced": False, "engines": {}})
        for key in ("rubric_version", "arms", "sessions", "requirements", "pooled", "pairs", "per_session", "limitation", "model_hash", "split_id", "forced"):
            self.assertIn(key, rep)
        self.assertEqual(rep["requirements"], 20)
        self.assertEqual(rep["pairs"]["none-full"]["win_tie_loss"], {"win": 0, "tie": 1, "loss": 2})
        self.assertEqual(len(rep["pooled"]["full"]["ci95"]), 2)
        md = ev.report_markdown(rep, ROWS)
        self.assertIn("| full |", md)
        self.assertIn("Largest losses of the best arm (full)", md)
        rep["forced"] = True
        self.assertIn("TUNED", ev.report_markdown(rep, ROWS).splitlines()[0])

    def test_session_row_from_judgments(self):
        reqs = [{"id": 1}, {"id": 2}, {"id": 3}]
        j = {"none": {"items": [{"id": 1, "verdict": "full"}, {"id": 2, "verdict": "partial"}, {"id": 3, "verdict": "none"}],
                      "contradictions": [{"text": "x", "requirement_id": None}]}}
        r = ev.session_row("s00", reqs, j)
        self.assertEqual(r["n"], 3)
        self.assertEqual(r["arms"]["none"]["full"], 1)
        self.assertEqual(r["arms"]["none"]["contra"], 1)
        self.assertAlmostEqual(ev.session_rate(r, "none", 0.5), 0.5)

    def test_impossible_judge_counts_flagged_without_repair(self):
        historic = [{"id": "d01", "n_req": 4, "old": {"full": 1, "partial": 1.5}, "new": {"full": 0, "partial": 2}}]
        self.assertIn("not a nonnegative integer", ev.count_errors(historic, historical=True)[0])
        self.assertEqual(historic[0]["old"]["partial"], 1.5)
        invalid = row("s1", 3, (1, 0, 0), (2, 0, 0))
        invalid["arms"]["full"]["partial"] = -1
        with self.assertRaises(ev.EvalRefused):
            ev.build_report([invalid], ARMS, {"partial_weight": 0.5, "bootstrap_resamples": 20, "bootstrap_seed": 0}, {})

    def test_diagnostic_score_never_establishes_human_acceptance(self):
        report = ev.build_report(ROWS, ARMS, {"partial_weight": 0.5, "bootstrap_resamples": 20, "bootstrap_seed": 0}, {})
        self.assertFalse(report["acceptance_eligible"])
        self.assertEqual(report["judge_calibration"]["status"], "uncalibrated")
        self.assertIn("cannot establish acceptance", ev.report_markdown(report, ROWS))


if __name__ == "__main__":
    unittest.main()
