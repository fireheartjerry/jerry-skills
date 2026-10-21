"""Scheduler: per-engine caps, the persisted queue, engine pauses after rate
limits, unpause, and kick from every command."""

import json
import os
import time

from helpers import SessionTest


class SchedulerTests(SessionTest):
    def test_per_engine_cap(self):
        env = {"AGENT_SESSIONS_MAX_CODEX": "1", "AGENT_SESSIONS_MAX": "3"}
        self.spawn("codex", "a", "@@sleep:8", env=env)
        cp = self.run_as("spawn", "codex", "b", self.work, "hi", env=env)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("AGENT_SESSIONS_MAX_CODEX", cp.stderr)
        self.spawn("claude", "c", "hi", env=env)
        self.assertEqual(self.wait_result("c")["status"], "ok")

    def test_queue_starts_when_slot_frees(self):
        env = {"AGENT_SESSIONS_MAX": "1"}
        self.spawn("claude", "a", "@@sleep:3", env=env)
        cp = self.spawn("claude", "b", "second", "--queue", env=env)
        self.assertIn("queued", cp.stdout)
        self.assertIn("queued", self.ok("list", env=env).stdout)
        status = json.loads(self.run_as("status", "b", "--json", env=env).stdout)
        self.assertEqual(status["queue"]["reason"], "cap")
        self.assertEqual(status["queue"]["position"], 1)
        # a's runner kicks b when it finishes; only `wait b` is needed.
        cp = self.wait("b", timeout=30, env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.result("b")["status"], "ok")
        self.assertIsNone(self.meta("b")["queue"])

    def test_queued_child_is_never_lost_and_can_be_killed(self):
        env = {"AGENT_SESSIONS_MAX": "1"}
        self.spawn("claude", "a", "@@sleep:20", env=env)
        self.spawn("claude", "b", "hi", "--queue", env=env)
        cp = self.wait("b", timeout=2, env=env)
        self.assertEqual(cp.returncode, 3)
        self.assertIn("queued", cp.stdout)
        cp = self.run_as("reap", "b", env=env)
        self.assertIn("queued", cp.stdout)
        cp = self.ok("kill", "b", env=env)
        res = self.result("b")
        self.assertEqual(res["status"], "killed")
        self.assertEqual(self.calls("b"), [])
        self.ok("kill", "a", env=env)

    def test_pause_on_rate_limit(self):
        self.spawn("codex", "x", "@@usage-limit:120")
        res = self.wait_result("x")
        self.assertEqual(res["failure_class"], "rate_limit")
        pause = self.pause("codex")
        self.assertIsNotNone(pause)
        self.assertTrue(115 <= pause["until_epoch"] - time.time() <= 130, pause)
        self.assertIn("paused: codex until", self.ok("list").stdout)
        cp = self.run_as("spawn", "codex", "y", self.work, "hi")
        self.assertEqual(cp.returncode, 2)
        self.assertRegex(cp.stderr, r"codex is paused until \d\d:\d\d after a usage limit; pass --queue to wait, "
                                    r"pick another engine, or run: agent-session unpause codex")
        cp = self.spawn("codex", "z", "hi", "--queue")
        self.assertIn("pause", cp.stdout)
        self.assertEqual(self.meta("z")["queue"]["reason"], "pause")
        cp = self.ok("unpause", "codex")
        self.assertIn("started", cp.stdout)
        self.assertEqual(self.wait_result("z")["status"], "ok")
        # an expired pause is ignored by spawn
        os.makedirs(os.path.join(self.state, "pauses"), exist_ok=True)
        with open(os.path.join(self.state, "pauses", "codex.json"), "w") as fh:
            json.dump({"until_epoch": time.time() - 5, "reason": "old"}, fh)
        self.spawn("codex", "w", "hi")
        self.assertEqual(self.wait_result("w")["status"], "ok")

    def test_kick_runs_from_wait_and_list(self):
        os.makedirs(os.path.join(self.state, "pauses"), exist_ok=True)
        with open(os.path.join(self.state, "pauses", "claude.json"), "w") as fh:
            json.dump({"until_epoch": time.time() + 600, "reason": "usage limit"}, fh)
        self.spawn("claude", "q", "hi", "--queue")
        self.assertEqual(self.meta("q")["queue"]["reason"], "pause")
        os.unlink(os.path.join(self.state, "pauses", "claude.json"))
        self.ok("list")
        self.wait_until(lambda: self.meta("q")["queue"] is None, timeout=10, message="queued child not started")
        self.assertEqual(self.wait_result("q")["status"], "ok")
