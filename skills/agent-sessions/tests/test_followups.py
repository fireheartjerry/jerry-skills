"""Follow-up behaviour: adopting the children of a host session that is gone,
resume --message after ok, verify or schema, template typos next to --after,
--add-dir and --diff, the judge's diff without blobs in the user's object
store, and the per-engine environment for read-only children."""

import json
import os
import signal
import subprocess
import sys

from helpers import SKILL, SessionTest, pid_alive, ps_lstart_under

DEAD_LSTART = "Mon Jan  1 00:00:00 2001"


def dead_pid():
    """The pid of a process that has exited (and been reaped)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class OwnerAdoptionTests(SessionTest):
    def lose(self, name):
        self.wait_until(lambda: self.has(name, "engine.pid"), message="engine not started")
        self.wait_session(name)
        info = self.read(name, "engine.pid")
        os.kill(self.progress(name)["runner_pid"], signal.SIGKILL)
        try:
            os.killpg(info["pgid"], signal.SIGKILL)
        except OSError:
            pass
        self.wait_until(lambda: not pid_alive(info["pid"]), timeout=5)
        self.wait_until(lambda: not self.tmux_alive(name), timeout=5)

    def set_owner(self, name, owner):
        path = os.path.join(self.sdir(name), "meta.json")
        meta = self.meta(name)
        meta["owner"] = owner
        with open(path, "w") as fh:
            json.dump(meta, fh)

    def test_children_of_a_gone_host_process_are_adopted(self):
        """After a reboot or in a new host session the old proc: token names a
        dead pid (or a pid reused by a later process): recover, list --mine,
        wait --mine and reap treat those children as the caller's."""
        self.spawn("claude", "old", "long job @@on-turn:1:hang")
        self.lose("old")
        gone = "proc:claude:%d:%s" % (dead_pid(), DEAD_LSTART)
        self.set_owner("old", gone)
        cp = self.ok("recover", "--dry-run")
        self.assertIn("would resume", cp.stdout)
        self.assertIn("old", cp.stdout)
        cp = self.ok("list", "--mine")
        self.assertIn("mine:adopted", cp.stdout)
        cp = self.ok("recover")
        self.assertIn("resumed", cp.stdout)
        self.assertEqual(self.wait_result("old", timeout=30)["status"], "ok")
        cp = self.ok("reap")
        self.assertFalse(os.path.exists(self.sdir("old")), cp.stdout)

    def test_pid_reused_by_a_later_process_counts_as_gone(self):
        self.fake_meta("reused", owner="proc:claude:%d:%s" % (os.getpid(), DEAD_LSTART))
        cp = self.run_as("wait", "--mine", "--timeout", "5")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)   # adopted, then found lost
        self.assertIn("reused", cp.stdout)
        self.assertEqual(self.result("reused")["status"], "lost")

    def test_live_host_process_keeps_its_children(self):
        lstart = ps_lstart_under("C", os.getpid())
        self.fake_meta("live", owner="proc:python3:%d:%s" % (os.getpid(), lstart))
        cp = self.ok("recover", "--dry-run")
        self.assertIn("nothing to recover", cp.stdout)
        self.assertIn("1 belongs to another owner (live)", cp.stdout)
        self.assertIn("recover --all", cp.stdout)
        self.assertNotIn("live", self.ok("list", "--mine").stdout.split("note")[0])

    def test_explicit_owners_are_never_adopted_and_get_a_hint(self):
        sess1 = {"AGENT_SESSIONS_OWNER": "sess1"}
        sess2 = {"AGENT_SESSIONS_OWNER": "sess2"}
        self.spawn("claude", "job", "long job @@on-turn:1:hang", env=sess1)
        self.wait_session("job")
        cp = self.ok("wait", "--mine", "--timeout", "1", env=sess2)
        self.assertIn("(no unfinished children of yours)", cp.stdout)
        self.assertIn("1 unfinished child of other owners (job)", cp.stdout)
        cp = self.ok("list", "--mine", env=sess2)
        self.assertIn("(no sessions)", cp.stdout)
        self.assertIn("`list` shows every owner's", cp.stdout)
        self.lose("job")
        cp = self.ok("recover", "--dry-run", env=sess2)
        self.assertIn("nothing to recover", cp.stdout)
        self.assertIn("recover --all", cp.stdout)
        self.assertIn("AGENT_SESSIONS_OWNER", cp.stdout)
        cp = self.ok("recover", "--dry-run", env=sess1)
        self.assertIn("would resume", cp.stdout)

    def test_child_callers_never_adopt(self):
        sys.path.insert(0, os.path.join(SKILL, "scripts"))
        from agent_sessions import procs
        gone = "proc:claude:%d:%s" % (dead_pid(), DEAD_LSTART)
        self.assertTrue(procs.owner_gone(gone))
        self.assertTrue(procs.owns(gone, "env:me"))
        self.assertFalse(procs.owns(gone, "child:x:1"))
        self.assertFalse(procs.owner_gone("env:someone"))
        self.assertFalse(procs.owner_gone("user:501"))
        lstart = ps_lstart_under("C", os.getpid())
        self.assertFalse(procs.owner_gone("proc:python3:%d:%s" % (os.getpid(), lstart)))


