"""Best-of-N: spawn --n, groups, select (with and without judges), collect and
reap of a group."""

import json
import os

from helpers import SessionTest, assert_valid_result

VERDICT = {"verdict": "revise", "confidence": 0.6, "summary": "Works; one gap.",
           "findings": [{"severity": "medium", "file": "a.txt", "line": 1, "issue": "untested", "fix": None}]}


class BestOfTests(SessionTest):
    def test_bestof_spawn_and_select(self):
        repo = self.repo()
        prompt = ("@@edit:f.txt=ok @@commit:done @@when:-2:crash @@when:-3:edit:g.txt=big_big_big "
                  "@@when:-3:commit:more")
        cp = self.ok("spawn", "claude", "feat", repo, prompt, "--n", "3", "--engines", "claude,codex",
                     "--verify", "test -f f.txt")
        self.assertIn("feat-1", cp.stdout)
        self.assertIn("select feat", cp.stdout)
        group = self.group("feat")
        self.assertEqual(group["members"], ["feat-1", "feat-2", "feat-3"])
        self.assertEqual((group["kind"], group["winner"], group["selection"]), ("best-of", None, None))
        self.assertEqual(group["owner"], "env:test-owner")
        self.assertTrue(group["created_iso"])
        metas = [self.meta("feat-%d" % k) for k in (1, 2, 3)]
        self.assertEqual([m["engine"] for m in metas], ["claude", "codex", "claude"])
        self.assertEqual([m["group"] for m in metas], ["feat"] * 3)
        self.assertEqual([m["verify"] for m in metas], ["test -f f.txt"] * 3)
        listed = self.ok("list", "--group", "feat").stdout
        for k in (1, 2, 3):
            self.assertIn("feat-%d" % k, listed)
        cp = self.wait("feat", timeout=60)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        results = [self.wait_result("feat-%d" % k) for k in (1, 2, 3)]
        self.assertEqual([r["status"] for r in results], ["ok", "error", "ok"])
        self.assertEqual(results[1]["failure_class"], "crash")
        self.assertEqual(results[1]["verify"]["status"], "skipped")
        for k in (1, 3):
            wt = self.meta("feat-%d" % k)["worktree"]
            self.assertEqual(wt["branch"], "agent-session/feat-%d" % k)
            self.assertTrue(os.path.isdir(wt["path"]))
            self.assertEqual(results[k - 1]["verify"]["status"], "pass")
        docs = json.loads(self.run_as("wait", "feat", "--json", "--timeout", "5").stdout)
        self.assertEqual(len(docs), 3)
        for doc in docs:
            assert_valid_result(doc, "wait feat --json")
        cp = self.run_as("select", "feat")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("winner    : feat-1 (agent-session/feat-1)", cp.stdout)
        for k in (1, 2, 3):
            self.assertIn("feat-%d" % k, cp.stdout)
        self.assertIn("rank      :", cp.stdout)
        group = self.group("feat")
        self.assertEqual(group["winner"], "feat-1")
        self.assertEqual([r["member"] for r in group["selection"]], ["feat-1", "feat-3", "feat-2"])
        self.assertLess(group["selection"][0]["insertions"], group["selection"][1]["insertions"])
        doc = json.loads(self.ok("select", "feat", "--json").stdout)
        self.assertEqual(doc["winner"], "feat-1")
        self.assertEqual(len(doc["rows"]), 3)
        cp = self.ok("collect", "feat", "--merge")
        self.assertIn("collected", cp.stdout)
        self.assertIn("reap feat-1", cp.stdout)
        self.assertTrue(os.path.exists(os.path.join(repo, "f.txt")))
        self.assertFalse(os.path.exists(os.path.join(repo, "g.txt")))
        cp = self.ok("reap", "feat")
        self.assertIn("kept branch agent-session/feat-3", cp.stdout)
        self.assertNotIn("kept branch agent-session/feat-1", cp.stdout)
        self.assertNotIn("kept branch agent-session/feat-2", cp.stdout)
        self.assertFalse(self.branch_exists(repo, "agent-session/feat-1"))
        self.assertTrue(self.branch_exists(repo, "agent-session/feat-3"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "groups", "feat.json")))

    def test_bestof_refusals(self):
        repo = self.repo()
        for n in ("1", "7", "x"):
            cp = self.run_as("spawn", "claude", "g", repo, "hi", "--n", n)
            self.assertEqual(cp.returncode, 2, n)
        cp = self.run_as("spawn", "claude", "g", self.work, "hi", "--n", "2")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("git repository", cp.stderr)
        cp = self.run_as("spawn", "claude", "g", repo, "hi", "--n", "2", "--steerable")
        self.assertEqual(cp.returncode, 2)
        cp = self.run_as("spawn", "claude", "g", repo, "hi", "--n", "2", "--engines", "claude,codex",
                         "--model", "sonnet")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--tier", cp.stderr)
        cp = self.run_as("spawn", "claude", "g", repo, "hi", "--engines", "claude,codex")
        self.assertEqual(cp.returncode, 2)
        cp = self.run_as("spawn", "claude", "g", repo, "hi", "--n", "2", "--engines", "claude,gpt")
        self.assertEqual(cp.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "groups", "g.json")))
        env = {"AGENT_SESSIONS_MAX": "2"}   # caps come from the caller's environment
        self.ok("spawn", "claude", "g", repo, "@@sleep:20", "--n", "4", env=env)
        states = [json.loads(self.run_as("status", "g-%d" % k, "--json", env=env).stdout)["state"]
                  for k in (1, 2, 3, 4)]
        self.assertEqual(states.count("queued"), 2, states)
        # select waits for every member; collect needs a winner
        cp = self.run_as("select", "g", env=env)
        self.assertEqual(cp.returncode, 3)
        self.assertIn("pending", cp.stdout)
        cp = self.run_as("select", "g", "--json", env=env)
        self.assertEqual(cp.returncode, 3)
        self.assertEqual(json.loads(cp.stdout)["status"], "pending")
        cp = self.run_as("collect", "g", "--merge", env=env)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no winner", cp.stderr)
        # names: the group name is taken; a second group needs --replace
        self.assertEqual(self.run_as("spawn", "claude", "g", self.work, "hi", env=env).returncode, 2)
        cp = self.run_as("spawn", "claude", "g", repo, "hi", "--n", "2", env=env)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("already exists", cp.stderr)
        for k in (1, 2, 3, 4):
            self.run_as("kill", "g-%d" % k, env=env)
        cp = self.run_as("spawn", "claude", "g", repo, "hi", "--n", "2", "--replace", env=env)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("g-3, g-4", cp.stderr)
        self.assertEqual(self.run_as("select", "nosuch").returncode, 2)

    def test_select_with_judge(self):
        repo = self.repo()
        self.ok("spawn", "claude", "s", repo, "@@edit:a.txt=same @@commit:same @@when:-3:crash", "--n", "3")
        self.assertEqual(self.wait("s", timeout=60).returncode, 1)
        approve = self.write_file("approve.json", json.dumps(dict(VERDICT, verdict="approve", findings=[])))
        revise = self.write_file("revise.json", json.dumps(VERDICT))
        env = {"FAKE_ENGINE_DIRECTIVES": "@@when:-1-judge:answer-file:%s @@when:-2-judge:answer-file:%s"
                                         % (revise, approve)}
        cp = self.run_as("select", "s", "--judge", env=env, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        judges = sorted(m for m in os.listdir(self.state) if m.endswith("-judge"))
        self.assertEqual(judges, ["s-1-judge", "s-2-judge"])   # only the ok members are judged
        for j in judges:
            self.assertEqual(self.meta(j)["engine"], "codex")
            self.assertTrue(self.meta(j)["readonly"])
            self.wait_result(j)
        self.assertEqual(self.judge_record("s-1")["verdict"], "revise")
        self.assertEqual(self.judge_record("s-2")["verdict"], "approve")
        self.assertIn("winner    : s-2", cp.stdout)
        rows = self.group("s")["selection"]
        self.assertEqual([(r["member"], r["judge"]) for r in rows],
                         [("s-2", "approve"), ("s-1", "revise"), ("s-3", None)])
        # Recorded verdicts are reused: a second select starts no judge.
        calls = len(self.calls())
        cp = self.run_as("select", "s", "--judge", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual(self.result_json("s-2")["judge"]["verdict"], "approve")
        # reap of the group takes the judges along
        self.ok("reap", "s", "--force")
        self.assertEqual(sorted(m for m in os.listdir(self.state) if m.startswith("s-")), [])

    def test_select_without_an_ok_member(self):
        repo = self.repo()
        self.ok("spawn", "codex", "bad", repo, "@@crash", "--n", "2")
        self.wait("bad", timeout=60)
        cp = self.run_as("select", "bad")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("winner    : none", cp.stdout)
        self.assertIsNone(self.group("bad")["winner"])
        doc = json.loads(self.run_as("select", "bad", "--json").stdout)
        self.assertIsNone(doc["winner"])
        self.assertEqual(self.run_as("collect", "bad", "--merge").returncode, 2)
