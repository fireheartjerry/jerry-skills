"""Long-running and unattended children: the limits line and the uncapped
warning at spawn, elapsed time and the stop time in status, AGE in list, the
repository features that break a worktree commit, a parent tree written during
the run, the verify gate's base commit, the judge's cap floor and the revise
round limit, and doctor's adapter flag check."""

import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest

from helpers import SKILL, SessionTest

sys.path.insert(0, os.path.join(SKILL, "scripts"))
from agent_sessions import gitops  # noqa: E402

VERDICT = {"verdict": "revise", "confidence": 0.7, "summary": "One gap.",
           "findings": [{"severity": "high", "file": "feature.txt", "line": 1, "issue": "no test", "fix": "add one"}]}


class LimitsTests(SessionTest):
    def test_spawn_states_limits_and_warns_when_uncapped(self):
        cp = self.spawn("claude", "free", "say hi")
        self.assertIn("limits    : none: no --budget-usd, --max-tokens or --deadline; per-turn timeout 3600s, "
                      "stall 900s", cp.stdout)
        self.assertIn("no spend cap on a child that runs with approvals off", cp.stderr)
        cp = self.spawn("claude", "capped", "say hi", "--budget-usd", "0.5", "--deadline", "600", "--retries", "1")
        self.assertIn("limits    : $0.5, deadline 600s; per-turn timeout 3600s, stall 900s, 1 retry", cp.stdout)
        self.assertNotIn("no spend cap", cp.stderr)
        cp = self.spawn("claude", "reader", "say hi", "--role", "research")
        self.assertNotIn("no spend cap", cp.stderr)
        for name in ("free", "capped", "reader"):
            self.wait_result(name)

    def test_status_reports_elapsed_and_when_it_stops(self):
        self.spawn("claude", "slow", "@@sleep:30 work", "--deadline", "600", "--max-tokens", "100000")
        self.wait_until(lambda: self.has("slow", "progress.json") and (self.progress("slow") or {}).get("phase") == "turn", message="turn never started")
        cp = self.run_as("status", "slow")
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        self.assertRegex(cp.stdout, r"elapsed   : \d+s since start; this turn \d+s")
        # The turn timeout (3600 s) is later than the 600 s deadline.
        self.assertRegex(cp.stdout, r"ends by   : \d\d:\d\d:\d\d at the latest \(--deadline, in \d+m\d\ds\)")
        self.assertIn("budget    : remaining 100000 tokens", cp.stdout)
        doc = json.loads(self.run_as("status", "slow", "--json").stdout)
        self.assertIsInstance(doc["elapsed_s"], int)
        self.assertIsInstance(doc["turn_elapsed_s"], int)
        self.assertEqual(doc["ends_by_reason"], "--deadline")
        self.assertAlmostEqual(doc["ends_by_epoch"], self.meta("slow")["deadline_epoch"], delta=1)
        listing = self.ok("list").stdout.splitlines()
        self.assertIn("AGE", listing[0].split())
        row = [l for l in listing if l.startswith("slow")][0]
        self.assertRegex(row, r"^slow\s+claude\s+\S+\s+\S+\s+\d+s\s")
        self.ok("kill", "slow")

    def test_uncapped_status_says_so(self):
        self.spawn("claude", "open", "@@sleep:30 work")
        self.wait_until(lambda: self.has("open", "progress.json") and (self.progress("open") or {}).get("phase") == "turn", message="turn never started")
        cp = self.run_as("status", "open")
        self.assertIn("budget    : no spend cap", cp.stdout)
        self.assertIn("(turn timeout 3600s, in ", cp.stdout)
        self.ok("kill", "open")


