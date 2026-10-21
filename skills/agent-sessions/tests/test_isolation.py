"""worktree subdir, branch handling, reap scoping and safety, owner token."""

import os
import shutil
import subprocess
import sys
import time

from helpers import SCRIPT, SessionTest, pid_alive


class IsolationTests(SessionTest):
    def repo(self):
        return self.make_repo(os.path.join(self.box, "repo"))

    def test_worktree_keeps_subdir_and_branch(self):
        repo = self.repo()
        sub = os.path.join(repo, "packages", "api")
        self.spawn("claude", "wt", "hi", "--worktree", directory=sub)
        meta = self.meta("wt")
        self.assertTrue(meta["workdir"].endswith(os.path.join("worktree", "packages", "api")), meta["workdir"])
        self.assertEqual(meta["worktree"]["branch"], "agent-session/wt")
        self.assertEqual(meta["worktree"]["prefix"], "packages/api/")
        self.assertTrue(self.branch_exists(repo, "agent-session/wt"))
        self.assertEqual(meta["worktree"]["base_commit"], self.head(repo))
        self.assertEqual(self.wait("wt").returncode, 0)
        self.assertEqual(os.path.realpath(self.calls("wt")[0]["cwd"]), os.path.realpath(meta["workdir"]))
        self.assertIn("worktree agent-session/wt", self.ok("result", "wt").stdout)

    def test_worktree_excludes_uncommitted_changes(self):
        repo = self.repo()
        with open(os.path.join(repo, "README.md"), "w") as fh:
            fh.write("modified but not committed\n")
        self.spawn("claude", "wt", "hi", "--worktree", directory=repo)
        with open(os.path.join(self.meta("wt")["worktree"]["path"], "README.md")) as fh:
            self.assertEqual(fh.read(), "committed\n")

    def test_worktree_requires_git_repo(self):
        cp = self.run_as("spawn", "claude", "x", self.work, "hi", "--worktree")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("git repository", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("x")))

    def test_worktree_existing_branch_is_clear_error(self):
        repo = self.repo()
        self.assertEqual(self.git(repo, "branch", "agent-session/x").returncode, 0)
        cp = self.run_as("spawn", "claude", "x", repo, "hi", "--worktree")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("agent-session/x", cp.stderr)
        self.assertIn("branch -D", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("x")))
        self.assertEqual(len(self.git(repo, "worktree", "list").stdout.strip().splitlines()), 1)

    def test_replace_reuses_name_of_worktree_child(self):
        repo = self.repo()
        self.spawn("claude", "wt", "first", "--worktree", directory=repo)
        self.assertEqual(self.wait("wt").returncode, 0)
        first_meta = self.meta("wt")
        # No commits on the branch: --replace reaps the old child, its worktree
        # and the merged branch, then starts the new one under the same name.
        self.spawn("claude", "wt", "second", "--worktree", "--replace", directory=repo)
        self.assertNotEqual(self.meta("wt")["conversation_id"], first_meta["conversation_id"])
        self.assertTrue(self.branch_exists(repo, "agent-session/wt"))
        self.assertEqual(self.wait("wt").returncode, 0)
        self.assertEqual(len(self.git(repo, "worktree", "list").stdout.strip().splitlines()), 2)
        # Unmerged commits: the branch is kept and the error says what to do.
        wt = self.meta("wt")["worktree"]["path"]
        with open(os.path.join(wt, "NEW.md"), "w") as fh:
            fh.write("child work\n")
        self.assertEqual(self.git(wt, "add", "NEW.md").returncode, 0)
        self.assertEqual(self.git(wt, "commit", "-q", "-m", "child work").returncode, 0)
        cp = self.run_as("spawn", "claude", "wt", repo, "third", "--worktree", "--replace")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("agent-session/wt", cp.stderr)
        self.assertIn("branch -D", cp.stderr)
        self.assertIn("kept", cp.stderr)
        self.assertTrue(self.branch_exists(repo, "agent-session/wt"))
        self.assertFalse(os.path.exists(self.sdir("wt")))
        self.assertEqual(len(self.git(repo, "worktree", "list").stdout.strip().splitlines()), 1)
        self.assertEqual(self.git(repo, "branch", "-D", "agent-session/wt").returncode, 0)
        self.spawn("claude", "wt", "fourth", "--worktree", directory=repo)
        self.assertEqual(self.wait("wt").returncode, 0)

    def test_reap_includes_descendants_and_abandoned_reservations(self):
        self.spawn("claude", "p", "@@spawn:claude,gc,%s,hi" % self.work)
        self.assertEqual(self.wait("p").returncode, 0)
        self.assertIn("exit=0", self.result("p")["answer"])
        self.assertEqual(self.wait("gc").returncode, 0)
        self.assertNotEqual(self.meta("gc")["owner"], self.meta("p")["owner"])
        stale = self.sdir("stale")
        os.makedirs(stale)
        with open(os.path.join(stale, "reserved"), "w"):
            pass
        old = time.time() - 600
        os.utime(os.path.join(stale, "reserved"), (old, old))
        cp = self.ok("reap", "--dry-run")
        self.assertIn("would reap p", cp.stdout)
        self.assertIn("would reap gc", cp.stdout)
        self.assertTrue(os.path.exists(self.sdir("gc")))
        cp = self.ok("reap")
        self.assertIn("reaped p", cp.stdout)
        self.assertIn("reaped gc", cp.stdout)
        self.assertIn("reaped stale (abandoned reservation", cp.stdout)
        self.assertFalse(os.path.exists(self.sdir("p")))
        self.assertFalse(os.path.exists(self.sdir("gc")))
        self.assertFalse(os.path.exists(stale))
        self.assertIn("(no sessions)", self.ok("list").stdout)

    def test_failed_spawn_cleans_up(self):
        repo = self.repo()
        bindir = os.path.join(self.box, "bin-broken-tmux")
        os.makedirs(bindir)
        real = shutil.which("tmux")
        wrapper = os.path.join(bindir, "tmux")
        with open(wrapper, "w") as fh:
            fh.write('#!/bin/bash\nfor a in "$@"; do\n  if [ "$a" = new-session ]; then\n'
                     '    echo "fake tmux: refusing new-session" >&2; exit 1\n  fi\ndone\nexec %s "$@"\n' % real)
        os.chmod(wrapper, 0o755)
        cp = self.run_as("spawn", "claude", "x", repo, "hi", "--worktree",
                         env={"PATH": bindir + os.pathsep + self.env["PATH"]})
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("refusing new-session", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("x")))
        self.assertFalse(self.branch_exists(repo, "agent-session/x"))
        self.assertEqual(len(self.git(repo, "worktree", "list").stdout.strip().splitlines()), 1)

    def test_reap_scope_defaults_to_owner(self):
        self.spawn("claude", "a", "hi", env={"AGENT_SESSIONS_OWNER": "a"})
        self.spawn("claude", "b", "hi", env={"AGENT_SESSIONS_OWNER": "b"})
        self.assertEqual(self.wait("a", "b").returncode, 0)
        listing = self.ok("list", env={"AGENT_SESSIONS_OWNER": "a"}).stdout
        self.assertIn("mine", listing)
        self.assertIn("env:b", listing)
        cp = self.ok("reap", env={"AGENT_SESSIONS_OWNER": "a"})
        self.assertIn("reaped a", cp.stdout)
        self.assertNotIn("reaped b", cp.stdout)
        self.assertFalse(os.path.exists(self.sdir("a")))
        self.assertTrue(os.path.exists(self.sdir("b")))
        cp = self.ok("reap", "--all", env={"AGENT_SESSIONS_OWNER": "a"})
        self.assertIn("reaped b", cp.stdout)
        self.assertFalse(os.path.exists(self.sdir("b")))
        self.assertIn("(no sessions)", self.ok("list").stdout)

    def test_reap_named_and_dry_run(self):
        self.spawn("claude", "x", "hi")
        self.spawn("claude", "y", "hi")
        self.assertEqual(self.wait("x", "y").returncode, 0)
        cp = self.ok("reap", "x", "--dry-run")
        self.assertIn("would reap x", cp.stdout)
        self.assertTrue(os.path.exists(self.sdir("x")))
        self.assertTrue(os.path.exists(self.sdir("y")))
        cp = self.ok("reap", "x")
        self.assertIn("reaped x", cp.stdout)
        self.assertIn("reaped 1, skipped 0, kept-branches 0", cp.stdout)
        self.assertFalse(os.path.exists(self.sdir("x")))
        self.assertTrue(os.path.exists(self.sdir("y")))

    def test_reap_skips_live_unless_force(self):
        self.spawn("claude", "x", "@@hang")
        self.wait_until(lambda: self.has("x", "engine.pid"))
        pid = self.read("x", "engine.pid")["pid"]
        cp = self.ok("reap")
        self.assertIn("skipped x: running", cp.stdout)
        self.assertTrue(pid_alive(pid))
        self.assertTrue(self.tmux_alive("x"))
        cp = self.ok("reap", "--force")
        self.assertIn("reaped x", cp.stdout)
        self.assertIn("killed first", cp.stdout)
        self.assertFalse(pid_alive(pid))
        self.assertFalse(os.path.exists(self.sdir("x")))

    def test_reap_refuses_dirty_worktree(self):
        repo = self.repo()
        self.spawn("claude", "wt", "hi", "--worktree", directory=repo)
        self.assertEqual(self.wait("wt").returncode, 0)
        wt = self.meta("wt")["worktree"]["path"]
        with open(os.path.join(wt, "README.md"), "a") as fh:
            fh.write("child scribble\n")
        cp = self.ok("reap")
        self.assertIn("uncommitted changes", cp.stdout)
        self.assertTrue(os.path.exists(self.sdir("wt")))
        self.assertTrue(os.path.isdir(wt))
        cp = self.ok("reap", "--force")
        self.assertIn("reaped wt", cp.stdout)
        self.assertFalse(os.path.exists(self.sdir("wt")))
        self.assertFalse(os.path.isdir(wt))
        self.assertFalse(self.branch_exists(repo, "agent-session/wt"))

    def test_reap_deletes_merged_branch_keeps_unmerged(self):
        repo = self.repo()
        self.spawn("claude", "a", "hi", "--worktree", directory=repo)
        self.spawn("claude", "b", "hi", "--worktree", directory=repo)
        self.assertEqual(self.wait("a", "b").returncode, 0)
        wt_b = self.meta("b")["worktree"]["path"]
        with open(os.path.join(wt_b, "NEW.md"), "w") as fh:
            fh.write("work by the child\n")
        self.assertEqual(self.git(wt_b, "add", "NEW.md").returncode, 0)
        cp = self.git(wt_b, "commit", "-q", "-m", "child work")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        cp = self.ok("reap")
        self.assertIn("reaped a", cp.stdout)
        self.assertIn("reaped b", cp.stdout)
        self.assertIn("kept branch agent-session/b", cp.stdout)
        self.assertIn("branch -D agent-session/b", cp.stdout)
        self.assertIn("kept-branches 1", cp.stdout)
        self.assertFalse(self.branch_exists(repo, "agent-session/a"))
        self.assertTrue(self.branch_exists(repo, "agent-session/b"))
        self.assertFalse(os.path.isdir(wt_b))
        self.assertFalse(os.path.exists(self.sdir("a")))
        self.assertFalse(os.path.exists(self.sdir("b")))
        self.assertEqual(self.git(repo, "worktree", "list").stdout.count("\n"), 1)

    def test_reap_lost_session(self):
        self.fake_meta("x")
        cp = self.ok("reap")
        self.assertIn("reaped x", cp.stdout)
        self.assertIn("lost", cp.stdout)
        self.assertFalse(os.path.exists(self.sdir("x")))
        self.fake_meta("y")
        cp = self.ok("reap", "--json")
        report = __import__("json").loads(cp.stdout)
        self.assertEqual(report["reaped"], 1)
        self.assertEqual(report["results"][0]["name"], "y")

    def test_lingering_child_is_reaped_and_tmux_ended(self):
        self.spawn("claude", "x", "hi", env={"AGENT_SESSIONS_LINGER": "60"})
        self.assertEqual(self.wait("x").returncode, 0)
        self.assertTrue(self.tmux_alive("x"))
        self.assertIn("/linger", self.ok("list").stdout)
        cp = self.ok("reap")
        self.assertIn("reaped x", cp.stdout)
        self.wait_until(lambda: not self.tmux_alive("x"), timeout=5)
        self.assertFalse(os.path.exists(self.sdir("x")))

    def test_owner_token_from_ancestor_process(self):
        bindir = os.path.join(self.box, "bin-owner")
        os.makedirs(bindir)
        fake_claude = os.path.join(bindir, "claude")
        os.symlink("/bin/bash", fake_claude)
        env = self.environ({"AGENT_SESSIONS_OWNER": None, "AGENT_SESSION_NAME": None})
        script = 'echo "bashpid=$$"; "$PY" "$AS" spawn claude x "$WORK" "hi @@sleep:0"'
        env.update({"PY": sys.executable, "AS": SCRIPT, "WORK": self.work})
        cp = subprocess.run([fake_claude, "-c", script], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        bashpid = [l for l in cp.stdout.splitlines() if l.startswith("bashpid=")][0].split("=", 1)[1]
        owner = self.meta("x")["owner"]
        self.assertTrue(owner.startswith("proc:claude:%s:" % bashpid), owner)
        listing = self.ok("list", env={"AGENT_SESSIONS_OWNER": None}).stdout
        self.assertIn("claude:%s" % bashpid, listing)
