"""Result contract v3: every result validates, v2 stays readable, structured
output with --schema, readonly denial warnings, cost estimates, git block."""

import json
import os

from helpers import SessionTest, assert_valid_result, price_env, read_json

SCHEMA = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}, "additionalProperties": False}


class ContractTests(SessionTest):
    """Quick contract checks; part of tests/run.sh --fast."""

    def schema_file(self, obj=None, text=None):
        return self.write_file("schema.json", text if text is not None else json.dumps(obj or SCHEMA))

    def test_every_result_validates(self):
        self.spawn("claude", "c", "hi @@slow:1")
        self.spawn("codex", "d", "hi @@slow:1")
        self.spawn("gemini", "g", "hi @@slow:1")
        cp = self.wait("c", "d", "g")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name, source in (("c", "engine"), ("d", None), ("g", None)):
            res = self.wait_result(name)
            self.assertEqual(res["version"], 3, name)
            self.assertIsNone(res["failure_class"], name)
            self.assertEqual(res["attempts"], 1)
            self.assertEqual(res["retries"], [])
            self.assertEqual(res["role"], "implementation")
            self.assertEqual(res["tier"], "cheap")
            self.assertTrue(res["engine_version"], name)
            self.assertGreater(res["usage"]["total_tokens"], 0, name)
            self.assertEqual(res["usage"]["total_tokens"], res["usage"]["input_tokens"] + res["usage"]["output_tokens"])
            self.assertEqual(res["cost_source"], source, name)
            self.assertEqual(res["cost_usd"], 0.001 if source == "engine" else None, name)
            self.assertIsNone(res["verify"])
            self.assertIsNone(res["git"])
            self.assertEqual(res["warnings"], [])
            self.assertEqual(res["budget"]["usd_limit"], None)
            self.assertTrue(res["events_file"].endswith("events.jsonl"))
            kinds = [e["type"] for e in self.events(name)]
            for kind in ("lifecycle", "turn_start", "assistant", "result", "turn_end"):
                self.assertIn(kind, kinds, (name, kinds))
            self.assertEqual(kinds[0], "lifecycle")
            self.assertEqual(kinds[-1], "lifecycle")
            turn = self.turn(name, 1)
            for key in ("attempt", "failure_class", "usage", "cost_usd", "cost_source", "structured_output",
                        "schema_valid", "denials", "tool_calls"):
                self.assertIn(key, turn, (name, key))
            self.assertEqual(turn["attempt"], 1)
            self.assertEqual(turn["denials"], [])
            prog = self.progress(name)
            for key in ("usage_so_far", "cost_so_far", "attempt", "stall_deadline_epoch"):
                self.assertIn(key, prog, key)
            self.assertEqual(prog["usage_so_far"], res["usage"])
        # The result view shows the counters and a stable status line.
        view = self.ok("result", "c").stdout
        self.assertIn("status    : ok", view)
        self.assertIn("tokens    : in 10  cached 0  out 5  total 15  cost: $0.0010 (engine)", view)

    def test_v2_result_still_readable(self):
        self.fake_meta("old")
        self.v2_result("old")
        listing = self.ok("list").stdout
        self.assertIn("old", listing)
        self.assertIn("ok", listing)
        cp = self.ok("result", "old")
        self.assertIn("reply#1: old", cp.stdout)
        self.assertIn("status    : ok", cp.stdout)
        cp = self.ok("result", "old", "--json")
        doc = json.loads(cp.stdout)
        self.assertEqual(doc["version"], 2)
        self.assertIsNone(doc["failure_class"])
        self.assertIsNone(doc["usage"])
        self.assertEqual(doc["warnings"], [])
        self.assertEqual(doc["attempts"], 1)
        assert_valid_result(doc, "normalized v2 result")
        # On disk the file is untouched.
        self.assertNotIn("failure_class", self.result("old"))
        cp = self.ok("wait", "old", "--timeout", "5")
        self.assertIn("reply#1: old", cp.stdout)
        cp = self.ok("status", "old", "--json")
        self.assertEqual(json.loads(cp.stdout)["state"], "ok")
        cp = self.ok("reap", "old")
        self.assertIn("reaped old", cp.stdout)
        self.assertFalse(os.path.exists(self.sdir("old")))

    def test_schema_refused_with_steerable(self):
        schema = self.schema_file()
        cp = self.run_as("spawn", "claude", "x", self.work, "hi", "--schema", schema, "--steerable")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("steerable", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("x")))

    def test_bad_schema_file_refused(self):
        cases = [
            (self.write_file("notjson.json", "{not json"), "not valid JSON"),
            (self.write_file("array.json", "[1, 2]"), "JSON object"),
            (os.path.join(self.box, "missing.json"), "cannot read"),
        ]
        for path, message in cases:
            cp = self.run_as("spawn", "claude", "x", self.work, "hi", "--schema", path)
            self.assertEqual(cp.returncode, 2, "%s: %s%s" % (path, cp.stdout, cp.stderr))
            self.assertIn(message, cp.stderr, path)
            self.assertFalse(os.path.exists(self.sdir("x")))
        # Unsupported keywords are a warning at spawn and in the result, not a refusal.
        loose = self.schema_file({"type": "object", "properties": {"n": {"type": "integer", "format": "int32"}},
                                  "patternProperties": {}})
        good = self.write_file("good.json", '{"n": 7}')
        cp = self.spawn("claude", "w", "answer @@answer-file:%s" % good, "--schema", loose)
        self.assertIn("not enforced", cp.stderr)
        self.assertIn("format", cp.stderr)
        self.assertIn("patternProperties", cp.stderr)
        self.assertEqual(self.wait("w").returncode, 0)
        res = self.wait_result("w")
        self.assertTrue(res["schema_valid"])
        self.assertTrue([w for w in res["warnings"] if "format" in w and "patternProperties" in w], res["warnings"])

    def test_role_implies_readonly_and_is_recorded(self):
        self.spawn("claude", "rev", "review this", "--role", "review")
        self.spawn("codex", "res", "research this", "--role", "research", "--write")
        self.assertEqual(self.wait("rev", "res").returncode, 0)
        rev, res = self.wait_result("rev"), self.wait_result("res")
        self.assertEqual(rev["role"], "review")
        self.assertTrue(self.meta("rev")["readonly"])
        self.assertIn("--permission-mode", self.calls("rev")[0]["argv"])
        self.assertEqual(res["role"], "research")
        self.assertFalse(self.meta("res")["readonly"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", self.calls("res")[0]["argv"])
        cp = self.run_as("spawn", "claude", "x", self.work, "hi", "--role", "boss")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("unknown role", cp.stderr)
        cp = self.run_as("spawn", "claude", "x", self.work, "hi", "--readonly", "--write")
        self.assertEqual(cp.returncode, 2)

    def test_meta_v3_fields(self):
        self.spawn("claude", "m", "hi", "--stall", "120")
        meta = self.meta("m")
        self.assertEqual(meta["version"], 3)
        self.assertEqual(meta["role"], "implementation")
        self.assertEqual(meta["stall_s"], 120)
        self.assertIsNone(meta["schema_file"])
        self.assertEqual(len(meta["prompt_sha256"]), 64)
        self.assertTrue(meta["engine_version"].startswith("9.9.9"))
        self.assertEqual(meta["budget"]["usd_limit"], None)
        self.assertEqual(self.wait("m").returncode, 0)
        read_json(os.path.join(self.state, "engine-versions.json"))


class V3ResultTests(SessionTest):
    """Structured output, denials, cost, git block and failure classes (full suite)."""

    def schema_file(self, obj=None, text=None):
        return self.write_file("schema.json", text if text is not None else json.dumps(obj or SCHEMA))

    def test_schema_output_claude_and_codex(self):
        schema = self.schema_file()
        good = self.write_file("good.json", '{"n": 7}')
        self.spawn("claude", "c", "answer @@answer-file:%s" % good, "--schema", schema)
        self.spawn("codex", "d", "answer @@answer-file:%s" % good, "--schema", schema)
        cp = self.wait("c", "d")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for name in ("c", "d"):
            res = self.wait_result(name)
            self.assertEqual(res["status"], "ok", res)
            self.assertEqual(res["structured_output"], {"n": 7}, name)
            self.assertTrue(res["schema_valid"], name)
            self.assertEqual(res["schema_file"], schema)
            self.assertEqual(res["turns"], 1)
            self.assertEqual(self.turn(name, 1)["structured_output"], {"n": 7})
            self.assertTrue(self.has(name, "schema.json"))
        c = self.calls("c")[0]["argv"]
        self.assertIn("--json-schema", c)
        self.assertEqual(json.loads(c[c.index("--json-schema") + 1]), SCHEMA)
        d = self.calls("d")[0]["argv"]
        self.assertIn("--output-schema", d)
        self.assertEqual(d[d.index("--output-schema") + 1], os.path.join(self.sdir("d"), "schema.json"))
        view = self.ok("result", "c").stdout
        self.assertIn("schema    : valid", view)
        self.assertEqual(self.turn("c", 1)["structured_source"], "engine")
        self.assertEqual(self.turn("d", 1)["structured_source"], "answer")

    def test_schema_output_from_fenced_block_and_gemini_prefix(self):
        schema = self.schema_file()
        fenced = self.write_file("fenced.md", 'Here you go:\n```json\n{"n": 3}\n```\nDone.')
        self.spawn("gemini", "g", "answer @@answer-file:%s" % fenced, "--schema", schema)
        self.assertEqual(self.wait("g").returncode, 0)
        res = self.wait_result("g")
        self.assertEqual(res["structured_output"], {"n": 3})
        self.assertTrue(res["schema_valid"])
        stdin = self.calls("g")[0]["stdin"]
        self.assertTrue(stdin.startswith("Your final reply must be only a JSON object"), stdin[:120])
        self.assertIn('"required"', stdin)
        self.assertIn("answer @@answer-file", stdin)

    def test_schema_fixup_then_fail(self):
        schema = self.schema_file()
        bad = self.write_file("bad.json", '{"n": "x"}')
        good = self.write_file("good.json", '{"n": 7}')
        # Turn 1 answers badly; the planted on-turn directive fixes turn 2.
        self.spawn("claude", "fix", "answer @@on-turn:2:answer-file:%s" % good,
                   "--schema", schema, env={"FAKE_ENGINE_ANSWER_FILE": bad})
        cp = self.wait("fix")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        res = self.wait_result("fix")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["turns"], 2)
        self.assertEqual(res["failed_turns"], [])
        self.assertTrue(res["schema_valid"])
        self.assertEqual(res["structured_output"], {"n": 7})
        self.assertFalse(self.turn("fix", 1)["schema_valid"])
        self.assertIsNone(self.turn("fix", 1)["structured_output"])
        calls = self.calls("fix")
        self.assertEqual(len(calls), 2)
        self.assertIn("--resume", calls[1]["argv"])
        self.assertIn("Reply with only the JSON object", calls[1]["stdin"])
        self.assertIn("$.n: expected integer, got string", calls[1]["stdin"])
        self.assertEqual(self.turn("fix", 2)["message_id"], "schema-fixup")
        kinds = [e["type"] for e in self.events("fix")]
        self.assertEqual(kinds.count("turn_start"), 2)
        # Without the fix the second answer is bad too: error, class schema, answer kept.
        self.spawn("claude", "fail", "answer", "--schema", schema, env={"FAKE_ENGINE_ANSWER_FILE": bad})
        cp = self.wait("fail")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        res = self.wait_result("fail")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "schema")
        self.assertEqual(res["turns"], 2)
        self.assertEqual(res["failed_turns"], [{"turn": 2, "status": "error"}])
        self.assertFalse(res["schema_valid"])
        self.assertIsNone(res["structured_output"])
        self.assertEqual(res["answer"], '{"n": "x"}')
        self.assertIn("did not validate after one fixup", res["reason"])
        self.assertIn("schema    : invalid", self.run_as("result", "fail").stdout)
        self.assertEqual(len(self.calls("fail")), 2)
        # No JSON at all is also a schema failure, with a clear error in the fixup prompt.
        self.spawn("claude", "none", "answer with prose only", "--schema", schema)
        self.assertEqual(self.wait("none").returncode, 1)
        self.assertEqual(self.wait_result("none")["failure_class"], "schema")
        self.assertIn("no JSON object found in the answer", self.calls("none")[1]["stdin"])

    def test_readonly_denials_are_warned(self):
        self.spawn("claude", "r", "look @@denied:2", "--readonly")
        self.assertEqual(self.wait("r").returncode, 0)
        res = self.wait_result("r")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["warnings"], res)
        self.assertIn("2 tool calls denied under --readonly", res["warnings"][0])
        self.assertIn("Read /tmp/denied-0.txt", res["warnings"][0])
        self.assertEqual(len(self.turn("r", 1)["denials"]), 2)
        self.assertEqual([e["type"] for e in self.events("r")].count("denied"), 2)
        view = self.ok("result", "r").stdout
        self.assertIn("warning   : 2 tool calls denied under --readonly", view)
        # A bypass child reports the same denials with different wording.
        self.spawn("claude", "w", "look @@denied:1")
        self.assertEqual(self.wait("w").returncode, 0)
        self.assertIn("1 tool call denied by the engine's permission settings", self.wait_result("w")["warnings"][0])

    def test_cost_estimate_from_price(self):
        self.spawn("codex", "free", "hi @@tokens:1000000,0")
        self.spawn("codex", "paid", "hi @@tokens:1000000,0", env=price_env("codex", inp=1.0, cached=0.1, output=10.0))
        self.spawn("gemini", "gp", "hi @@tokens:100000,100000", env=price_env("gemini", inp=1.0, cached=0.5, output=2.0))
        self.assertEqual(self.wait("free", "paid", "gp").returncode, 0)
        free, paid, gp = self.wait_result("free"), self.wait_result("paid"), self.wait_result("gp")
        self.assertIsNone(free["cost_usd"])
        self.assertIsNone(free["cost_source"])
        self.assertEqual(paid["cost_usd"], 1.0)
        self.assertEqual(paid["cost_source"], "estimate")
        self.assertEqual(gp["cost_usd"], 0.3)
        self.assertEqual(gp["usage"]["total_tokens"], 200000)
        self.assertIn("cost: $1.0000 (estimate)", self.ok("result", "paid").stdout)
        cp = self.run_as("spawn", "codex", "x", self.work, "hi", env={"AGENT_SESSIONS_CODEX_PRICE": "input=abc"})
        self.assertEqual(cp.returncode, 2)
        self.assertIn("AGENT_SESSIONS_CODEX_PRICE", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("x")))

    def test_git_block_for_worktree_child(self):
        repo = self.make_repo(os.path.join(self.box, "repo"))
        self.spawn("claude", "wt", "work @@sleep:4", "--worktree", directory=repo)
        wt = self.meta("wt")["worktree"]["path"]
        with open(os.path.join(wt, "new.txt"), "w") as fh:
            fh.write("one\ntwo\n")
        with open(os.path.join(wt, "README.md"), "a") as fh:
            fh.write("more\n")
        self.assertEqual(self.git(wt, "add", "new.txt").returncode, 0)
        self.assertEqual(self.git(wt, "commit", "-q", "-m", "add").returncode, 0)
        with open(os.path.join(wt, "untracked.txt"), "w") as fh:
            fh.write("x\n")
        self.assertEqual(self.wait("wt").returncode, 0)
        res = self.wait_result("wt")
        g = res["git"]
        self.assertEqual(g["branch"], "agent-session/wt")
        self.assertEqual(g["base_commit"], self.meta("wt")["worktree"]["base_commit"])
        self.assertEqual(g["head_commit"], self.head(wt))
        self.assertNotEqual(g["head_commit"], g["base_commit"])
        self.assertEqual(g["commits"], 1)
        self.assertEqual(g["files_changed"], 2)
        self.assertEqual(g["insertions"], 3)
        self.assertEqual(g["deletions"], 0)
        self.assertTrue(g["dirty"])
        self.assertEqual(g["changed_files"], ["README.md", "new.txt", "untracked.txt"])
        self.assertIn("git       : agent-session/wt: 1 commit, 2 files changed (+3 -0)  dirty", self.ok("result", "wt").stdout)

    def test_failure_classes_present_for_errors(self):
        self.spawn("claude", "a", "@@exit:3")
        self.spawn("claude", "b", "@@is-error")
        self.spawn("codex", "c", "@@noanswer")
        self.spawn("gemini", "d", "@@is-error")
        self.assertEqual(self.wait("a", "b", "c", "d").returncode, 1)
        self.assertEqual(self.wait_result("a")["failure_class"], "crash")
        self.assertEqual(self.wait_result("b")["failure_class"], "engine_error")
        self.assertEqual(self.wait_result("c")["failure_class"], "engine_error")
        self.assertEqual(self.wait_result("d")["failure_class"], "engine_error")
        self.assertIn("(ended: completed, crash)", self.run_as("result", "a").stdout)
        for name in ("a", "b", "c", "d"):
            self.assertEqual(self.turn(name, 1)["failure_class"], self.result(name)["failure_class"])
        self.fake_meta("lost")
        self.assertEqual(self.wait("lost", timeout=5).returncode, 1)
        self.assertEqual(self.wait_result("lost")["failure_class"], "lost")
        self.spawn("claude", "k", "@@hang")
        self.wait_until(lambda: self.has("k", "engine.pid"))
        self.ok("kill", "k")
        self.assertEqual(self.wait_result("k")["failure_class"], "killed")
        self.spawn("claude", "t", "@@sleep:30", "--timeout", "5")
        self.assertEqual(self.wait("t", timeout=40).returncode, 1)
        self.assertEqual(self.wait_result("t")["failure_class"], "timeout")
