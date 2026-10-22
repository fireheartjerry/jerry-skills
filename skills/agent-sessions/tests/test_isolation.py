"""worktree subdir, branch handling, reap scoping and safety, owner token,
roles (read-only by default for review and research), --base and --copy."""

import json
import os
import shutil
import subprocess
import sys
import time

from helpers import SCRIPT, SessionTest, pid_alive, script_module


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
        # Unmerged commits: --replace is refused before anything is removed,
        # and the error names both ways out (collect, or discard by tip sha).
        wt = self.meta("wt")["worktree"]["path"]
        with open(os.path.join(wt, "NEW.md"), "w") as fh:
            fh.write("child work\n")
        self.assertEqual(self.git(wt, "add", "NEW.md").returncode, 0)
        self.assertEqual(self.git(wt, "commit", "-q", "-m", "child work").returncode, 0)
        tip = self.head(wt)
        cp = self.run_as("spawn", "claude", "wt", repo, "third", "--worktree", "--replace")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("agent-session/wt", cp.stderr)
        self.assertIn("not merged", cp.stderr)
        self.assertIn("collect wt --merge", cp.stderr)
        self.assertIn("--discard %s" % tip[:12], cp.stderr)
        self.assertTrue(self.branch_exists(repo, "agent-session/wt"))
        self.assertTrue(self.has("wt", "result.json"))
        self.assertEqual(len(self.git(repo, "worktree", "list").stdout.strip().splitlines()), 2)
        # A dirty worktree is refused the same way.
        with open(os.path.join(wt, "DIRTY.md"), "w") as fh:
            fh.write("uncommitted\n")
        cp = self.run_as("spawn", "claude", "wt", repo, "third", "--worktree", "--replace")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("uncommitted changes", cp.stderr)
        os.unlink(os.path.join(wt, "DIRTY.md"))
        self.ok("reap", "wt", "--discard", tip)
        self.assertFalse(self.branch_exists(repo, "agent-session/wt"))
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
        # A dry run reports the branch the real reap would keep.
        cp = self.ok("reap", "--dry-run")
        self.assertIn("would keep branch agent-session/b", cp.stdout)
        self.assertIn("kept-branches 1", cp.stdout)
        self.assertTrue(os.path.exists(self.sdir("b")))
        cp = self.ok("reap")
        self.assertIn("reaped a", cp.stdout)
        self.assertIn("reaped b", cp.stdout)
        self.assertIn("kept branch agent-session/b", cp.stdout)
        self.assertIn("branch -D agent-session/b", cp.stdout)
        # The child's state is gone, so the hint is plain git, not `collect`.
        self.assertIn("merge agent-session/b", cp.stdout)
        self.assertNotIn("collect", cp.stdout)
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

    def test_reap_retires_lingering_runner(self):
        """reap ends a lingering runner and waits for it before removing the
        state, so its last progress.json write cannot leave the directory
        behind."""
        self.spawn("claude", "x", "hi", env={"AGENT_SESSIONS_LINGER": "60"})
        self.wait_result("x")
        self.wait_until(lambda: self.progress("x").get("phase") == "linger", message="runner not lingering")
        runner = self.progress("x")["runner_pid"]
        self.assertIn("reaped x", self.ok("reap", "x").stdout)
        self.assertFalse(pid_alive(runner), "the runner outlived the reap")
        self.assertFalse(os.path.exists(self.sdir("x")))

    def test_owner_token_from_ancestor_process(self):
        """The owner is the nearest engine ancestor, found by its command name
        or, when that differs (Linux reports the executable's own name, macOS
        argv[0]), by argv[0]: a binary called claude and a process started as
        claude both count."""
        bindir = os.path.join(self.box, "bin-owner")
        os.makedirs(bindir)
        fake_claude = os.path.join(bindir, "claude")
        os.symlink("/bin/bash", fake_claude)
        # bash 5.1+ (and dash, zsh) exec the last command of `-c` in place of
        # the shell, which would drop the claude process from the spawn's
        # ancestry; the trailing exit keeps the spawn its child, as the tool
        # shell of a real engine is.
        body = 'echo "bashpid=$$"; "$PY" "$AS" spawn claude %s "$WORK" "hi @@sleep:0"; exit $?'
        cases = (("x", [fake_claude, "-c", body % "x"], {}),
                 ("y", ["/bin/bash", "-c", 'exec -a claude /bin/bash -c "$INNER"'], {"INNER": body % "y"}))
        for name, argv, extra in cases:
            env = self.environ({"AGENT_SESSIONS_OWNER": None, "AGENT_SESSION_NAME": None})
            env.update({"PY": sys.executable, "AS": SCRIPT, "WORK": self.work}, **extra)
            cp = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=60)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            bashpid = [l for l in cp.stdout.splitlines() if l.startswith("bashpid=")][0].split("=", 1)[1]
            owner = self.meta(name)["owner"]
            self.assertTrue(owner.startswith("proc:claude:%s:" % bashpid), (name, owner))
            # The spawning shell has exited by now, so a caller with another
            # token sees the child as adopted; the recorded owner is unchanged.
            rows = json.loads(self.ok("list", "--json", env={"AGENT_SESSIONS_OWNER": None}).stdout)
            self.assertTrue([r for r in rows if r["name"] == name and r["owner"] == owner], rows)

    def test_owner_token_argv0_fallback(self):
        """ps_argv0 reads argv[0] of a live process; owner_token takes the
        command name first and argv[0] only when the name is not an engine."""
        mod = script_module()
        procs = sys.modules["agent_sessions.procs"]
        proc = subprocess.Popen(["/bin/bash", "-c", "exec -a fancy-name /bin/sleep 30"])
        try:
            self.wait_until(lambda: os.path.basename(procs.ps_argv0(proc.pid) or "") == "fancy-name",
                            timeout=10, message="ps_argv0 never saw argv[0]")
        finally:
            proc.kill()
            proc.wait()
        self.assertIsNone(procs.ps_argv0(proc.pid))
        top = os.getppid()
        chain = {top: (501, "Tue Oct 20 17:26:46 2026", "/usr/bin/python3"),
                 501: (502, "Tue Oct 20 17:26:45 2026", "bash"),
                 502: (503, "Tue Oct 20 17:26:44 2026", "node"),
                 503: (1, "Tue Oct 20 17:26:43 2026", "zsh")}
        argv0 = {top: "python3", 501: "/opt/engines/bin/claude", 502: "codex", 503: "-zsh"}
        saved = (procs.ps_parent, procs.ps_argv0, os.environ.pop("AGENT_SESSIONS_OWNER", None),
                 os.environ.pop("AGENT_SESSION_NAME", None))
        procs.ps_parent, procs.ps_argv0 = chain.get, argv0.get
        try:
            self.assertEqual(mod.owner_token(), "proc:claude:501:Tue Oct 20 17:26:45 2026")
            argv0[501] = "bash"
            self.assertEqual(mod.owner_token(), "proc:node:502:Tue Oct 20 17:26:44 2026")
        finally:
            procs.ps_parent, procs.ps_argv0 = saved[0], saved[1]
            for key, value in (("AGENT_SESSIONS_OWNER", saved[2]), ("AGENT_SESSION_NAME", saved[3])):
                if value is not None:
                    os.environ[key] = value

    def test_role_defaults_readonly(self):
        self.spawn("claude", "rv", "look", "--role", "review")
        self.spawn("claude", "rw", "look", "--role", "review", "--write")
        self.spawn("gemini", "rs", "look", "--role", "research")
        self.spawn("codex", "rc", "look", "--role", "research")
        self.spawn("claude", "im", "build")
        for name in ("rv", "rw", "rs", "rc", "im"):
            self.wait_result(name)
        argv = self.calls("rv")[0]["argv"]
        self.assertIn("dontAsk", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("--dangerously-skip-permissions", self.calls("rw")[0]["argv"])
        self.assertIn("--dangerously-skip-permissions", self.calls("im")[0]["argv"])
        argv = self.calls("rs")[0]["argv"]
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "plan")
        argv = self.calls("rc")[0]["argv"]
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        for name, role, readonly in (("rv", "review", True), ("rw", "review", False), ("rs", "research", True),
                                     ("rc", "research", True), ("im", "implementation", False)):
            self.assertEqual(self.meta(name)["role"], role)
            self.assertEqual(self.meta(name)["readonly"], readonly)
            self.assertEqual(self.result(name)["role"], role)
        cp = self.run_as("spawn", "claude", "bad", self.work, "x", "--role", "boss")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("unknown role", cp.stderr)
        cp = self.run_as("spawn", "claude", "bad", self.work, "x", "--readonly", "--write")
        self.assertEqual(cp.returncode, 2)
        # A reviewer has nothing to verify unless it may write.
        cp = self.run_as("spawn", "claude", "bad", self.work, "x", "--role", "review", "--verify", "true")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--write", cp.stderr)

    def test_base_and_copy(self):
        repo = self.repo()
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write(".env\nnode_modules/\n")
        self.commit_all(repo, "ignore")
        self.assertEqual(self.git(repo, "checkout", "-q", "-b", "feature").returncode, 0)
        with open(os.path.join(repo, "feature.txt"), "w") as fh:
            fh.write("feature\n")
        self.commit_all(repo, "feature")
        feature_tip = self.head(repo)
        self.assertEqual(self.git(repo, "checkout", "-q", "main").returncode, 0)
        with open(os.path.join(repo, ".env"), "w") as fh:
            fh.write("SECRET=1\n")
        os.makedirs(os.path.join(repo, "node_modules", "pkg"))
        with open(os.path.join(repo, "node_modules", "x.js"), "w") as fh:
            fh.write("x\n")
        with open(os.path.join(repo, "node_modules", "pkg", "index.js"), "w") as fh:
            fh.write("pkg\n")
        os.symlink("pkg/index.js", os.path.join(repo, "node_modules", "link.js"))
        self.spawn("claude", "b", "hi", "--worktree", "--base", "feature",
                   "--copy", ".env,node_modules,nope,../outside,.git/config", directory=repo)
        res = self.wait_result("b")
        wt = self.meta("b")["worktree"]
        self.assertEqual(wt["base_commit"], feature_tip)
        self.assertEqual(wt["base_ref"], "feature")
        self.assertEqual(res["git"]["base_commit"], feature_tip)
        self.assertTrue(os.path.exists(os.path.join(wt["path"], "feature.txt")))
        with open(os.path.join(wt["path"], ".env")) as fh:
            self.assertEqual(fh.read(), "SECRET=1\n")
        self.assertTrue(os.path.exists(os.path.join(wt["path"], "node_modules", "pkg", "index.js")))
        self.assertTrue(os.path.islink(os.path.join(wt["path"], "node_modules", "link.js")))
        warnings = res["warnings"]
        self.assertTrue(any("nope" in w and "not found" in w for w in warnings), warnings)
        self.assertTrue(any("../outside" in w for w in warnings), warnings)
        self.assertTrue(any(".git/config" in w for w in warnings), warnings)
        self.assertFalse(os.path.exists(os.path.join(self.box, "outside")))
        # The copied, ignored files do not make the worktree dirty.
        self.assertFalse(res["git"]["dirty"])
        self.assertEqual(self.run_as("spawn", "claude", "c", repo, "hi", "--base", "main").returncode, 2)
        self.assertEqual(self.run_as("spawn", "claude", "c", repo, "hi", "--copy", ".env").returncode, 2)
        cp = self.run_as("spawn", "claude", "c", repo, "hi", "--worktree", "--base", "nosuch")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("nosuch", cp.stderr)

    def test_base_resolved_at_start_for_stacked_children(self):
        """--after a --base agent-session/a stacks on a's finished branch: the
        base is resolved when the child starts, not when it is spawned."""
        repo = self.repo()
        self.spawn("claude", "a", "@@sleep:1 @@edit:a.txt=from_a @@commit:a_work", "--worktree", directory=repo)
        self.spawn("claude", "b", "build on a", "--worktree", "--after", "a", "--base", "agent-session/a",
                   directory=repo)
        res = self.wait_result("b", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        a_tip = self.git(repo, "rev-parse", "agent-session/a").stdout.strip()
        wt = self.meta("b")["worktree"]
        self.assertEqual(wt["base_commit"], a_tip)
        self.assertTrue(os.path.exists(os.path.join(wt["path"], "a.txt")))