class RepoFeatureTests(SessionTest):
    def test_features_detected(self):
        # Only this repository's configuration: a developer's global
        # core.hooksPath or commit.gpgsign must not leak in.
        saved = dict((k, os.environ.get(k)) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"))
        os.environ.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        try:
            self.check_features()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def check_features(self):
        repo = self.repo()
        self.assertEqual(gitops.repo_features(repo), [])
        self.write_file(os.path.join(repo, ".gitmodules"), "[submodule \"x\"]\n")
        self.write_file(os.path.join(repo, ".gitattributes"), "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        self.write_file(os.path.join(repo, ".pre-commit-config.yaml"), "repos: []\n")
        self.assertEqual(self.git(repo, "config", "commit.gpgsign", "true").returncode, 0)
        keys = [k for k, _ in gitops.repo_features(repo)]
        self.assertEqual(keys, ["hooks", "signing", "submodules", "lfs"])

    def test_failing_commit_hook_is_warned_and_explained(self):
        repo = self.repo()
        hook = os.path.join(repo, ".git", "hooks", "pre-commit")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\necho 'lint: node_modules missing' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        cp = self.spawn("claude", "hooked", "add it @@edit:new.txt=hi @@commit:add_it", "--worktree",
                        "--max-tokens", "100000", directory=repo)
        self.assertIn("the repository has commit hooks (pre-commit)", cp.stderr)
        self.assertIn("never --no-verify", cp.stderr)
        res = self.wait_result("hooked")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["git"]["dirty"])
        self.assertEqual(res["git"]["commits"], 0)
        warning = [w for w in res["warnings"] if "uncommitted changes" in w]
        self.assertTrue(warning, res["warnings"])
        self.assertIn("new.txt", warning[0])
        self.assertIn("commit hooks (pre-commit)", warning[0])
        self.assertIn("uncommitted changes", self.ok("result", "hooked").stdout)
        cp = self.run_as("collect", "hooked", "--merge", cwd=repo)
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("has uncommitted changes", cp.stderr)


class ParentTreeTests(SessionTest):
    def test_absolute_path_write_into_parent_is_reported(self):
        repo = self.repo()
        stray = os.path.join(repo, "stray.txt")
        self.spawn("claude", "leaky", "@@edit:%s=oops @@edit:inside.txt=ok @@commit:work" % stray, "--worktree",
                   "--max-tokens", "100000", directory=repo)
        res = self.wait_result("leaky")
        self.assertTrue(os.path.exists(stray))
        moved = [w for w in res["warnings"] if "changed during the run" in w]
        self.assertTrue(moved, res["warnings"])
        self.assertIn("stray.txt", moved[0])
        self.assertIn("absolute path", moved[0])

    def test_untouched_parent_gives_no_warning(self):
        repo = self.repo()
        self.write_file(os.path.join(repo, "mine.txt"), "user work\n")
        self.spawn("claude", "clean", "@@edit:inside.txt=ok @@commit:work", "--worktree", "--max-tokens", "100000",
                   directory=repo)
        res = self.wait_result("clean")
        self.assertFalse([w for w in res["warnings"] if "changed during the run" in w], res["warnings"])


class GateEnvTests(SessionTest):
    def test_verify_gets_base_and_worktree_and_can_check_scope(self):
        repo = self.repo()
        out = os.path.join(self.box, "gate-env.txt")
        verify = 'echo "$AGENT_SESSION_BASE|$AGENT_SESSION_WORKTREE" > %s' % out
        self.spawn("claude", "envd", "@@edit:src/a.txt=a @@commit:work", "--worktree", "--verify", verify,
                   "--max-tokens", "100000", directory=repo)
        res = self.wait_result("envd")
        self.assertEqual(res["verify"]["status"], "pass")
        with open(out) as fh:
            base, worktree = fh.read().strip().split("|")
        wt = self.meta("envd")["worktree"]
        self.assertEqual(base, wt["base_commit"])
        self.assertEqual(worktree, wt["path"])
        # The documented path-scope recipe fails a change outside src/.
        scope = 'test -n "$AGENT_SESSION_BASE" && ! git diff --name-only "$AGENT_SESSION_BASE" HEAD | grep -qv "^src/"'
        self.spawn("claude", "scoped", "@@edit:src/b.txt=b @@edit:README.md=changed @@commit:work", "--worktree",
                   "--verify", scope, "--verify-fixups", "0", "--max-tokens", "100000", directory=repo)
        res = self.wait_result("scoped")
        self.assertEqual((res["status"], res["failure_class"]), ("error", "verify"))
        self.spawn("claude", "inscope", "@@edit:src/c.txt=c @@commit:work", "--worktree", "--verify", scope,
                   "--verify-fixups", "0", "--max-tokens", "100000", directory=repo)
        self.assertEqual(self.wait_result("inscope")["verify"]["status"], "pass")


class JudgeLoopTests(SessionTest):
    def test_revise_rounds_stop_after_two(self):
        repo = self.repo()
        self.spawn("claude", "x", "add the feature @@edit:feature.txt=new_feature @@commit:add_feature",
                   "--worktree", "--max-tokens", "100000", directory=repo)
        self.assertEqual(self.wait_result("x")["status"], "ok")
        env = {"FAKE_ENGINE_ANSWER_FILE": self.write_file("verdict.json", json.dumps(VERDICT))}
        for k in (1, 2):
            cp = self.run_as("judge", "x", "--timeout", "30", env=env)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("revise round %d of 2" % k, cp.stdout)
            self.ok("resume", "x", "--message", "Findings to check: feature.txt:1 no test. Commit your work.")
            self.assertEqual(self.wait_result("x")["status"], "ok")
        cp = self.run_as("judge", "x", "--timeout", "30", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        nxt = [l for l in cp.stdout.splitlines() if l.startswith("next")][0]
        self.assertIn("stop: x has had 2 revise rounds", nxt)
        self.assertNotIn("agent-session resume", nxt)

    def test_codex_judge_cap_below_floor_warns_and_explains_the_failure(self):
        repo = self.repo()
        self.spawn("claude", "y", "@@edit:f.txt=x @@commit:f", "--worktree", "--max-tokens", "100000", directory=repo)
        self.assertEqual(self.wait_result("y")["status"], "ok")
        cp = self.run_as("judge", "y", "--max-tokens", "1", "--timeout", "30")
        self.assertIn("below what a codex judge reads", cp.stderr)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("(budget)", cp.stdout)
        self.assertIn("--max-tokens 400000 or more", cp.stdout)
        # A claude judge has no such floor: it can stop mid-turn.
        cp = self.run_as("judge", "y", "--engine", "claude", "--max-tokens", "1000000", "--timeout", "1")
        self.assertNotIn("below what a codex judge reads", cp.stderr)


class DoctorFlagTests(SessionTest):
    def test_doctor_checks_claude_and_codex_help(self):
        cp = self.ok("doctor")
        self.assertRegex(cp.stdout, r"(?m)^ok\s+claude-flags\s*:?.*claude --help lists the 12 flags")
        self.assertRegex(cp.stdout, r"(?m)^ok\s+codex-flags\s*:?.*codex exec --help lists the 7 flags")
        self.assertIn("last tested live with 2.1.308", cp.stdout)
        cp = self.run_as("doctor", env={"FAKE_ENGINE_CLAUDE_HELP_DROP": "--strict-mcp-config"})
        line = [l for l in cp.stdout.splitlines() if "claude-flags" in l][0]
        self.assertTrue(line.startswith("warn"), line)
        self.assertIn("--strict-mcp-config", line)
        cp = self.run_as("doctor", "--engine", "codex", env={"FAKE_ENGINE_CODEX_HELP_DROP": "--output-schema"})
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertTrue(re.search(r"(?m)^FAIL\s+codex-flags", cp.stdout), cp.stdout)


class ScenarioRunnerTests(unittest.TestCase):
    """evals/run_scenarios.py pieces that spend nothing."""

    def load(self):
        path = os.path.join(SKILL, "evals", "run_scenarios.py")
        spec = importlib.util.spec_from_file_location("run_scenarios", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, path

    def test_offline_pieces(self):
        mod, path = self.load()
        ids = [s["id"] for s in mod.load_json("scenarios.json")["scenarios"]]
        self.assertTrue(set(mod.HARD) <= set(ids), set(mod.HARD) - set(ids))
        self.assertEqual(mod.parse_grade('```json\n{"score": 11.4, "missed": []}\n```')["score"], 10)
        self.assertIsNone(mod.parse_grade("no json here"))
        b = mod.Budget(0.10)
        self.assertTrue(b.allows(0.08))
        b.charge(None, 0.08)
        self.assertFalse(b.allows(0.08))
        events = [{"type": "assistant", "message": {"content": [
                      {"type": "tool_use", "input": {"command": "agent-session status x"}}]}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "content": "state : running"}]}},
                  {"type": "result", "result": "Still running."}]
        text = mod.transcript_of(events)
        self.assertIn("BASH: agent-session status x", text)
        self.assertIn("OUTPUT: state : running", text)
        self.assertIn("FINAL REPLY: Still running.", text)
        self.assertNotIn("SKILL.md ---", mod.executor_system("none", "/r"))
        self.assertIn("--- SKILL.md ---", mod.executor_system("skill", "/r"))
        cp = subprocess.run([sys.executable, path, "--dry-run"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("would run 16 executor and 16 grader calls", cp.stdout)
        self.assertIn("hard cap $0.80", cp.stdout)


if __name__ == "__main__":
    unittest.main()
