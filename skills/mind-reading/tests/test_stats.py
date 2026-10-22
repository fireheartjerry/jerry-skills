import unittest

import helpers  # noqa: F401  (path setup)
from mr import budget, engine
from mr.stats import format_rate, rate_block, sign_test, wilson


class WilsonTests(unittest.TestCase):
    def test_known_values(self):
        p, lo, hi = wilson(5, 10)
        self.assertAlmostEqual(p, 0.5)
        self.assertAlmostEqual(lo, 0.2366, places=4)
        self.assertAlmostEqual(hi, 0.7634, places=4)
        p, lo, hi = wilson(0, 10)
        self.assertEqual((p, lo), (0.0, 0.0))
        self.assertAlmostEqual(hi, 0.2775, places=4)
        p, lo, hi = wilson(10, 10)
        self.assertAlmostEqual(lo, 0.7225, places=4)
        self.assertEqual(hi, 1.0)

    def test_empty_and_block(self):
        self.assertEqual(wilson(0, 0), (None, None, None))
        self.assertEqual(rate_block(0, 0), {"k": 0, "n": 0, "rate": None, "lo": None, "hi": None})
        self.assertEqual(rate_block(1, 4)["rate"], 0.25)
        self.assertEqual(format_rate(rate_block(0, 0)), "n/a (0/0)")
        self.assertEqual(format_rate(rate_block(5, 10)), "50% (5/10, 95% CI 24-76%)")


class SignTestTests(unittest.TestCase):
    def test_exact_two_sided(self):
        self.assertEqual(sign_test(0, 0), 1.0)
        self.assertEqual(sign_test(8, 8), 1.0)
        self.assertAlmostEqual(sign_test(9, 1), 0.021484375)  # 2 * (1 + 10) / 1024
        self.assertAlmostEqual(sign_test(1, 9), 0.021484375)
        self.assertAlmostEqual(sign_test(10, 0), 2.0 / 1024)
        self.assertAlmostEqual(sign_test(6, 4), 0.75390625)


class _Backend:
    """Each call costs `cost` (or reports `tokens` only, like codex)."""

    def __init__(self, cost=None, tokens=None):
        self.cost, self.tokens, self.version, self.calls = cost, tokens, "test", 0

    def call(self, unit, prompt, attempt):
        self.calls += 1
        return engine.CallResult(True, data={"x": 1}, tokens=self.tokens, cost=self.cost, exit_code=0)


SCHEMA = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}


class EngineBudgetTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg = helpers.init_home(helpers.tmpdir() / "home")

    def units(self, n, eng):
        return [engine.Unit("u%d" % i, "p" * 100, SCHEMA, eng.run_dir / ("u%d.json" % i)) for i in range(n)]

    def test_reservation_stops_before_the_call_that_would_pass_the_cap(self):
        be = _Backend(cost=0.4)
        eng = engine.Engine(self.paths, self.cfg, "fake", "mine", backend=be, max_cost_usd=1.0, concurrency=1,
                            cost_estimate=lambda u, p: 0.4, log=helpers.quiet)
        res = eng.run(self.units(4, eng))
        self.assertEqual((len(res["ok"]), len(res["stopped"])), (2, 2))
        self.assertLessEqual(res["cost_usd"], 1.0)
        rows = engine.read_calls(eng.run_dir)
        self.assertEqual(rows[0]["prompt_chars"], 100)
        self.assertEqual(rows[0]["reserved_usd"], 0.4)

    def test_without_estimate_the_cap_checks_spend_only(self):
        eng = engine.Engine(self.paths, self.cfg, "fake", "mine", backend=_Backend(cost=0.4), max_cost_usd=1.0, concurrency=1,
                            log=helpers.quiet)
        res = eng.run(self.units(4, eng))
        self.assertEqual(len(res["ok"]), 3)  # 0.0, 0.4, 0.8 are under the cap when each call starts

    def test_learned_rate_raises_a_low_estimate(self):
        eng = engine.Engine(self.paths, self.cfg, "fake", "mine", backend=_Backend(cost=0.5), max_cost_usd=1.0, concurrency=1,
                            cost_estimate=lambda u, p: 0.01, log=helpers.quiet)
        res = eng.run(self.units(4, eng))
        # after one paid call the run expects 0.5 * 1.2 per call of the same size: 0.5 + 0.6 > 1.0 stops the second,
        # where the static estimate alone (0.01) would have let it start and end at the cap
        self.assertEqual(len(res["ok"]), 1)

    def test_codex_cost_from_tokens_and_rate(self):
        self.cfg["codex_usd_per_million_tokens"] = 10.0
        eng = engine.Engine(self.paths, self.cfg, "codex", "mine", backend=_Backend(tokens=50000), max_cost_usd=1.2,
                            concurrency=1, log=helpers.quiet)
        res = eng.run(self.units(4, eng))
        self.assertEqual(len(res["ok"]), 3)
        self.assertAlmostEqual(res["cost_usd"], 1.5)

    def test_max_calls_counts_calls_in_flight(self):
        be = _Backend(cost=0.0)
        eng = engine.Engine(self.paths, self.cfg, "fake", "mine", backend=be, max_calls=2, concurrency=4, log=helpers.quiet)
        res = eng.run(self.units(6, eng))
        self.assertEqual(be.calls, 2)
        self.assertEqual(len(res["stopped"]), 4)


class EstimatorTests(unittest.TestCase):
    def test_price_table_and_safety(self):
        _, cfg = helpers.init_home(helpers.tmpdir() / "home")
        self.assertEqual(budget.price(cfg, "claude-sonnet-x"), (3.0, 15.0))
        self.assertEqual(budget.price(cfg, "something-else"), (5.0, 25.0))
        est = budget.estimator(cfg, "sonnet", "mine")(None, "x" * 400000)
        self.assertAlmostEqual(est, 2.0 * (100000 * 3.0 + 8000 * 15.0) / 1e6)

    def test_budget_bookkeeping(self):
        b = budget.Budget(3.0, 10)
        b.charge({"cost_usd": 1.25, "calls": 4})
        self.assertAlmostEqual(b.remaining_cost(), 1.75)
        self.assertAlmostEqual(b.remaining_cost(reserve=1.0), 0.75)
        self.assertEqual(b.remaining_calls(), 6)
        self.assertFalse(b.exhausted())
        b.charge({"cost_usd": 2.0, "calls": 1})
        self.assertTrue(b.exhausted())


if __name__ == "__main__":
    unittest.main()
