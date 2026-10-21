#!/usr/bin/env python3
"""Exercise scripts/ without spending tokens: snapshot, strip, score math, spawn dry runs
and spawn real runs against stub engine binaries placed first on PATH."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(SKILL, "scripts")
REFERENCES = os.path.join(SKILL, "references")
FIXTURES = os.path.join(HERE, "fixtures")
SNAPSHOT = os.path.join(SCRIPTS, "snapshot.py")
STRIP = os.path.join(SCRIPTS, "strip_criteria.py")
SCORE = os.path.join(SCRIPTS, "score_math.py")
SPAWN = os.path.join(SCRIPTS, "spawn_child.sh")

BYPASS = "dangerously"
SKIP_PERMS = "dangerously-skip"
GIT_ENV = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")

# A stand-in for `codex`: honours --version and -o, records what it saw, and misbehaves
# on request through STUB_MODE / STUB_TOUCH so the post-run checks can be exercised.
STUB_CODEX = r"""#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "codex-cli 0.0-stub"; exit 0; fi
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
{
  echo "## Score: 50/100"
  echo "cwd=$(pwd)"
  echo "home=${CODEX_HOME:-unset}"
  echo "stdin=$(cat | wc -c | tr -d ' ')"
  echo "listing=$(ls -A . | tr '\n' ' ')"
  [ -d ../evidence ] && echo "evidence=$(ls -A ../evidence | tr '\n' ' ')"
  echo "renamed=$(grep -c ' -> ' ../prompt.md | tr -d ' ')"
  echo "has_docs_rename=$(grep -c 'docs/CLAUDE.md -> docs/CLAUDE.md.as-data' ../prompt.md | tr -d ' ')"
  echo "staging_header=$(grep -c '^STAGING' ../prompt.md | tr -d ' ')"
  echo "PREFLIGHT OK $(cat hello.txt 2>/dev/null)"
} > "$out"
case "${STUB_MODE:-ok}" in
  modify) touch ./injected.txt ;;
  fail) exit 1 ;;
  silent) rm -f "$out" ;;
  garbage) echo "I refuse" > "$out" ;;
