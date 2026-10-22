"""resume and recover: continuing a lost, killed or crashed child in the same
engine conversation (reboot simulations), the refusals, and queueing under
the cap."""

import json
import os
import re
import shutil
import signal

from helpers import SessionTest, pid_alive, read_json


def line(stdout, key, name):
    """The `key : name ...` line of human output (keys are padded)."""
    for text in stdout.splitlines():
        if re.match(r"^%s\s*: %s\b" % (re.escape(key), re.escape(name)), text):
            return text
    raise AssertionError("no '%s : %s' line in %r" % (key, name, stdout))


class ResumeTests(SessionTest):
    def lose(self, name):
        """Simulate a reboot: SIGKILL the runner and the engine's process group,
        so nothing gets the chance to write a result."""
        self.wait_until(lambda: self.has(name, "engine.pid"), message="engine not started")
        self.wait_session(name)
        info = self.read(name, "engine.pid")
        prog = self.progress(name)
        os.kill(prog["runner_pid"], signal.SIGKILL)
        try:
            os.killpg(info["pgid"], signal.SIGKILL)
        except OSError:
            pass
        self.wait_until(lambda: not pid_alive(info["pid"]), timeout=5)
        self.wait_until(lambda: not self.tmux_alive(name), timeout=5)

    def init_session(self, name):
        """The session or thread id the engine reported in its first init event."""
        for ev in self.events(name):
            data = ev.get("data") or {}
            if ev["type"] == "init" and (data.get("thread_id") or data.get("session_id")):
                return data.get("thread_id") or data.get("session_id")
        raise AssertionError("no init event with a session id for %s" % name)

    def test_resume_lost_claude_and_codex(self):
        for engine in ("claude", "codex", "gemini"):
            name = "x-%s" % engine
            self.spawn(engine, name, "long job @@on-turn:1:hang")
            self.lose(name)
            cp = self.wait(name, timeout=5)
            self.assertEqual(cp.returncode, 1)
            self.assertEqual(self.result(name)["status"], "lost")
            self.assertEqual(self.result(name)["failure_class"], "lost")
            first_session = self.meta(name)["conversation_id"] if engine == "claude" else self.init_session(name)
            cp = self.ok("resume", name)
            self.assertIn("resumed", cp.stdout)
            self.assertIn(first_session, cp.stdout)
            res = self.wait_result(name, timeout=30)
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertEqual(len(res["resumes"]), 1)
            self.assertEqual(res["resumes"][0]["from_status"], "lost")
            self.assertIsNone(res["resumes"][0]["message"])
            lost = read_json(os.path.join(self.sdir(name), "attempts", "1.result.json"))
            self.assertEqual(lost["status"], "lost")
            call = self.calls(name)[-1]
            self.assertIn("You were interrupted", call["stdin"])
            self.assertIn("Re-read any file you changed", call["stdin"])
            if engine == "codex":
                self.assertEqual(call["argv"][:3], ["exec", "resume", first_session])
            else:
                self.assertEqual(call["argv"][call["argv"].index("--resume") + 1], first_session)
                self.assertNotIn("--session-id", call["argv"])
            cp = self.run_as("resume", name)
            self.assertEqual(cp.returncode, 2)
            self.assertIn("nothing to resume", cp.stderr)

    def test_resume_after_tmux_server_killed(self):
        """The literal reboot scenario for the tmux backend: the server dies.
        tmux hangs up the runner, which records killed (SIGHUP); resume
        continues the same conversation."""
        self.spawn("claude", "x", "long job @@on-turn:1:hang")
        self.wait_until(lambda: self.has("x", "engine.pid"), message="engine not started")
        self.wait_session("x")
        engine = self.read("x", "engine.pid")["pid"]
        sid = self.meta("x")["conversation_id"]
        self.tmux("kill-server")
        self.wait_until(lambda: not pid_alive(engine), timeout=20, message="engine survived the tmux server")
        cp = self.wait("x", timeout=20)
        self.assertEqual(cp.returncode, 1)
        res = self.result("x")
        self.assertIn(res["status"], ("killed", "lost"), res)
        self.ok("resume", "x")
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        call = self.calls("x")[-1]
        self.assertEqual(call["argv"][call["argv"].index("--resume") + 1], sid)

    def test_resume_with_message_and_crash(self):
        self.spawn("claude", "x", "@@exit:3")
        res = self.wait_result("x")
        self.assertEqual(res["failure_class"], "crash")
        cp = self.ok("resume", "x", "--message", "try once more", "--json")
        doc = json.loads(cp.stdout)
        self.assertEqual(doc["state"], "resumed")
        res = self.wait_result("x", timeout=20)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.calls("x")[-1]["stdin"], "try once more")
        self.assertEqual(res["turns"], 2)
        self.assertEqual(res["resumes"][0]["message"], "try once more")
        self.assertEqual(res["resumes"][0]["from_status"], "error")

    def test_resume_refusals(self):
        """auth, budget, schema, verify and dependency failures are fixed at
        the cause, not resumed; no session id or a missing workdir means a
        fresh spawn. Every refusal exits 2 and leaves the result untouched."""
        self.spawn("claude", "auth", "@@auth-fail")
        self.spawn("claude", "bud", "@@tokens:90,20", "--max-tokens", "100")
        self.spawn("claude", "ver", "x", "--verify", "false", "--verify-fixups", "0")
        schema = self.write_file("s.json", json.dumps({"type": "object", "required": ["n"]}))
        self.spawn("claude", "sch", "not json", "--schema", schema)
        self.spawn("claude", "up", "@@exit:1")
        self.spawn("claude", "dep", "never", "--after", "up")
        expected = {"auth": ("auth", "login"), "bud": ("budget", "budget"), "ver": ("verify", "verify"),
                    "sch": ("schema", "schema"), "dep": ("dependency", "upstream")}
        for name, (cls, word) in sorted(expected.items()):
            res = self.wait_result(name, timeout=30)
            self.assertEqual(res["failure_class"], cls, (name, res.get("reason")))
            with open(os.path.join(self.sdir(name), "result.json")) as fh:
                before = fh.read()
            cp = self.run_as("resume", name)
            self.assertEqual(cp.returncode, 2, (name, cp.stdout, cp.stderr))
            self.assertIn(word, cp.stderr)
            with open(os.path.join(self.sdir(name), "result.json")) as fh:
                self.assertEqual(fh.read(), before)
            self.assertFalse(self.has(name, "attempts"))
        cp = self.run_as("resume", "dep", "--json")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(json.loads(cp.stdout)["state"], "refused")
        # No session id: a lost codex child that never reported a thread.
        self.fake_meta("nosid", engine="codex", conversation_id=None)
        self.assertEqual(self.wait("nosid", timeout=5).returncode, 1)
        cp = self.run_as("resume", "nosid")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no session id", cp.stderr)
        # Workdir gone.
        gone = os.path.join(self.box, "gone")
        os.makedirs(gone)
        self.spawn("claude", "wd", "work @@on-turn:1:hang", directory=gone)
        self.lose("wd")
        self.assertEqual(self.wait("wd", timeout=5).returncode, 1)
        shutil.rmtree(gone)
        cp = self.run_as("resume", "wd")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no longer exists", cp.stderr)
        # A running child is not resumed.
        self.spawn("claude", "run", "@@sleep:10")
        cp = self.run_as("resume", "run")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("still running", cp.stderr)
        self.ok("kill", "run")
        self.assertEqual(self.run_as("resume", "nobody").returncode, 2)

    def test_resume_queued_under_cap(self):
        env = {"AGENT_SESSIONS_MAX": "1"}
        self.spawn("claude", "x", "work @@on-turn:1:hang", env=env)
        self.lose("x")
        self.assertEqual(self.wait("x", timeout=5, env=env).returncode, 1)
        self.spawn("claude", "busy", "@@sleep:3", env=env)
        cp = self.ok("resume", "x", env=env)
        self.assertIn("queued", cp.stdout)
        self.assertIn("cap", cp.stdout)
        self.assertFalse(self.has("x", "result.json"))
        cp = self.run_as("status", "x", "--json", env=env)
        self.assertEqual(cp.returncode, 3)
        self.assertEqual(json.loads(cp.stdout)["state"], "queued")
        self.assertIn("queued", self.ok("list", env=env).stdout)
        res = self.wait_result("x", timeout=40)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(len(res["resumes"]), 1)
        self.assertEqual(self.calls("x")[-1]["argv"][self.calls("x")[-1]["argv"].index("--resume") + 1],
                         self.meta("x")["conversation_id"])

    def turn2_init(self, name):
        path = os.path.join(self.state, name, "events.jsonl")
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as fh:
            return any(json.loads(line).get("turn") == 2 and json.loads(line).get("type") == "init"
                       for line in fh if line.strip())

    def test_resume_steerable_continues_conversation(self):
        """A lost steerable child resumes into its waiting loop; --message is
        the next turn and later sends keep working."""
        self.spawn("claude", "s", "open", "--steerable")
        self.ok("turn", "s", "1", "--timeout", "20")
        self.ok("send", "s", "@@hang", "--no-wait")
        self.wait_until(lambda: self.has("s", "engine.pid") and self.read("s", "engine.pid").get("turn") == 2,
                        message="turn 2 not started")
        # engine.pid appears at Popen; the engine has stored the turn-2 prompt
        # in its conversation only once it emits init. Losing it earlier makes
        # the resumed prompt number 2 instead of 3.
        self.wait_until(lambda: self.turn2_init("s"), message="turn 2 engine never initialised")
        self.lose("s")
        self.assertEqual(self.wait("s", timeout=5).returncode, 1)
        self.ok("resume", "s", "--message", "carry on")
        cp = self.ok("turn", "s", "2", "--timeout", "30")
        self.assertIn("reply#3: carry on", cp.stdout)
        cp = self.ok("send", "s", "more", "--timeout", "30")
        self.assertIn("more", cp.stdout)
        self.ok("stop", "s")
        res = self.wait_result("s", timeout=20)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["resumes"][0]["message"], "carry on")
        sid = self.meta("s")["conversation_id"]
        for call in self.calls("s")[1:]:
            self.assertEqual(call["argv"][call["argv"].index("--resume") + 1], sid)

    def test_recover_all_and_dry_run(self):
        env = {"AGENT_SESSIONS_MAX": "1"}
        self.spawn("claude", "c1", "work @@on-turn:1:hang", env=env)
        self.lose("c1")
        self.fake_meta("c2", engine="codex", conversation_id=None, owner="env:test-owner")
        self.spawn("claude", "busy", "@@sleep:4", env=env)
        cp = self.ok("recover", "--dry-run", env=env)
        line(cp.stdout, "would resume", "c1")
        self.assertIn("skipped", cp.stdout)
        self.assertIn("no session id", cp.stdout)
        self.assertFalse(self.has("c1", "attempts"))
        cp = self.ok("recover", "--dry-run", "--json", env=env)
        rows = dict((r["name"], r["state"]) for r in json.loads(cp.stdout)["children"])
        self.assertEqual(rows, {"c1": "would resume", "c2": "skipped"})
        cp = self.ok("recover", env=env)
        self.assertIn("cap", line(cp.stdout, "queued", "c1"))
        self.assertIn("no session id", line(cp.stdout, "skipped", "c2"))
        self.assertIn("respawn", cp.stdout)
        self.assertIn("re-read", cp.stdout)
        res = self.wait_result("c1", timeout=40)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(self.result("c2")["status"], "lost")
        self.assertEqual(self.result("busy")["status"], "ok")
        cp = self.ok("recover", env=env)
        line(cp.stdout, "skipped", "c2")
        self.assertNotIn("c1", cp.stdout)

    def test_recover_scope_mine_and_all(self):
        other = {"AGENT_SESSIONS_OWNER": "someone-else"}
        self.spawn("claude", "theirs", "work @@on-turn:1:hang", env=other)
        self.lose("theirs")
        cp = self.ok("recover")
        self.assertIn("nothing to recover", cp.stdout)
        self.assertFalse(self.has("theirs", "attempts"))
        cp = self.ok("recover", "--all", "--json")
        doc = json.loads(cp.stdout)
        self.assertFalse(doc["dry_run"])
        self.assertEqual([(r["name"], r["state"]) for r in doc["children"]], [("theirs", "resumed")])
        res = self.wait_result("theirs", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
