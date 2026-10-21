"""send bookkeeping, inbox atomicity, stop, idle, status aggregation, codex resume,
send --interrupt."""

import os
import time

from helpers import SessionTest, pid_alive


class SteerableTests(SessionTest):
    def steerable(self, engine, name, prompt, *extra, **kw):
        return self.spawn(engine, name, prompt, "--steerable", *extra, **kw)

    def test_send_right_after_spawn_gets_its_own_reply(self):
        self.steerable("claude", "x", "first @@sleep:4")
        cp = self.ok("send", "x", "second")
        self.assertIn("reply#2: second", cp.stdout)
        mid = self.queued_id(cp.stdout)
        self.assertEqual(self.turn("x", 2)["message_id"], mid)
        self.assertEqual(self.turn("x", 1)["message_id"], "initial")
        self.assertEqual(self.turn("x", 2)["answer"], "reply#2: second")
        self.assertIn("turn      : 2", cp.stdout)

    def test_send_waits_for_right_turn_under_backlog(self):
        self.steerable("claude", "x", "first @@sleep:1")
        ids = []
        for text in ("m1", "m2", "m3"):
            ids.append(self.queued_id(self.ok("send", "x", text, "--no-wait").stdout))
        cp = self.ok("send", "x", "fourth")
        self.assertIn("reply#5: fourth", cp.stdout)
        ids.append(self.queued_id(cp.stdout))
        messages = os.path.join(self.sdir("x"), "messages")
        self.assertEqual(sorted(os.listdir(messages)), sorted(ids))
        turns = []
        for mid in ids:
            with open(os.path.join(messages, mid)) as fh:
                turns.append(int(fh.read().strip()))
        self.assertEqual(turns, [2, 3, 4, 5])

    def test_inbox_atomic_and_dotfiles_ignored(self):
        self.steerable("claude", "x", "first")
        junk = os.path.join(self.sdir("x"), "inbox", ".tmp-junk")
        with open(junk, "w") as fh:
            fh.write("partial write")
        for i in range(30):
            self.ok("send", "x", "msg%d" % i, "--no-wait")
        cp = self.run_as("turn", "x", "31", "--timeout", "60", timeout=90)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for k in range(2, 32):
            self.assertEqual(self.turn("x", k)["answer"], "reply#%d: msg%d" % (k, k - 2))
        self.assertTrue(os.path.exists(junk))
        with open(junk) as fh:
            self.assertEqual(fh.read(), "partial write")
        self.assertEqual(self.ok("turn", "x", "last").returncode, 0)

    def test_claude_resume_uses_same_session_id(self):
        self.steerable("claude", "x", "first")
        self.ok("send", "x", "second")
        calls = self.calls("x")
        self.assertEqual(len(calls), 2)
        first, second = calls[0]["argv"], calls[1]["argv"]
        sid = first[first.index("--session-id") + 1]
        self.assertEqual(sid, self.meta("x")["conversation_id"])
        self.assertIn("--resume", second)
        self.assertEqual(second[second.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", second)
        self.assertIn("--model", second)
        conv = os.path.join(self.fake_home, "sessions", "claude-%s.json" % sid)
        with open(conv) as fh:
            self.assertEqual(len(__import__("json").load(fh)), 2)

    def test_codex_resume_by_thread_id_with_model_and_skip_check(self):
        self.steerable("codex", "x", "first", "--model", "m1")
        self.ok("send", "x", "second")
        calls = self.calls("x")
        self.assertEqual(len(calls), 2)
        tid = self.meta("x")["conversation_id"]
        self.assertTrue(tid)
        second = calls[1]["argv"]
        self.assertEqual(second[:3], ["exec", "resume", tid])
        self.assertEqual(second[second.index("-m") + 1], "m1")
        self.assertIn("--skip-git-repo-check", second)
        self.assertEqual(second[-1], "-")
        for call in calls:
            self.assertNotIn("--last", call["argv"])
        self.assertEqual(self.turn("x", 2)["answer"], "reply#2: second")
        self.assertEqual(self.turn("x", 2)["session_id"], tid)

    def test_codex_thread_id_fallback_from_rollout(self):
        self.steerable("codex", "x", "first @@no-thread-event")
        self.wait_until(lambda: self.has("x", "turns", "1.json"))
        log = self.ok("log", "x").stdout
        self.assertIn("recovered from rollout", log)
        tid = self.meta("x")["conversation_id"]
        self.assertTrue(tid)
        self.ok("send", "x", "second")
        self.assertEqual(self.calls("x")[1]["argv"][:3], ["exec", "resume", tid])

    def test_codex_without_thread_id_fails_send_clearly(self):
        self.steerable("codex", "x", "first @@no-thread-event @@no-rollout")
        self.wait_until(lambda: self.has("x", "turns", "1.json"))
        self.assertIsNone(self.meta("x")["conversation_id"])
        cp = self.run_as("send", "x", "second")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("no codex thread id", cp.stdout + cp.stderr)
        self.assertEqual(len(self.calls("x")), 1)

    def test_opening_reply_via_turn_and_wait_hint(self):
        """`wait` on a steerable child returns only after stop or idle; the
        opening reply is `turn <name> 1`, and wait says so."""
        self.steerable("claude", "x", "first @@sleep:2")
        cp = self.run_as("turn", "x", "1", "--timeout", "30")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("reply#1: first", cp.stdout)
        cp = self.wait("x", timeout=2)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("waiting", cp.stdout)
        self.assertIn("agent-session turn x 1", cp.stdout)
        cp = self.run_as("result", "x")
        self.assertEqual(cp.returncode, 3)
        self.assertIn("agent-session turn x 1", cp.stdout)
        spawn_out = self.spawn("claude", "y", "hi", "--steerable").stdout
        self.assertIn("agent-session turn y 1 --timeout", spawn_out)

    def test_send_message_starting_with_dashes(self):
        self.steerable("claude", "x", "first")
        cp = self.ok("send", "x", "--version is a word here, not an option")
        self.assertIn("reply#2: --version is a word here", cp.stdout)
        cp = self.run_as("send", "x", "--bogus-option")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("unknown option", cp.stderr)

    def test_stop_ends_after_current_turn(self):
        self.steerable("claude", "x", "first")
        self.wait_until(lambda: self.has("x", "turns", "1.json"))
        self.ok("send", "x", "slow @@sleep:3", "--no-wait")
        cp = self.ok("stop", "x")
        self.assertIn("after its current turn and any queued messages", cp.stdout)
        cp = self.run_as("send", "x", "late")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("any queued messages", cp.stderr)
        cp = self.wait("x")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        res = self.result("x")
        self.assertEqual(res["ended"], "stopped")
        self.assertEqual(res["turns"], 2)
        self.assertEqual(res["status"], "ok")
        cp = self.run_as("send", "x", "after")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("has finished", cp.stderr)

    def test_idle_exit(self):
        self.steerable("claude", "x", "first", env={"AGENT_SESSIONS_IDLE": "2"})
        res = self.wait_result("x", timeout=6)
        self.assertEqual(res["ended"], "idle")
        self.assertEqual(res["status"], "ok")
        self.spawn("claude", "y", "hi", env={"AGENT_SESSIONS_MAX": "1"})

    def test_failed_turn_makes_status_error(self):
        self.steerable("claude", "x", "first")
        cp = self.run_as("send", "x", "crash @@exit:2 @@noanswer")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("status    : error", cp.stdout)
        self.ok("stop", "x")
        cp = self.wait("x")
        self.assertEqual(cp.returncode, 1)
        res = self.result("x")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failed_turns"], [{"turn": 2, "status": "error"}])
        self.assertIsNone(res["answer"])
        self.assertEqual(res["turns"], 2)
        self.assertEqual(res["exit_code"], 2)

    def test_turn_timeout_marks_turn_and_session(self):
        self.steerable("claude", "x", "first", "--timeout", "5")
        start = time.time()
        cp = self.run_as("send", "x", "@@hang")
        elapsed = time.time() - start
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("timeout", cp.stdout)
        self.assertGreaterEqual(elapsed, 4.5)
        self.assertLess(elapsed, 20)
        self.assertTrue(self.tmux_alive("x"))
        cp = self.ok("send", "x", "next")
        self.assertIn("reply#3: next", cp.stdout)
        self.ok("stop", "x")
        self.assertEqual(self.wait("x").returncode, 1)
        res = self.result("x")
        self.assertEqual(res["status"], "error")
        self.assertIn({"turn": 2, "status": "timeout"}, res["failed_turns"])

    def test_send_rejections(self):
        self.spawn("claude", "one", "hi @@sleep:3")
        cp = self.run_as("send", "one", "hi")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--steerable", cp.stderr)
        self.steerable("claude", "done", "first")
        self.ok("stop", "done")
        self.assertEqual(self.wait("done").returncode, 0)
        cp = self.run_as("send", "done", "hi")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("has finished", cp.stderr)
        self.fake_meta("dead", steerable=True)
        cp = self.run_as("send", "dead", "hi")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no longer running", cp.stderr)
        cp = self.run_as("send", "one")
        self.assertEqual(cp.returncode, 2)

    def test_send_pending_then_turn_by_message(self):
        self.steerable("claude", "x", "first")
        self.wait_until(lambda: self.has("x", "turns", "1.json"))
        cp = self.run_as("send", "x", "slow @@sleep:6", "--timeout", "1")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        mid = self.queued_id(cp.stdout)
        self.assertIn("pending", cp.stdout)
        cp = self.run_as("turn", "x", "--message", mid)
        self.assertEqual(cp.returncode, 3)
        cp = self.run_as("turn", "x", "--message", mid, "--timeout", "30")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("reply#2", cp.stdout)

    def test_kill_mid_turn_steerable(self):
        self.steerable("claude", "x", "first")
        self.wait_until(lambda: self.has("x", "turns", "1.json"))
        self.ok("send", "x", "@@hang", "--no-wait")
        self.wait_until(lambda: self.has("x", "engine.pid") and self.read("x", "engine.pid")["turn"] == 2)
        pid = self.read("x", "engine.pid")["pid"]
        cp = self.ok("kill", "x")
        self.assertIn("terminated", cp.stdout)
        res = self.result("x")
        self.assertEqual(res["status"], "killed")
        self.assertEqual(res["turns"], 2)
        self.assertEqual(self.turn("x", 2)["status"], "killed")
        self.assertFalse(pid_alive(pid))

    def test_send_interrupt(self):
        """--interrupt runs the INT/TERM/KILL ladder on the running turn, which
        ends as killed; the message then runs as the next turn and the session
        keeps going. An interrupted turn does not decide the session's status:
        it is listed in interrupted_turns, not failed_turns."""
        self.steerable("claude", "x", "start")
        self.ok("turn", "x", "1", "--timeout", "20")
        self.ok("send", "x", "@@hang", "--no-wait")
        self.wait_until(lambda: (self.progress("x").get("phase") == "turn" and self.progress("x").get("turn") == 2),
                        message="turn 2 not running")
        cp = self.ok("send", "x", "next", "--interrupt", "--timeout", "30")
        self.assertIn("interrupt : turn 2 is being stopped", cp.stdout)
        self.assertIn("reply#3: next", cp.stdout)
        self.assertEqual(self.turn("x", 2)["status"], "killed")
        self.assertTrue(self.turn("x", 2).get("interrupted"))
        self.assertEqual(self.signals("x")[0]["signal"], "SIGINT")
        # The session is still usable after the interrupt.
        cp = self.ok("send", "x", "after", "--timeout", "30")
        self.assertIn("reply#4: after", cp.stdout)
        self.ok("stop", "x")
        res = self.wait_result("x", timeout=20)
        self.assertEqual(res["failed_turns"], [])
        self.assertEqual(res["interrupted_turns"], [2])
        self.assertEqual(res["ended"], "stopped")
        self.assertEqual(res["turns"], 4)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertIsNone(res["failure_class"])
        self.assertEqual(self.wait("x").returncode, 0)

    def test_send_interrupt_when_idle_is_plain_send(self):
        """With no turn running, --interrupt has nothing to stop and the
        message is an ordinary next turn; a stale marker never hits a later turn."""
        self.steerable("claude", "x", "start")
        self.ok("turn", "x", "1", "--timeout", "20")
        cp = self.ok("send", "x", "hello", "--interrupt", "--timeout", "30")
        self.assertNotIn("interrupt :", cp.stdout)
        self.assertIn("reply#2: hello", cp.stdout)
        self.ok("stop", "x")
        res = self.wait_result("x", timeout=20)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["failed_turns"], [])
