"""Engine adapters (gemini flags, parsers, classification, engines command) and
the JSON Schema validator."""

import json
import os
import sys

from helpers import SCRIPT, SessionTest, script_module


class ParserTests(SessionTest):
    def test_gemini_flags_and_parse(self):
        self.env["AGENT_SESSIONS_MAX_GEMINI"] = "3"   # three at once, above the default engine cap of 2
        self.spawn("gemini", "g", "hello gemini")
        self.spawn("gemini", "ro", "hello", "--readonly")
        self.spawn("gemini", "std", "hello", "--tier", "standard")
        self.assertEqual(self.wait("g", "ro", "std").returncode, 0)
        call = self.calls("g")[0]
        self.assertEqual(call["argv"], ["--output-format", "stream-json", "--approval-mode", "yolo", "--skip-trust",
                                        "--model", "flash"])
        self.assertNotIn("-p", call["argv"])
        self.assertIsNone(call["positional"])
        self.assertEqual(call["stdin"], "hello gemini")
        res = self.wait_result("g")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["answer"].startswith("reply#1: hello gemini"))
        self.assertEqual(res["session_id"], self.meta("g")["conversation_id"])
        self.assertTrue(res["session_id"])
        self.assertEqual(res["engine"], "gemini")
        ro = self.calls("ro")[0]["argv"]
        self.assertEqual(ro[ro.index("--approval-mode") + 1], "plan")
        self.assertNotIn("yolo", ro)
        std = self.calls("std")[0]["argv"]
        self.assertEqual(std[std.index("--model") + 1], "pro")
        # Exit codes map to the documented meanings.
        self.spawn("gemini", "e53", "@@exit:53")
        self.spawn("gemini", "e55", "@@exit:55")
        self.spawn("gemini", "e42", "@@exit:42")
        self.assertEqual(self.wait("e53", "e55", "e42").returncode, 1)
        self.assertEqual(self.wait_result("e53")["failure_class"], "budget")
        self.assertIn("turn limit", self.result("e53")["reason"])
        e55 = self.wait_result("e55")
        self.assertEqual(e55["failure_class"], "engine_error")
        self.assertIn("--skip-trust", e55["reason"])
        self.assertEqual(self.wait_result("e42")["failure_class"], "engine_error")
        # A steerable gemini child resumes by session id.
        self.spawn("gemini", "s", "first", "--steerable")
        cp = self.ok("send", "s", "second")
        self.assertIn("reply#2: second", cp.stdout)
        second = self.calls("s")[1]["argv"]
        self.assertEqual(second[second.index("--resume") + 1], self.meta("s")["conversation_id"])
        self.ok("stop", "s")
        self.assertEqual(self.wait("s").returncode, 0)

    def test_engines_command(self):
        cp = self.ok("engines", "--json")
        report = json.loads(cp.stdout)
        self.assertEqual(sorted(report), ["claude", "codex", "gemini"])
        self.assertEqual(report["codex"]["auth"], "ok")
        # `claude auth status` is a free check of the login (the fake reports logged in).
        self.assertEqual(report["claude"]["auth"], "ok")
        self.assertFalse(report["gemini"]["verified"])
        self.assertTrue(report["claude"]["verified"])
        self.assertTrue(report["claude"]["capabilities"]["native_budget_usd"])
        self.assertTrue(report["claude"]["capabilities"]["structured_output"])
        self.assertFalse(report["codex"]["capabilities"]["reports_cost"])
        self.assertFalse(report["codex"]["capabilities"]["price_configured"])
        self.assertTrue(report["codex"]["capabilities"]["structured_output"])     # native --output-schema
        self.assertNotIn("price_configured", report["claude"]["capabilities"])   # claude reports its cost
        self.assertEqual(report["gemini"]["missing_flags"], [])
        self.assertIsNone(report["claude"]["missing_flags"])
        self.assertEqual(report["claude"]["tiers"], {"cheap": "haiku", "standard": "sonnet", "deep": "opus"})
        self.assertEqual(report["gemini"]["tiers"]["cheap"], "flash")
        self.assertEqual(report["codex"]["tiers"]["cheap"], None)
        self.assertTrue(report["gemini"]["version"].startswith("0.9.9"))
        cp = self.ok("engines", "--json", env={"FAKE_ENGINE_CODEX_LOGGED_OUT": "1",
                                                "AGENT_SESSIONS_CODEX_PRICE": "input=1,cached=0.1,output=10",
                                                "AGENT_SESSIONS_CODEX_MODEL_CHEAP": "mini"})
        report = json.loads(cp.stdout)
        self.assertEqual(report["codex"]["auth"], "missing")
        self.assertTrue(report["codex"]["capabilities"]["price_configured"])
        self.assertEqual(report["codex"]["tiers"]["cheap"], "mini")
        human = self.ok("engines").stdout
        self.assertIn("experimental", human)
        lines = human.splitlines()
        codex_at = [i for i, l in enumerate(lines) if l.startswith("codex ")][0]
        self.assertTrue(lines[codex_at + 3].lstrip().startswith("cannot"), lines[codex_at + 3])
        self.assertNotIn("structured_output", lines[codex_at + 3])
        # A gemini CLI whose --help lacks an adapter flag is flagged before any spawn.
        env = {"FAKE_ENGINE_GEMINI_HELP_DROP": "--skip-trust"}
        report = json.loads(self.ok("engines", "--json", env=env).stdout)
        self.assertEqual(report["gemini"]["missing_flags"], ["--skip-trust"])
        self.assertIn("--help lacks --skip-trust", self.ok("engines", env=env).stdout)
        cp = self.run_as("doctor", env=env)
        self.assertIn("gemini --help does not list --skip-trust", cp.stdout)
        self.assertEqual(self.run_as("doctor", "--engine", "gemini", env=env).returncode, 1)
        self.assertIn("--help lists", self.run_as("doctor").stdout)
        self.assertIn("auth ok", human)
        cp = self.run_as("engines", "--json", env={"PATH": self.restricted_path(engines=("claude", "codex"))})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        report = json.loads(cp.stdout)
        self.assertIsNone(report["gemini"]["installed"])
        self.assertIsNone(report["gemini"]["version"])
        self.assertTrue(report["claude"]["installed"])
        cp = self.run_as("engines", env={"PATH": self.restricted_path(engines=())})
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("not installed", cp.stdout)

    def test_parsers_unit(self):
        mod = script_module()
        events = []
        p = mod.ClaudeParser(lambda kind, text, data=None: events.append((kind, text, data)))
        p.feed({"type": "system", "subtype": "init", "session_id": "s1", "model": "haiku"})
        p.feed({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
                "error": "rate_limit", "retry_delay_ms": 1000})
        p.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"},
                                                             {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
        p.feed({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s1",
                "total_cost_usd": 0.5, "num_turns": 2,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "modelUsage": {"m1": {"inputTokens": 100, "outputTokens": 10, "cacheReadInputTokens": 50,
                                      "cacheCreationInputTokens": 5},
                               "m2": {"inputTokens": 1, "outputTokens": 1}},
                "structured_output": {"n": 1},
                "permission_denials": [{"tool_name": "Write", "tool_input": {"file_path": "/x"}}]})
        self.assertEqual([e[0] for e in events], ["init", "api_retry", "assistant", "tool_start", "result"])
        self.assertEqual(p.usage, {"input_tokens": 106, "cached_input_tokens": 50, "output_tokens": 11,
                                   "reasoning_output_tokens": 0, "total_tokens": 167})
        self.assertEqual(p.structured_output, {"n": 1})
        self.assertEqual(p.denials, [{"tool": "Write", "target": "/x"}])
        self.assertEqual(p.retry_delay_ms, 1000)
        self.assertEqual(p.tool_calls, 1)
        self.assertTrue(p.got_terminal)
        self.assertIn("1 tool call denied under --readonly: Write /x", p.denial_summary(True))

        c = mod.CodexParser(lambda kind, text, data=None: events.append((kind, text, data)))
        c.feed({"type": "thread.started", "thread_id": "t1"})
        c.feed({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py"}]}})
        c.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}})
        c.feed({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                  "output_tokens": 20, "reasoning_output_tokens": 5}})
        self.assertEqual(c.usage, {"input_tokens": 60, "cached_input_tokens": 40, "output_tokens": 20,
                                   "reasoning_output_tokens": 5, "total_tokens": 120})
        self.assertEqual(c.session_id, "t1")
        self.assertEqual(c.answer, "answer")
        self.assertEqual(events[-2][0], "assistant")

        g = mod.GeminiParser(lambda kind, text, data=None: events.append((kind, text, data)))
        g.feed({"type": "init", "sessionId": "g1", "model": "pro"})
        g.feed({"type": "result", "status": "success", "response": "ok",
                "stats": {"models": {"a": {"tokens": {"prompt": 10, "candidates": 5, "cached": 2}},
                                     "b": {"tokens": {"prompt": 1, "candidates": 1}}}}})
        self.assertEqual(g.session_id, "g1")
        self.assertEqual(g.usage, {"input_tokens": 9, "cached_input_tokens": 2, "output_tokens": 6,
                                   "reasoning_output_tokens": 0, "total_tokens": 17})
        self.assertIsNone(mod.normalize_usage({}, "claude"))
        self.assertIsNone(mod.normalize_usage(None, "codex"))
        self.assertIsNone(mod.normalize_usage({"duration_ms": 5}, "gemini"))
        self.assertEqual(mod.parse_price("input=1.25,cached=0.125,output=10"),
                         {"input": 1.25, "cached": 0.125, "output": 10.0})
        self.assertEqual(mod.parse_price("input=2,output=4")["cached"], 2.0)
        for bad in ("input=1", "foo=1,output=2", "input=x,output=1", "input=-1,output=1",
                    "input=nan,output=1", "input=1,output=inf"):
            with self.assertRaises(ValueError):
                mod.parse_price(bad)
        self.assertEqual(mod.estimate_cost({"input_tokens": 1000000, "cached_input_tokens": 0, "output_tokens": 0},
                                           {"input": 1.0, "cached": 0.1, "output": 10.0}), 1.0)

    def test_classify_unit(self):
        mod = script_module()
        claude = mod.engine_for("claude")

        def parser(**attrs):
            p = mod.ClaudeParser(lambda *a: None)
            for key, value in attrs.items():
                setattr(p, key, value)
            return p

        self.assertEqual(claude.classify(parser(), 0, "", ("killed", "kill requested")), ("killed", "killed", "kill requested"))
        self.assertEqual(claude.classify(parser(), 0, "", ("timeout", "turn exceeded 5s"))[1], "timeout")
        self.assertEqual(claude.classify(parser(got_terminal=True, is_error=True, subtype="error_max_budget_usd"),
                                         1, "", None)[1], "budget")
        self.assertEqual(claude.classify(parser(got_terminal=True, is_error=True,
                                                subtype="error_max_structured_output_retries"), 1, "", None)[1], "schema")
        self.assertEqual(claude.classify(parser(), 1, "API Error: 429 Too Many Requests", None)[1], "rate_limit")
        self.assertEqual(claude.classify(parser(), 1, "Invalid API key", None)[1], "auth")
        # The reason carries the evidence: the last stderr line, or the engine's error text.
        self.assertEqual(claude.classify(parser(), 1, "starting\nInvalid API key\n\n", None),
                         ("error", "auth", "engine exited 1: Invalid API key"))
        self.assertEqual(claude.classify(parser(got_terminal=True, is_error=True, error_text="API Error: 429"),
                                         1, "", None), ("error", "rate_limit", "engine exited 1: API Error: 429"))
        self.assertEqual(claude.classify(parser(), 1, "", None), ("error", "crash", "engine exited 1"))
        self.assertEqual(claude.classify(parser(), 0, "", None), ("error", "crash", "no terminal event (no result event from engine)"))
        status, cls, reason = claude.classify(parser(got_terminal=True, is_error=True, subtype="error_during_execution",
                                                     error_text="boom", answer="boom"), 0, "", None)
        self.assertEqual((status, cls), ("error", "engine_error"))
        self.assertEqual(reason, "engine reported error_during_execution")
        self.assertEqual(claude.classify(parser(got_terminal=True, answer="x"), 0, "", None), ("ok", None, None))
        codex = mod.engine_for("codex")
        c = mod.CodexParser(lambda *a: None)
        c.feed({"type": "turn.failed", "error": {"message": "exceeded retry limit, last status: 429 Too Many Requests"}})
        self.assertEqual(codex.classify(c, 1, "", None)[1], "rate_limit")
        c = mod.CodexParser(lambda *a: None)
        c.feed({"type": "turn.completed", "usage": {}})
        self.assertEqual(codex.classify(c, 0, "", None), ("error", "engine_error", "no agent message from engine"))
        gemini = mod.engine_for("gemini")
        self.assertEqual(gemini.classify(mod.GeminiParser(lambda *a: None), 53, "", None)[1], "budget")
        self.assertEqual(gemini.classify(mod.GeminiParser(lambda *a: None), 1, "429 RESOURCE_EXHAUSTED", None)[1],
                         "rate_limit")
        self.assertEqual(gemini.readonly_flags(), ["--approval-mode", "plan"])
        self.assertEqual(mod.ENGINES, ("claude", "codex", "gemini"))

    def test_text_class_is_anchored(self):
        mod = script_module()
        cases = [
            ("API Error: 429 {\"type\":\"rate_limit_error\"}", "rate_limit"),
            ("exceeded retry limit, last status: 429 Too Many Requests", "rate_limit"),
            ("You've hit your usage limit. Try again in 120 seconds", "rate_limit"),
            ("429 RESOURCE_EXHAUSTED", "rate_limit"),
            ("insufficient_quota: You exceeded your current quota", "rate_limit"),
            ("API Error: 529 {\"type\":\"overloaded_error\"}", "rate_limit"),
            ("Invalid API key \u00b7 Please run /login", "auth"),
            ("Not logged in", "auth"),
            ("Authentication failed", "auth"),
            ("HTTP 401 Unauthorized", "auth"),
            ("OAuth token has expired", "auth"),
            # Not provider errors:
            ("cp: build/out.bin: Disk quota exceeded", None),
            ("Traceback: error at line 403 of parse.py", None),
            ("loaded credentials helper from ~/.config; segfault", None),
            ("authentication module imported; panic: nil map", None),
            ("tests: 429 passed, 0 failed", None),
        ]
        for text, want in cases:
            self.assertEqual(mod.text_class(text), want, text)
        # Overload is retried but is no account quota: it must not pause the engine.
        self.assertTrue(mod.OVERLOADED_RE.search("overloaded_error"))
        self.assertFalse(mod.RATE_LIMIT_RE.search("API Error: 529 overloaded_error"))

    def test_build_cmd_unit(self):
        mod = script_module()
        meta = {"binary": "/bin/claude", "conversation_id": "abc", "model": "haiku", "readonly": False, "name": "x"}
        cmd = mod.engine_for("claude").build_cmd(meta, 1, {"answer": "/a", "schema": None})
        self.assertEqual(cmd, ["/bin/claude", "-p", "--output-format", "stream-json", "--verbose", "--session-id", "abc",
                               "--model", "haiku", "--dangerously-skip-permissions"])
        cmd = mod.engine_for("claude").build_cmd(dict(meta, readonly=True), 2, {"answer": "/a", "schema": None})
        self.assertEqual(cmd[5:7], ["--resume", "abc"])
        self.assertIn("--strict-mcp-config", cmd)
        meta = {"binary": "/bin/codex", "conversation_id": "t", "model": None, "readonly": True, "name": "x"}
        cmd = mod.engine_for("codex").build_cmd(meta, 2, {"answer": "/a", "schema": "/s.json"})
        self.assertEqual(cmd, ["/bin/codex", "exec", "resume", "t", "--json", "--skip-git-repo-check", "-o", "/a",
                               "--output-schema", "/s.json", "-c", 'sandbox_mode="read-only"', "-"])
        meta = {"binary": "/bin/gemini", "conversation_id": "g", "model": "pro", "readonly": False, "name": "x"}
        cmd = mod.engine_for("gemini").build_cmd(meta, 2, {"answer": "/a", "schema": None})
        self.assertEqual(cmd, ["/bin/gemini", "--output-format", "stream-json", "--approval-mode", "yolo",
                               "--skip-trust", "--model", "pro", "--resume", "g"])