class FollowUpResumeTests(SessionTest):
    def test_resume_after_ok_needs_a_message_and_continues_the_conversation(self):
        self.spawn("claude", "x", "first answer")
        res = self.wait_result("x")
        self.assertEqual(res["status"], "ok")
        cp = self.run_as("resume", "x")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--message", cp.stderr)
        # A stored verdict describes the run it judged; resuming moves it aside.
        with open(os.path.join(self.sdir("x"), "judge.json"), "w") as fh:
            fh.write("{}")
        self.ok("resume", "x", "--message", "Findings to check and fix if valid: none")
        res = self.wait_result("x", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["turns"], 2)
        self.assertEqual(res["resumes"][0]["from_status"], "ok")
        self.assertEqual(self.calls("x")[-1]["stdin"], "Findings to check and fix if valid: none")
        argv = self.calls("x")[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], self.meta("x")["conversation_id"])
        self.assertFalse(self.has("x", "judge.json"))
        self.assertTrue(self.has("x", "attempts", "1.judge.json"))

    def test_resume_after_verify_failure_reruns_verify(self):
        self.spawn("claude", "v", "do it", "--verify", "test -f fixed.txt", "--verify-fixups", "0")
        res = self.wait_result("v")
        self.assertEqual(res["failure_class"], "verify")
        cp = self.run_as("resume", "v")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("--message", cp.stderr)
        self.ok("resume", "v", "--message", "fix it @@edit:fixed.txt=done")
        res = self.wait_result("v", timeout=30)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(res["verify"]["status"], "pass")
        self.assertEqual(res["resumes"][0]["from_status"], "error")

    def test_auth_budget_and_dependency_stay_refused_with_a_message(self):
        self.spawn("claude", "a", "@@auth-fail")
        self.assertEqual(self.wait_result("a")["failure_class"], "auth")
        cp = self.run_as("resume", "a", "--message", "try again")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("login", cp.stderr)


class TemplateTypoTests(SessionTest):
    def test_typo_next_to_after_is_refused(self):
        self.spawn("claude", "research", "@@sleep:20")
        cp = self.run_as("spawn", "claude", "impl", self.work, "Build: {{answer:reserch}}", "--after", "research")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("did you mean 'research'", cp.stderr)
        cp = self.run_as("spawn", "claude", "impl", self.work, "Build: {{answer:unrelated}}", "--after", "research")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("this spawn has --after research", cp.stderr)
        # Without --after, a near miss of an existing child is still a typo.
        cp = self.run_as("spawn", "claude", "impl", self.work, "Build: {{answer:reserch}}")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("did you mean 'research'", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("impl")))
        # No --after and nothing similar: literal text with a warning.
        cp = self.spawn("claude", "doc", "Quoted: {{answer:zzzzzz}}")
        self.assertIn("names no existing child", cp.stderr)
        self.ok("kill", "research")


