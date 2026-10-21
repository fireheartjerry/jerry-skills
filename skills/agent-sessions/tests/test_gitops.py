"""diff, collect, conflicts, and the lost-work-proof reap (snapshots, --discard)."""

import json
import os
import subprocess
import time

from helpers import SessionTest


class GitOpsTests(SessionTest):
    def child(self, name, repo, prompt="hi"):
        self.spawn("claude", name, prompt, "--worktree", directory=repo)
        self.assertEqual(self.wait_result(name)["status"], "ok")
        return self.meta(name)["worktree"]["path"]

    def test_diff_and_collect(self):
        repo = self.repo()
        wt = self.child("x", repo, "@@edit:feature.txt=one @@commit:first")
        cp = self.ok("diff", "x", "--stat")
        self.assertIn("feature.txt", cp.stdout)
        with open(os.path.join(wt, "feature.txt"), "a") as fh:
            fh.write("dirty\n")
        with open(os.path.join(wt, "new.txt"), "w") as fh:
            fh.write("untracked\n")
        cp = self.ok("diff", "x")
        self.assertIn("+dirty", cp.stdout)          # uncommitted tracked change against base
        self.assertIn("+one", cp.stdout)            # committed change against base
        self.assertIn("untracked : new.txt", cp.stdout)
        self.assertNotIn("+one", self.ok("diff", "x", "--base", "HEAD").stdout)
        cp = self.run_as("collect", "x", "--merge")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("uncommitted", cp.stderr)
        self.assertIn("agent-session diff x", cp.stderr)
        self.commit_all(wt, "second")
        before = self.git(repo, "rev-list", "--count", "HEAD").stdout.strip()
        cp = self.ok("collect", "x", "--branch")
        self.assertIn("agent-session/x", cp.stdout)
        self.assertIn("2 commits", cp.stdout)
        doc = json.loads(self.ok("collect", "x", "--branch", "--json").stdout)
        self.assertEqual(sorted(k for k in doc if k != "commits"),
                         ["base", "branch", "commit", "head", "merged_into", "mode"])
        self.assertEqual((doc["mode"], doc["branch"], doc["head"]), ("branch", "agent-session/x", self.head(wt)))
        # A dirty parent tree is refused before git would refuse it.
        with open(os.path.join(repo, "README.md"), "a") as fh:
            fh.write("parent edit\n")
        cp = self.run_as("collect", "x", "--squash")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("parent tree", cp.stderr)
        self.git(repo, "checkout", "--", "README.md")
        self.assertEqual(self.run_as("collect", "x").returncode, 2)                       # no mode
        self.assertEqual(self.run_as("collect", "x", "--merge", "--squash").returncode, 2)  # two modes
        cp = self.ok("collect", "x", "--squash")
        after = self.git(repo, "rev-list", "--count", "HEAD").stdout.strip()
        self.assertEqual(int(after), int(before) + 1)
        self.assertTrue(os.path.exists(os.path.join(repo, "feature.txt")))
        self.assertIn("reap x --discard %s" % self.git(repo, "rev-parse", "agent-session/x").stdout.strip(), cp.stdout)
        # cherry-pick one commit of another child
        wt2 = self.child("y", repo, "@@edit:other.txt=two @@commit:other")
        sha = self.head(wt2)
        cp = self.run_as("collect", "y", "--cherry-pick", "deadbeef")
        self.assertEqual(cp.returncode, 2)
        doc = json.loads(self.ok("collect", "y", "--cherry-pick", sha, "--json").stdout)
        self.assertEqual((doc["mode"], doc["commit"], doc["merged_into"]), ("cherry-pick", sha, "main"))
        self.assertEqual(doc["head"], self.head(repo))
        self.assertTrue(os.path.exists(os.path.join(repo, "other.txt")))
        # nothing to collect
        self.child("z", repo)
        cp = self.run_as("collect", "z", "--merge")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("nothing to collect", cp.stderr)

    def test_diff_shared_tree(self):
        repo = self.repo()
        self.spawn("claude", "s", "@@edit:README.md=changed", directory=repo)
        self.wait_result("s")
        cp = self.ok("diff", "s")
        self.assertIn("README.md", cp.stdout)
        self.assertIn("shared tree", cp.stdout)
        self.spawn("claude", "n", "hi")
        self.wait_result("n")
        self.assertEqual(self.run_as("diff", "n").returncode, 2)

    def test_conflicts(self):
        repo = self.repo()
        self.child("a", repo, "@@edit:shared.txt=a")
        self.child("b", repo, "@@edit:shared.txt=b @@commit:b")
        self.child("c", repo, "@@edit:other.txt=c")
        with open(os.path.join(repo, "shared.txt"), "w") as fh:
            fh.write("parent\n")
        cp = self.run_as("conflicts")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("a <-> b : shared.txt", cp.stdout)
        self.assertIn("a <-> (parent tree) : shared.txt", cp.stdout)
        self.assertIn("b <-> (parent tree) : shared.txt", cp.stdout)
        self.assertNotIn("c <->", cp.stdout)
        doc = json.loads(self.run_as("conflicts", "--json").stdout)
        self.assertEqual(sorted(doc["members"]), ["(parent tree)", "a", "b", "c"])
        self.assertEqual(doc["members"]["c"], ["other.txt"])
        self.assertEqual(sorted((o["a"], o["b"], tuple(o["files"])) for o in doc["overlaps"]),
                         [("a", "(parent tree)", ("shared.txt",)), ("a", "b", ("shared.txt",)),
                          ("b", "(parent tree)", ("shared.txt",))])
        self.assertEqual(self.run_as("conflicts", "a", "b").returncode, 1)
        os.unlink(os.path.join(repo, "shared.txt"))
        self.assertEqual(self.run_as("conflicts", "c").returncode, 0)
        self.spawn("claude", "plain", "hi")
        self.wait_result("plain")
        self.assertEqual(self.run_as("conflicts", "plain").returncode, 2)

    def test_conflicts_sees_parent_commits_after_spawn(self):
        # The parent branch moves on after the spawn: the most common source of
        # merge conflicts, invisible to a dirty-files-only comparison.
        repo = self.repo()
        self.child("c2", repo, "@@edit:h.txt=child @@commit:child")
        with open(os.path.join(repo, "h.txt"), "w") as fh:
            fh.write("parent\n")
        self.commit_all(repo, "parent h")
        cp = self.run_as("conflicts", "c2")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("c2 <-> (parent tree) : h.txt", cp.stdout)
        self.assertIn("merge conflict (simulated): c2 <-> (parent tree) : h.txt", cp.stdout)
        doc = json.loads(self.run_as("conflicts", "c2", "--json").stdout)
        self.assertEqual(doc["merge_simulation"], "git merge-tree")
        self.assertEqual(doc["merge_conflicts"], [{"a": "c2", "b": "(parent tree)", "files": ["h.txt"]}])
        # collect stops on the conflict, aborts, and exits 1 (a failed operation, not a usage error).
        head = self.head(repo)
        cp = self.run_as("collect", "c2", "--merge")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("aborted", cp.stderr)
        self.assertEqual(self.head(repo), head)
        self.assertEqual(self.git(repo, "status", "--porcelain").stdout.strip(), "")
        # A parent commit to a different file is no overlap.
        self.child("c3", repo, "@@edit:k.txt=child @@commit:child")
        with open(os.path.join(repo, "other.txt"), "w") as fh:
            fh.write("parent\n")
        self.commit_all(repo, "parent other")
        self.assertEqual(self.run_as("conflicts", "c3").returncode, 0)

    def test_reap_snapshots_before_force(self):
        repo = self.repo()
        wt = self.child("x", repo)
        with open(os.path.join(wt, "notes.txt"), "w") as fh:
            fh.write("unsaved work\n")
        with open(os.path.join(wt, "README.md"), "w") as fh:
            fh.write("edited tracked file\n")
        with open(os.path.join(wt, ".gitignore"), "w") as fh:
            fh.write("*.log\n")
        with open(os.path.join(wt, "build.log"), "w") as fh:
            fh.write("ignored\n")
        self.git(wt, "add", "README.md")     # staged and unstaged changes are both captured
        cp = self.run_as("reap", "x")
        self.assertIn("uncommitted", cp.stdout)
        self.assertTrue(os.path.isdir(wt))
        cp = self.ok("reap", "x", "--force")
        self.assertIn("snapshot  : refs/agent-sessions/x/snapshot-", cp.stdout)
        ref = [l for l in cp.stdout.splitlines() if l.startswith("snapshot")][0].split(":", 1)[1].split()[0]
        self.assertEqual(self.git(repo, "show-ref", "--verify", ref).returncode, 0)
        self.assertIn("notes.txt", self.git(repo, "ls-tree", "-r", "--name-only", ref).stdout)
        self.assertEqual(self.git(repo, "show", "%s:notes.txt" % ref).stdout, "unsaved work\n")
        self.assertEqual(self.git(repo, "show", "%s:README.md" % ref).stdout, "edited tracked file\n")
        self.assertNotIn("build.log", self.git(repo, "ls-tree", "-r", "--name-only", ref).stdout)
        self.assertEqual(self.git(repo, "rev-parse", "%s^" % ref).stdout.strip(), self.head(repo))
        self.assertIn("restore: git -C %s worktree add <dir> %s" % (repo, ref), cp.stdout)
        self.assertFalse(os.path.isdir(wt))
        cp = self.ok("snapshots", cwd=repo)
        self.assertIn(ref, cp.stdout)
        doc = json.loads(self.ok("snapshots", "--json", cwd=repo).stdout)
        self.assertEqual([(s["ref"], s["name"], s["root"]) for s in doc["snapshots"]], [(ref, "x", repo)])
        self.assertEqual(self.run_as("snapshots", "--older-than", "0d").returncode, 2)   # needs --prune
        self.assertEqual(self.run_as("snapshots", "--prune", "--older-than", "3h").returncode, 2)
        cp = self.ok("snapshots", "--prune", cwd=repo)                                   # default 30 days
        self.assertIn("pruned 0", cp.stdout)
        cp = self.ok("snapshots", "--prune", "--older-than", "0d", cwd=repo)
        self.assertIn("pruned 1", cp.stdout)
        self.assertNotEqual(self.git(repo, "show-ref", "--verify", ref).returncode, 0)

    def test_snapshot_age_is_when_it_was_taken(self):
        # A --discard snapshot points at the branch tip, whose commit date can be
        # years old; its age must still count from the snapshot, or a prune
        # would delete commits the user discarded moments ago.
        repo = self.repo()
        old = {"GIT_AUTHOR_DATE": "2015-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2015-01-01T00:00:00Z"}
        wt = self.child("x", repo, "@@edit:work.txt=w")
        self.git(wt, "add", "-A")
        env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
                   GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com", **old)
        subprocess.run(["git", "-C", wt, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "old work"],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        tip = self.head(wt)
        self.ok("reap", "x", "--discard", tip)
        doc = json.loads(self.ok("snapshots", "--json", cwd=repo).stdout)
        self.assertEqual(len(doc["snapshots"]), 1)
        self.assertLess(abs(doc["snapshots"][0]["epoch"] - time.time()), 600)
        line = [l for l in self.ok("snapshots", cwd=repo).stdout.splitlines() if tip[:12] in l][0]
        self.assertEqual(line.split()[1], "0h", line)
        cp = self.ok("snapshots", "--prune", "--older-than", "30d", cwd=repo)
        self.assertIn("pruned 0", cp.stdout)
        snaps = self.git(repo, "for-each-ref", "--format=%(objectname)", "refs/agent-sessions/").stdout.split()
        self.assertIn(tip, snaps)

    def test_reap_discard_requires_tip_sha(self):
        repo = self.repo()
        wt = self.child("x", repo, "@@edit:work.txt=w @@commit:work")
        tip = self.head(wt)
        cp = self.run_as("reap", "x", "--discard", "deadbeef")
        self.assertEqual(cp.returncode, 2)
        self.assertIn(tip, cp.stderr)
        self.assertTrue(self.branch_exists(repo, "agent-session/x"))
        self.assertEqual(self.run_as("reap", "x", "--discard", tip[:6]).returncode, 2)   # 7+ characters
        self.assertTrue(self.has("x", "meta.json"))
        self.assertTrue(self.branch_exists(repo, "agent-session/x"))
        cp = self.ok("reap", "x", "--discard", tip[:10])
        self.assertIn("snapshot", cp.stdout)
        self.assertIn("discarded branch agent-session/x", cp.stdout)
        self.assertFalse(self.branch_exists(repo, "agent-session/x"))
        snaps = self.git(repo, "for-each-ref", "--format=%(objectname)", "refs/agent-sessions/").stdout.split()
        self.assertIn(tip, snaps)
        # --force alone never deletes commits
        wt = self.child("y", repo, "@@edit:keep.txt=k @@commit:keep")
        cp = self.ok("reap", "y", "--force")
        self.assertIn("kept branch agent-session/y", cp.stdout)
        self.assertTrue(self.branch_exists(repo, "agent-session/y"))
