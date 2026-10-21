"""Reliability core: stall watchdog, the INT/TERM/KILL ladder, post-result
linger, failure classification, bounded retries and the deadline."""

import time

from helpers import SessionTest, pid_alive


class ReliabilityTests(SessionTest):
    def test_stall_watchdog(self):
        self.spawn("claude", "x", "@@stall:30", "--stall", "2")
        start = time.time()
        res = self.wait_result("x", timeout=25)
        self.assertLess(time.time() - start, 25)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "stall")
        self.assertIn("2s", res["reason"])
        pid = self.calls("x")[0]["pid"]
        self.wait_until(lambda: not pid_alive(pid), timeout=5, message="engine still alive")
        self.assertIn("stall", [e["type"] for e in self.events("x")])
        # --stall 0 disables the watchdog.
        self.spawn("claude", "y", "@@stall:3", "--stall", "0")
        self.assertEqual(self.wait_result("y", timeout=20)["status"], "ok")

    def test_kill_ladder_int_first(self):
        self.spawn("codex", "x", "@@stall:30", "--stall", "2")
        res = self.wait_result("x", timeout=25)
        self.assertEqual(res["failure_class"], "stall")
        sigs = [s["signal"] for s in self.signals("x")]
        self.assertTrue(sigs, "no signal recorded")
        self.assertEqual(sigs[0], "SIGINT")

    def test_ladder_escalates_past_int(self):
        # An engine that ignores SIGTERM after SIGINT is gone still dies: INT is
        # handled by the fake (it exits), so use a timeout on a TERM-ignoring hang.
        self.spawn("claude", "x", "@@hang-hard", "--timeout", "5")
        res = self.wait_result("x", timeout=40)
        self.assertEqual(res["status"], "timeout")
        pid = self.calls("x")[0]["pid"]
        self.wait_until(lambda: not pid_alive(pid), timeout=10, message="engine survived the ladder")

    def test_post_result_linger_terminated(self):
        # claude `result`, codex `turn.completed` and gemini `result` are all terminal events.
        engines = ("claude", "codex", "gemini")
        start = time.time()
        for engine in engines:
            self.spawn(engine, "x-%s" % engine, "@@linger:60")
        for engine in engines:
            name = "x-%s" % engine
            res = self.wait_result(name, timeout=30)
            self.assertEqual(res["status"], "ok", (engine, res.get("reason")))
            self.assertTrue(any("lingered" in w for w in res["warnings"]), (engine, res["warnings"]))
            self.assertFalse(pid_alive(self.calls(name)[0]["pid"]), engine)
        self.assertLess(time.time() - start, 25)

    def test_background_task_result_is_not_the_end(self):
        # claude -p reports a result, waits for its background task, then reports
        # the real one. The first result must not end the turn, even when the
        # wait is longer than AGENT_SESSIONS_POST_RESULT_WAIT.
        env = {"AGENT_SESSIONS_POST_RESULT_WAIT": "1"}
        self.spawn("claude", "x", "@@bg-task:4 @@cost:0.004 hello", env=env)
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertTrue(res["answer"].startswith("reply#1"), res["answer"])
        self.assertFalse(any("lingered" in w for w in res["warnings"]), res["warnings"])
        self.assertEqual(res["cost_usd"], 0.004)  # the last result's cumulative cost
        self.assertEqual(self.signals("x"), [])
        self.assertIn("task", [e["type"] for e in self.events("x")])

    def test_empty_placeholder_result_ignored(self):
        env = {"AGENT_SESSIONS_POST_RESULT_WAIT": "1"}
        self.spawn("claude", "x", "@@empty-lead:3 hello", env=env)
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertTrue(res["answer"].startswith("reply#1"), res["answer"])
        self.assertEqual(self.signals("x"), [])

    def test_watchdogs_stay_armed_while_background_task_pending(self):
        self.spawn("claude", "x", "@@bg-task:30 hello", "--stall", "2",
                   env={"AGENT_SESSIONS_POST_RESULT_WAIT": "1"})
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "stall")

    def test_exit_with_pending_background_task_warns(self):
        self.spawn("claude", "x", "@@bg-task @@bg-exit hello")
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertTrue(any("background task" in w for w in res["warnings"]), res["warnings"])

    def test_retry_tells_fresh_conversation_about_partial_changes(self):
        repo = self.repo()
        self.spawn("claude", "x", "@@tool-calls:2 @@edit:partial.txt=half @@fail-once:crash-after-edit do it",
                   "--retries", "1", directory=repo)
        res = self.wait_result("x", timeout=40)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        prompts = [c["stdin"] for c in self.calls("x")]
        self.assertEqual(len(prompts), 2)
        self.assertFalse(prompts[0].startswith("Note from the runner"))
        self.assertTrue(prompts[1].startswith("Note from the runner: an earlier attempt at this task stopped (crash)"),
                        prompts[1])
        self.assertIn("files changed: partial.txt", prompts[1])
        self.assertTrue(prompts[1].endswith("do it"))
        # Nothing changed (a rate limit before any tool call): no note.
        self.spawn("claude", "y", "@@fail-once:crash plain", "--retries", "1", directory=repo)
        self.assertEqual(self.wait_result("y", timeout=40)["status"], "ok")
        self.assertFalse(any(c["stdin"].startswith("Note") for c in self.calls("y")))

    def test_failure_classes(self):
        cases = [
            ("claude", "@@rate-limit", "rate_limit"), ("codex", "@@rate-limit", "rate_limit"),
            ("gemini", "@@rate-limit", "rate_limit"),
            ("claude", "@@usage-limit:120", "rate_limit"), ("codex", "@@usage-limit:120", "rate_limit"),
            ("claude", "@@auth-fail", "auth"), ("codex", "@@auth-fail", "auth"), ("gemini", "@@auth-fail", "auth"),
            ("claude", "@@crash", "crash"), ("codex", "@@crash", "crash"), ("gemini", "@@crash", "crash"),
            ("claude", "@@noanswer", "crash"), ("gemini", "@@noanswer", "crash"),
            ("codex", "@@noanswer", "engine_error"), ("claude", "@@is-error", "engine_error"),
        ]
        # One at a time: a rate-limited child pauses its engine for the next spawn.
        for i, (engine, directive, cls) in enumerate(cases):
            name = "f%d" % i
            self.spawn(engine, name, directive)
            res = self.wait_result(name, timeout=30)
            self.ok("unpause", engine)
            self.assertEqual(res["status"], "error", (engine, directive))
            self.assertEqual(res["failure_class"], cls, (engine, directive, res.get("reason")))
            self.assertTrue(res.get("reason"), (engine, directive))
            self.assertEqual(res["retries"], [])
            self.assertEqual(res["attempts"], 1)

    def test_retries_bounded_and_backoff(self):
        self.spawn("claude", "x", "@@fail-once:rate-limit hello", "--retries", "2")
        res = self.wait_result("x", timeout=40)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(res["retries"][0]["failure_class"], "rate_limit")
        self.assertLessEqual(res["retries"][0]["waited_s"], 2)
        calls = self.calls("x")
        self.assertEqual(len(calls), 2)
        first = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
        second = calls[1]["argv"][calls[1]["argv"].index("--session-id") + 1]
        self.assertNotEqual(first, second)
        self.assertEqual(self.meta("x")["conversation_id"], second)
        self.assertTrue(self.has("x", "turns", "1.attempt1.json"))
        self.assertIn("retry", [e["type"] for e in self.events("x")])
        self.ok("unpause", "claude")
        # A permanent failure stops after the bound.
        self.spawn("claude", "y", "@@rate-limit", "--retries", "1")
        res = self.wait_result("y", timeout=40)
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(res["status"], "error")
        self.ok("unpause", "claude")
        # auth is never retried.
        self.spawn("claude", "z", "@@auth-fail", "--retries", "3")
        res = self.wait_result("z", timeout=20)
        self.assertEqual(res["attempts"], 1)
        self.assertEqual(res["failure_class"], "auth")

    def test_crash_retry_codex_fresh_thread(self):
        self.spawn("codex", "x", "@@fail-once:crash go", "--retries", "1")
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(res["retries"][0]["waited_s"], 5)
        calls = self.calls("x")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("resume", calls[1]["argv"])

    def test_retry_respects_deadline(self):
        self.spawn("claude", "x", "@@usage-limit:60", "--deadline", "8", "--retries", "3")
        start = time.time()
        res = self.wait_result("x", timeout=20)
        self.assertLess(time.time() - start, 12)
        self.assertLessEqual(res["attempts"], 2)
        self.assertEqual(res["failure_class"], "rate_limit")
        self.assertIn("deadline", res["reason"])

    def test_deadline_spans_turns(self):
        self.spawn("claude", "x", "first", "--steerable", "--deadline", "8")
        self.ok("turn", "x", "1", "--timeout", "20")
        self.ok("send", "x", "@@sleep:4", "--timeout", "20")
        cp = self.run_as("send", "x", "@@sleep:4", "--timeout", "20")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        res = self.wait_result("x", timeout=20)
        self.assertEqual(res["status"], "timeout")
        self.assertEqual(res["failure_class"], "timeout")
        self.assertEqual(self.turn("x", 3)["failure_class"], "timeout")