class ReadableDirTests(SessionTest):
    def test_add_dir_reaches_each_engine(self):
        extra = os.path.join(self.box, "extra")
        os.makedirs(extra)
        real = os.path.realpath(extra)
        self.spawn("claude", "c", "hi", "--role", "review", "--add-dir", extra)
        self.spawn("gemini", "g", "hi", "--role", "review", "--add-dir", extra)
        self.spawn("codex", "x", "hi", "--role", "review", "--add-dir", extra)
        self.assertEqual(self.wait("c", "g", "x").returncode, 0)
        argv = self.calls("c")[0]["argv"]
        self.assertEqual(argv[argv.index("--add-dir") + 1], real)
        self.assertIn("dontAsk", argv)
        argv = self.calls("g")[0]["argv"]
        self.assertEqual(argv[argv.index("--include-directories") + 1], real)
        self.assertNotIn("--add-dir", self.calls("x")[0]["argv"])
        cp = self.run_as("spawn", "claude", "bad", self.work, "hi", "--add-dir", os.path.join(self.box, "nope"))
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no such directory", cp.stderr)

    def test_diff_mounts_the_parents_change_without_touching_the_tree(self):
        repo = self.repo()
        with open(os.path.join(repo, "README.md"), "a") as fh:
            fh.write("uncommitted line\n")
        with open(os.path.join(repo, "new.txt"), "w") as fh:
            fh.write("brand new\n")
        before = self.git(repo, "status", "--porcelain").stdout
        self.spawn("claude", "rev", "What is wrong with this change?", "--role", "review", "--diff", "HEAD",
                   directory=repo)
        self.assertEqual(self.wait("rev").returncode, 0)
        patch_p = os.path.join(self.sdir("rev"), "input", "diff.patch")
        with open(patch_p) as fh:
            patch = fh.read()
        self.assertIn("+uncommitted line", patch)
        self.assertIn("+brand new", patch)
        call = self.calls("rev")[0]
        self.assertIn(patch_p, call["stdin"])
        self.assertEqual(call["argv"][call["argv"].index("--add-dir") + 1], os.path.dirname(patch_p))
        self.assertEqual(self.git(repo, "status", "--porcelain").stdout, before)
        blob = self.git(repo, "hash-object", "new.txt").stdout.strip()
        self.assertNotEqual(self.git(repo, "cat-file", "-e", blob).returncode, 0,
                            "--diff wrote a blob for an untracked file into the user's object store")
        clean = self.repo("clean")
        cp = self.run_as("spawn", "claude", "rev2", clean, "review", "--role", "review", "--diff", "HEAD")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("nothing to review", cp.stderr)
        cp = self.run_as("spawn", "claude", "rev3", self.work, "review", "--role", "review", "--diff", "HEAD")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("git repository", cp.stderr)
        cp = self.run_as("spawn", "claude", "rev4", repo, "review", "--role", "review", "--diff", "no-such-ref")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("not a commit", cp.stderr)


