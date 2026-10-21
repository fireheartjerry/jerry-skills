"""The verify gate: pass, fail, fixup turns, timeouts and refusals."""

import json

from helpers import SessionTest


class VerifyTests(SessionTest):
    def test_verify_pass_fail_fixup(self):
        self.spawn("claude", "p", "@@edit:done.txt=x", "--verify", "test -f done.txt")
        res = self.wait_result("p")
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["verify"]["status"], "pass")
        self.assertEqual(res["verify"]["exit_code"], 0)
        self.assertTrue(self.has("p", "verify.1.out"))
        self.assertIn("verify    : pass", self.ok("result", "p").stdout)
        self.assertIn("verify", [e["type"] for e in self.events("p")])

        self.spawn("claude", "f", "no edit here", "--verify", "test -f missing.txt", "--verify-fixups", "0")
        res = self.wait_result("f")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "verify")
        self.assertEqual(res["turns"], 1)
        self.assertEqual(res["verify"]["status"], "fail")
        self.assertIn("verification failed after 0 fixups", res["reason"])
        self.assertIn("verify    : fail exit 1", self.run_as("result", "f").stdout)

        self.spawn("codex", "fx", "start @@on-turn:2:edit:later.txt=y", "--verify", "test -f later.txt")
        res = self.wait_result("fx")
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["turns"], 2)
        self.assertEqual(res["verify"]["status"], "pass")
        self.assertEqual(res["verify"]["attempts"], 2)
        fixup = self.calls("fx")[1]["stdin"]
        self.assertIn("test -f later.txt", fixup)
        self.assertIn("exited 1", fixup)
        self.assertIn("Do not change the verification command", fixup)

    def test_verify_timeout_and_refusals(self):
        self.spawn("claude", "t", "hi", "--verify", "sleep 30", "--verify-timeout", "2")
        res = self.wait_result("t", timeout=30)
        self.assertEqual(res["verify"]["status"], "error")
        self.assertEqual(res["failure_class"], "verify")
        cp = self.run_as("spawn", "claude", "r", self.work, "hi", "--verify", "true", "--readonly")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("read-only", cp.stderr)
        cp = self.run_as("spawn", "claude", "r", self.work, "hi", "--verify", "true", "--role", "review")
        self.assertEqual(cp.returncode, 2)
        self.spawn("claude", "s", "hi", "--steerable", "--verify", "test -f steer.txt")
        self.ok("turn", "s", "1", "--timeout", "20")
        self.ok("send", "s", "@@edit:steer.txt=z", "--timeout", "20")
        self.ok("stop", "s")
        res = self.wait_result("s", timeout=30)
        self.assertEqual(res["verify"]["status"], "pass")
        self.assertEqual(res["verify"]["attempts"], 1)
        self.assertEqual(res["status"], "ok")

    def test_verify_skipped_when_child_fails(self):
        self.spawn("claude", "x", "@@exit:3", "--verify", "true")
        res = self.wait_result("x")
        self.assertEqual(res["verify"]["status"], "skipped")
        self.assertEqual(res["status"], "error")
        self.assertNotEqual(res["failure_class"], "verify")

    def test_verify_env_phase_views_and_schema_order(self):
        # Runs in the workdir with the child's environment; status shows the verify phase.
        self.spawn("claude", "v", "@@edit:ok.txt=1",
                   "--verify", 'sleep 2; test "$AGENT_SESSION_NAME" = v && test -f ok.txt && echo verified')
        prog = self.wait_until(lambda: self.has("v", "progress.json") and self.progress("v").get("phase") == "verify"
                               and self.progress("v"), timeout=20, message="no verify phase")
        self.assertEqual(prog["verify"]["attempt"], 1)
        status = json.loads(self.run_as("status", "v", "--json").stdout)
        self.assertEqual(status["phase"], "verify")
        self.assertEqual(status["verify"]["command"], self.meta("v")["verify"])
        res = self.wait_result("v", timeout=30)
        self.assertEqual(res["verify"]["status"], "pass")
        self.assertIn("verified", res["verify"]["output_tail"])
        self.assertGreaterEqual(res["verify"]["duration_s"], 1.5)
        with open(res["verify"]["output_file"]) as fh:
            self.assertIn("verified", fh.read())
        self.assertRegex(self.ok("wait", "v", "--timeout", "5").stdout, r"verify\s+: pass \(\d+(\.\d+)?s\)")
        # A failing verify names its output file in the views.
        self.spawn("codex", "w", "no edit", "--verify", "echo broken >&2; exit 4", "--verify-fixups", "1")
        res = self.wait_result("w", timeout=30)
        self.assertEqual((res["status"], res["failure_class"]), ("error", "verify"))
        self.assertEqual(res["verify"]["exit_code"], 4)
        self.assertEqual(res["verify"]["attempts"], 2)
        self.assertEqual(res["turns"], 2)
        self.assertIn("verification failed after 1 fixup", res["reason"])
        self.assertTrue(res["verify"]["output_file"].endswith("verify.2.out"))
        self.assertIn("broken", res["verify"]["output_tail"])
        self.assertIn("fail exit 4 (see %s)" % res["verify"]["output_file"], self.run_as("result", "w").stdout)
        self.assertIn("broken", self.calls("w")[1]["stdin"])
        self.assertIn("verify-fixup", [t["message_id"] for t in (self.turn("w", 1), self.turn("w", 2))])
        # --schema: the schema fixup runs first, verify after; a schema failure skips verify.
        schema = self.write_file("schema.json", json.dumps(
            {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}))
        good = self.write_file("good.json", '{"n": 1}')
        bad = self.write_file("bad.json", '{"n": "x"}')
        self.spawn("claude", "sv", "@@answer-file:%s @@on-turn:2:answer-file:%s @@on-turn:2:edit:s.txt=1"
                   % (bad, good), "--schema", schema, "--verify", "test -f s.txt")
        res = self.wait_result("sv", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertTrue(res["schema_valid"])
        self.assertEqual((res["turns"], res["verify"]["status"], res["verify"]["attempts"]), (2, "pass", 1))
        self.spawn("claude", "sx", "@@answer-file:%s" % bad, "--schema", schema, "--verify", "true")
        res = self.wait_result("sx", timeout=30)
        self.assertEqual((res["failure_class"], res["verify"]["status"]), ("schema", "skipped"))

    def test_verify_option_errors(self):
        for extra in (["--verify", ""], ["--verify", "  "], ["--verify-fixups", "1"],
                      ["--verify", "true", "--verify-fixups", "9"], ["--verify", "true", "--verify-timeout", "0"]):
            cp = self.run_as("spawn", "claude", "e", self.work, "hi", *extra)
            self.assertEqual(cp.returncode, 2, extra)
        self.assertFalse(self.has("e", "meta.json"))
        meta = json.loads(self.ok("spawn", "claude", "d", self.work, "hi", "--verify", "true", "--json").stdout)
        self.assertEqual((meta["verify_fixups"], meta["verify_timeout_s"]), (1, 600))
        meta = json.loads(self.ok("spawn", "claude", "ds", self.work, "hi", "--verify", "true", "--steerable",
                                  "--json").stdout)
        self.assertEqual(meta["verify_fixups"], 0)
        self.ok("kill", "ds")