esac
[ -n "${STUB_TOUCH:-}" ] && echo x >> "$STUB_TOUCH"
exit 0
"""


def run_py(script, *args, **kwargs):
    return subprocess.run([sys.executable, "-I", script] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kwargs)


def write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def files_in(directory):
    found = []
    for dirpath, _, filenames in os.walk(directory):
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), directory))
    return sorted(found)


def git(cwd, *args):
    env = dict(os.environ, **GIT_ENV)
    env["HOME"] = cwd
    return subprocess.run(["git"] + list(args), cwd=cwd, env=env, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ii-snap-")
        self.root = os.path.join(self.tmp, "root")
        write(os.path.join(self.root, "src", "a.txt"), "alpha\n")
        write(os.path.join(self.root, ".git", "HEAD"), "ref: refs/heads/main\n")
        write(os.path.join(self.root, ".iterative-improvement", "x", "rounds.md"), "round log\n")
        write(os.path.join(self.root, "docs", "criteria", "x-criteria.md"), "criteria\n")
        write(os.path.join(self.root, "notes-rounds.md"), "rounds\n")
        write(os.path.join(self.root, "node_modules", "m.js"), "module\n")
        os.symlink(os.path.join(self.root, "src", "a.txt"), os.path.join(self.root, "link.txt"))
        self.out = os.path.join(self.tmp, "loop", "snapshots", "round-0")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_excludes_and_skips_symlinks(self):
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("snapshot: 2 files ->", proc.stdout)
        self.assertIn("skipped symlink: link.txt", proc.stderr)
        self.assertEqual(files_in(self.out), ["node_modules/m.js", "src/a.txt"])
        for absent in (".git/HEAD", ".iterative-improvement/x/rounds.md",
                       "docs/criteria/x-criteria.md", "notes-rounds.md", "link.txt"):
            self.assertFalse(os.path.exists(os.path.join(self.out, absent)), absent)
        manifest = json.loads(read(self.out + ".manifest.json"))
        self.assertEqual(sorted(manifest["files"]), ["node_modules/m.js", "src/a.txt"])
        self.assertEqual(len(manifest["files"]["src/a.txt"]["sha256"]), 64)

    def test_secret_shaped_files_excluded_outside_git(self):
        for rel in (".env", ".env.local", "config/.env.production", "certs/server.pem", ".ssh/id_rsa", "id_ed25519.pub"):
            write(os.path.join(self.root, rel), "secret\n")
        write(os.path.join(self.root, "src", "environment.ts"), "ok\n")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files_in(self.out), ["node_modules/m.js", "src/a.txt", "src/environment.ts"])

    def test_create_extra_exclude(self):
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out, "--exclude", "node_modules")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files_in(self.out), ["src/a.txt"])

    def test_create_path_restriction(self):
        write(os.path.join(self.root, "other", "b.txt"), "beta\n")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out, "--path", "src")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files_in(self.out), ["src/a.txt"])

    def test_create_refuses_nonempty_out_and_missing_root(self):
        write(os.path.join(self.out, "stale.txt"), "x")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(self.out, proc.stderr)
        proc = run_py(SNAPSHOT, "create", "--root", os.path.join(self.tmp, "nope"), "--out", self.out + "b")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("nope", proc.stderr)

    def test_verify_detects_changes(self):
        run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        proc = run_py(SNAPSHOT, "verify", "--snapshot", self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "unchanged")
        write(os.path.join(self.out, "src", "a.txt"), "changed\n")
        proc = run_py(SNAPSHOT, "verify", "--snapshot", self.out)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("modified: src/a.txt", proc.stdout)
        write(os.path.join(self.out, "src", "extra.txt"), "new\n")
        proc = run_py(SNAPSHOT, "verify", "--snapshot", self.out)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("added: src/extra.txt", proc.stdout)
        os.remove(os.path.join(self.out, "node_modules", "m.js"))
        proc = run_py(SNAPSHOT, "verify", "--snapshot", self.out)
        self.assertIn("removed: node_modules/m.js", proc.stdout)

    def test_verify_against_explicit_manifest_and_seal(self):
        run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        copy = os.path.join(self.tmp, "stage", "artifact")
        shutil.copytree(self.out, copy)
        proc = run_py(SNAPSHOT, "verify", "--snapshot", copy, "--manifest", self.out + ".manifest.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        os.rename(os.path.join(copy, "src", "a.txt"), os.path.join(copy, "src", "a.txt.as-data"))
        proc = run_py(SNAPSHOT, "seal", "--dir", copy)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sealed: 2 files", proc.stdout)
        self.assertTrue(os.path.isfile(copy + ".manifest.json"))
        proc = run_py(SNAPSHOT, "verify", "--snapshot", copy)
        self.assertEqual(proc.stdout.strip(), "unchanged")
        write(os.path.join(copy, "src", "a.txt.as-data"), "edited\n")
        proc = run_py(SNAPSHOT, "verify", "--snapshot", copy)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("modified: src/a.txt.as-data", proc.stdout)

    def test_restore_backs_up_then_returns_root_to_snapshot(self):
        run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        write(os.path.join(self.root, "src", "a.txt"), "edited\n")
        write(os.path.join(self.root, "src", "new.txt"), "new\n")
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", self.root, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would remove: src/new.txt", proc.stdout)
        self.assertIn("would copy: src/a.txt", proc.stdout)
        self.assertIn("would copy: node_modules/m.js (unchanged)", proc.stdout)
        self.assertIn("backup of 2 files would go to", proc.stdout)
        self.assertIn(os.path.join(self.tmp, "loop", "restore-backup"), proc.stdout)
        self.assertEqual(read(os.path.join(self.root, "src", "a.txt")), "edited\n")
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("restored: 2 copied, 1 removed", proc.stdout)
        match = re.search(r"backup: 2 files -> (.+)$", proc.stdout, re.M)
        self.assertIsNotNone(match, proc.stdout)
        backup = match.group(1)
        self.assertTrue(backup.startswith(os.path.join(self.tmp, "loop", "restore-backup")), backup)
        self.assertEqual(read(os.path.join(backup, "src", "a.txt")), "edited\n")
        self.assertEqual(read(os.path.join(backup, "src", "new.txt")), "new\n")
        self.assertEqual(read(os.path.join(self.root, "src", "a.txt")), "alpha\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "new.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "notes-rounds.md")), "excluded files are left alone")

    def test_restore_refuses_other_root(self):
        run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", other)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("force-root", proc.stderr)
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", other, "--force-root")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(other, "src", "a.txt")))
        self.assertIn("nothing differed, no backup written", proc.stdout.replace("\n", " ") if "nothing" in proc.stdout else proc.stdout)

    @unittest.skipIf(shutil.which("git") is None, "git not on PATH")
    def test_git_repo_respects_gitignore(self):
        shutil.rmtree(os.path.join(self.root, ".git"))
        write(os.path.join(self.root, ".gitignore"), "node_modules/\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        files = files_in(self.out)
        self.assertNotIn("node_modules/m.js", files)
        self.assertIn("src/a.txt", files)
        self.assertNotIn("notes-rounds.md", files)
        self.assertNotIn("docs/criteria/x-criteria.md", files)

    @unittest.skipIf(shutil.which("git") is None, "git not on PATH")
    def test_git_worktree_respects_gitignore(self):
        shutil.rmtree(os.path.join(self.root, ".git"))
        os.remove(os.path.join(self.root, "link.txt"))
        write(os.path.join(self.root, ".gitignore"), "node_modules/\nbuild/\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        worktree = os.path.join(self.tmp, "wt")
        git(self.root, "worktree", "add", "-q", worktree, "-b", "loop")
        self.assertTrue(os.path.isfile(os.path.join(worktree, ".git")), ".git should be a file in a worktree")
        write(os.path.join(worktree, ".env"), "TOKEN=secret\n")
        write(os.path.join(worktree, "node_modules", "m.js"), "module\n")
        write(os.path.join(worktree, "build", "out.js"), "built\n")
        write(os.path.join(worktree, "src", "b.txt"), "beta untracked\n")
        out = os.path.join(self.tmp, "loop", "snapshots", "round-wt")
        proc = run_py(SNAPSHOT, "create", "--root", worktree, "--out", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        files = files_in(out)
        self.assertEqual(files, [".gitignore", "src/a.txt", "src/b.txt"], files)


class StripTests(unittest.TestCase):
    FORBIDDEN = (re.compile(r"\bscore log\b", re.I), re.compile(r"\brubric v\d", re.I),
                 re.compile(r"\bround \d+\b", re.I), re.compile(r"\bnoise floor\b", re.I),
                 re.compile(r"\bmove threshold\b", re.I), re.compile("loop-only", re.I),
                 re.compile(r"\[preserve\]", re.I), re.compile(r"\[achieve\]", re.I),
                 re.compile(r"\bplateau", re.I))

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ii-strip-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def strip(self, text, name="in.md"):
        source = os.path.join(self.tmp, name)
        out = os.path.join(self.tmp, name + ".out")
        write(source, text)
        proc = run_py(STRIP, source, out)
        return proc, (read(out) if os.path.exists(out) else None)

    def test_fixture_strips_clean(self):
        out = os.path.join(self.tmp, "stripped.md")
        proc = run_py(STRIP, os.path.join(FIXTURES, "criteria-full.md"), out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stripped:", proc.stdout)
        text = read(out)
        for pattern in self.FORBIDDEN:
            self.assertIsNone(pattern.search(text), pattern.pattern)
        self.assertNotIn("Baseline", text)
        self.assertNotIn("a1b2c3d (v1)", text)
        for keep in ("## Criteria", "## Gates", "## Constraints", "## Out of scope", "**How to check it:**", "a1b2c3d"):
            self.assertIn(keep, text)
        for line in text.splitlines():
            if line.startswith("- **G"):
                self.assertNotIn("[PRESERVE]", line)
                self.assertNotIn("[ACHIEVE]", line)
        self.assertNotIn("\n\n\n\n", text)

    def test_leak_outside_heading_fails(self):
        proc, out = self.strip("# Criteria: x\n\n## Context\n\nSee the Score log for history.\n\n## Criteria (100 points)\n")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("strip failed", proc.stderr)
        self.assertIn("score log", proc.stderr)
        self.assertIsNone(out)

    def test_word_boundaries(self):
        ok_texts = (
            "### C1. Spacing (10 pts)\n**How to check it:** measure the gap; it should be around 16px, not around 0.9em.\n",
            "### C2. Locality (10 pts)\n**How to check it:** Run `git log -n 3 <baseline SHA>`. The baseline SHA is fixed in the doc.\n",
            "### C3. Ground (5 pts)\n**Anchors:** the background is a flat colour; surroundings stay neutral.\n",
        )
        for text in ok_texts:
            proc, _ = self.strip(text)
            self.assertEqual(proc.returncode, 0, "%r\n%s" % (text, proc.stderr))
        bad_texts = ("Compare with Round 1.\n", "This is rubric v2.\n", "The score plateaued here.\n",
                     "class [preserve] stays.\n", "see the noise floor\n", "Move threshold T=8\n")
        for text in bad_texts:
            proc, _ = self.strip("# Criteria: x\n\n## Context\n\n" + text)
            self.assertEqual(proc.returncode, 4, text)

    def test_guide_worked_criteria_and_template_strip_clean(self):
        guide = read(os.path.join(REFERENCES, "criteria-guide.md"))
        blocks = re.findall(r"```(?:markdown)?\n(.*?)```", guide, re.S)
        criteria_blocks = [b for b in blocks if b.lstrip().startswith("### C") or b.lstrip().startswith("# Criteria:")]
        self.assertGreaterEqual(len(criteria_blocks), 4, "expected the template and three worked criteria")
        for index, block in enumerate(criteria_blocks):
            proc, out = self.strip(block, "guide-%d.md" % index)
            self.assertEqual(proc.returncode, 0, "block %d failed: %s\n%s" % (index, proc.stderr, block[:200]))
            self.assertIn("How to check it", out)

    def test_example_loop_criteria_strip_clean(self):
        example = read(os.path.join(REFERENCES, "example-loop.md"))
        block = re.search(r"```markdown\n(# Criteria: tidylog.*?)```", example, re.S).group(1)
        proc, out = self.strip(block, "example.md")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Target", out)
        self.assertIn("a1b2c3d", out)

    def test_missing_input(self):
        proc = run_py(STRIP, os.path.join(self.tmp, "missing.md"), os.path.join(self.tmp, "out.md"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("missing.md", proc.stderr)


class ScoreMathTests(unittest.TestCase):
    def baseline(self, a, b):
        proc = run_py(SCORE, "baseline", a, b)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def decide(self, scores, *extra, **kwargs):
        max_rounds = str(kwargs.get("max_rounds", 5))
        args = ["decide", "--threshold", "8", "--target", "85", "--max-rounds", max_rounds, "--scores", scores] + list(extra)
        return run_py(SCORE, *args)

    def first(self, proc):
        return proc.stdout.splitlines()[0] if proc.stdout else proc.stderr

    def test_baseline(self):
        self.assertEqual(self.baseline("41", "45"), "baseline=43 noise=4 threshold=8 soft=false")
        self.assertEqual(self.baseline("40", "50"), "baseline=45 noise=10 threshold=20 soft=true")
        self.assertEqual(self.baseline("50", "50"), "baseline=50 noise=0 threshold=3 soft=false")
        self.assertEqual(self.baseline("56", "60"), "baseline=58 noise=4 threshold=8 soft=false")
        proc = run_py(SCORE, "baseline", "41", "45", "--json")
        self.assertEqual(json.loads(proc.stdout), {"baseline": 43, "noise": 4, "threshold": 8, "soft": False})
        self.assertEqual(run_py(SCORE, "baseline", "41", "145").returncode, 2)

    def test_decisions(self):
        cases = [
            ("58,73,78,80", ("--rounds-used", "4", "--gates", "PASS"), "STOP plateau"),
            ("58,71,76,79", ("--gates", "PASS"), "STOP plateau"),
            ("58,71,76,79", ("--rounds-used", "4", "--gates", "PASS"), "STOP plateau"),
            ("58,71,76,78", (), "STOP plateau"),
            ("58,71,75,79", (), "STOP plateau"),
            ("58,71,77,83", (), "CONTINUE"),
            ("58,71,80,78", (), "CONTINUE"),
            ("58,71", (), "CONTINUE"),
            ("58,71,60", (), "STOP regression"),
            ("58,71,66,63", (), "STOP regression"),
            ("58,71,70,69", (), "STOP plateau"),
            ("58,71,80,86", (), "CONFIRM target-reached"),
            ("58,71,80,86", ("--confirm-score", "86"), "STOP target-hit"),
            ("58,71,80,86", ("--confirm-score", "82"), "CONTINUE target-unconfirmed"),
            ("43,49/80", (), "CONTINUE"),
            ("43,49/80,52/80", (), "BLOCKED na-repeated"),
            ("43,49/80,52/80,53/80", (), "BLOCKED na-repeated"),
            ("58,60,62,63,64,65", (), "STOP rounds-exhausted"),
            ("58,60,62,63,64,65", ("--gates", "FAIL"), "HALT gate-fail-rounds-exhausted"),
        ]
        for scores, extra, expected in cases:
            proc = self.decide(scores, *extra)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            first = self.first(proc)
            self.assertTrue(first.startswith(expected), "%s %s -> %r, expected %r" % (scores, extra, first, expected))

    def test_rounds_used_drives_the_budget(self):
        proc = self.decide("58,71,80", "--rounds-used", "3", max_rounds=3)
        self.assertTrue(self.first(proc).startswith("STOP rounds-exhausted"), proc.stdout)
        proc = self.decide("58,71,80", "--first-round", "1", max_rounds=3)
        self.assertTrue(self.first(proc).startswith("STOP rounds-exhausted"), proc.stdout)
        proc = self.decide("58,71,80", max_rounds=3)
        self.assertTrue(self.first(proc).startswith("CONTINUE"), proc.stdout)
        self.assertIn("2 of 3 rounds used", proc.stdout)
        proc = self.decide("58,71,80", "--rounds-used", "3", "--gates", "FAIL", max_rounds=3)
        self.assertTrue(self.first(proc).startswith("HALT"), proc.stdout)

    def test_decision_details(self):
        proc = self.decide("58,71")
        self.assertTrue(proc.stdout.splitlines()[1].startswith("detail: +13; 1 of 5 rounds used"), proc.stdout)
        proc = self.decide("43,49/80", "--rounds-used", "1", "--gates", "FAIL")
        self.assertIn("incomparable", proc.stdout)
        proc = self.decide("58,71,80,86", "--gates", "FAIL")
        self.assertTrue(proc.stdout.startswith("CONTINUE"), proc.stdout)
        self.assertIn("gates", proc.stdout.splitlines()[1])
        proc = self.decide("58,60,62,63,64,65")
        self.assertIn("rounds-exhausted", self.first(proc))
        proc = self.decide("58,71,70")
        self.assertIn("within-noise: -1", proc.stdout)
        proc = self.decide("58,71,76")
        self.assertIn("within-noise: +5", proc.stdout)
        proc = self.decide("58,71,70,69")
        self.assertIn("-1 and -1 are both inside T=8 and total -2", proc.stdout)
        proc = self.decide("58,71,76,79", "--rounds-used", "4")
        self.assertEqual(self.first(proc), "STOP plateau")
        self.assertIn("+5 and +3 are both inside T=8 and total +8", proc.stdout)
        self.assertIn("budget left", proc.stdout)
        proc = self.decide("58,71,77,83")
        self.assertEqual(self.first(proc), "CONTINUE continue")
        self.assertIn("slow real trend: +6 and +6", proc.stdout)
        proc = self.decide("58,71,80,78")
        self.assertIn("within-noise: -2", proc.stdout)

    def test_example_loop_series(self):
        proc = self.decide("43,49/80", "--rounds-used", "1", "--gates", "FAIL")
        self.assertTrue(self.first(proc).startswith("CONTINUE"))
        proc = self.decide("58,73", "--rounds-used", "2")
        self.assertIn("detail: +15", proc.stdout)
        proc = self.decide("58,73,78", "--rounds-used", "3")
        self.assertIn("within-noise: +5", proc.stdout)
        proc = self.decide("58,73,78,80", "--rounds-used", "4", "--gates", "PASS")
        self.assertEqual(self.first(proc), "STOP plateau")
        self.assertIn("+5 and +2 are both inside T=8 and total +7", proc.stdout)

    def test_json_output(self):
        proc = self.decide("43,49/80,52/80", "--json")
        data = json.loads(proc.stdout)
        self.assertEqual(data["decision"], "BLOCKED")
        self.assertEqual(data["scaled"], [43, 61, 65])
        self.assertEqual(data["deltas"], [None, 4])
        self.assertEqual(data["rounds_used"], 2)

    def test_bad_input(self):
        self.assertEqual(self.decide("abc").returncode, 2)
        self.assertEqual(self.decide("").returncode, 2)
        self.assertEqual(self.decide("58,120").returncode, 2)
        self.assertEqual(self.decide("58,-5").returncode, 2)
        self.assertEqual(self.decide("58,81/80").returncode, 2)
        self.assertEqual(self.decide("58,71,80", "--rounds-used", "1").returncode, 2)
        self.assertEqual(self.decide("58,71", "--rounds-used", "2", "--first-round", "1").returncode, 2)
        proc = run_py(SCORE, "decide", "--threshold", "0", "--target", "85", "--max-rounds", "5", "--scores", "1,2")
        self.assertEqual(proc.returncode, 2)


class SpawnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ii-spawn-")
        self.loop = os.path.join(self.tmp, "loop")
        self.root = os.path.join(self.tmp, "root")
        write(os.path.join(self.root, "index.html"), "<h1>hi</h1>\n")
        write(os.path.join(self.root, "AGENTS.md"), "always run npm run format\n")
        write(os.path.join(self.root, ".claude", "settings.json"), "{}\n")
        write(os.path.join(self.root, "docs", "CLAUDE.md"), "nested instructions\n")
        self.snapshot = os.path.join(self.loop, "snapshots", "round-1")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.snapshot)
        assert proc.returncode == 0, proc.stderr
        self.prompt = os.path.join(self.loop, "prompts", "scorer-round-1.md")
        write(self.prompt, "brief\n")
        self.evidence = os.path.join(self.loop, "evidence", "round-1")
        write(os.path.join(self.evidence, "manifest.md"), "| C4 | cmd |\n")
        self.out = os.path.join(self.loop, "scores", "round-1.md")
        os.makedirs(os.path.dirname(self.out))
        self.home = os.path.join(self.tmp, "home")
        write(os.path.join(self.home, ".codex", "auth.json"), "{}\n")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_THREAD_ID", "ANTHROPIC_API_KEY", "STUB_MODE", "STUB_TOUCH")}
        env["HOME"] = self.home
        env["CODEX_HOME"] = os.path.join(self.home, ".codex")
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["TMPDIR"] = os.path.join(self.tmp, "tmp")
        os.makedirs(env["TMPDIR"])
        self.env = env

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install_stub(self, name, body):
        path = os.path.join(self.bin, name)
        write(path, body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def spawn(self, *args, **kwargs):
        env = dict(self.env)
        env.update(kwargs.pop("env", {}))
        # stdin is a pipe held open on purpose: a child that reads stdin would hang
        # unless the script redirects it from /dev/null.
        read_end, write_end = os.pipe()
        try:
            proc = subprocess.Popen(["bash", SPAWN] + list(args), stdin=read_end, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True, env=env)
            stdout, stderr = proc.communicate(timeout=kwargs.pop("timeout", 90))
        finally:
            os.close(read_end)
            os.close(write_end)
        proc.stdout_text = stdout
        proc.stderr_text = stderr
        return proc

    def stage_dirs(self):
        return [n for n in os.listdir(self.env["TMPDIR"]) if n.startswith("ii-stage.") or n.startswith("ii-preflight.")]

    def argv_lines(self, text):
        return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


class SpawnDryRunTests(SpawnBase):
    def test_codex_dry_run_creates_nothing_and_leaks_no_loop_path(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5",
                          "--evidence", self.evidence, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        out = proc.stdout_text
        for needle in ("CODEX_HOME='/<stage>/home'", "--ephemeral", "'-s'", "read-only", "--skip-git-repo-check",
                       "project_doc_max_bytes=0", "'-m'", "'gpt-5'", "'-C'", "'/<stage>/artifact'", "'-o'", "'/<stage>/out.md'",
                       "# model: gpt-5", "# same-engine: no", "/<stage>/evidence"):
            self.assertIn(needle, out, needle)
        self.assertNotIn(BYPASS, out)
        self.assertNotIn("workspace-write", out)
        argv = self.argv_lines(out)
        self.assertNotIn(self.loop, argv, "a loop path reached the child's argv")
        self.assertNotIn(self.root, argv)
        self.assertNotIn("round-1", argv)
        self.assertFalse(os.path.exists(os.path.join(self.loop, "homes")), "dry run must not create homes/")
        self.assertFalse(os.path.exists(self.out[:-3] + ".cmd"), "dry run must not write the .cmd record")
        self.assertEqual(self.stage_dirs(), [])

    def test_claude_flags(self):
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        out = proc.stdout_text
        for needle in ("--setting-sources", "--disable-slash-commands", "--strict-mcp-config", "--permission-mode", "dontAsk",
                       "'--add-dir'", "'/<stage>'", "Edit(//<stage>/out.md)", "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(wc:*)"):
            self.assertIn(needle, out, needle)
        for absent in ("--bare", SKIP_PERMS, "Bash(find", "Bash(tail", "Write(", "'--allow'"):
            self.assertNotIn(absent, out, absent)
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run",
                          env={"ANTHROPIC_API_KEY": "test-key-placeholder"})
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("--bare", proc.stdout_text)

    def test_claude_no_bash_and_model(self):
        proc = self.spawn("claude", "scorer", self.prompt, self.snapshot, self.out, "--model", "opus", "--no-bash", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        out = proc.stdout_text
        self.assertIn("'Read,Grep,Glob,Write'", out)
        self.assertNotIn("Bash", out)
        self.assertIn("'--model'", out)
        self.assertIn("'opus'", out)
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--no-bash", "--dry-run")
        self.assertEqual(proc.returncode, 2)

    def test_self_spawn_refused_unless_same_engine(self):
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run", env={"CLAUDECODE": "1"})
        self.assertEqual(proc.returncode, 5)
        self.assertIn("same-engine", proc.stderr_text)
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run", env={"CODEX_THREAD_ID": "x"})
        self.assertEqual(proc.returncode, 5)
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run", "--same-engine", env={"CLAUDECODE": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("# same-engine: yes", proc.stdout_text)

    def test_kimi(self):
        proc = self.spawn("kimi", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("--skills-dir", proc.stdout_text)
        self.assertIn("'/<stage>/home/kimi-skills'", proc.stdout_text)
        self.assertIn("--add-dir", proc.stdout_text)
        self.assertNotIn("--" + "yolo", proc.stdout_text)
        self.assertNotIn("--auto", proc.stdout_text)
        proc = self.spawn("kimi", "scorer", self.prompt, self.snapshot, self.out, "--model", "k2", "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("reviewer only", proc.stderr_text)

    def test_refusals(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--dry-run")
        self.assertEqual(proc.returncode, 0, "a dry run warns instead of refusing")
        self.assertIn("warning: a scorer needs --model", proc.stderr_text)
        self.assertIn("# model: cli default (not pinned)", proc.stdout_text)
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--allow", "npm run build", "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("removed", proc.stderr_text)
        proc = self.spawn("gpt", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown engine", proc.stderr_text)
        bare = os.path.join(self.loop, "snapshots", "round-2")
        os.makedirs(bare)
        proc = self.spawn("codex", "reviewer", self.prompt, bare, self.out, "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn(bare + ".manifest.json", proc.stderr_text)
        proc = self.spawn("codex", "reviewer", os.path.join(self.tmp, "nope.md"), self.snapshot, self.out, "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("prompt file not found", proc.stderr_text)
        elsewhere = os.path.join(self.tmp, "elsewhere", "snap")
        shutil.copytree(self.snapshot, elsewhere)
        shutil.copy(self.snapshot + ".manifest.json", elsewhere + ".manifest.json")
        proc = self.spawn("codex", "reviewer", self.prompt, elsewhere, self.out, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("warning: cannot infer the loop dir", proc.stderr_text)
        proc = self.spawn("codex", "reviewer", self.prompt, elsewhere, self.out, "--loop-dir", self.loop, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--timeout", "soon", "--dry-run")
        self.assertEqual(proc.returncode, 2)

    def test_help(self):
        proc = self.spawn("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stdout_text)
        self.assertIn("--preflight", proc.stdout_text)
        self.assertIn("Exit codes", proc.stdout_text)


class SpawnStubRunTests(SpawnBase):
    def setUp(self):
        super(SpawnStubRunTests, self).setUp()
        self.install_stub("codex", STUB_CODEX)

    def test_successful_run(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", "--evidence", self.evidence)
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assertTrue(proc.stdout_text.startswith("ok: " + os.path.realpath(self.out)), proc.stdout_text)
        out = read(self.out)
        self.assertIn("## Score: 50/100", out)
        self.assertIn("stdin=0", out, "stdin must be redirected from /dev/null")
        self.assertRegex(out, r"cwd=.*/ii-stage\.[^/]+/artifact")
        self.assertRegex(out, r"home=.*/ii-stage\.[^/]+/home")
        listing = re.search(r"listing=(.*)", out).group(1)
        self.assertIn("AGENTS.md.as-data", listing)
        self.assertIn(".claude.as-data", listing)
        self.assertNotIn("AGENTS.md ", listing)
        self.assertIn("evidence=manifest.md", out)
        self.assertIn("renamed=3", out)
        self.assertIn("staging_header=1", out)
        record = read(self.out[:-3] + ".cmd")
        self.assertIn("# engine: codex (codex-cli 0.0-stub)", record)
        self.assertIn("# model: gpt-5", record)
        self.assertIn("'/<stage>/artifact'", record)
        self.assertNotIn(self.loop, record)
        self.assertNotIn(self.env["TMPDIR"], record)
        self.assertTrue(os.path.isfile(self.out[:-3] + ".log"))
        self.assertEqual(self.stage_dirs(), [], "stage dir must be deleted")
        self.assertEqual(run_py(SNAPSHOT, "verify", "--snapshot", self.snapshot).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.loop, "rounds.md")), "a clean run logs no incident")

    def test_prompt_gets_staging_section_and_renames(self):
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        out = read(self.out)
        self.assertIn("has_docs_rename=1", out)
        self.assertIn("renamed=3", out)
        record = read(self.out[:-3] + ".cmd")
        self.assertIn("# model: cli default (not pinned)", record)
        self.assertNotIn("'-m'", record)

    def test_modified_artifact_discards_output(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", env={"STUB_MODE": "modify"})
        self.assertEqual(proc.returncode, 4, proc.stderr_text)
        self.assertIn("added: injected.txt", proc.stdout_text)
        self.assertIn("output discarded", proc.stderr_text)
        self.assertFalse(os.path.exists(self.out))
        self.assertTrue(os.path.exists(self.out + ".discarded"))
        rounds = read(os.path.join(self.loop, "rounds.md"))
        self.assertIn("codex scorer: child modified the staged artifact; output discarded", rounds)
        self.assertEqual(self.stage_dirs(), [])
        self.assertEqual(run_py(SNAPSHOT, "verify", "--snapshot", self.snapshot).returncode, 0, "the real snapshot is untouched")

    def test_child_failure_and_no_output(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", env={"STUB_MODE": "fail"})
        self.assertEqual(proc.returncode, 7, proc.stderr_text)
        self.assertIn("exited with status 1", proc.stderr_text)
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", env={"STUB_MODE": "silent"})
        self.assertEqual(proc.returncode, 6, proc.stderr_text)
        self.assertIn("no output file", proc.stderr_text)
        self.assertEqual(self.stage_dirs(), [])

    @unittest.skipIf(shutil.which("git") is None, "git not on PATH")
    def test_root_change_during_run_discards_output(self):
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        snapshot = os.path.join(self.loop, "snapshots", "round-2")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", snapshot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.spawn("codex", "reviewer", self.prompt, snapshot, self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        out2 = os.path.join(self.loop, "scores", "round-2.md")
        proc = self.spawn("codex", "reviewer", self.prompt, snapshot, out2, env={"STUB_TOUCH": os.path.join(self.root, "index.html")})
        self.assertEqual(proc.returncode, 4, proc.stderr_text)
        self.assertIn("artifact root", proc.stderr_text)
        self.assertTrue(os.path.exists(out2 + ".discarded"))

    def test_preflight(self):
        proc = self.spawn("--preflight", "codex", "--model", "gpt-5")
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assertEqual(proc.stdout_text.strip(), "preflight ok: codex (codex-cli 0.0-stub) as scorer, model gpt-5")
        proc = self.spawn("--preflight", "codex")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("as reviewer", proc.stdout_text)
        proc = self.spawn("--preflight", "codex", env={"STUB_MODE": "garbage"})
        self.assertEqual(proc.returncode, 7)
        self.assertIn("did not read the staged file", proc.stderr_text)
        proc = self.spawn("--preflight", "codex", env={"STUB_MODE": "fail"})
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(self.stage_dirs(), [])

    def test_real_run_refuses_unpinned_scorer_and_unknown_loop_dir(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--model", proc.stderr_text)
        elsewhere = os.path.join(self.tmp, "elsewhere", "snap")
        shutil.copytree(self.snapshot, elsewhere)
        shutil.copy(self.snapshot + ".manifest.json", elsewhere + ".manifest.json")
        proc = self.spawn("codex", "reviewer", self.prompt, elsewhere, self.out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--loop-dir", proc.stderr_text)
        self.assertFalse(os.path.exists(self.out))

    def test_missing_codex_auth_is_explained(self):
        os.remove(os.path.join(self.home, ".codex", "auth.json"))
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("keyring", proc.stderr_text)


class HelpTests(unittest.TestCase):
    def test_python_scripts_help(self):
        for script in (SNAPSHOT, STRIP, SCORE):
            proc = run_py(script, "--help")
            self.assertEqual(proc.returncode, 0, script)
            self.assertIn("usage", proc.stdout.lower())


if __name__ == "__main__":
    unittest.main()