class JudgeDiffTests(SessionTest):
    VERDICT = {"verdict": "approve", "confidence": 0.9, "summary": "fine", "findings": []}

    def test_shared_tree_judge_reads_new_files_without_writing_blobs(self):
        repo = self.repo()
        self.spawn("codex", "x", "add it @@edit:added.txt=fresh_content", directory=repo)
        self.assertEqual(self.wait_result("x")["status"], "ok")
        verdict = self.write_file("verdict.json", json.dumps(self.VERDICT))
        cp = self.run_as("judge", "x", "--timeout", "30", env={"FAKE_ENGINE_ANSWER_FILE": verdict})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        with open(os.path.join(self.sdir("x"), "judge", "diff.patch")) as fh:
            self.assertIn("+fresh content", fh.read())
        blob = self.git(repo, "hash-object", "added.txt").stdout.strip()
        self.assertNotEqual(self.git(repo, "cat-file", "-e", blob).returncode, 0)
        # The claude judge of a codex child may read the judged tree.
        argv = self.calls("x-judge")[0]["argv"]
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertIn(repo, dirs)
        self.assertIn("which you can read", self.calls("x-judge")[0]["stdin"])

    def test_untracked_files_over_the_limit_are_left_out(self):
        repo = self.repo()
        for i in range(4):
            with open(os.path.join(repo, "data%d.bin" % i), "w") as fh:
                fh.write("x\n")
        with open(os.path.join(repo, "README.md"), "a") as fh:
            fh.write("tracked change\n")
        code = ("import sys, json; sys.path.insert(0, sys.argv[1]); from agent_sessions import gitops\n"
                "gitops.UNTRACKED_DIFF_FILES = 2\n"
                "cp, note = gitops.tree_diff(sys.argv[2], 'HEAD', sys.argv[3])\n"
                "print(json.dumps({'rc': cp.returncode, 'patch': cp.stdout, 'note': note}))")
        index = os.path.join(self.box, "idx")
        cp = subprocess.run([sys.executable, "-c", code, os.path.join(SKILL, "scripts"), repo, index],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                            env=self.environ())
        self.assertEqual(cp.returncode, 0, cp.stderr)
        doc = json.loads(cp.stdout)
        self.assertEqual(doc["rc"], 0)
        self.assertIn("+tracked change", doc["patch"])
        self.assertNotIn("data0.bin", doc["patch"])
        self.assertIn("4 untracked files", doc["note"])
        self.assertIn(".gitignore", doc["note"])
        self.assertFalse(os.path.exists(index))


class ReadonlyEnvTests(SessionTest):
    KEYS = {"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-oai-test", "AWS_PROFILE": "admin"}

    def test_readonly_child_gets_only_its_own_engine_variables(self):
        cp = self.spawn("claude", "r", "hi", "--role", "review", env=self.KEYS)
        self.assertIn("ANTHROPIC_*", cp.stdout)
        self.assertIn("withheld", cp.stdout)
        self.spawn("claude", "w", "hi", env=self.KEYS)
        self.spawn("codex", "rc", "hi", "--role", "review", env=self.KEYS)
        self.assertEqual(self.wait("r", "w", "rc").returncode, 0)
        env = self.calls("r")[0]["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-test")
        self.assertIsNone(env["OPENAI_API_KEY"])
        self.assertIsNone(env["AWS_PROFILE"])
        env = self.calls("rc")[0]["env"]
        self.assertIsNone(env["ANTHROPIC_API_KEY"])
        self.assertEqual(env["OPENAI_API_KEY"], "sk-oai-test")
        # A write child keeps the full set: it can read the same keys from disk.
        env = self.calls("w")[0]["env"]
        self.assertEqual((env["ANTHROPIC_API_KEY"], env["OPENAI_API_KEY"], env["AWS_PROFILE"]),
                         ("sk-ant-test", "sk-oai-test", "admin"))

    def test_bedrock_switch_keeps_aws_and_deny_list_wins(self):
        env = dict(self.KEYS, CLAUDE_CODE_USE_BEDROCK="1")
        self.spawn("claude", "b", "hi", "--role", "review", env=env)
        deny = dict(self.KEYS, AGENT_SESSIONS_DENY_ENV="OPENAI_*,AWS_PROFILE")
        self.spawn("claude", "d", "hi", env=deny)
        self.assertEqual(self.wait("b", "d").returncode, 0)
        got = self.calls("b")[0]["env"]
        self.assertEqual(got["AWS_PROFILE"], "admin")
        self.assertEqual(got["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertIsNone(got["OPENAI_API_KEY"])
        got = self.calls("d")[0]["env"]
        self.assertIsNone(got["OPENAI_API_KEY"])
        self.assertIsNone(got["AWS_PROFILE"])
        self.assertEqual(got["ANTHROPIC_API_KEY"], "sk-ant-test")
