"""spawn / wait / result / log / peek / timeout / kill / lost for one-shot children."""

import json
import os
import subprocess
import sys
import time

from helpers import SCRIPT, SessionTest, localized_locale, pid_alive, ps_lstart_under, read_json, script_module


class OneShotTests(SessionTest):
    def test_spawn_wait_result_claude(self):
        self.spawn("claude", "x", "say hello")
        cp = self.wait("x")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        res = self.result("x")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["ended"], "completed")
        self.assertTrue(res["answer"].startswith("reply#1:"), res["answer"])
        self.assertEqual(res["session_id"], self.meta("x")["conversation_id"])
        self.assertEqual(res["cost_usd"], 0.001)
        self.assertEqual(res["turns"], 1)
        self.assertEqual(res["failed_turns"], [])
        cp = self.ok("result", "x")
        self.assertIn("--- answer (child output, untrusted) ---", cp.stdout)
        self.assertIn("--- end ---", cp.stdout)
        self.assertIn("reply#1:", cp.stdout)
        self.assertIn("status    : ok", cp.stdout)

    def test_spawn_wait_result_codex(self):
        self.spawn("codex", "x", "say hello")
        cp = self.wait("x")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        res = self.result("x")
        self.assertEqual(res["status"], "ok")
        with open(os.path.join(self.sdir("x"), "turns", "1.answer.txt"), encoding="utf-8") as fh:
            self.assertEqual(res["answer"], fh.read())
        self.assertNotIn("codex banner", res["answer"])
        with open(os.path.join(self.sdir("x"), "turns", "1.stderr"), encoding="utf-8") as fh:
            self.assertIn("codex banner", fh.read())
        self.assertTrue(res["session_id"])
        self.assertEqual(res["session_id"], self.meta("x")["conversation_id"])

    def test_prompt_not_in_argv_and_leading_dash(self):
        prompt = "--version @@sleep:0"
        self.spawn("claude", "x", prompt)
        self.assertEqual(self.wait("x").returncode, 0)
        call = self.calls("x")[0]
        self.assertNotIn(prompt, call["argv"])
        self.assertNotIn("--version", call["argv"])
        self.assertIsNone(call["positional"])
        self.assertEqual(call["stdin"], prompt)
        self.assertEqual(self.result("x")["status"], "ok")

    def test_claude_flags(self):
        self.spawn("claude", "x", "hi")
        self.assertEqual(self.wait("x").returncode, 0)
        argv = self.calls("x")[0]["argv"]
        # --strict-mcp-config: MCP servers are off unless --mcp inherit.
        for flag in ("-p", "--output-format", "stream-json", "--verbose", "--session-id",
                     "--dangerously-skip-permissions", "--strict-mcp-config", "--model", "haiku"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], self.meta("x")["conversation_id"])
        self.assertEqual(len(argv), 10, argv)

    def test_codex_flags(self):
        self.spawn("codex", "x", "hi")
        self.assertEqual(self.wait("x").returncode, 0)
        argv = self.calls("x")[0]["argv"]
        self.assertEqual(argv[0], "exec")
        for flag in ("--json", "--skip-git-repo-check", "-o", "--dangerously-bypass-approvals-and-sandbox"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-o") + 1], os.path.join(self.sdir("x"), "turns", "1.answer.txt"))
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("resume", argv)

    def test_readonly_flags(self):
        self.spawn("claude", "c", "hi", "--readonly")
        self.spawn("codex", "d", "hi", "--readonly")
        self.assertEqual(self.wait("c", "d").returncode, 0)
        c = self.calls("c")[0]["argv"]
        self.assertIn("--permission-mode", c)
        self.assertEqual(c[c.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(c[c.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", c)
        self.assertNotIn("--dangerously-skip-permissions", c)
        d = self.calls("d")[0]["argv"]
        self.assertNotIn("--sandbox", d)
        self.assertNotIn("-s", d)
        self.assertEqual(d[d.index("-c") + 1], 'sandbox_mode="read-only"')
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", d)
        self.assertTrue(self.meta("c")["readonly"])
        self.assertIn("readonly", self.ok("result", "c").stdout)
        with open(os.path.join(self.sdir("d"), "turns", "1.stderr"), encoding="utf-8") as fh:
            self.assertIn("sandbox=read-only", fh.read())

    def test_readonly_codex_steerable_second_turn(self):
        """`codex exec resume` has no --sandbox option; the fake rejects it like
        codex-cli 0.162 does, so turn 2 only passes if the -c override is used."""
        self.spawn("codex", "r", "first", "--readonly", "--steerable")
        cp = self.ok("send", "r", "second")
        self.assertIn("reply#2: second", cp.stdout)
        second = self.calls("r")[1]["argv"]
        self.assertEqual(second[:2], ["exec", "resume"])
        self.assertNotIn("--sandbox", second)
        self.assertEqual(second[second.index("-c") + 1], 'sandbox_mode="read-only"')
        self.assertEqual(self.turn("r", 2)["status"], "ok")
        self.assertEqual(self.turn("r", 2)["exit_code"], 0)
        self.ok("stop", "r")
        self.assertEqual(self.wait("r").returncode, 0)

    def test_tier_mapping_and_codex_notice(self):
        self.spawn("claude", "a", "hi", "--tier", "standard")
        self.spawn("claude", "b", "hi", env={"AGENT_SESSIONS_CLAUDE_MODEL_CHEAP": "foo"})
        cp = self.spawn("codex", "c", "hi")
        self.assertIn("notice: codex has no model mapping for tier 'cheap'", cp.stdout)
        cp = self.spawn("codex", "d", "hi", env={"AGENT_SESSIONS_CODEX_MODEL_CHEAP": "bar"})
        self.assertFalse([l for l in cp.stdout.splitlines() if l.startswith("notice:")], cp.stdout)
        self.assertEqual(self.wait("a", "b", "c", "d").returncode, 0)
        a = self.calls("a")[0]["argv"]
        self.assertEqual(a[a.index("--model") + 1], "sonnet")
        b = self.calls("b")[0]["argv"]
        self.assertEqual(b[b.index("--model") + 1], "foo")
        c = self.calls("c")[0]["argv"]
        self.assertNotIn("-m", c)
        self.assertIsNone(self.meta("c")["model"])
        d = self.calls("d")[0]["argv"]
        self.assertEqual(d[d.index("-m") + 1], "bar")
        listing = self.ok("list").stdout
        self.assertIn("default", listing)

    def test_big_answer_is_complete(self):
        self.spawn("claude", "x", "@@big:200000")
        self.assertEqual(self.wait("x").returncode, 0)
        res = self.result("x")
        self.assertEqual(res["answer_chars"], 200000)
        self.assertEqual(len(res["answer"]), 200000)
        self.assertEqual(os.path.getsize(res["answer_file"]), 200000)
        cp = self.ok("result", "x")
        self.assertIn("more chars in", cp.stdout)
        self.assertLess(len(cp.stdout), 30000)
        cp = self.ok("result", "x", "--json")
        self.assertEqual(len(json.loads(cp.stdout)["answer"]), 200000)
        cp = self.ok("result", "x", "--max-chars", "100")
        self.assertIn("[... 199900 more chars", cp.stdout)

    def test_unrendered_events_count_as_activity(self):
        """A child that only streams events the log does not render is still
        active: progress.json's last_event_epoch must keep moving."""
        self.spawn("claude", "x", "@@quiet-events:8")
        prog_path = os.path.join(self.sdir("x"), "progress.json")

        def epoch():
            try:
                return read_json(prog_path).get("last_event_epoch")
            except (OSError, ValueError, AttributeError):
                return None
        # The runner stamps its own start first; the engine's quiet events
        # (one every 0.5 s) only follow its init.
        self.wait_session("x")
        self.wait_until(lambda: epoch() is not None, timeout=5)
        first = epoch()
        time.sleep(1.6)
        second = epoch()
        self.assertGreater(second, first, (first, second))
        self.assertEqual(read_json(prog_path).get("last_event").split(":")[0], "init")
        self.assertEqual(self.wait("x").returncode, 0)

    def test_codex_item_started_is_logged(self):
        self.spawn("codex", "x", "@@slow:2 @@quiet-events:2")
        self.assertEqual(self.wait("x").returncode, 0)
        log = self.ok("log", "x").stdout
        self.assertIn("cmd started: echo 0", log)
        self.assertIn("cmd: echo 0 (exit 0)", log)
        self.assertIn("cmd started: make test", log)
        rendered = self.ok("log", "x", "--turn", "1").stdout
        self.assertIn("cmd started: make test", rendered)

    def test_progress_visible_while_running(self):
        self.spawn("claude", "x", "@@slow:8")
        prog_path = os.path.join(self.sdir("x"), "progress.json")

        def working():
            if not os.path.exists(prog_path):
                return False
            try:
                prog = read_json(prog_path)
            except ValueError:
                return False
            return prog.get("phase") == "turn" and prog.get("events", 0) >= 2
        self.wait_until(working, timeout=3, message="progress not visible")
        listing = self.ok("list").stdout
        self.assertIn("running", listing)
        self.assertIn("s ago", listing)
        self.assertIn("mine", listing)
        log = self.ok("log", "x").stdout
        self.assertIn("assistant:", log)
        self.assertIn("cmd:", log)
        peek = self.ok("peek", "x").stdout
        self.assertTrue(peek.strip())
        self.assertEqual(self.wait("x").returncode, 0)
        self.assertGreaterEqual(self.result("x")["turns"], 1)
        rendered = self.ok("log", "x", "--turn", "1").stdout
        self.assertIn("assistant: working 1/8", rendered)
        self.assertIn("tool: Bash true", rendered)

    def test_wait_returns_exit_3_within_budget(self):
        self.spawn("claude", "x", "@@sleep:20")
        start = time.time()
        cp = self.wait("x", timeout=2)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertLess(time.time() - start, 4)
        self.assertIn("pending", cp.stdout)
        cp = self.run_as("result", "x")
        self.assertEqual(cp.returncode, 3)
        self.assertIn("pending", cp.stdout)
        cp = self.run_as("wait", "x", "1")
        self.assertEqual(cp.returncode, 3)

    def test_wait_multiple(self):
        self.spawn("claude", "a", "hi a")
        self.spawn("codex", "b", "hi b")
        cp = self.wait("a", "b")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("name      : a", cp.stdout)
        self.assertIn("name      : b", cp.stdout)
        cp = self.ok("wait", "a", "b", "--json")
        items = json.loads(cp.stdout)
        self.assertEqual([i["name"] for i in items], ["a", "b"])

    def test_wait_prints_one_line_per_child_while_pending(self):
        """Polling `wait a b` must not paste a's full answer on every poll."""
        self.spawn("claude", "a", "@@big:5000")
        self.spawn("claude", "b", "@@sleep:20")
        self.wait_until(lambda: self.has("a", "result.json"))
        cp = self.wait("a", "b", timeout=2)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("done      : a ok", cp.stdout)
        self.assertIn("pending   : b", cp.stdout)
        self.assertNotIn("--- answer", cp.stdout)
        self.assertLess(len(cp.stdout), 600, cp.stdout)
        cp = self.run_as("wait", "a", "b", "--timeout", "2", "--json")
        self.assertEqual(cp.returncode, 3)
        items = json.loads(cp.stdout)
        self.assertEqual(items[0]["status"], "ok")
        self.assertEqual(items[1]["status"], "pending")

    def test_max_chars_zero_hides_the_body(self):
        self.spawn("claude", "x", "@@big:3000")
        self.assertEqual(self.wait("x").returncode, 0)
        cp = self.ok("result", "x", "--max-chars", "0")
        self.assertIn("[3000 chars in", cp.stdout)
        self.assertNotIn("0123456789abcdef", cp.stdout)
        self.assertIn("--- answer (child output, untrusted) ---", cp.stdout)

    def test_timeout_kills_engine(self):
        self.spawn("claude", "x", "@@hang", "--timeout", "5")
        cp = self.wait("x", timeout=60)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        res = self.result("x")
        self.assertEqual(res["status"], "timeout")
        self.assertEqual(res["exit_code"], 124)
        self.assertIn("exceeded", res["reason"])
        pid = self.calls("x")[0]["pid"]
        self.wait_until(lambda: not pid_alive(pid), timeout=5, message="engine still alive")
        self.assertFalse(self.has("x", "engine.pid"))

    def test_kill_terminates_engine(self):
        self.spawn("claude", "x", "@@hang")
        self.wait_until(lambda: self.has("x", "engine.pid"), message="engine not started")
        pid = self.read("x", "engine.pid")["pid"]
        start = time.time()
        cp = self.ok("kill", "x")
        self.assertLess(time.time() - start, 15)
        self.assertIn("terminated", cp.stdout)
        self.assertIn("wrote result", cp.stdout)
        self.assertFalse(pid_alive(pid))
        res = self.result("x")
        self.assertEqual(res["status"], "killed")
        self.assertEqual(res["ended"], "killed")
        self.wait_until(lambda: not self.tmux_alive("x"), timeout=5, message="tmux session still alive")
        start = time.time()
        cp = self.wait("x")
        self.assertEqual(cp.returncode, 1)
        self.assertLess(time.time() - start, 3)

    def test_kill_escalates_to_sigkill(self):
        self.spawn("claude", "x", "@@hang-hard")
        self.wait_until(lambda: self.has("x", "engine.pid"), message="engine not started")
        self.wait_session("x")   # SIGTERM is ignored from here on
        pid = self.read("x", "engine.pid")["pid"]
        cp = self.ok("kill", "x", "--grace", "2")
        self.assertIn("force-killed", cp.stdout)
        self.assertFalse(pid_alive(pid))
        self.assertEqual(self.result("x")["status"], "killed")

    def test_kill_does_not_prefix_match(self):
        self.spawn("claude", "review-db", "@@hang")
        self.wait_until(lambda: self.has("review-db", "engine.pid"))
        cp = self.run_as("kill", "review")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no session 'review'", cp.stderr)
        self.assertTrue(self.tmux_alive("review-db"))
        self.assertEqual(self.run_as("peek", "review").returncode, 2)
        self.assertEqual(self.ok("peek", "review-db").returncode, 0)

    def test_kill_lingering_and_finished(self):
        self.spawn("claude", "x", "hi", env={"AGENT_SESSIONS_LINGER": "60"})
        self.assertEqual(self.wait("x").returncode, 0)
        self.assertTrue(self.tmux_alive("x"))
        before = self.result("x")
        cp = self.ok("kill", "x")
        self.assertIn("ended linger for x", cp.stdout)
        self.wait_until(lambda: not self.tmux_alive("x"), timeout=5)
        self.assertEqual(self.result("x"), before)
        self.spawn("claude", "y", "hi")
        self.assertEqual(self.wait("y").returncode, 0)
        self.wait_until(lambda: not self.tmux_alive("y"), timeout=10)
        cp = self.ok("kill", "y")
        self.assertIn("already finished", cp.stdout)
        self.assertIn("ok", cp.stdout)

    def test_external_tmux_kill_yields_killed_result(self):
        self.spawn("claude", "x", "@@hang")
        self.wait_until(lambda: self.has("x", "engine.pid"))
        pid = self.read("x", "engine.pid")["pid"]
        self.assertEqual(self.tmux("kill-session", "-t", "=as-x").returncode, 0)
        res = self.wait_result("x", timeout=5)
        self.assertEqual(res["status"], "killed")
        self.assertIn("SIG", res["reason"])
        self.wait_until(lambda: not pid_alive(pid), timeout=5)

    def test_liveness_uses_the_recorded_tmux_socket(self):
        """Agent hosts drop shell variables between tool calls. A caller without
        AGENT_SESSIONS_TMUX_SOCKET must still see the child as alive, and must
        never write a `lost` result for it."""
        self.spawn("claude", "x", "@@hang")
        self.wait_until(lambda: self.has("x", "engine.pid"))
        no_sock = {"AGENT_SESSIONS_TMUX_SOCKET": None}
        cp = self.run_as("wait", "x", "--timeout", "2", env=no_sock)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertFalse(self.has("x", "result.json"))
        cp = self.run_as("result", "x", env=no_sock)
        self.assertEqual(cp.returncode, 3)
        self.assertIn("running", self.ok("list", env=no_sock).stdout)
        cp = self.ok("reap", env=no_sock)
        self.assertIn("skipped x: running", cp.stdout)
        self.assertTrue(os.path.exists(self.sdir("x")))
        self.assertTrue(self.ok("peek", "x", env=no_sock).stdout.strip())
        # A different socket in the environment must not matter either.
        other = {"AGENT_SESSIONS_TMUX_SOCKET": self.another_socket()}
        self.assertEqual(self.run_as("wait", "x", "--timeout", "1", env=other).returncode, 3)
        cp = self.ok("kill", "x", env=no_sock)
        self.assertIn("terminated", cp.stdout)
        self.assertEqual(self.result("x")["status"], "killed")
        self.wait_until(lambda: not self.tmux_alive("x"), timeout=5)

    def test_runner_liveness_guards_lost(self):
        """Even with tmux unreachable, a running runner recorded in progress.json
        keeps the child from being declared lost."""
        self.spawn("claude", "x", "@@sleep:6")
        self.wait_until(lambda: self.has("x", "progress.json") and self.read("x", "progress.json").get("runner_pid"))
        prog = self.read("x", "progress.json")
        self.assertTrue(pid_alive(prog["runner_pid"]))
        self.assertTrue(prog["runner_lstart"])
        # Point the recorded socket at a server that does not exist.
        meta_path = os.path.join(self.sdir("x"), "meta.json")
        meta = read_json(meta_path)
        meta["tmux_socket"] = self.another_socket()
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        cp = self.run_as("wait", "x", "--timeout", "1")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertFalse(self.has("x", "result.json"))
        self.assertIn("running", self.ok("list").stdout)
        self.assertIn("skipped x: running", self.ok("reap").stdout)
        res = self.wait_result("x", timeout=15)
        self.assertEqual(res["status"], "ok")

    def test_kill_across_locales(self):
        """engine.pid's lstart is recorded by the runner and compared by the
        caller; neither may depend on the locale of the process that ran ps."""
        loc = localized_locale()
        if not loc:
            self.skipTest("no installed locale changes `ps -o lstart=` output")
        self.spawn("claude", "x", "@@hang", env={"LC_ALL": loc, "LANG": loc})
        self.wait_until(lambda: self.has("x", "engine.pid"))
        info = self.read("x", "engine.pid")
        self.assertEqual(info["lstart"], ps_lstart_under("C", info["pid"]))
        # engine.pid is written as soon as the engine is started; the fake
        # records its call a few milliseconds later.
        self.wait_until(lambda: self.calls("x"), timeout=10, message="fake engine did not record its call")
        self.assertEqual(self.calls("x")[0]["env"]["LC_ALL"], loc)
        cp = self.ok("kill", "x", env={"LC_ALL": "C", "LANG": "C"})
        self.assertIn("terminated", cp.stdout)
        self.assertFalse(pid_alive(info["pid"]))
        self.spawn("claude", "y", "@@hang", env={"LC_ALL": "C", "LANG": "C"})
        self.wait_until(lambda: self.has("y", "engine.pid"))
        pid = self.read("y", "engine.pid")["pid"]
        cp = self.ok("kill", "y", env={"LC_ALL": loc, "LANG": loc})
        self.assertIn("terminated", cp.stdout)
        self.assertFalse(pid_alive(pid))

    def test_ps_parsing_is_locale_independent(self):
        loc = localized_locale()
        if not loc:
            self.skipTest("no installed locale changes `ps -o lstart=` output")
        mod = script_module()
        saved = {k: os.environ.get(k) for k in ("LC_ALL", "LANG")}
        os.environ["LC_ALL"] = os.environ["LANG"] = loc
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            info = mod.ps_parent(os.getpid())
            self.assertIsNotNone(info)
            ppid, lstart, comm = info
            self.assertEqual(ppid, os.getppid())
            self.assertEqual(lstart, ps_lstart_under("C", os.getpid()))
            self.assertEqual(len(lstart.split()), 5, lstart)
            self.assertIn("python", os.path.basename(comm).lower())
            recorded = mod.ps_lstart(proc.pid)
            self.assertEqual(recorded, ps_lstart_under("C", proc.pid))
            self.assertTrue(mod.engine_alive({"pid": proc.pid, "lstart": recorded}))
            self.assertFalse(mod.engine_alive({"pid": proc.pid, "lstart": ps_lstart_under(loc, proc.pid)}))
        finally:
            proc.kill()
            proc.wait()
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_env_reaches_engine_through_private_file_not_argv(self):
        probe = {"FAKE_ENGINE_PROBE": "via-file", "ANTHROPIC_API_KEY": "sk-test-not-real",
                 "HTTPS_PROXY": "http://proxy.test:3128", "AWS_PROFILE": "bedrock",
                 "UNRELATED_SECRET": "opt-in", "AGENT_SESSIONS_PASS_ENV": "UNRELATED_*"}
        self.spawn("claude", "x", "@@sleep:6", env=probe)
        self.wait_until(lambda: self.has("x", "engine.pid"))
        self.wait_until(lambda: not self.has("x", "env.json"), timeout=5, message="env.json not deleted")
        self.wait_until(lambda: self.calls("x"), timeout=10, message="fake engine did not record its call")
        call = self.calls("x")[0]["env"]
        for key, value in probe.items():
            if key != "AGENT_SESSIONS_PASS_ENV":
                self.assertEqual(call[key], value, key)
        cp = self.tmux("show-environment", "-t", "=as-x")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("AGENT_SESSION_NAME=x", cp.stdout)
        for secret in ("sk-test-not-real", "ANTHROPIC_API_KEY", "proxy.test", "opt-in", "FAKE_ENGINE_PROBE"):
            self.assertNotIn(secret, cp.stdout, cp.stdout)
        # Without the opt-in, an unrelated variable stays with the spawner.
        self.spawn("claude", "y", "hi", env={"UNRELATED_SECRET": "stay-home"})
        self.assertEqual(self.wait("x", "y").returncode, 0)
        self.assertIsNone(self.calls("y")[0]["env"]["UNRELATED_SECRET"])

    def test_tmux_server_environment_does_not_reach_engine(self):
        # A tmux server someone else started (the user's default server) holds
        # whatever was exported when it started. None of it may reach the engine.
        env = self.environ({"UNRELATED_SECRET": "server-secret", "ANTHROPIC_API_KEY": "sk-server-stale"})
        cp = subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", "user"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.spawn("claude", "x", "hi", env={"ANTHROPIC_API_KEY": None})
        self.assertEqual(self.wait("x").returncode, 0)
        call = self.calls("x")[0]["env"]
        self.assertIsNone(call["UNRELATED_SECRET"])
        self.assertIsNone(call["ANTHROPIC_API_KEY"])
        self.assertEqual(call["AGENT_SESSION_NAME"], "x")

    def test_remain_on_exit_config_does_not_keep_dead_sessions(self):
        conf = os.path.join(self.box, "tmux.conf")
        with open(conf, "w") as fh:
            fh.write("set-option -g remain-on-exit on\n")
        self.spawn("claude", "x", "hi", env={"AGENT_SESSIONS_TMUX_CONF": conf})
        self.assertEqual(self.wait("x").returncode, 0)
        cp = self.tmux("show-options", "-g", "remain-on-exit")
        self.assertIn("on", cp.stdout, "the test config was not loaded: %s" % cp.stdout)
        self.wait_until(lambda: not self.tmux_alive("x"), timeout=10, message="session kept alive by remain-on-exit")
        self.assertEqual(self.result("x")["status"], "ok")

    def test_depth_comes_from_parent_meta_not_only_env(self):
        self.fake_meta("deep", depth=2)
        cp = self.run_as("spawn", "claude", "x", self.work, "hi",
                         env={"AGENT_SESSION_NAME": "deep", "AGENT_SESSION_DEPTH": None})
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("depth", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("x")))
        self.fake_meta("shallow", depth=1, started_epoch=1700000000.5)
        self.spawn("claude", "y", "@@depth", env={"AGENT_SESSION_NAME": "shallow", "AGENT_SESSION_DEPTH": None,
                                                  "AGENT_SESSION_STARTED": "1700000000"})
        self.assertEqual(self.meta("y")["depth"], 2)
        self.assertEqual(self.meta("y")["owner"], "child:shallow:1700000000")
        self.assertEqual(self.wait("y").returncode, 0)
        self.assertEqual(self.result("y")["answer"], "depth=2 name=y")

    def test_kill_takes_grandchildren_with_it(self):
        """Children a child spawned run in their own tmux sessions; killing the
        child alone would leave them running and holding cap slots."""
        self.spawn("claude", "p", "@@spawn:claude,gc,%s,@@hang" % self.work, "--steerable")
        cp = self.run_as("turn", "p", "1", "--timeout", "30")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("exit=0", self.turn("p", 1)["answer"])
        gc_meta = self.meta("gc")
        self.assertEqual(gc_meta["owner"], "child:p:%d" % int(self.meta("p")["started_epoch"]))
        self.assertEqual(gc_meta["depth"], 2)
        self.wait_until(lambda: self.has("gc", "engine.pid"))
        gc_pid = self.read("gc", "engine.pid")["pid"]
        listing = self.ok("list").stdout
        self.assertIn("child:p", listing)
        cp = self.ok("kill", "p")
        self.assertIn("child     : gc killed too", cp.stdout)
        self.assertEqual(self.result("p")["status"], "killed")
        self.assertEqual(self.result("gc")["status"], "killed")
        self.assertFalse(pid_alive(gc_pid))
        self.wait_until(lambda: not self.tmux_alive("gc"), timeout=5)

    def test_lost_detection(self):
        self.fake_meta("x")
        start = time.time()
        cp = self.wait("x", timeout=5)
        self.assertLess(time.time() - start, 3)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertEqual(self.result("x")["status"], "lost")
        self.assertEqual(self.result("x")["ended"], "lost")
        cp = self.run_as("result", "x")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("lost", cp.stdout)
        self.assertIn("lost", self.ok("list").stdout)

    def test_lost_with_live_engine_is_reported(self):
        self.fake_meta("x")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
        try:
            lstart = subprocess.run(["ps", "-o", "lstart=", "-p", str(proc.pid)], stdout=subprocess.PIPE,
                                    universal_newlines=True).stdout.strip()
            with open(os.path.join(self.sdir("x"), "engine.pid"), "w") as fh:
                json.dump({"pid": proc.pid, "pgid": os.getpgid(proc.pid), "lstart": lstart, "turn": 1,
                           "runner_pid": 1}, fh)
            cp = self.run_as("result", "x")
            self.assertEqual(cp.returncode, 1)
            self.assertIn("engine pid %d still alive" % proc.pid, cp.stdout)
            cp = self.ok("kill", "x")
            self.assertIn("terminated", cp.stdout)
            self.wait_until(lambda: proc.poll() is not None, timeout=5, message="orphan not killed")
            self.assertEqual(self.result("x")["status"], "killed")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_error_status_from_exit_code_and_is_error(self):
        self.spawn("claude", "a", "@@exit:3 @@stderr:boom_went_the_engine")
        self.spawn("claude", "b", "@@is-error")
        self.spawn("claude", "c", "@@noanswer")
        cp = self.wait("a", "b", "c")
        self.assertEqual(cp.returncode, 1)
        a = self.result("a")
        self.assertEqual(a["status"], "error")
        self.assertEqual(a["exit_code"], 3)
        self.assertIn("boom went the engine", a["stderr_tail"])
        self.assertIn("--- stderr (child output, untrusted) ---", self.run_as("result", "a").stdout)
        b = self.result("b")
        self.assertEqual(b["status"], "error")
        self.assertEqual(b["failed_turns"], [{"turn": 1, "status": "error"}])
        c = self.result("c")
        self.assertEqual(c["status"], "error")
        self.assertIsNone(c["answer"])
        self.assertIn("no result event", c["reason"])

    def test_name_reuse_refused_then_replace(self):
        self.spawn("claude", "x", "first")
        self.assertEqual(self.wait("x").returncode, 0)
        first_meta = self.meta("x")
        cp = self.run_as("spawn", "claude", "x", self.work, "again")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--replace", cp.stderr)
        self.assertIn("reap", cp.stderr)
        self.assertEqual(self.meta("x"), first_meta)
        self.spawn("claude", "x", "second @@sleep:10", "--replace")
        self.assertNotEqual(self.meta("x")["conversation_id"], first_meta["conversation_id"])
        self.assertFalse(self.has("x", "result.json"))
        cp = self.run_as("spawn", "claude", "x", self.work, "third", "--replace")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("is running", cp.stderr)

    def test_replace_retires_lingering_runner(self):
        """Replacing a finished child that still lingers ends its runner
        first: on its way out that runner writes progress.json, which must not
        land in the new child's state directory."""
        self.spawn("claude", "x", "first", env={"AGENT_SESSIONS_LINGER": "60"})
        self.wait_result("x")
        self.wait_until(lambda: self.progress("x").get("phase") == "linger", message="runner not lingering")
        old = self.progress("x")["runner_pid"]
        self.spawn("claude", "x", "second @@sleep:2", "--replace")
        self.assertFalse(pid_alive(old), "the old runner outlived the replace")
        self.wait_until(lambda: self.has("x", "progress.json") and self.progress("x").get("runner_pid"),
                        message="new runner wrote no progress")
        self.assertNotEqual(self.progress("x")["runner_pid"], old)
        self.assertEqual(self.wait_result("x")["status"], "ok")
        self.assertNotEqual(self.progress("x")["runner_pid"], old)

    def test_cap_is_enforced_atomically(self):
        env = self.environ({"AGENT_SESSIONS_MAX": "2"})
        procs = []
        for i in range(4):
            procs.append(subprocess.Popen(
                [sys.executable, SCRIPT, "spawn", "claude",
                 "c%d" % i, self.work, "@@sleep:10"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True))
        outcomes = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=60)
            outcomes.append((p.returncode, stdout, stderr))
        codes = sorted(o[0] for o in outcomes)
        self.assertEqual(codes, [0, 0, 2, 2], outcomes)
        for code, _out, errtext in outcomes:
            if code == 2:
                self.assertIn("cap 2", errtext)
        listing = self.ok("list").stdout
        self.assertEqual(listing.count("running"), 2, listing)

    def test_unrelated_tmux_session_does_not_count(self):
        self.assertEqual(self.tmux("new-session", "-d", "-s", "as-foo", "sleep 30").returncode, 0)
        self.spawn("claude", "x", "hi", env={"AGENT_SESSIONS_MAX": "1"})
        self.assertEqual(self.wait("x").returncode, 0)

    def test_finished_children_do_not_count(self):
        self.spawn("claude", "a", "hi", env={"AGENT_SESSIONS_MAX": "1", "AGENT_SESSIONS_LINGER": "60"})
        self.assertEqual(self.wait("a").returncode, 0)
        self.assertTrue(self.tmux_alive("a"))
        self.spawn("claude", "b", "hi", env={"AGENT_SESSIONS_MAX": "1"})
        self.assertEqual(self.wait("b").returncode, 0)

    def test_env_passthrough_to_engine(self):
        self.spawn("claude", "x", "@@depth")
        self.spawn("claude", "y", "@@depth", env={"AGENT_SESSION_DEPTH": "1"})
        self.assertEqual(self.wait("x", "y").returncode, 0)
        self.assertEqual(self.result("x")["answer"], "depth=1 name=x")
        self.assertEqual(self.result("y")["answer"], "depth=2 name=y")
        self.assertEqual(self.meta("y")["depth"], 2)

    def test_grandchild_cannot_spawn(self):
        self.spawn("claude", "x", "@@spawn:claude,gc,%s,hi" % self.work, env={"AGENT_SESSION_DEPTH": "1"})
        self.assertEqual(self.wait("x").returncode, 0)
        answer = self.result("x")["answer"]
        self.assertIn("exit=2", answer)
        self.assertIn("depth", answer)
        self.assertFalse(os.path.exists(self.sdir("gc")))
