"""The process backend: the runner as a detached process group with its
output in pane.log, used when tmux is missing or AGENT_SESSIONS_BACKEND=process."""

import os
import signal
import time

from helpers import SessionTest, pid_alive


class ProcessBackendTests(SessionTest):
    def setUp(self):
        SessionTest.setUp(self)
        self.env["PATH"] = self.restricted_path(tmux=False)
        self.set_backend("process")

    def test_process_backend_without_tmux(self):
        cp = self.spawn("claude", "x", "hello @@slow:4")
        self.assertIn("pane.log", cp.stdout)
        self.assertNotIn("attach", cp.stdout)
        self.assertEqual(self.meta("x")["backend"], "process")
        self.assertIsNone(self.meta("x")["tmux_socket"])
        self.assertIn("running", self.ok("list").stdout)
        self.wait_until(lambda: "turn 1" in (self.run_as("peek", "x").stdout or ""), timeout=10,
                        message="peek shows no pane output")
        cp = self.wait("x", timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("reply#1: hello", cp.stdout)
        self.assertTrue(self.has("x", "pane.log"))
        # No linger: the runner exits right after the result (and hooks).
        runner = self.progress("x")["runner_pid"]
        self.wait_until(lambda: not pid_alive(runner), timeout=10, message="process runner lingered")
        self.spawn("claude", "h", "@@hang")
        self.wait_until(lambda: self.has("h", "engine.pid"), message="engine not started")
        pid = self.read("h", "engine.pid")["pid"]
        cp = self.ok("kill", "h")
        self.assertIn("terminated", cp.stdout)
        self.assertIn("wrote result", cp.stdout)
        self.assertFalse(pid_alive(pid))
        self.assertEqual(self.result("h")["status"], "killed")
        cp = self.ok("doctor")
        self.assertIn("ok    backend: process (tmux not found)", cp.stdout)
        self.assertIn("warn  tmux: not found", cp.stdout)
        cp = self.run_as("doctor", "--backend", "tmux")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("FAIL  tmux", cp.stdout)
        cp = self.run_as("spawn", "claude", "t", self.work, "hi", "--backend", "tmux")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("tmux not found", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("t")))

    def test_fallback_notice_without_setting(self):
        """No AGENT_SESSIONS_BACKEND and no tmux: spawn falls back to the
        process backend with a one-line notice; doctor agrees and passes."""
        env = {"AGENT_SESSIONS_BACKEND": None}
        cp = self.spawn("claude", "x", "hi", env=env)
        self.assertIn("using the process backend", cp.stderr)
        self.assertEqual(self.meta("x")["backend"], "process")
        self.assertEqual(self.wait("x", env=env).returncode, 0)
        cp = self.ok("doctor", env=env)
        self.assertIn("backend: process (tmux not found)", cp.stdout)

    def test_process_backend_steerable(self):
        self.spawn("claude", "s", "open", "--steerable")
        self.ok("turn", "s", "1", "--timeout", "20")
        cp = self.ok("send", "s", "second", "--timeout", "30")
        self.assertIn("reply#2: second", cp.stdout)
        self.ok("stop", "s")
        res = self.wait_result("s", timeout=20)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["turns"], 2)

    def test_process_backend_lost_detection(self):
        self.spawn("claude", "x", "work @@on-turn:1:hang")
        self.wait_until(lambda: self.has("x", "engine.pid"), message="engine not started")
        self.wait_session("x")
        info = self.read("x", "engine.pid")
        os.kill(self.progress("x")["runner_pid"], signal.SIGKILL)
        os.killpg(info["pgid"], signal.SIGKILL)
        self.wait_until(lambda: not pid_alive(info["pid"]), timeout=5)
        cp = self.wait("x", timeout=5)
        self.assertEqual(cp.returncode, 1)
        res = self.result("x")
        self.assertEqual(res["status"], "lost")
        self.assertIn("runner process gone", res["reason"])
        self.ok("resume", "x")
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["resumes"][0]["from_status"], "lost")

    def test_kill_ends_a_hung_runner(self):
        """A runner that is stopped (SIGSTOP) never writes its result; kill
        terminates the engine, then the runner itself, and writes the result."""
        self.spawn("claude", "x", "@@hang")
        self.wait_until(lambda: self.has("x", "engine.pid"), message="engine not started")
        runner = self.progress("x")["runner_pid"]
        engine = self.read("x", "engine.pid")["pid"]
        os.kill(runner, signal.SIGSTOP)
        try:
            started = time.time()
            cp = self.ok("kill", "x", "--grace", "2", timeout=60)
            self.assertIn("did not respond; terminated", cp.stdout)
            self.assertLess(time.time() - started, 30)
        finally:
            try:
                os.kill(runner, signal.SIGCONT)
            except OSError:
                pass
        self.assertFalse(pid_alive(engine))
        self.wait_until(lambda: not pid_alive(runner), timeout=10, message="runner survived kill")
        res = self.result("x")
        self.assertEqual(res["status"], "killed")
        self.assertIn("did not respond", res["reason"])
