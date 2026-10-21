"""Budgets: per-child USD, token and turn limits, refusals without a price,
and tree ledgers shared by a child and its descendants."""

import json

from helpers import SessionTest, price_env


class BudgetTests(SessionTest):
    def test_budget_flags_claude_native(self):
        self.spawn("claude", "x", "@@cost:0.9", "--budget-usd", "0.5", "--max-turns", "3")
        res = self.wait_result("x")
        argv = self.calls("x")[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.5")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "3")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "budget")
        self.assertIn("0.5", res["reason"])
        self.assertEqual(res["budget"]["usd_limit"], 0.5)
        self.assertEqual(res["budget"]["turns_limit"], 3)
        self.assertTrue(res["answer"], "the answer is kept when the budget is exceeded")
        self.assertIn("budget", [e["type"] for e in self.events("x")])

    def test_budget_tokens_all_engines(self):
        for engine in ("claude", "codex", "gemini"):
            self.spawn(engine, "over-%s" % engine, "@@tokens:90,20", "--max-tokens", "100")
            res = self.wait_result("over-%s" % engine)
            self.assertEqual(res["failure_class"], "budget", engine)
            self.assertIn("100", res["reason"])
            self.spawn(engine, "under-%s" % engine, "@@tokens:50,20", "--max-tokens", "100")
            res = self.wait_result("under-%s" % engine)
            self.assertEqual(res["status"], "ok", engine)
            self.assertEqual(res["usage"]["total_tokens"], 70)

    def test_max_turns_codex_tool_calls(self):
        self.spawn("codex", "x", "@@tool-calls:5 @@sleep:20", "--max-turns", "2")
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["failure_class"], "budget")
        self.assertIn("tool calls", res["reason"])
        self.assertTrue(self.signals("x"), "the ladder did not run")
        # finished before the runner noticed: still marked after the turn
        self.spawn("codex", "y", "@@tool-calls:5", "--max-turns", "2")
        res = self.wait_result("y")
        self.assertEqual(res["failure_class"], "budget")

    def test_usd_budget_refused_without_price(self):
        cp = self.run_as("spawn", "codex", "x", self.work, "hi", "--budget-usd", "1")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("AGENT_SESSIONS_CODEX_PRICE", cp.stderr)
        cp = self.run_as("spawn", "gemini", "x", self.work, "hi", "--tree-budget-usd", "1")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("AGENT_SESSIONS_GEMINI_PRICE", cp.stderr)
        env = price_env("codex", 1.0, 0.1, 10.0)
        self.spawn("codex", "x", "@@tokens:1000000,0", "--budget-usd", "5", env=env)
        res = self.wait_result("x")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["cost_usd"], 1.0)
        self.assertEqual(res["cost_source"], "estimate")

    def test_tree_ledger(self):
        cp = self.spawn("claude", "r", "@@cost:0.001", "--tree-budget-usd", "0.0025")
        self.wait_result("r")
        ledger_id = self.meta("r")["budget"]["ledger"]
        self.assertTrue(ledger_id)
        self.assertIn("tree ledger %s" % ledger_id, cp.stdout)
        env = {"AGENT_SESSIONS_LEDGER": ledger_id}
        self.spawn("claude", "c1", "@@cost:0.001", env=env)
        self.assertEqual(self.wait_result("c1")["status"], "ok")
        self.assertAlmostEqual(self.ledger(ledger_id)["spent_usd"], 0.002)
        self.spawn("claude", "c2", "@@cost:0.001", env=env)
        self.assertEqual(self.wait_result("c2")["status"], "ok")
        argv = self.calls("c2")[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.0005")
        cp = self.run_as("spawn", "claude", "c3", self.work, "hi", env=env)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("tree budget exhausted", cp.stderr)
        self.assertIn("c1", cp.stderr)
        doc = json.loads(self.ok("budget", "--json", env=env).stdout)
        ledger = [l for l in doc["ledgers"] if l["id"] == ledger_id][0]
        self.assertEqual(sorted(ledger["children"]), ["c1", "c2", "r"])
        self.assertIn("ledger", self.ok("budget").stdout)

    def test_ledger_reaches_grandchildren_through_the_engine_env(self):
        self.spawn("claude", "r", "@@spawn:claude,gc,%s,@@cost:0.002" % self.work, "--tree-max-tokens", "1000")
        self.wait_result("r")
        ledger_id = self.meta("r")["budget"]["ledger"]
        self.assertEqual(self.meta("gc")["budget"]["ledger"], ledger_id)
        self.wait_result("gc")
        self.assertIn("gc", self.ledger(ledger_id)["children"])
