"""Follow-through of the hardening pass: caps on judges, the costs a view
reports when an engine gives tokens only, the best-of budget line, the group
note of conflicts, plan defaults, the gemini MCP warning, recover's note on
other owners and the shared-tree diff's exclusions."""

import json
import os
import subprocess
import sys

from helpers import SKILL, SessionTest

VERDICT = {"verdict": "approve", "confidence": 0.9, "summary": "fine", "findings": []}


class JudgeCapTests(SessionTest):
    def test_judge_caps_reach_the_judge_and_unknown_cost_shows_tokens(self):
        repo = self.repo()
        self.spawn("claude", "w", "@@edit:f.txt=one @@commit:first", "--worktree", directory=repo)
        self.assertEqual(self.wait_result("w")["status"], "ok")
        verdict = self.write_file("v.json", json.dumps(VERDICT))
        cp = self.run_as("judge", "w", "--max-tokens", "5000", "--timeout", "30",
                         env={"FAKE_ENGINE_ANSWER_FILE": verdict})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.meta("w-judge")["budget"]["tokens_limit"], 5000)
        self.assertEqual(self.meta("w-judge")["mcp"], "none")
        # The codex judge reports tokens only: the verdict line says so, never $0.
        self.assertIn("tokens, cost unknown", cp.stdout)
        cp = self.run_as("judge", "w", "--budget-usd", "0.5", "--engine", "codex", "--timeout", "30")
        self.assertEqual(cp.returncode, 0)          # the stored verdict is reused, nothing new starts
        cp = self.run_as("judge", "w", "--max-tokens", "0")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--max-tokens must be at least 1", cp.stderr)

    def test_result_view_reports_tokens_when_cost_is_unknown(self):
        self.spawn("codex", "c", "hi @@tokens:100,20")
        self.wait_result("c")
        cp = self.ok("result", "c")
        self.assertIn("total 120", cp.stdout)
        self.assertIn("cost: unknown", cp.stdout)
        self.assertIn("AGENT_SESSIONS_CODEX_PRICE", cp.stdout)


class GroupTests(SessionTest):
    def test_best_of_budget_line_and_conflicts_note(self):
        repo = self.repo()
        cp = self.ok("spawn", "claude", "g", repo, "@@edit:same.txt=x @@commit:c", "--n", "2", "--budget-usd", "0.5")
        self.assertIn("$0.5 per member, so up to 2 x that for the group", cp.stdout)
        self.assertIn("access    : full: approvals and sandbox off", cp.stdout)
        self.assertEqual(self.wait("g", timeout=60).returncode, 0)
        cp = self.run_as("conflicts", "g")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("g-1 <-> g-2 : same.txt", cp.stdout)
        self.assertIn("check the winner against the parent", cp.stdout)
        cp = self.run_as("conflicts", "g-1")
        self.assertNotIn("check the winner", cp.stdout)
        self.ok("reap", "g", "--force")


class PlanDefaultsTests(SessionTest):
    def test_defaults_may_set_engine_and_a_node_may_use_prompt_file(self):
        self.write_file("p.txt", "from a file")
        plan = {"name": "pd", "dir": self.work, "defaults": {"engine": "claude"},
                "nodes": [{"name": "a", "prompt-file": "p.txt"}]}
        path = self.write_file("plan.json", json.dumps(plan))
        self.ok("plan", "run", path)
        self.assertEqual(self.wait_result("pd-a")["status"], "ok")
        self.assertEqual(self.calls("pd-a")[0]["stdin"], "from a file")
        plan = {"name": "pe", "dir": self.work, "defaults": {"after": ["a"]},
                "nodes": [{"name": "a", "engine": "claude", "prompt": "x"}]}
        cp = self.run_as("plan", "run", self.write_file("plan2.json", json.dumps(plan)))
        self.assertEqual(cp.returncode, 2)
        self.assertIn("'after' belongs on a node", cp.stderr)


class EngineNoticeTests(SessionTest):
    def test_gemini_says_mcp_none_is_not_enforced(self):
        cp = self.spawn("gemini", "gm", "hi")
        self.assertIn("--mcp none is not enforced", cp.stderr)
        self.assertIn("MCP servers not switched off", cp.stdout)
        self.wait_result("gm")


class OwnershipTests(SessionTest):
    def test_recover_json_names_recoverable_children_of_other_owners(self):
        self.fake_meta("someone", owner="env:other-owner")
        doc = json.loads(self.ok("recover", "--dry-run", "--json").stdout)
        self.assertEqual(doc["children"], [])
        self.assertEqual(doc["other_owners"], ["someone"])
        self.assertFalse(self.has("someone", "result.json"))     # another owner's child is left alone

    def test_help_forms(self):
        self.assertIn("usage:", self.ok("help").stdout)
        self.assertEqual(self.run_as("help", "_run").returncode, 2)
        # A prompt after -- may contain --help.
        cp = self.ok("spawn", "claude", "hh", self.work, "--", "explain --help output")
        self.assertIn("spawned   : hh", cp.stdout)
        self.wait_result("hh")


class TreeDiffTests(SessionTest):
    def test_exclusions_and_spaced_paths(self):
        repo = self.repo()
        with open(os.path.join(repo, "sp ace.txt"), "w") as fh:
            fh.write("base\n")
        self.commit_all(repo, "spaced")
        with open(os.path.join(repo, "sp ace.txt"), "a") as fh:
            fh.write("user work\n")
        with open(os.path.join(repo, "child new.txt"), "w") as fh:
            fh.write("child work\n")
        code = ("import sys, json; sys.path.insert(0, sys.argv[1]); from agent_sessions import gitops\n"
                "cp, note = gitops.tree_diff(sys.argv[2], 'HEAD', sys.argv[3], exclude=['sp ace.txt'])\n"
                "print(json.dumps({'rc': cp.returncode, 'patch': cp.stdout, 'note': note}))")
        cp = subprocess.run([sys.executable, "-c", code, os.path.join(SKILL, "scripts"), repo,
                             os.path.join(self.box, "idx")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, env=self.environ())
        self.assertEqual(cp.returncode, 0, cp.stderr)
        doc = json.loads(cp.stdout)
        self.assertIn("+child work", doc["patch"])
        self.assertNotIn("user work", doc["patch"])
        self.assertIn("1 path already uncommitted at spawn left out (sp ace.txt)", doc["note"])
        self.assertEqual(self.git(repo, "status", "--porcelain").stdout.count("\n"), 2)   # tree untouched
