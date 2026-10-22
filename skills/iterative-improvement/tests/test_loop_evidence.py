#!/usr/bin/env python3
"""Exercise scripts/loop.py and scripts/evidence.py without spending tokens."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.abspath(os.path.join(HERE, "..")), "scripts")
LOOP = os.path.join(SCRIPTS, "loop.py")
EVIDENCE = os.path.join(SCRIPTS, "evidence.py")
SNAPSHOT = os.path.join(SCRIPTS, "snapshot.py")
GIT_ENV = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}


def run_py(script, *args, **kwargs):
    return subprocess.run([sys.executable, "-I", script] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, env=clean_env(), **kwargs)


def write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def git(cwd, *args):
    env = clean_env()
    env.update(GIT_ENV)
    env["HOME"] = cwd
    return subprocess.run(["git"] + list(args), cwd=cwd, env=env, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout


HAS_GIT = shutil.which("git") is not None


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ii-loop-"))
        self.root = os.path.join(self.tmp, "repo")
        write(os.path.join(self.root, "README.md"), "# tool\n")
        write(os.path.join(self.root, "src", "cli.py"), "print(1)\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def init(self, root, *extra):
        return run_py(LOOP, "init", "--root", root, "--slug", "readme", "--scope", "README.md", *extra)

    @unittest.skipUnless(HAS_GIT, "git not on PATH")
    def test_init_in_a_repo_puts_loop_in_the_git_dir(self):
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        proc = self.init(self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        loop = proc.stdout.strip()
        self.assertEqual(loop, os.path.join(self.root, ".git", "iterative-improvement", "readme"))
        for name in ("prompts", "reviews", "scores", "evidence", "snapshots", "tools"):
            self.assertTrue(os.path.isdir(os.path.join(loop, name)), name)
        rounds = read(os.path.join(loop, "rounds.md"))
        self.assertRegex(rounds, r"- Kickoff \(UTC\): \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        self.assertIn("- Scope: README.md", rounds)
        self.assertIn("- HEAD at kickoff: ", rounds)
        self.assertIn("- Scope status at kickoff: clean", rounds)
        self.assertEqual(git(self.root, "status", "--porcelain"), "", "LOOP must be invisible to git and repo tools")
        proc = self.init(self.root)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("loop.py status", proc.stderr)

    @unittest.skipUnless(HAS_GIT, "git not on PATH")
    def test_init_in_a_worktree_and_dirty_scope_warning(self):
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        worktree = os.path.join(self.tmp, "wt")
        git(self.root, "worktree", "add", "-q", worktree, "-b", "loop")
        write(os.path.join(worktree, "README.md"), "# tool, edited\n")
        proc = self.init(worktree)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        loop = proc.stdout.strip()
        self.assertTrue(loop.startswith(os.path.join(self.root, ".git", "worktrees")), loop)
        self.assertIn("uncommitted changes", proc.stderr)
        self.assertEqual(git(worktree, "status", "--porcelain").strip(), "M README.md")

    def test_init_outside_a_repo_and_bad_input(self):
        proc = self.init(self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), os.path.join(self.root, ".iterative-improvement", "readme"))
        self.assertIn("Not a git repo", read(os.path.join(proc.stdout.strip(), "rounds.md")))
        proc = run_py(LOOP, "init", "--root", self.root, "--slug", "Bad Slug")
        self.assertEqual(proc.returncode, 2)
        proc = run_py(LOOP, "init", "--root", os.path.join(self.tmp, "nope"), "--slug", "x")
        self.assertEqual(proc.returncode, 2)
        proc = run_py(LOOP, "status", "--loop", os.path.join(self.tmp, "nope"))
        self.assertEqual(proc.returncode, 2)

    def test_status_reads_the_loop(self):
        loop = self.init(self.root).stdout.strip()
        proc = run_py(LOOP, "status", "--loop", loop)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("next: round 0", proc.stdout)
        self.assertIn("criteria: not found", proc.stdout)
        os.makedirs(os.path.join(loop, "snapshots", "round-0"))
        write(os.path.join(loop, "scores", "round-0.md"), "## Score: 52/100\n")
        record = ("# spawn_child.sh record\n# engine: codex (codex-cli 0.163.0)\n# role: scorer\n# model: gpt-5\n"
                  "'codex' \\\n  'exec'\n")
        write(os.path.join(loop, "scores", "round-0.cmd"), record)
        write(os.path.join(loop, "scores", "round-1.cmd"), record.replace("0.163.0", "0.163.4"))
        write(os.path.join(loop, "scores", "round-2.cmd"), record.replace("0.163.0", "0.164.0"))
        write(os.path.join(loop, "scores", "round-3.cmd"), record.replace("gpt-5", "gpt-5-mini"))
        write(os.path.join(loop, "readme-criteria.md"),
              "# Criteria: x\n**Rubric version:** v1\n\n## Score log\n\n| Round | Rubric | Raw | Adj max | Scaled |\n"
              "|---|---|---|---|---|\n| 0 | v1 | 52 | 100 | 52 |\n")
        proc = run_py(LOOP, "status", "--loop", loop)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("scorer pin: scores/round-0.cmd; differing: round-2.cmd, round-3.cmd", proc.stdout,
                      "a patch update is the same instrument; a minor update or a new model is not")
        self.assertIn("rubric v1; series 52", proc.stdout)
        self.assertIn("next: run decide below", proc.stdout)
        self.assertIn("decide: score_math.py decide --threshold 3 --target 90 --max-rounds 2 --scores 52 "
                      "--rounds-used 0 --gates PASS|FAIL", proc.stdout)

    def full_loop(self, *extra):
        proc = self.init(self.root, "--mode", "full", *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def status_with_budget(self, loop, budget):
        write(os.path.join(loop, "readme-criteria.md"),
              "# Criteria: x\n**Rubric version:** v1\n**Mode:** full\n**Target:** 85/100   **Budget:** %s\n"
              "**Move threshold:** T=8\n\n## Score log\n\n| Round | Rubric | Raw | Adj max | Scaled |\n|---|---|---|---|---|\n"
              "| 0 | v1 | 43 | 100 | 43 |\n| 1 | v1 | 52 | 100 | 52 |\n" % budget)
        os.makedirs(os.path.join(loop, "snapshots", "round-1"), exist_ok=True)
        write(os.path.join(loop, "scores", "round-1.md"), "## Score: 52/100\n")
        proc = run_py(LOOP, "status", "--loop", loop)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_status_never_reads_minutes_as_rounds(self):
        out = self.status_with_budget(self.full_loop(), "90 minutes (4 rounds max)")
        self.assertIn("--threshold 8 --target 85 --max-rounds 4 --scores 43,52 --rounds-used 1", out)
        shutil.rmtree(os.path.join(self.root, ".iterative-improvement"))
        out = self.status_with_budget(self.full_loop(), "90 minutes")
        self.assertIn("--max-rounds 5 ", out, "no rounds in the label: init's full-mode default")
        shutil.rmtree(os.path.join(self.root, ".iterative-improvement"))
        out = self.status_with_budget(self.full_loop("--rounds", "3"), "90 minutes (4 rounds max)")
        self.assertIn("--max-rounds 3 ", out, "an explicit --rounds is the user's ceiling")

    def test_init_records_the_users_decisions(self):
        loop = self.full_loop("--rounds", "4", "--commits", "yes", "--budget", "90 minutes",
                              "--engines", "codex gpt-5 (OpenAI) reviewer and scorer, 17 runs",
                              "--off-limits", "site/tokens.css", "--off-limits", "LICENSE")
        rounds = read(os.path.join(loop, "rounds.md"))
        for line in ("- Round ceiling: 4", "- Commits allowed: yes", "- Budget: 90 minutes",
                     "- Engines and vendors: codex gpt-5 (OpenAI) reviewer and scorer, 17 runs",
                     "- Off limits: site/tokens.css, LICENSE"):
            self.assertIn(line, rounds)
        proc = run_py(LOOP, "status", "--loop", loop)
        self.assertIn("decisions: rounds 4; commits yes; budget 90 minutes; engines codex gpt-5", proc.stdout)
        self.assertIn("off limits site/tokens.css, LICENSE", proc.stdout)
        shutil.rmtree(os.path.join(self.root, ".iterative-improvement"))
        rounds = read(os.path.join(self.init(self.root).stdout.strip(), "rounds.md"))
        self.assertIn("- Round ceiling: 2 (default)", rounds)
        self.assertIn("- Commits allowed: no", rounds)
        self.assertEqual(run_py(LOOP, "init", "--root", self.root, "--slug", "y", "--rounds", "0").returncode, 2)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ii-ev-"))
        self.dir = os.path.join(self.tmp, "loop", "evidence", "round-0")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self, check, *command, **kwargs):
        extra = list(kwargs.get("extra", []))
        return run_py(EVIDENCE, "run", "--dir", self.dir, "--check", check, *(extra + ["--"] + list(command)))

    def rows(self):
        return [line for line in read(os.path.join(self.dir, "manifest.md")).splitlines() if re.match(r"^\| [A-Z]\d+ ", line)]

    def test_runs_once_logs_and_refuses_a_second_capture(self):
        proc = self.run_check("G1", "bash", "-c", "echo built; echo shot > c3.png; exit 3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("G1 attempt 1, exit 3", proc.stdout)
        self.assertEqual(read(os.path.join(self.dir, "G1-1.log")), "built\n")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "c3.png")), "a relative capture lands in the evidence dir")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0], r"^\| G1 \| bash -c .* \| \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| 1 \| exit 3; output G1-1.log; files: c3.png \|$")
        proc = self.run_check("G1", "true")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already captured", proc.stderr)
        proc = self.run_check("G1", "true", extra=["--retake", "server was down"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("retake: server was down", self.rows()[1])
        self.assertEqual(len(self.rows()), 2)

    def test_timeout_is_recorded_as_a_tool_failure(self):
        proc = self.run_check("C7", "sleep", "30", extra=["--timeout", "1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("timed out", proc.stdout)
        self.assertIn("timed out after 1s", self.rows()[0])

    def test_snapshot_must_match_the_tree(self):
        root = os.path.join(self.tmp, "root")
        write(os.path.join(root, "index.html"), "<p>a</p>\n")
        snap = os.path.join(self.tmp, "loop", "snapshots", "round-0")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", root, "--out", snap).returncode, 0)
        proc = self.run_check("G1", "true", extra=["--snapshot", snap])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(self.rows()[0], r"snapshot [0-9a-f]{12} \|$")
        write(os.path.join(root, "index.html"), "<p>b</p>\n")
        proc = self.run_check("G2", "true", extra=["--snapshot", snap])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("changed: index.html", proc.stderr)

    def test_view_hides_the_manifest_and_check_ids(self):
        self.run_check("C3", "bash", "-c", "echo blur")
        self.run_check("G2", "bash", "-c", "echo 390 > overflow.txt")
        out = os.path.join(self.tmp, "loop", "evidence", "view-round-0")
        proc = run_py(EVIDENCE, "view", "--dir", self.dir, "--out", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = sorted(os.listdir(out))
        self.assertNotIn("manifest.md", names)
        self.assertEqual(names, ["capture-1.log", "capture-2.log", "overflow.txt"])
        proc = run_py(EVIDENCE, "view", "--dir", self.dir, "--out", out)
        self.assertEqual(proc.returncode, 2)

    def test_help(self):
        for script, words in ((LOOP, ("init", "status")), (EVIDENCE, ("run", "view"))):
            proc = run_py(script, "--help")
            self.assertEqual(proc.returncode, 0)
            for word in words:
                self.assertIn(word, proc.stdout)


if __name__ == "__main__":
    unittest.main()
