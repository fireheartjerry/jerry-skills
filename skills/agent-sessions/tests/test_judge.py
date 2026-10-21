"""judge: a read-only child of the other engine, held to JUDGE_SCHEMA, whose
verdict is recorded in judge.json and merged into result/status --json."""

import json
import os

from helpers import SessionTest, script_module

VERDICT = {"verdict": "revise", "confidence": 0.7, "summary": "Mostly right; two gaps.",
           "findings": [{"severity": "high", "file": "feature.txt", "line": 1, "issue": "no test", "fix": "add one"},
                        {"severity": "low", "file": None, "line": None, "issue": "naming", "fix": None}]}


class JudgeTests(SessionTest):
    def verdict_file(self, name, obj):
        return self.write_file(name, json.dumps(obj) if not isinstance(obj, str) else obj)

    def judged_child(self, repo, name="x"):
        self.spawn("claude", name, "add the feature @@edit:feature.txt=new_feature @@commit:add_feature",
                   "--worktree", directory=repo)
        self.assertEqual(self.wait_result(name)["status"], "ok")

    def test_judge_cross_engine_readonly_schema(self):
        repo = self.repo()
        self.judged_child(repo)
        env = {"FAKE_ENGINE_ANSWER_FILE": self.verdict_file("verdict.json", VERDICT)}
        cp = self.run_as("judge", "x", "--timeout", "30", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("verdict   : revise (codex standard, 2 findings, ", cp.stdout)
        self.assertIn("confidence : 0.7", cp.stdout)
        self.assertIn("(judge output, untrusted)", cp.stdout)
        self.assertIn("[high] feature.txt:1: no test", cp.stdout)
        jm = self.meta("x-judge")
        self.assertEqual(jm["engine"], "codex")
        self.assertTrue(jm["readonly"])
        self.assertEqual(jm["role"], "review")
        self.assertEqual(jm["workdir"], self.sdir("x"))
        self.assertEqual(jm["judge_of"], "x")
        self.assertEqual(jm["tier"], "standard")
        call = self.calls("x-judge")[0]
        self.assertIn('sandbox_mode="read-only"', call["argv"])
        self.assertIn("--output-schema", call["argv"])
        self.assertIn("judge/brief.md", call["stdin"])
        self.assertIn("./worktree", call["stdin"])
        self.assertIn("do not run commands that change files", call["stdin"])
        for f in ("brief.md", "answer.md", "diff.patch", "schema.json"):
            self.assertTrue(self.has("x", "judge", f), f)
        jdir = os.path.join(self.sdir("x"), "judge")
        with open(os.path.join(jdir, "diff.patch")) as fh:
            self.assertIn("+new feature", fh.read())
        with open(os.path.join(jdir, "brief.md")) as fh:
            self.assertIn("add the feature", fh.read())
        with open(os.path.join(jdir, "answer.md")) as fh:
            self.assertEqual(fh.read(), self.result("x")["answer"])
        self.assertEqual(self.read("x", "judge", "schema.json"), script_module().JUDGE_SCHEMA)
        self.wait_result("x-judge")
        judge = self.judge_record("x")
        self.assertEqual((judge["engine"], judge["tier"], judge["judge_child"]), ("codex", "standard", "x-judge"))
        self.assertEqual(judge["judged_run"]["finished_iso"], self.result("x")["finished_iso"])
        res = self.result_json("x")
        self.assertEqual(res["judge"]["verdict"], "revise")
        self.assertEqual(len(res["judge"]["findings"]), 2)
        status = json.loads(self.ok("status", "x", "--json").stdout)
        self.assertEqual(status["judge"]["verdict"], "revise")
        self.assertIn("judge     : revise", self.ok("result", "x").stdout)
        # Asking again prints the stored verdict without a new judge run.
        cp = self.run_as("judge", "x", "--json", env=env)
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(json.loads(cp.stdout)["verdict"], "revise")
        self.assertEqual(len(self.calls("x-judge")), 1)
        # The judge child is not listed for reap on its own; reaping x takes it along.
        self.ok("reap", "x", "--force")
        self.assertFalse(os.path.exists(self.sdir("x-judge")))

    def test_invalid_verdict_fails_then_retries(self):
        repo = self.repo()
        self.judged_child(repo)
        bad = {"FAKE_ENGINE_ANSWER_FILE": self.verdict_file("bad.json", {"verdict": "maybe"})}
        cp = self.run_as("judge", "x", "--engine", "claude", "--timeout", "30", env=bad)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("schema", cp.stdout)
        self.assertIn("starts a new judge", cp.stdout)
        self.assertFalse(self.has("x", "judge.json"))
        jres = self.wait_result("x-judge")
        self.assertEqual(jres["failure_class"], "schema")
        self.assertTrue(self.meta("x-judge")["readonly"])
        # Asking again after a failed judge is the request to retry: a new judge runs.
        good = {"FAKE_ENGINE_ANSWER_FILE": self.verdict_file("verdict.json", VERDICT)}
        cp = self.run_as("judge", "x", "--engine", "claude", "--timeout", "30", env=good)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.judge_record("x")["engine"], "claude")
        prompts = [c for c in self.calls("x-judge") if "independent reviewer" in c["stdin"]]
        self.assertEqual(len(prompts), 2)

    def test_judge_pending_then_reuse(self):
        repo = self.repo()
        self.judged_child(repo)
        env = {"FAKE_ENGINE_ANSWER_FILE": self.verdict_file("verdict.json", VERDICT),
               "FAKE_ENGINE_DIRECTIVES": "@@sleep:4"}
        cp = self.run_as("judge", "x", "--timeout", "1", env=env)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertIn("pending", cp.stdout)
        self.assertIn("reused", cp.stderr)
        cp = self.run_as("judge", "x", "--timeout", "1", "--json", env=env)
        self.assertEqual(cp.returncode, 3)
        self.assertEqual(json.loads(cp.stdout)["status"], "pending")
        cp = self.run_as("judge", "x", "--timeout", "30", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(len(self.calls("x-judge")), 1)
        self.judge_record("x")

    def test_new_run_gets_a_new_judge(self):
        repo = self.repo()
        self.judged_child(repo)
        env = {"FAKE_ENGINE_ANSWER_FILE": self.verdict_file("verdict.json", VERDICT)}
        self.ok("judge", "x", "--timeout", "30", env=env)
        self.wait_result("x-judge")
        # A resumed child writes a new result (new finished_iso); the old verdict
        # describes other work and must not be served for it.
        path = os.path.join(self.sdir("x"), "result.json")
        res = self.result("x")
        res["finished_iso"] = "2099-01-01T00:00:00+0000"
        with open(path, "w") as fh:
            json.dump(res, fh)
        approve = {"FAKE_ENGINE_ANSWER_FILE": self.verdict_file("approve.json",
                                                                dict(VERDICT, verdict="approve", findings=[]))}
        cp = self.run_as("judge", "x", "--timeout", "30", env=approve)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("verdict   : approve", cp.stdout)
        self.assertEqual(len(self.calls("x-judge")), 2)
        judge = self.judge_record("x")
        self.assertEqual(judge["judged_run"]["finished_iso"], "2099-01-01T00:00:00+0000")

    def test_judge_without_git_and_with_brief(self):
        self.spawn("codex", "plain", "summarise the notes", "--tree-max-tokens", "100000")
        self.wait_result("plain")
        ledger = self.meta("plain")["budget"]["ledger"]
        self.assertTrue(ledger)
        brief = self.write_file("brief.txt", "The brief written by the parent.\n")
        env = {"FAKE_ENGINE_ANSWER_FILE": self.verdict_file("verdict.json", dict(VERDICT, verdict="approve",
                                                                                  findings=[]))}
        cp = self.run_as("judge", "plain", "--brief", brief, "--tier", "cheap", "--timeout", "30", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("verdict   : approve (claude cheap, 0 findings, ", cp.stdout)
        self.assertFalse(self.has("plain", "judge", "diff.patch"))
        with open(os.path.join(self.sdir("plain"), "judge", "brief.md")) as fh:
            self.assertEqual(fh.read(), "The brief written by the parent.\n")
        call = self.calls("plain-judge")[0]
        self.assertIn(self.work, call["stdin"])
        self.assertIn("--json-schema", call["argv"])
        self.assertNotIn("--dangerously-skip-permissions", call["argv"])
        judge = self.judge_record("plain")
        self.assertEqual((judge["engine"], judge["tier"]), ("claude", "cheap"))
        # The judge spends from the judged child's tree ledger.
        self.assertEqual(self.meta("plain-judge")["budget"]["ledger"], ledger)
        self.assertIn("plain-judge", self.ledger(ledger)["children"])
        self.assertIsNotNone(judge["cost_usd"])

    def test_judge_refusals(self):
        self.spawn("claude", "busy", "@@sleep:10")
        cp = self.run_as("judge", "busy")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("has not finished", cp.stderr)
        self.ok("kill", "busy")
        self.spawn("claude", "done", "hi")
        self.wait_result("done")
        self.spawn("claude", "done-judge", "an unrelated child that took the name")
        self.wait_result("done-judge")
        cp = self.run_as("judge", "done")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("not the judge of 'done'", cp.stderr)
        self.assertEqual(len(self.calls("done-judge")), 1)
        for bad in (["--engine", "gpt"], ["--tier", "huge"]):
            cp = self.run_as("judge", "done", *bad)
            self.assertEqual(cp.returncode, 2, bad)
        long_name = "n" * 36
        self.spawn("claude", long_name, "hi")
        self.wait_result(long_name)
        cp = self.run_as("judge", long_name)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("40 characters", cp.stderr)
        self.assertEqual(self.run_as("judge", "nosuch").returncode, 2)