class SchemaTests(SessionTest):
    def check(self, schema, instance, valid, contains=None):
        mod = script_module()
        errors = mod.validate(instance, schema)
        self.assertEqual(not errors, valid, "%r against %r -> %r" % (instance, schema, errors))
        if contains:
            self.assertTrue(any(contains in e for e in errors), (contains, errors))
        return errors

    def test_schema_validator_subset(self):
        self.check({"type": "integer"}, 3, True)
        self.check({"type": "integer"}, 3.0, True)
        self.check({"type": "integer"}, 3.5, False, "expected integer, got number")
        self.check({"type": "integer"}, True, False)
        self.check({"type": "number"}, 3, True)
        self.check({"type": "number"}, True, False)
        self.check({"type": "boolean"}, True, True)
        self.check({"type": "null"}, None, True)
        self.check({"type": ["string", "null"]}, None, True)
        self.check({"type": ["string", "null"]}, 1, False, "expected string or null, got integer")
        self.check({"type": "array"}, [], True)
        self.check({"type": "object"}, [], False)
        self.check({"properties": {"a": {"type": "string"}}}, {"a": 1}, False, "$.a: expected string")
        self.check({"required": ["a", "b"]}, {"a": 1}, False, "missing required property 'b'")
        self.check({"properties": {"a": {}}, "additionalProperties": False}, {"a": 1, "b": 2}, False,
                   "$.b: additional property not allowed")
        self.check({"additionalProperties": {"type": "integer"}}, {"x": "s"}, False, "$.x: expected integer")
        self.check({"additionalProperties": {"type": "integer"}}, {"x": 1}, True)
        self.check({"enum": [1, "a", None]}, "a", True)
        self.check({"enum": [1, "a"]}, 2, False, "is not one of")
        self.check({"enum": [1]}, True, False)
        self.check({"const": {"k": [1, 2]}}, {"k": [1, 2]}, True)
        self.check({"const": 1}, 2, False, "expected the constant")
        self.check({"items": {"type": "integer"}}, [1, "x"], False, "$[1]: expected integer")
        self.check({"minItems": 2}, [1], False, "at least 2 items")
        self.check({"maxItems": 1}, [1, 2], False, "at most 1 items")
        self.check({"minimum": 0, "maximum": 1}, 0.5, True)
        self.check({"minimum": 0}, -1, False, "below the minimum")
        self.check({"maximum": 1}, 2, False, "above the maximum")
        self.check({"minLength": 2}, "a", False, "shorter than 2")
        self.check({"maxLength": 1}, "ab", False, "longer than 1")
        self.check({"pattern": "^a+$"}, "aaa", True)
        self.check({"pattern": "^a+$"}, "b", False, "does not match pattern")
        self.check({"pattern": "("}, "b", False, "invalid pattern")
        self.check({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 1, True)
        self.check({"anyOf": [{"type": "string"}, {"type": "integer"}]}, None, False, "anyOf")
        self.check({"oneOf": [{"type": "number"}, {"type": "integer"}]}, 1, False, "matches 2 schemas in oneOf")
        self.check({"oneOf": [{"type": "string"}, {"type": "integer"}]}, 1, True)
        self.check({"allOf": [{"type": "integer"}, {"minimum": 5}]}, 3, False, "allOf[1]")
        self.check({"allOf": [{"type": "integer"}, {"minimum": 5}]}, 7, True)
        self.check({"not": {"type": "string"}}, "s", False, "must not match")
        self.check({"not": {"type": "string"}}, 1, True)
        ref = {"definitions": {"pos": {"type": "integer", "minimum": 1}},
               "properties": {"n": {"$ref": "#/definitions/pos"}, "m": {"$ref": "#/$defs/neg"}},
               "$defs": {"neg": {"type": "integer", "maximum": -1}}}
        self.check(ref, {"n": 1, "m": -1}, True)
        self.check(ref, {"n": 0, "m": -1}, False, "$.n: 0 is below the minimum 1")
        self.check({"properties": {"n": {"$ref": "#/definitions/missing"}}}, {"n": 1}, False, "unresolved $ref")
        self.check({"properties": {"n": {"$ref": "http://x/y"}}}, {"n": 1}, False, "unsupported $ref")
        self.check({"properties": {"a": {"$ref": "#/definitions/loop"}}, "definitions": {"loop": {"$ref": "#/definitions/loop"}}},
                   {"a": 1}, False, "nesting deeper")
        nested = {"properties": {"items": {"type": "array", "items": {"properties": {"x": {"type": "integer"}},
                                                                      "required": ["x"]}}}}
        errors = self.check(nested, {"items": [{"x": 1}, {"x": "no"}, {}]}, False)
        self.assertEqual(errors, ["$.items[1].x: expected integer, got string", "$.items[2]: missing required property 'x'"])
        self.check(True, "anything", True)
        self.check(False, "anything", False, "no value is allowed")
        self.check({"type": "integer", "unknownKeyword": 1}, 1, True)

    def test_unknown_keywords_reported_once(self):
        mod = script_module()
        schema = {"type": "object", "title": "t", "description": "d", "default": {},
                  "properties": {"a": {"type": "string", "format": "email", "minLength": 1},
                                 "b": {"items": {"format": "date", "exclusiveMinimum": 0}},
                                 "c": {"anyOf": [{"multipleOf": 2}], "not": {"uniqueItems": True}}},
                  "patternProperties": {}, "definitions": {"d": {"dependencies": {}}}}
        self.assertEqual(mod.unknown_keywords(schema),
                         ["dependencies", "exclusiveMinimum", "format", "multipleOf", "patternProperties", "uniqueItems"])
        self.assertEqual(mod.unknown_keywords({"type": "object", "$schema": "x", "$id": "y"}), [])
        err, unknown = mod.check_schema([1])
        self.assertIn("JSON object", err)
        self.assertEqual(mod.check_schema({"format": "x"}), (None, ["format"]))

    def test_builtin_schemas_and_schema_command(self):
        mod = script_module()
        for name in ("result", "judge", "event"):
            cp = self.ok("schema", name)
            self.assertEqual(json.loads(cp.stdout), mod.SCHEMAS[name])
        self.assertEqual(json.loads(self.ok("schema").stdout), mod.RESULT_SCHEMA)
        cp = self.run_as("schema", "bogus")
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(mod.unknown_keywords(mod.RESULT_SCHEMA), [])
        self.assertEqual(mod.unknown_keywords(mod.EVENT_SCHEMA), [])
        self.assertEqual(mod.unknown_keywords(mod.JUDGE_SCHEMA), [])
        verdict = {"verdict": "revise", "confidence": 0.7, "summary": "s",
                   "findings": [{"severity": "high", "file": "a.py", "line": 3, "issue": "bug", "fix": None}]}
        self.assertEqual(mod.validate(verdict, mod.JUDGE_SCHEMA), [])
        self.assertTrue(mod.validate(dict(verdict, verdict="maybe"), mod.JUDGE_SCHEMA))
        self.assertTrue(mod.validate(dict(verdict, extra=1), mod.JUDGE_SCHEMA))
        partial = {"severity": "low", "issue": "x"}
        self.assertTrue(mod.validate(dict(verdict, findings=[partial]), mod.JUDGE_SCHEMA))

        def strict(node, path="$"):
            # OpenAI strict structured output (codex --output-schema): every
            # object lists all its properties as required and forbids extras.
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                    self.assertEqual(sorted(node.get("required", [])), sorted(node.get("properties", {})), path)
                for key, sub in node.items():
                    strict(sub, "%s.%s" % (path, key))
            elif isinstance(node, list):
                for i, sub in enumerate(node):
                    strict(sub, "%s[%d]" % (path, i))
        strict(mod.JUDGE_SCHEMA)
        event = {"ts": 1.0, "iso": "x", "turn": 1, "attempt": 1, "type": "assistant", "text": "t", "data": None}
        self.assertEqual(mod.validate(event, mod.EVENT_SCHEMA), [])
        self.assertTrue(mod.validate(dict(event, type="weird"), mod.EVENT_SCHEMA))
        self.assertIn("rate_limit", mod.FAILURE_CLASSES)
        self.assertEqual(len(mod.FAILURE_CLASSES), 12)
        self.assertEqual(sys.executable and mod.VERSION, "3.1.0")
        self.assertTrue(os.path.exists(SCRIPT))
