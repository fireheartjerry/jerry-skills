#!/usr/bin/env python3
"""Exercise scripts/ without spending tokens: snapshot, strip, score math, spawn dry runs
and spawn real runs against stub engine binaries placed first on PATH."""

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
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
# A stand-in for `kimi` (no sandbox): writes ../out.md and can touch a file in ROOT.
STUB_KIMI = r"""#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "kimi 0.0-stub"; exit 0; fi
echo "review" > ../out.md
[ -n "${STUB_TOUCH:-}" ] && echo x >> "$STUB_TOUCH"
exit 0
"""

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
  echo "auth=$(cat "${CODEX_HOME:-/nonexistent}/auth.json" 2>&1 | head -c 40)"
  echo "PREFLIGHT OK $(cat hello.txt 2>/dev/null)"
} > "$out"
case "${STUB_MODE:-ok}" in
  modify) touch ./injected.txt ;;
  fail) exit 1 ;;
  silent) rm -f "$out" ;;
  garbage) echo "I refuse" > "$out" ;;
  usage) printf 'codex\nDONE\ntokens used\n19,783\n' ;;
  usage-oneline) echo "tokens used: 1234" ;;
  sleep) sleep 60 & echo "$! $$" > "$STUB_PIDS"; wait ;;
  rotate) rm -f "$CODEX_HOME/auth.json"; echo '{"rotated":1}' > "$CODEX_HOME/auth.json" ;;
  vision) python3 -c '
import struct, sys, zlib
data = open(sys.argv[1], "rb").read()
at = data.index(b"IDAT")
size = struct.unpack(">I", data[at - 4:at])[0]
pixel = tuple(zlib.decompress(data[at + 4:at + 4 + size])[1:4])
names = {(220, 20, 20): "red", (20, 170, 40): "green", (20, 40, 220): "blue",
         (240, 220, 20): "yellow", (220, 20, 220): "magenta", (20, 210, 220): "cyan"}
print("PREFLIGHT OK " + names[pixel])
' ../evidence/swatch.png > "$out" ;;
  blind) echo "PREFLIGHT OK red" > "$out" ;;
esac
[ -n "${STUB_TOUCH:-}" ] && echo x >> "$STUB_TOUCH"
exit 0
"""

# A stand-in for `claude -p`: writes ../out.md (its cwd is the staged artifact) and, under
# --output-format json, prints the one JSON result object claude prints, with a cost.
STUB_CLAUDE = r"""#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "2.1.0 (Claude Code stub)"; exit 0; fi
echo "## Score: 50/100" > ../out.md
case " $* " in
  *" --output-format json "*) echo '{"type":"result","is_error":false,"total_cost_usd":0.01234,"result":"done"}' ;;
  *) echo "done" ;;
esac
case "${STUB_MODE:-ok}" in
  sleep) sleep 60 & echo "$! $$" > "$STUB_PIDS"; wait ;;
esac
exit 0
"""


def run_py(script, *args, **kwargs):
    kwargs.setdefault("env", clean_env())
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


def clean_env(extra=None):
    """os.environ without the GIT_* variables a git hook exports, so a test run inside a
    hook never writes fixtures into the caller's index."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    env.update(extra or {})
    return env


def git(cwd, *args):
    env = clean_env(GIT_ENV)
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
        self.assertRegex(proc.stdout, r"^snapshot: 3 files \(\d+ KB\) ->")
        self.assertIn("skipped symlink: link.txt", proc.stderr)
        self.assertIn("skipped criteria doc: docs/criteria/x-criteria.md", proc.stderr)
        self.assertEqual(files_in(self.out), ["node_modules/m.js", "notes-rounds.md", "src/a.txt"])
        for absent in (".git/HEAD", ".iterative-improvement/x/rounds.md", "docs/criteria/x-criteria.md", "link.txt"):
            self.assertFalse(os.path.exists(os.path.join(self.out, absent)), absent)
        manifest = json.loads(read(self.out + ".manifest.json"))
        self.assertEqual(sorted(manifest["files"]), ["node_modules/m.js", "notes-rounds.md", "src/a.txt"])
        self.assertEqual(len(manifest["files"]["src/a.txt"]["sha256"]), 64)

    def test_pair_stages_both_rounds_blind(self):
        first = self.out
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", self.root, "--path", "src", "--out", first).returncode, 0)
        write(os.path.join(self.root, "src", "a.txt"), "alpha, improved\n")
        second = os.path.join(self.tmp, "loop", "snapshots", "round-3")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", self.root, "--path", "src", "--out", second).returncode, 0)
        views = []
        for label in ("0", "3"):
            view = os.path.join(self.tmp, "loop", "evidence", "view-round-" + label)
            write(os.path.join(view, "capture-1.png"), "png of round %s\n" % label)
            write(os.path.join(view, "sub", "page.txt"), "text of round %s\n" % label)
            views.append(view)
        out = os.path.join(self.tmp, "loop", "snapshots", "pair")
        proc = run_py(SNAPSHOT, "pair", "--first", first, "--second", second, "--out", out,
                      "--first-evidence", views[0], "--second-evidence", views[1])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(os.listdir(out)), ["a", "b"])
        mapping = dict(line.split(": ", 1) for line in read(out + ".mapping").splitlines())
        self.assertEqual(sorted(mapping.values()), sorted([os.path.abspath(first), os.path.abspath(second)]))
        for label in ("a", "b"):
            round_label = "3" if mapping[label].endswith("round-3") else "0"
            expected = "alpha, improved\n" if round_label == "3" else "alpha\n"
            self.assertEqual(read(os.path.join(out, label, "src", "a.txt")), expected)
            self.assertEqual(read(os.path.join(out + "-evidence", label + "-capture-1.png")),
                             "png of round %s\n" % round_label)
            self.assertEqual(read(os.path.join(out + "-evidence", label + "-sub-page.txt")),
                             "text of round %s\n" % round_label)
        self.assertNotIn("round", " ".join(os.listdir(out + "-evidence")), "file names must not say which round")
        manifest = json.loads(read(out + ".manifest.json"))
        self.assertEqual(manifest["root"], "", "no ROOT: spawn_child.sh then skips the ROOT fingerprint")
        self.assertEqual(sorted(manifest["files"]), ["a/src/a.txt", "b/src/a.txt"])
        self.assertEqual(run_py(SNAPSHOT, "verify", "--snapshot", out).stdout.strip(), "unchanged")
        # Order is random: over 30 pairings both orders appear.
        orders = set()
        for index in range(30):
            target = os.path.join(self.tmp, "pairs", str(index))
            self.assertEqual(run_py(SNAPSHOT, "pair", "--first", first, "--second", second, "--out", target).returncode, 0)
            orders.add(read(target + ".mapping").splitlines()[0].endswith("round-0"))
        self.assertEqual(orders, {True, False})
        # Refusals: a non-empty out, a raw evidence dir with its manifest, a changed snapshot.
        self.assertEqual(run_py(SNAPSHOT, "pair", "--first", first, "--second", second, "--out", out).returncode, 2)
        write(os.path.join(views[0], "manifest.md"), "| C1 | cmd |\n")
        proc = run_py(SNAPSHOT, "pair", "--first", first, "--second", second, "--out", out + "2",
                      "--first-evidence", views[0], "--second-evidence", views[1])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("pass the evidence.py view", proc.stderr)
        write(os.path.join(second, "src", "a.txt"), "tampered\n")
        proc = run_py(SNAPSHOT, "pair", "--first", first, "--second", second, "--out", out + "3")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("no longer matches its manifest", proc.stderr)

    def test_secret_shaped_files_excluded_outside_git(self):
        secrets = (".env", ".env.local", "config/.env.production", "certs/server.pem", ".ssh/id_rsa", "id_ed25519.pub",
                   ".npmrc", ".pypirc", ".netrc", "credentials.json", "packages/api/secrets.yaml", "terraform.tfvars",
                   "server.key", "keystore.p12", "cert.pfx", ".aws/credentials", "packages/api/service-account.json",
                   "config/.ENV", "key.PEM", "deploy/id_ecdsa_ci", "client_secret_123.json")
        for rel in secrets:
            write(os.path.join(self.root, rel), "secret\n")
        # Assembled so the repository's secret scanner does not flag this test file.
        write(os.path.join(self.root, "notes", "key.txt"), "-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n")
        kept = ("src/environment.ts", ".env.example", "src/id_utils.py", "docs/id_card.md", "src/id_generator.py",
                "src/secretary.py", "docs/credentials-howto.md")
        for rel in kept:
            write(os.path.join(self.root, rel), "ok\n")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files_in(self.out), sorted(["node_modules/m.js", "notes-rounds.md", "src/a.txt"] + list(kept)))
        for rel in secrets + ("notes/key.txt",):
            self.assertIn("skipped secret-shaped", proc.stderr)
            self.assertRegex(proc.stderr, r"(?m)^skipped secret-shaped \([^)]*\): %s$" % re.escape(rel), rel)
        self.assertIn("contains a private key block", proc.stderr)
        out2 = self.out + "-inc"
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", out2, "--include", ".npmrc",
                      "--include", "docs/criteria/*")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(".npmrc", files_in(out2))
        self.assertIn("docs/criteria/x-criteria.md", files_in(out2))
        self.assertNotIn(".pypirc", files_in(out2))

    def test_create_extra_exclude(self):
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out, "--exclude", "node_modules")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files_in(self.out), ["notes-rounds.md", "src/a.txt"])

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

    def test_create_refuses_an_empty_snapshot(self):
        only_secrets = os.path.join(self.tmp, "only-secrets")
        write(os.path.join(only_secrets, ".env"), "KEY=x\n")
        proc = run_py(SNAPSHOT, "create", "--root", only_secrets, "--out", self.out + "-e")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("no files to snapshot", proc.stderr)
        self.assertFalse(os.path.exists(self.out + "-e.manifest.json"), "no manifest for an empty snapshot")

    @unittest.skipIf(shutil.which("git") is None, "git not on PATH")
    def test_create_refuses_a_root_under_a_gitignored_path(self):
        repo = os.path.join(self.tmp, "repo")
        write(os.path.join(repo, ".gitignore"), "scratch/\n")
        write(os.path.join(repo, "scratch", "root", "hello.txt"), "marigold\n")
        git(repo, "init", "-q")
        proc = run_py(SNAPSHOT, "create", "--root", os.path.join(repo, "scratch", "root"), "--out", self.out + "-g")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("gitignored", proc.stderr)

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
        self.assertIn("sealed: 3 files", proc.stdout)
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
        self.assertNotIn("node_modules/m.js", proc.stdout, "unchanged files are not rewritten")
        self.assertIn("dry run: 1 to copy, 1 to remove", proc.stdout)
        self.assertIn(os.path.join(self.tmp, "loop", "restore-backup"), proc.stdout)
        self.assertEqual(read(os.path.join(self.root, "src", "a.txt")), "edited\n")
        before = os.stat(os.path.join(self.root, "node_modules", "m.js")).st_mtime_ns
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("restored: 1 copied, 1 removed", proc.stdout)
        match = re.search(r"backup: 2 files -> (.+)$", proc.stdout, re.M)
        self.assertIsNotNone(match, proc.stdout)
        backup = match.group(1)
        self.assertTrue(backup.startswith(os.path.join(self.tmp, "loop", "restore-backup")), backup)
        self.assertEqual(read(os.path.join(backup, "src", "a.txt")), "edited\n")
        self.assertEqual(read(os.path.join(backup, "src", "new.txt")), "new\n")
        self.assertEqual(read(os.path.join(self.root, "src", "a.txt")), "alpha\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "new.txt")))
        self.assertEqual(os.stat(os.path.join(self.root, "node_modules", "m.js")).st_mtime_ns, before)
        self.assertTrue(os.path.exists(os.path.join(self.root, "docs", "criteria", "x-criteria.md")),
                        "excluded files are left alone")

    def test_restore_gives_restored_files_a_fresh_mtime(self):
        # A build tool compares mtimes: a restored source must look newer than the
        # output built from the edit, or the revert never reaches the build.
        run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        old = 1500000000
        os.utime(os.path.join(self.out, "src", "a.txt"), (old, old))
        write(os.path.join(self.root, "src", "a.txt"), "edited\n")
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(os.stat(os.path.join(self.root, "src", "a.txt")).st_mtime, old + 1000)

    def test_restore_only_and_expect_protect_other_peoples_edits(self):
        run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        write(os.path.join(self.root, "src", "a.txt"), "loop edit\n")
        later = os.path.join(self.tmp, "loop", "snapshots", "round-1")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", self.root, "--out", later).returncode, 0)
        write(os.path.join(self.root, "node_modules", "m.js"), "teammate edit\n")
        write(os.path.join(self.root, "src", "teammate.txt"), "teammate file\n")
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", self.root, "--expect", later, "--dry-run")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("not the loop's change: node_modules/m.js", proc.stdout)
        self.assertIn("not the loop's change: src/teammate.txt", proc.stdout)
        self.assertNotIn("not the loop's change: src/a.txt", proc.stdout)
        self.assertIn("--only", proc.stderr)
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", self.root, "--expect", later,
                      "--only", "src/a.txt")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("restored: 1 copied, 0 removed", proc.stdout)
        self.assertEqual(read(os.path.join(self.root, "src", "a.txt")), "alpha\n")
        self.assertEqual(read(os.path.join(self.root, "node_modules", "m.js")), "teammate edit\n")
        self.assertTrue(os.path.exists(os.path.join(self.root, "src", "teammate.txt")))
        proc = run_py(SNAPSHOT, "restore", "--snapshot", self.out, "--root", self.root, "--only", "src", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines()[0], "would remove: src/teammate.txt")
        sub = os.path.join(self.tmp, "loop", "snapshots", "round-sub")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", self.root, "--path", "src", "--out", sub).returncode, 0)
        proc = run_py(SNAPSHOT, "restore", "--snapshot", sub, "--root", self.root, "--only", "node_modules")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("outside the snapshot's paths", proc.stderr)

    def test_create_dry_run_and_whole_root_limit(self):
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out, "--path", "src", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines()[0], "would copy: src/a.txt")
        self.assertIn("dry run: 1 files", proc.stdout)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".manifest.json"))
        for index in range(501):
            write(os.path.join(self.root, "many", "f%03d.txt" % index), "x\n")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("refusing a whole-root snapshot of 504 files", proc.stderr)
        self.assertIn("--path", proc.stderr)
        self.assertFalse(os.path.exists(self.out))
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out, "--path", "src")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--out", self.out + "-w", "--whole-root")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("snapshot: 504 files", proc.stdout)

    def test_fingerprint_is_scoped_and_sees_untracked_content(self):
        sub = os.path.join(self.tmp, "loop", "snapshots", "round-fp")
        write(os.path.join(self.root, "src", "draft.txt"), "draft\n")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", self.root, "--path", "src", "--out", sub).returncode, 0)
        saved = os.path.join(self.tmp, "fp.json")
        proc = run_py(SNAPSHOT, "fingerprint", "--snapshot", sub, "--save", saved)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"^fingerprint: 2 files, [0-9a-f]{12}$")
        write(os.path.join(self.root, "node_modules", "m.js"), "outside the scope\n")
        proc = run_py(SNAPSHOT, "fingerprint", "--snapshot", sub, "--compare", saved)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "unchanged"), "an edit outside the scope never counts")
        write(os.path.join(self.root, "src", "draft.txt"), "draft, edited\n")
        write(os.path.join(self.root, "src", "added.txt"), "new\n")
        proc = run_py(SNAPSHOT, "fingerprint", "--snapshot", sub, "--compare", saved)
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertEqual(proc.stdout.splitlines(), ["added: src/added.txt", "changed: src/draft.txt"])
        whole = os.path.join(self.tmp, "fp-whole.json")
        run_py(SNAPSHOT, "fingerprint", "--snapshot", sub, "--save", whole, "--whole-root")
        write(os.path.join(self.root, "node_modules", "m.js"), "outside again\n")
        proc = run_py(SNAPSHOT, "fingerprint", "--snapshot", sub, "--compare", whole, "--whole-root")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("changed: node_modules/m.js", proc.stdout)

    @unittest.skipIf(shutil.which("git") is None, "git not on PATH")
    def test_fingerprint_in_git_covers_untracked_files_without_index_lock(self):
        shutil.rmtree(os.path.join(self.root, ".git"))
        git(self.root, "init", "-q")
        git(self.root, "add", "src")
        git(self.root, "commit", "-q", "-m", "init")
        write(os.path.join(self.root, "src", "untracked.txt"), "one\n")
        sub = os.path.join(self.tmp, "loop", "snapshots", "round-g")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", self.root, "--path", "src", "--out", sub).returncode, 0)
        saved = os.path.join(self.tmp, "fp.json")
        index = os.path.join(self.root, ".git", "index")
        mtime = os.stat(index).st_mtime_ns
        run_py(SNAPSHOT, "fingerprint", "--snapshot", sub, "--save", saved)
        write(os.path.join(self.root, "src", "untracked.txt"), "two\n")
        proc = run_py(SNAPSHOT, "fingerprint", "--snapshot", sub, "--compare", saved)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("changed: src/untracked.txt", proc.stdout)
        self.assertEqual(os.stat(index).st_mtime_ns, mtime, "the fingerprint must not rewrite the index")

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
        self.assertIn("notes-rounds.md", files)
        self.assertNotIn("docs/criteria/x-criteria.md", files)
        self.assertRegex(proc.stderr, r"skipped 1 gitignored entry in scope: node_modules")

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
        self.assertEqual(files, [".gitignore", "notes-rounds.md", "src/a.txt", "src/b.txt"], files)


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

    def test_unterminated_loop_only_block_fails(self):
        text = ("# Criteria: x\n\n### C1. One (50 pts)\nkeep\n<!-- loop-only -->\n**Target:** 90\n\n"
                "### C2. Two (50 pts)\n\n## Gates\n- G1\n")
        proc, out = self.strip(text)
        self.assertEqual(proc.returncode, 4, proc.stdout)
        self.assertIn("unterminated <!-- loop-only --> starting at line 5", proc.stderr)
        self.assertIsNone(out)

    def test_fenced_heading_does_not_end_a_removed_section(self):
        text = ("# Criteria: x\n\n## Criteria (100 points)\n\nkeep me\n\n## Amendments\n\n- v2: changed C1\n"
                "```\n## pipeline step\n```\nThe reviewer was Codex and it cost 40 minutes.\n\n## Constraints\n\nkept\n")
        proc, out = self.strip(text)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Codex", out)
        self.assertNotIn("pipeline step", out)
        self.assertIn("## Constraints", out)
        self.assertIn("keep me", out)
        fenced = "# Criteria: x\n\n## Context\n\n```\n**Target:** shown as an example\n```\n"
        proc, out = self.strip(fenced)
        self.assertEqual(proc.returncode, 4, "a fenced Target label is still residue: %s" % out)

    def test_label_variants_are_removed(self):
        header = ("# Criteria: x\n* **Target:** 91\n+ **Budget:** 2 rounds\n**Target score:** 92\nTarget: 93\n"
                  "> **Target:** 94\n1. **Mode:** full\n- **Baseline SHA:** a1b2c3d\n\n## Criteria (100 points)\n\n"
                  "### C1. Weight (100 pts)\nBudget: under 50 KB of JavaScript\n")
        proc, out = self.strip(header)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for leaked in ("91", "2 rounds", "92", "93", "94", "full"):
            self.assertNotIn(leaked, out, leaked)
        self.assertIn("Budget: under 50 KB of JavaScript", out, "a plain label inside a criterion is content")
        proc, _ = self.strip("# Criteria: x\n\n## Context\n\nAim high. **Target:** 90\n")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("Target or Budget label", proc.stderr)

    def test_allow_and_error_names_the_source_line(self):
        text = "# Criteria: x\n<!-- loop-only -->\n**Mode:** lite\n<!-- /loop-only -->\n\n## Context\n\nLearning curve has no plateau.\n"
        proc, out = self.strip(text)
        self.assertEqual(proc.returncode, 4)
        self.assertIn('found "plateau" at line 8 of', proc.stderr)
        self.assertIn("Learning curve has no plateau.", proc.stderr)
        self.assertIn('--allow-term "plateau"', proc.stderr)
        self.assertIn("never hand-edit", proc.stderr)
        source = os.path.join(self.tmp, "in.md")
        proc = run_py(STRIP, source, source + ".out", "--allow-term", "plateau")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("allowed: plateau", proc.stdout)
        proc = run_py(STRIP, source, source + ".out", "--allow-term", "[PRESERVE]")
        self.assertEqual(proc.returncode, 2)
        write(source, "# x\n\nRound 2 of the tutorial compiles; receiver noise floor stays low.\n")
        self.assertEqual(run_py(STRIP, source, source + ".out", "--allow-term", "round <N>", "--allow-term", "noise floor").returncode, 0)

    def test_invalid_utf8_is_a_clean_error(self):
        source = os.path.join(self.tmp, "bad.md")
        with open(source, "wb") as handle:
            handle.write(b"\xe9\n")
        proc = run_py(STRIP, source, source + ".out")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not UTF-8", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_input(self):
        proc = run_py(STRIP, os.path.join(self.tmp, "missing.md"), os.path.join(self.tmp, "out.md"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("missing.md", proc.stderr)


class ScoreMathTests(unittest.TestCase):
    def baseline(self, *runs):
        proc = run_py(SCORE, "baseline", *runs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def baseline_lines(self, *runs):
        return self.baseline(*runs).splitlines()

    def budget(self, *args):
        return run_py(SCORE, "budget", *args)

    def decide(self, scores, *extra, **kwargs):
        max_rounds = str(kwargs.get("max_rounds", 5))
        extra = list(extra)
        if "--gates" not in extra:
            extra += ["--gates", "PASS"]
        args = ["decide", "--threshold", "8", "--target", "85", "--max-rounds", max_rounds, "--scores", scores] + extra
        return run_py(SCORE, *args)

    def first(self, proc):
        return proc.stdout.splitlines()[0] if proc.stdout else proc.stderr

    def test_baseline(self):
        self.assertEqual(self.baseline("41", "45"), "baseline=43 noise=4 threshold=8 soft=false third-run=no")
        self.assertEqual(self.baseline("56", "60"), "baseline=58 noise=4 threshold=8 soft=false third-run=no")
        lines = self.baseline_lines("40", "50")
        self.assertEqual(lines[0], "baseline=45 noise=10 threshold=20 soft=false third-run=needed",
                         "two runs never claim soft: the third run decides")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[1].startswith("detail: two runs 10 apart"), lines[1])
        self.assertIn("run a third on the same snapshot and evidence", lines[1])
        lines = self.baseline_lines("50", "50")
        self.assertEqual(lines[0], "baseline=50 noise=0 threshold=3 soft=false third-run=needed")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[1].startswith("detail: two identical runs"), lines[1])
        self.assertIn("pass all three runs", lines[1])
        self.assertEqual(self.baseline("41", "44"), "baseline=43 noise=3 threshold=6 soft=false third-run=no",
                         "42.5 rounds half up")
        self.assertEqual(self.baseline("41", "49"), "baseline=45 noise=8 threshold=16 soft=false third-run=no",
                         "noise of exactly 8 is neither soft nor a third-run case")
        proc = run_py(SCORE, "baseline", "41", "45", "--json")
        self.assertEqual(json.loads(proc.stdout), {
            "baseline": 43, "noise": 4, "threshold": 8, "soft": False,
            "third_run": "no", "outlier": None, "runs": [41, 45], "detail": None})
        data = json.loads(run_py(SCORE, "baseline", "50", "50", "--json").stdout)
        self.assertEqual(data["third_run"], "needed")
        self.assertIsNone(data["outlier"])
        self.assertIn("identical", data["detail"])
        self.assertEqual(run_py(SCORE, "baseline", "41", "145").returncode, 2)
        self.assertEqual(run_py(SCORE, "baseline", "41", "-1").returncode, 2)
        self.assertEqual(run_py(SCORE, "baseline", "41", "abc").returncode, 2)
        proc = run_py(SCORE, "baseline", "41")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("two or three runs", proc.stderr)

    def test_baseline_three_runs(self):
        self.assertEqual(self.baseline("41", "45", "43"),
                         "baseline=43 noise=4 threshold=8 soft=false third-run=done outlier=none")
        self.assertEqual(self.baseline("52", "52", "52"),
                         "baseline=52 noise=0 threshold=3 soft=false third-run=done outlier=none")
        lines = self.baseline_lines("40", "50", "44")
        self.assertEqual(lines[0], "baseline=45 noise=10 threshold=20 soft=true third-run=done outlier=none",
                         "50 is only 6 from 44: no run is an outlier, and the noise is the full spread")
        lines = self.baseline_lines("40", "50", "46")
        self.assertEqual(lines[0], "baseline=45 noise=10 threshold=20 soft=true third-run=done outlier=none")
        lines = self.baseline_lines("30", "52", "50")
        self.assertEqual(lines[0], "baseline=51 noise=2 threshold=4 soft=false third-run=done outlier=30")
        self.assertIn("run 30 is an outlier, 20 from the nearest run, against 52 and 50", lines[1])
        lines = self.baseline_lines("40", "51", "52")
        self.assertEqual(lines[0], "baseline=52 noise=1 threshold=3 soft=false third-run=done outlier=40")
        lines = self.baseline_lines("10", "50", "50")
        self.assertEqual(lines[0], "baseline=50 noise=0 threshold=3 soft=true third-run=done outlier=10",
                         "a run 40 away is an outlier, and the instrument that produced it is soft")
        self.assertIn("more than 20 away", lines[1])
        lines = self.baseline_lines("40", "50", "45")
        self.assertEqual(lines[0], "baseline=45 noise=10 threshold=20 soft=true third-run=done outlier=none",
                         "equal gaps: no outlier, soft")
        self.assertTrue(lines[1].startswith("detail: no single outlier: the three runs spread over 10"), lines[1])
        lines = self.baseline_lines("40", "60", "50")
        self.assertEqual(lines[0], "baseline=50 noise=20 threshold=40 soft=true third-run=done outlier=none")
        lines = self.baseline_lines("10", "30", "60")
        self.assertEqual(lines[0], "baseline=33 noise=50 threshold=100 soft=true third-run=done outlier=none",
                         "unequal gaps both wider than 8: soft, no outlier")
        self.assertEqual(self.baseline("41", "49", "45"),
                         "baseline=45 noise=8 threshold=16 soft=false third-run=done outlier=none",
                         "a range of exactly 8 is still a clean triple")
        proc = run_py(SCORE, "baseline", "40", "50", "44", "47")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("two or three runs", proc.stderr)
        self.assertEqual(run_py(SCORE, "baseline", "40", "50", "144").returncode, 2)
        data = json.loads(run_py(SCORE, "baseline", "30", "52", "50", "--json").stdout)
        self.assertEqual(data["outlier"], 30)
        self.assertEqual(data["third_run"], "done")
        self.assertEqual(data["runs"], [30, 52, 50])
        self.assertEqual(data["baseline"], 51)
        self.assertEqual(data["noise"], 2)
        self.assertFalse(data["soft"])
        self.assertIn("outlier", data["detail"])
        data = json.loads(run_py(SCORE, "baseline", "41", "45", "43", "--json").stdout)
        self.assertIsNone(data["outlier"])
        self.assertIsNone(data["detail"])
        data = json.loads(run_py(SCORE, "baseline", "40", "50", "45", "--json").stdout)
        self.assertTrue(data["soft"])
        self.assertIsNone(data["outlier"])
        self.assertIn("soft", data["detail"])
        self.assertIn("no single outlier: the three runs spread over 10 (gaps 5 and 5)", data["detail"])
        self.assertNotIn("no two runs agree", data["detail"], "adjacent runs 5 apart do agree within 8")
        data = json.loads(run_py(SCORE, "baseline", "40", "50", "--json").stdout)
        self.assertFalse(data["soft"])
        self.assertEqual(data["third_run"], "needed")

    def test_budget(self):
        proc = self.budget("--total", "90", "--spent", "35", "--per-round", "12")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[0], "remaining=55 rounds-used=0 rounds-affordable=4 max-rounds=4")
        self.assertTrue(lines[1].startswith("detail: a round costs 12; 4 more rounds fit in the remaining 55"), lines[1])
        self.assertIn("pass --max-rounds 4", lines[1])
        proc = self.budget("--total", "90", "--spent", "35", "--per-round", "12", "--rounds", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.first(proc), "remaining=55 rounds-used=0 rounds-affordable=4 max-rounds=2")
        self.assertIn("pass --max-rounds 2", proc.stdout)
        proc = self.budget("--total", "90", "--spent", "35", "--per-round", "12", "--rounds", "9")
        self.assertEqual(self.first(proc), "remaining=55 rounds-used=0 rounds-affordable=4 max-rounds=4", "the ceiling never raises max-rounds")
        proc = self.budget("--total", "15", "--spent", "14.2", "--per-round", "2.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.first(proc), "remaining=0.8 rounds-used=0 rounds-affordable=0 max-rounds=0")
        self.assertIn("stop now", proc.stdout)
        proc = self.budget("--total", "15", "--spent", "20", "--per-round", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.first(proc), "remaining=-5 rounds-used=0 rounds-affordable=0 max-rounds=0")
        self.assertIn("stop now and report", proc.stdout)
        proc = self.budget("--total", "3", "--spent", "0", "--per-round", "0.1")
        self.assertEqual(self.first(proc), "remaining=3 rounds-used=0 rounds-affordable=30 max-rounds=30", "decimal, not float, division")
        proc = self.budget("--total", "24", "--spent", "12", "--per-round", "12")
        self.assertEqual(self.first(proc), "remaining=12 rounds-used=0 rounds-affordable=1 max-rounds=1")
        self.assertIn("1 more round fits", proc.stdout)
        for bad, flag in ((("--total", "15", "--spent", "0", "--per-round", "0"), "--per-round"),
                          (("--total", "0", "--spent", "0", "--per-round", "2"), "--total"),
                          (("--total", "15", "--spent", "-1", "--per-round", "2"), "--spent"),
                          (("--total", "x", "--spent", "0", "--per-round", "2"), "--total"),
                          (("--total", "15", "--spent", "0", "--per-round", "2", "--rounds", "0"), "--rounds"),
                          (("--total", "15", "--spent", "0", "--per-round", "2", "--rounds-used", "-1"), "--rounds-used")):
            proc = self.budget(*bad)
            self.assertEqual(proc.returncode, 2, bad)
            self.assertIn(flag, proc.stderr, bad)
        proc = self.budget("--total", "90", "--spent", "35", "--per-round", "12", "--rounds", "2", "--json")
        data = json.loads(proc.stdout)
        self.assertEqual(sorted(data), ["detail", "max_rounds", "per_round", "remaining", "rounds_affordable", "rounds_used"])
        self.assertEqual(data["remaining"], 55)
        self.assertEqual(data["rounds_affordable"], 4)
        self.assertEqual(data["max_rounds"], 2)
        self.assertEqual(data["per_round"], 12)
        self.assertIn("--max-rounds 2", data["detail"])
        data = json.loads(self.budget("--total", "15", "--spent", "14.2", "--per-round", "2.1", "--json").stdout)
        self.assertAlmostEqual(data["remaining"], 0.8)
        self.assertEqual(data["max_rounds"], 0)

    def test_budget_counts_rounds_already_run(self):
        # Regression: max-rounds used to count only the rounds still affordable, so a
        # budget checked after round 3 told decide the ceiling was 1 and stopped early.
        proc = self.budget("--total", "90", "--spent", "66", "--per-round", "20", "--rounds", "5", "--rounds-used", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.first(proc), "remaining=24 rounds-used=3 rounds-affordable=1 max-rounds=4")
        self.assertIn("pass --max-rounds 4", proc.stdout)
        max_rounds = json.loads(self.budget("--total", "90", "--spent", "66", "--per-round", "20", "--rounds", "5",
                                            "--rounds-used", "3", "--json").stdout)["max_rounds"]
        proc = self.decide("43,60,70", "--rounds-used", "3", max_rounds=max_rounds)
        self.assertEqual(self.first(proc), "CONTINUE continue", proc.stdout)
        self.assertIn("3 of 4 rounds used", proc.stdout)
        proc = self.decide("43,60,70", "--rounds-used", "3", "--gates", "FAIL", max_rounds=max_rounds)
        self.assertEqual(self.first(proc), "CONTINUE continue", "a failing gate with a round left is not HALT")
        # The ceiling still caps it, and a spent budget ends the loop at the rounds run.
        proc = self.budget("--total", "200", "--spent", "60", "--per-round", "20", "--rounds", "5", "--rounds-used", "3")
        self.assertEqual(self.first(proc), "remaining=140 rounds-used=3 rounds-affordable=7 max-rounds=5")
        proc = self.budget("--total", "90", "--spent", "80", "--per-round", "20", "--rounds-used", "3")
        self.assertEqual(self.first(proc), "remaining=10 rounds-used=3 rounds-affordable=0 max-rounds=3")
        proc = self.decide("43,60,70", "--rounds-used", "3", max_rounds=3)
        self.assertEqual(self.first(proc), "STOP rounds-exhausted")
        proc = self.budget("--total", "200", "--spent", "60", "--per-round", "20", "--rounds", "3", "--rounds-used", "3")
        self.assertIn("round ceiling 3 is reached", proc.stdout)

    def test_confirm_score_and_target_are_checked(self):
        proc = self.decide("58,71,80,86", "--confirm-score", "500")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        proc = self.decide("58,71,80,69/80", "--confirm-score", "70/80", "--gates", "PASS")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.decide("58,71,80,86", "--confirm-score", "69/80")
        self.assertEqual(proc.returncode, 2, "a confirmation with a different adjusted max is a different instrument")
        self.assertIn("different adjusted max", proc.stderr)
        for target in ("101", "-1", "500"):
            proc = run_py(SCORE, "decide", "--threshold", "3", "--target", target, "--max-rounds", "5", "--scores", "50,60",
                          "--gates", "PASS")
            self.assertEqual(proc.returncode, 2, target)
            self.assertIn("--target", proc.stderr)
        proc = run_py(SCORE, "decide", "--threshold", "3", "--target", "100", "--max-rounds", "5", "--scores", "50,60",
                      "--gates", "PASS")
        self.assertEqual(proc.returncode, 0, proc.stderr)

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

    def test_check_criteria(self):
        proc = run_py(SCORE, "check-criteria", os.path.join(FIXTURES, "criteria-full.md"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "ok: 9 criteria summing to 100 (full mode), 3 gates")
        proc = run_py(SCORE, "check-criteria", os.path.join(FIXTURES, "criteria-full.md"), "--mode", "lite")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("9 criteria; lite mode takes 3 to 5", proc.stdout)
        tmp = tempfile.mkdtemp(prefix="ii-crit-")
        self.addCleanup(shutil.rmtree, tmp, True)
        doc = os.path.join(tmp, "x-criteria.md")
        write(doc, "# Criteria: x\n**Status:** DERIVED, not user-approved\n\n## Gates\n\n- **G1.** build passes\n\n"
                   "## Criteria (100 points)\n\n### C1. One (50 pts)\n**How to check it:** read it\n**Anchors:**\n"
                   "- `0`: no\n- `half`: some\n- `full`: yes\n\n### C2. Two (40 pts)\n**Source:** README.md\n"
                   "**Anchors:**\n- `0`: no\n- `20`: some\n- `30`: yes\n")
        proc = run_py(SCORE, "check-criteria", doc, "--mode", "lite")
        self.assertEqual(proc.returncode, 3)
        for problem in ("weights sum to 90, not 100", "2 criteria; lite mode takes 3 to 5",
                        "C1 anchors must be numbers", "C1 has no **Source:** line", "C2 has no **How to check it:** line",
                        "C2 last anchor is 30, not its weight 40", "G1 has no check: command"):
            self.assertIn(problem, proc.stdout, problem)
        self.assertNotIn("C2 has no **Source:**", proc.stdout)

    def test_check_criteria_passes_the_shipped_examples(self):
        tmp = tempfile.mkdtemp(prefix="ii-crit-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # The worked full-mode doc an agent copies must lint clean as written.
        example = read(os.path.join(REFERENCES, "example-loop.md"))
        block = re.search(r"```markdown\n(# Criteria: tidylog.*?)```", example, re.S).group(1)
        doc = os.path.join(tmp, "example-criteria.md")
        write(doc, block)
        proc = run_py(SCORE, "check-criteria", doc, "--mode", "full")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "ok: 8 criteria summing to 100 (full mode), 3 gates")
        # The DERIVED criterion in troubleshooting.md carries its [ASSUMED] tag on the heading.
        trouble = read(os.path.join(REFERENCES, "troubleshooting.md"))
        derived = re.search(r"```markdown\n(### C4\..*?)```", trouble, re.S).group(1)
        self.assertIn("(12 pts) [ASSUMED:", derived)
        doc = os.path.join(tmp, "derived-criteria.md")
        write(doc, "# Criteria: site\n**Status:** DERIVED, not user-approved. Sources: README.md\n\n"
                   "## Criteria (100 points)\n\n"
                   + derived + "**Anchors:**\n- `0`: three or more clicks\n- `6`: two clicks\n- `12`: one click\n\n"
                   "### C5. Hero (44 pts) [ASSUMED: weight] [ASSUMED: threshold]\n**Source:** README.md\n"
                   "**How to check it:** read it\n**Anchors:** `0`: no. `22`: some. `44`: yes.\n\n"
                   "### C6. Copy (44 pts)\n**Source:** docs/\n**How to check it:** count words\n"
                   "**Anchors:**\n- `0`: no\n- `22`: some\n- `44`: yes\n")
        proc = run_py(SCORE, "check-criteria", doc, "--mode", "lite")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "ok: 3 criteria summing to 100 (lite mode), 0 gates")
        # A tag is not a loophole: text other than [..] after "(w pts)" is still not a heading.
        write(doc, read(doc).replace("### C6. Copy (44 pts)", "### C6. Copy (44 pts) extra"))
        proc = run_py(SCORE, "check-criteria", doc, "--mode", "lite")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("weights sum to 56, not 100", proc.stdout)

    def test_example_lite_is_consistent(self):
        """The lite exemplar's criteria doc lints and strips clean, and every decide line it
        quotes prints what it says."""
        tmp = tempfile.mkdtemp(prefix="ii-lite-")
        self.addCleanup(shutil.rmtree, tmp, True)
        example = read(os.path.join(REFERENCES, "example-lite.md"))
        block = re.search(r"```markdown\n(# Criteria: tidylog README.*?)```", example, re.S).group(1)
        doc = os.path.join(tmp, "readme-criteria.md")
        write(doc, block)
        proc = run_py(SCORE, "check-criteria", doc, "--mode", "lite")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "ok: 3 criteria summing to 100 (lite mode), 2 gates")
        self.assertIn(proc.stdout.strip(), example)
        proc = run_py(STRIP, doc, os.path.join(tmp, "stripped.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Target", read(os.path.join(tmp, "stripped.md")))
        lines = re.findall(r"^S/score_math\.py (decide .*)$", example, re.M)
        self.assertEqual(len(lines), 2)
        first = run_py(SCORE, *lines[0].split())
        self.assertEqual(first.stdout.splitlines()[0], "CONTINUE continue")
        self.assertIn("+35; 1 of 2 rounds used", first.stdout)
        second = run_py(SCORE, *lines[1].split())
        self.assertEqual(second.stdout.splitlines()[0], "CONFIRM target-reached")
        confirmed = run_py(SCORE, *(lines[1].split() + ["--confirm-score", "89"]))
        self.assertEqual(confirmed.stdout.splitlines()[0], "STOP target-hit")
        # Per-criterion points in the prose add up to each quoted total.
        for parts, total in (((0, 15, 26), 41), ((35, 15, 26), 76), ((35, 30, 26), 91)):
            self.assertEqual(sum(parts), total)
            self.assertIn("C1 %d, C2 %d, C3 %d" % parts, example)

    def test_check_scorer(self):
        fixture = os.path.join(FIXTURES, "scorer-output.md")
        proc = run_py(SCORE, "check-scorer", fixture)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.splitlines()[0], "token=49/80 scaled=61 na=C4,C7 gates=G1:PASS,G2:FAIL,G3:PASS")
        self.assertIn("a gate is not PASS", proc.stdout)
        proc = run_py(SCORE, "check-scorer", fixture, "--criteria", os.path.join(FIXTURES, "criteria-full.md"))
        self.assertEqual(proc.returncode, 3, "the fixture is a v1 output; the doc is v2")
        self.assertIn("C6 max is 14, but its weight in the criteria doc is 10", proc.stdout)
        self.assertIn("criteria missing from the output: C9", proc.stdout)
        tmp = tempfile.mkdtemp(prefix="ii-out-")
        self.addCleanup(shutil.rmtree, tmp, True)
        bad = os.path.join(tmp, "out.md")
        text = read(fixture).replace("## Score: 49/80", "## Score: 52/80").replace("- C7: needs", "- C9: needs")
        write(bad, text)
        proc = run_py(SCORE, "check-scorer", bad)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("per-criterion points sum to 49, but the Score line says 52", proc.stdout)
        self.assertIn("Scaled is 61, but round(52 * 100 / 80) is 65", proc.stdout)
        self.assertIn("C7 is N/A but not listed under ## Unscoreable", proc.stdout)
        full = os.path.join(tmp, "full.md")
        write(full, "## Score: 58/100\n## Scaled: 58\n\n## Gates\n| Gate | Result | Evidence |\n|---|---|---|\n"
                    "| G1 | PASS | log |\n\n## Per-criterion\n| # | Criterion | Points | Max | Evidence |\n|---|---|---|---|---|\n"
                    "| C1 | a | 30 | 60 | x |\n| C2 | b | 28 | 40 | y |\n\n## Unscoreable\nnone\n\n## Notes\nnone\n")
        proc = run_py(SCORE, "check-scorer", full)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "token=58 scaled=58 na=none gates=G1:PASS")

    def test_check_scorer_tolerates_formatting_habits(self):
        """Ordinary model formatting must not cost a billed rerun: emphasis, units, a
        "/max" in the points cell and other heading levels read the same as the brief's
        format, while the arithmetic stays strict."""
        tmp = tempfile.mkdtemp(prefix="ii-out-")
        self.addCleanup(shutil.rmtree, tmp, True)
        base = ("## Score: 70/100\n## Scaled: 70\n\n## Gates\n| Gate | Result | Evidence |\n|---|---|---|\n"
                "| G1 | PASS | log |\n\n## Per-criterion\n| # | Criterion | Points | Max | Evidence |\n|---|---|---|---|---|\n"
                "| C1 | a | 30 | 50 | x |\n| C2 | b | 40 | 50 | y |\n\n## Unscoreable\nnone\n\n## Notes\nnone\n")
        variants = {
            "bold cells": base.replace("| C1 | a | 30 | 50 |", "| **C1** | a | **30** | **50** |"),
            "bold score": base.replace("## Score: 70/100", "## Score: **70/100**"),
            "points over max": base.replace("| 30 | 50 |", "| 30/50 | 50 |"),
            "pts in points": base.replace("| 30 | 50 |", "| 30 pts | 50 |"),
            "pts in max": base.replace("| 30 | 50 |", "| 30 | 50 pts |"),
            "scaled over 100": base.replace("## Scaled: 70", "## Scaled: 70/100"),
            "h1 score": base.replace("## Score:", "# Score:"),
            "spaced points": base.replace("## Score: 70/100", "## Score: 70 / 100 points"),
            "bold gate": base.replace("| G1 | PASS |", "| G1 | **PASS** |"),
            "ticks and h3 sections": base.replace("| C2 | b | 40 |", "| `C2` | b | `40` |")
                                         .replace("## Per-criterion", "### Per-criterion"),
            "alignment row": base.replace("|---|---|---|---|---|", "|:--|:--|--:|--:|---|"),
        }
        for name, text in variants.items():
            path = os.path.join(tmp, "out.md")
            write(path, text)
            proc = run_py(SCORE, "check-scorer", path)
            self.assertEqual(proc.returncode, 0, "%s: %s%s" % (name, proc.stdout, proc.stderr))
            self.assertEqual(proc.stdout.strip(), "token=70 scaled=70 na=none gates=G1:PASS", name)
        strict = {
            "points over another max": (base.replace("| 30 | 50 |", "| 30/60 | 50 |"), "C1 points are not a number"),
            "wrong sum": (base.replace("| 30 | 50 |", "| **35** | 50 |"),
                          "per-criterion points sum to 75, but the Score line says 70"),
            "wrong scaled": (base.replace("## Scaled: 70", "## Scaled: 71/100"), "Scaled is 71"),
        }
        for name, (text, problem) in strict.items():
            path = os.path.join(tmp, "out.md")
            write(path, text)
            proc = run_py(SCORE, "check-scorer", path)
            self.assertEqual(proc.returncode, 3, name)
            self.assertIn(problem, proc.stdout, name)

    def test_bad_input(self):
        self.assertEqual(self.decide("abc").returncode, 2)
        self.assertEqual(self.decide("").returncode, 2)
        self.assertEqual(self.decide("58,120").returncode, 2)
        self.assertEqual(self.decide("58,-5").returncode, 2)
        self.assertEqual(self.decide("58,81/80").returncode, 2)
        self.assertEqual(self.decide("58,71,80", "--rounds-used", "1").returncode, 2)
        self.assertEqual(self.decide("58,71", "--rounds-used", "2", "--first-round", "1").returncode, 2)
        proc = run_py(SCORE, "decide", "--threshold", "0", "--target", "85", "--max-rounds", "5", "--scores", "1,2",
                      "--gates", "PASS")
        self.assertEqual(proc.returncode, 2)
        for bad in (("--scores", "nan,50"), ("--scores", "50/nan,60/nan"), ("--scores", "50,60/inf"),
                    ("--threshold", "nan"), ("--threshold", "inf"), ("--target", "nan")):
            args = {"--threshold": "3", "--target": "85", "--scores": "50,60"}
            args[bad[0]] = bad[1]
            proc = run_py(SCORE, "decide", "--max-rounds", "5", "--gates", "PASS", *sum(args.items(), ()))
            self.assertEqual(proc.returncode, 2, bad)
            self.assertNotIn("Traceback", proc.stderr, bad)
        proc = run_py(SCORE, "decide", "--threshold", "3", "--target", "85", "--max-rounds", "2", "--scores", "40,41,42",
                      "--rounds-used", "2")
        self.assertEqual(proc.returncode, 2, "--gates is required")
        self.assertIn("--gates", proc.stderr)

    def test_budget_zero_rounds_feeds_decide(self):
        max_rounds = json.loads(self.budget("--total", "15", "--spent", "14.2", "--per-round", "2.1", "--json").stdout)["max_rounds"]
        self.assertEqual(max_rounds, 0)
        proc = self.decide("50", "--rounds-used", "0", max_rounds=max_rounds)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.first(proc), "STOP rounds-exhausted")


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
        env = {k: v for k, v in clean_env().items()
               if k not in ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_THREAD_ID", "ANTHROPIC_API_KEY", "STUB_MODE", "STUB_TOUCH",
                            "CDPATH")}
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

    def isolated_path(self):
        """PATH with the stubs, python3 and the system dirs only, so no real engine is found."""
        pybin = os.path.join(self.tmp, "pybin")
        if not os.path.isdir(pybin):
            os.makedirs(pybin)
            os.symlink(sys.executable, os.path.join(pybin, "python3"))
        return os.pathsep.join([self.bin, pybin, "/usr/bin", "/bin"])

    def spawn(self, *args, **kwargs):
        env = dict(self.env)
        env.update(kwargs.pop("env", {}))
        script = kwargs.pop("script", SPAWN)
        # stdin is a pipe held open on purpose: a child that reads stdin would hang
        # unless the script redirects it from /dev/null.
        read_end, write_end = os.pipe()
        try:
            proc = subprocess.Popen(["bash", script] + list(args), stdin=read_end, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True, env=env,
                                    cwd=kwargs.pop("cwd", None))
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

    def dry_run_record(self, *args):
        """The .cmd record as a dry run renders it (from the record header onwards)."""
        proc = self.spawn(*(list(args) + ["--dry-run"]))
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        out = proc.stdout_text
        return out[out.index("# spawn_child.sh record"):]

    def assert_footer(self, log_path, usage="not reported (the CLI printed no token count)"):
        lines = read(log_path).splitlines()
        self.assertGreaterEqual(len(lines), 4, lines)
        footer = lines[-4:]
        self.assertRegex(footer[0], r"^# started: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(footer[1], r"^# finished: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(footer[2], r"^# duration: \d+s$")
        self.assertEqual(footer[3], "# usage: " + usage)
        return footer


class SpawnDryRunTests(SpawnBase):
    def test_codex_dry_run_creates_nothing_and_leaks_no_loop_path(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5",
                          "--evidence", self.evidence, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        out = proc.stdout_text
        for needle in ("CODEX_HOME='/<stage>/engine-home'", "--ephemeral", "'-s'", "read-only", "--skip-git-repo-check",
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
                       "'--add-dir'", "'/<stage>'", "'Read,Grep,Glob,Write'", "Edit(//<stage>/out.md)",
                       "--no-session-persistence"):
            self.assertIn(needle, out, needle)
        for absent in ("--bare", SKIP_PERMS, "Bash", "Write(", "'--allow'"):
            self.assertNotIn(absent, out, absent)
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run",
                          env={"ANTHROPIC_API_KEY": "test-key-placeholder"})
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("--bare", proc.stdout_text)

    def test_claude_allow_rules_are_stage_scoped(self):
        # Every allow rule names a path under the stage; a bare "Read", "Grep" or "Glob"
        # rule would let the child read the user's home, the loop dir and the repo.
        record = self.dry_run_record("claude", "scorer", self.prompt, self.snapshot, self.out, "--model", "opus")
        argv = self.argv_lines(record)
        rules = re.findall(r"'((?:Read|Grep|Glob|Edit|Write|Bash)[^']*)'", argv.split("--allowedTools", 1)[1])
        self.assertEqual(rules, ["Read(//<stage>/**)", "Grep(//<stage>/**)", "Glob(//<stage>/**)", "Edit(//<stage>/out.md)"])
        self.assertIn("'--model'", argv)
        self.assertIn("'opus'", argv)
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--no-bash", "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("never get Bash", proc.stderr_text)

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
        self.assertEqual(proc.returncode, 2, "kimi has no sandbox: it needs an explicit opt-in")
        self.assertIn("--unsandboxed", proc.stderr_text)
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--unsandboxed", "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("kimi only", proc.stderr_text)
        proc = self.spawn("kimi", "reviewer", self.prompt, self.snapshot, self.out, "--unsandboxed", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("# unsandboxed: yes", proc.stdout_text)
        self.assertIn("--skills-dir", proc.stdout_text)
        self.assertIn("'/<stage>/engine-home/kimi-skills'", proc.stdout_text)
        self.assertIn("--add-dir", proc.stdout_text)
        self.assertNotIn("--" + "yolo", proc.stdout_text)
        self.assertNotIn("--auto", proc.stdout_text)
        proc = self.spawn("kimi", "scorer", self.prompt, self.snapshot, self.out, "--model", "k2", "--unsandboxed", "--dry-run")
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
        args = ("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", "--evidence", self.evidence)
        expected_record = self.dry_run_record(*args)
        proc = self.spawn(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assertTrue(proc.stdout_text.startswith("ok: " + os.path.realpath(self.out)), proc.stdout_text)
        self.assertRegex(proc.stdout_text, r"^ok: \S+ \(\d+ bytes, \d+s\); record \S+\.cmd\n$")
        self.assertEqual(read(self.out[:-3] + ".cmd"), expected_record, "the .cmd pin must not carry timing")
        self.assert_footer(self.out[:-3] + ".log")
        self.assertNotIn("tokens used", read(self.out[:-3] + ".log"))
        out = read(self.out)
        self.assertIn("## Score: 50/100", out)
        self.assertIn("stdin=0", out, "stdin must be redirected from /dev/null")
        self.assertRegex(out, r"cwd=.*/ii-stage\.[^/]+/artifact")
        self.assertRegex(out, r"home=.*/ii-stage\.[^/]+/engine-home")
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
        self.assertNotIn("# duration", record, "timing belongs to the .log, not the pin")
        self.assertEqual(run_py(SNAPSHOT, "verify", "--snapshot", self.snapshot).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.loop, "rounds.md")), "a clean run logs no incident")

    def test_usage_line_is_captured(self):
        # codex-cli 0.163.0 prints "tokens used" and the count on the next line (the
        # stub replays the tail of a real log); the footer must carry the number.
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, env={"STUB_MODE": "usage"})
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assert_footer(self.out[:-3] + ".log", usage="tokens used: 19,783")
        self.assertNotIn("tokens used", read(self.out[:-3] + ".cmd"))
        os.remove(self.out[:-3] + ".log")
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, env={"STUB_MODE": "usage-oneline"})
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assert_footer(self.out[:-3] + ".log", usage="tokens used: 1234")
        log = read(self.out[:-3] + ".log")
        self.assertEqual(log.count("tokens used: 1234"), 2, "the child's line and the footer's copy of it")

    def test_empty_snapshot_is_refused(self):
        empty = os.path.join(self.loop, "snapshots", "round-9")
        os.makedirs(empty)
        with open(empty + ".manifest.json", "w") as handle:
            json.dump({"root": self.root, "paths": [], "excludes": [], "files": {}}, handle)
        proc = self.spawn("codex", "reviewer", self.prompt, empty, self.out)
        self.assertEqual(proc.returncode, 3, proc.stderr_text)
        self.assertIn("lists no files", proc.stderr_text)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out[:-3] + ".cmd"), "nothing ran, so no record")

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
        self.assert_footer(self.out[:-3] + ".log")
        rounds = read(os.path.join(self.loop, "rounds.md"))
        self.assertIn("codex scorer: child modified the staged artifact; output discarded", rounds)
        self.assertEqual(self.stage_dirs(), [])
        self.assertEqual(run_py(SNAPSHOT, "verify", "--snapshot", self.snapshot).returncode, 0, "the real snapshot is untouched")

    def test_child_failure_and_no_output(self):
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", env={"STUB_MODE": "fail"})
        self.assertEqual(proc.returncode, 7, proc.stderr_text)
        self.assertIn("exited with status 1", proc.stderr_text)
        self.assert_footer(self.out[:-3] + ".log")
        os.remove(self.out[:-3] + ".log")
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", env={"STUB_MODE": "silent"})
        self.assertEqual(proc.returncode, 6, proc.stderr_text)
        self.assertIn("no output file", proc.stderr_text)
        self.assert_footer(self.out[:-3] + ".log")
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
        self.assertIn("changed: index.html", proc.stdout_text)
        self.assertIn("the artifact's files in", proc.stderr_text)
        self.assertIn("switching engine does not help", proc.stderr_text)
        self.assertTrue(os.path.exists(out2 + ".discarded"))
        self.assertIn("output discarded", read(os.path.join(self.loop, "rounds.md")))

    @unittest.skipIf(shutil.which("git") is None, "git not on PATH")
    def test_root_check_is_scoped_to_the_snapshot_paths(self):
        write(os.path.join(self.root, "apps", "web", "Home.tsx"), "export {}\n")
        write(os.path.join(self.root, "pkg", "README.md"), "# pkg\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        write(os.path.join(self.root, "pkg", "notes.txt"), "untracked, in scope\n")
        snapshot = os.path.join(self.loop, "snapshots", "round-3")
        proc = run_py(SNAPSHOT, "create", "--root", self.root, "--path", "pkg", "--out", snapshot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # A teammate edits a tracked file and adds an untracked one outside the scope: kept.
        for target in (os.path.join(self.root, "apps", "web", "Home.tsx"), os.path.join(self.root, "apps", "NEW.txt")):
            out = os.path.join(self.loop, "scores", "r-%d.md" % len(target))
            proc = self.spawn("codex", "reviewer", self.prompt, snapshot, out, env={"STUB_TOUCH": target})
            self.assertEqual(proc.returncode, 0, proc.stderr_text)
            self.assertTrue(os.path.isfile(out))
        # The content of an untracked file inside the scope changes: discarded.
        out = os.path.join(self.loop, "scores", "r-untracked.md")
        proc = self.spawn("codex", "reviewer", self.prompt, snapshot, out,
                          env={"STUB_TOUCH": os.path.join(self.root, "pkg", "notes.txt")})
        self.assertEqual(proc.returncode, 4, proc.stderr_text)
        self.assertIn("changed: pkg/notes.txt", proc.stdout_text)

    def test_root_check_works_outside_git(self):
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out,
                          env={"STUB_TOUCH": os.path.join(self.root, "index.html")})
        self.assertEqual(proc.returncode, 4, proc.stderr_text)
        self.assertIn("changed: index.html", proc.stdout_text)

    def test_kimi_root_check_covers_the_whole_root(self):
        self.install_stub("kimi", STUB_KIMI)
        write(os.path.join(self.root, "pkg", "a.md"), "a\n")
        snapshot = os.path.join(self.loop, "snapshots", "round-4")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", self.root, "--path", "pkg", "--out", snapshot).returncode, 0)
        proc = self.spawn("kimi", "reviewer", self.prompt, snapshot, self.out, "--unsandboxed",
                          env={"STUB_TOUCH": os.path.join(self.root, "elsewhere.txt")})
        self.assertEqual(proc.returncode, 4, proc.stderr_text)
        self.assertIn("added: elsewhere.txt", proc.stdout_text)
        self.assertIn("kimi run", proc.stderr_text)

    def test_mixed_case_instruction_files_are_renamed(self):
        root = os.path.join(self.tmp, "mixed")
        write(os.path.join(root, "index.html"), "<p>x</p>\n")
        write(os.path.join(root, "agents.md"), "secret instruction\n")
        write(os.path.join(root, "Claude.md"), "more\n")
        write(os.path.join(root, ".Claude", "settings.json"), "{}\n")
        write(os.path.join(root, "sub", "gemini.MD"), "nested\n")
        snapshot = os.path.join(self.loop, "snapshots", "round-5")
        self.assertEqual(run_py(SNAPSHOT, "create", "--root", root, "--out", snapshot).returncode, 0)
        proc = self.spawn("codex", "reviewer", self.prompt, snapshot, self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        listing = re.search(r"listing=(.*)", read(self.out)).group(1).split()
        self.assertEqual(sorted(listing), sorted([".Claude.as-data", "Claude.md.as-data", "agents.md.as-data",
                                                  "index.html", "sub"]))
        self.assertIn("renamed=4", read(self.out))

    def test_timeout_kills_the_whole_process_group(self):
        pids = os.path.join(self.tmp, "pids")
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--timeout", "2",
                          env={"STUB_MODE": "sleep", "STUB_PIDS": pids, "PATH": self.isolated_path()})
        self.assertEqual(proc.returncode, 7, proc.stderr_text)
        self.assertIn("timed out after 2s", proc.stderr_text)
        self.assertIn("timed out after 2s", read(os.path.join(self.loop, "rounds.md")))
        self.assertIn("# timeout: 2s", read(self.out[:-3] + ".log"))
        self.assert_no_survivors(pids)
        self.assertEqual(self.stage_dirs(), [])

    def test_signal_stops_the_child_and_cleans_up(self):
        pids = os.path.join(self.tmp, "pids")
        env = dict(self.env, STUB_MODE="sleep", STUB_PIDS=pids)
        proc = subprocess.Popen(["bash", SPAWN, "codex", "reviewer", self.prompt, self.snapshot, self.out],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=env)
        for _ in range(200):
            if os.path.exists(pids) and read(pids).strip():
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(pids), "the stub never started")
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 143, stderr)
        self.assertIn("interrupted by signal 15", stderr)
        self.assert_no_survivors(pids)
        self.assertEqual(self.stage_dirs(), [])
        self.assertIn("interrupted by signal 15", read(os.path.join(self.loop, "rounds.md")))

    def assert_no_survivors(self, pids_file):
        pids = [int(p) for p in read(pids_file).split()]
        self.assertEqual(len(pids), 2)
        for pid in pids:
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                self.fail("process %d outlived the run" % pid)

    def wait_for(self, path, seconds=30):
        for _ in range(int(seconds * 20)):
            if os.path.exists(path) and read(path).strip():
                return read(path).strip()
            time.sleep(0.05)
        self.fail("%s never appeared" % path)

    def test_sigkill_of_the_script_still_stops_the_engine(self):
        """A harness that kills the shell outright runs no trap; the wrapper notices its
        parent is gone and kills the engine's process group within a few seconds."""
        pids = os.path.join(self.tmp, "pids")
        env = dict(self.env, STUB_MODE="sleep", STUB_PIDS=pids)
        proc = subprocess.Popen(["bash", SPAWN, "codex", "reviewer", self.prompt, self.snapshot, self.out],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.wait_for(pids)
        proc.kill()
        proc.communicate(timeout=30)
        self.assert_no_survivors(pids)

    def test_background_run_returns_at_once_and_records_its_exit(self):
        base = self.out[:-3]
        started = time.time()
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--model", "gpt-5", "--background")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertRegex(proc.stdout_text, r"^started: pid \d+ in the background")
        self.assertEqual(self.wait_for(base + ".status"), "0")
        self.assertLess(time.time() - started, 60)
        self.assertIn("## Score: 50/100", read(self.out))
        self.assertTrue(read(base + ".spawn").startswith("ok: "), read(base + ".spawn"))
        self.assertEqual(read(base + ".cmd"), self.dry_run_record("codex", "scorer", self.prompt, self.snapshot,
                                                                  self.out, "--model", "gpt-5"),
                         "--background is not part of the pin")
        self.assert_footer(base + ".log")
        # Argument errors still surface in the foreground, before anything detaches.
        proc = self.spawn("codex", "scorer", self.prompt, self.snapshot, self.out, "--background")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a scorer needs --model", proc.stderr_text)

    def test_background_run_is_listed_and_stops_cleanly_on_term(self):
        pids = os.path.join(self.tmp, "pids")
        base = self.out[:-3]
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--background",
                          env={"STUB_MODE": "sleep", "STUB_PIDS": pids})
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.wait_for(pids)
        status = run_py(os.path.join(SCRIPTS, "loop.py"), "status", "--loop", self.loop)
        self.assertIn("children not finished: scores/round-1 (running, pid %s)" % read(base + ".pid").strip(),
                      status.stdout)
        os.kill(int(read(base + ".pid")), signal.SIGTERM)
        self.assertEqual(self.wait_for(base + ".status"), "143")
        self.assert_no_survivors(pids)
        self.assertEqual(self.stage_dirs(), [])
        status = run_py(os.path.join(SCRIPTS, "loop.py"), "status", "--loop", self.loop)
        self.assertIn("children not finished: none", status.stdout)

    def test_max_usd_caps_claude_and_reports_cost(self):
        self.install_stub("claude", STUB_CLAUDE)
        args = ("claude", "scorer", self.prompt, self.snapshot, self.out, "--model", "claude-x-1", "--max-usd", "0.50")
        record = self.dry_run_record(*args)
        self.assertIn("'--max-budget-usd' \\\n  '0.50'", record)
        self.assertIn("'--output-format' \\\n  'json'", record)
        proc = self.spawn(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assert_footer(self.out[:-3] + ".log", usage="cost USD 0.0123 (claude total_cost_usd)")
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, "--max-usd", "0.50")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("claude only", proc.stderr_text)
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--max-usd", "lots")
        self.assertEqual(proc.returncode, 2)

    def test_vision_preflight(self):
        proc = self.spawn("--preflight", "codex", "--vision", env={"STUB_MODE": "vision", "II_VISION_COLOUR": "magenta"})
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assertRegex(proc.stdout_text.strip(), r"^preflight ok: codex .* as reviewer, vision ok \(named magenta\), \d+s$")
        proc = self.spawn("--preflight", "codex", "--vision", env={"STUB_MODE": "vision"})
        self.assertEqual(proc.returncode, 0, "a random colour is read too: " + proc.stderr_text)
        proc = self.spawn("--preflight", "codex", "--vision", env={"STUB_MODE": "blind", "II_VISION_COLOUR": "blue"})
        self.assertEqual(proc.returncode, 7)
        self.assertIn("the image was blue, the child said red", proc.stderr_text)
        self.assertIn("convert them to text evidence", proc.stderr_text)
        self.assertEqual(self.stage_dirs(), [])

    def test_rotated_codex_auth_is_copied_back(self):
        auth = os.path.join(self.home, ".codex", "auth.json")
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, env={"STUB_MODE": "rotate"})
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("copied it back", proc.stderr_text)
        self.assertEqual(read(auth).strip(), '{"rotated":1}')
        self.assertEqual(read(auth + ".bak"), "{}\n")

    def test_record_carries_no_timeout(self):
        default = self.dry_run_record("codex", "reviewer", self.prompt, self.snapshot, self.out)
        longer = self.dry_run_record("codex", "reviewer", self.prompt, self.snapshot, self.out, "--timeout", "1800")
        self.assertEqual(default, longer, "the timeout is not part of the scorer pin")
        self.assertNotIn("timeout", default)

    def test_cdpath_and_relative_codex_home(self):
        decoy = os.path.join(self.tmp, "decoy")
        os.makedirs(os.path.join(decoy, "scores"))
        proc = self.spawn("codex", "reviewer", os.path.relpath(self.prompt, self.loop), "snapshots/round-1",
                          "scores/rel.md", "--dry-run", cwd=self.loop, env={"CDPATH": decoy})
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("# record: %s" % os.path.join(os.path.realpath(self.loop), "scores", "rel.cmd"), proc.stdout_text)
        proc = self.spawn("codex", "reviewer", self.prompt, self.snapshot, self.out, cwd=self.tmp,
                          env={"CODEX_HOME": os.path.join("home", ".codex")})
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("auth={}", read(self.out))

    def test_preflight(self):
        proc = self.spawn("--preflight", "codex", "--model", "gpt-5")
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assertRegex(proc.stdout_text.strip(), r"^preflight ok: codex \(codex-cli 0\.0-stub\) as scorer, model gpt-5, \d+s$")
        self.assertRegex(proc.stdout_text.strip(), r"^preflight ok: .*, \d+s$")
        proc = self.spawn("--preflight", "codex")
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("as reviewer", proc.stdout_text)
        self.assertRegex(proc.stdout_text.strip(), r"^preflight ok: codex \(codex-cli 0\.0-stub\) as reviewer, \d+s$")
        proc = self.spawn("--preflight", "codex", env={"STUB_MODE": "garbage"})
        self.assertEqual(proc.returncode, 7)
        self.assertIn("did not read the staged file", proc.stderr_text)
        proc = self.spawn("--preflight", "codex", env={"STUB_MODE": "fail"})
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(self.stage_dirs(), [])

    def test_preflight_by_bare_name_from_the_scripts_dir(self):
        proc = self.spawn("--preflight", "codex", script="spawn_child.sh", cwd=SCRIPTS)
        self.assertEqual(proc.returncode, 0, proc.stderr_text + proc.stdout_text)
        self.assertIn("preflight ok: codex", proc.stdout_text)

    def test_check_is_offline_and_names_what_is_missing(self):
        env = {"PATH": self.isolated_path()}
        proc = self.spawn("--check", "codex", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout_text + proc.stderr_text)
        self.assertRegex(proc.stdout_text, r"(?m)^ok    python3 \d+\.\d+")
        self.assertRegex(proc.stdout_text, r"(?m)^ok    codex: .*\(codex-cli 0\.0-stub\)$")
        self.assertIn("ok    codex: auth.json at", proc.stdout_text)
        self.assertIn("outside any repo", proc.stdout_text)
        proc = self.spawn("--check", "claude", env=env)
        self.assertEqual(proc.returncode, 7)
        self.assertIn("FAIL  claude: not on PATH; install it with", proc.stdout_text)
        proc = self.spawn("--check", env=env)
        self.assertEqual(proc.returncode, 0, "one usable engine is enough when none is named")
        self.assertIn("FAIL  kimi: not on PATH", proc.stdout_text)
        os.remove(os.path.join(self.home, ".codex", "auth.json"))
        proc = self.spawn("--check", "codex", env=env)
        self.assertEqual(proc.returncode, 7)
        self.assertIn("keyring", proc.stdout_text)
        proc = self.spawn("--check", "claude", env=dict(env, CLAUDECODE="1"))
        self.assertIn("needs --same-engine", proc.stdout_text)
        self.assertEqual(self.stage_dirs(), [])

    def test_missing_engine_is_named_before_any_model_call(self):
        env = {"PATH": self.isolated_path()}
        proc = self.spawn("--preflight", "claude", env=env)
        self.assertEqual(proc.returncode, 7)
        self.assertIn("not on PATH", proc.stderr_text)
        self.assertIn("no model call was made", proc.stderr_text)
        self.assertEqual(self.stage_dirs(), [])
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("claude is not on PATH", proc.stderr_text)
        self.assertFalse(os.path.exists(self.out[:-3] + ".cmd"))
        proc = self.spawn("claude", "reviewer", self.prompt, self.snapshot, self.out, "--dry-run", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr_text)
        self.assertIn("warning: claude is not on PATH", proc.stderr_text)

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
        for script in (SNAPSHOT, STRIP, SCORE, os.path.join(SCRIPTS, "loop.py"), os.path.join(SCRIPTS, "evidence.py")):
            proc = run_py(script, "--help")
            self.assertEqual(proc.returncode, 0, script)
            self.assertIn("usage", proc.stdout.lower())

    def test_score_math_subcommand_help(self):
        for sub in ("baseline", "budget", "decide", "check-criteria", "check-scorer"):
            proc = run_py(SCORE, sub, "--help")
            self.assertEqual(proc.returncode, 0, sub + "\n" + proc.stderr)
            self.assertIn("usage", proc.stdout.lower())
        proc = run_py(SCORE, "--help")
        for phrase in ("budget --total", "third-run", "total round ceiling", "--rounds-used"):
            self.assertIn(phrase, proc.stdout, phrase)

    def test_spawn_help_prints_the_whole_header(self):
        proc = subprocess.run(["bash", SPAWN, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("Spawn an isolated"), proc.stdout[:80])
        self.assertIn("--unsandboxed", proc.stdout)
        self.assertTrue(proc.stdout.rstrip().endswith("failed the preflight."), proc.stdout[-120:])
        self.assertNotIn("set -eu", proc.stdout)
        for phrase in ("--check", "process group", "--path scope"):
            self.assertIn(phrase, proc.stdout, phrase)


if __name__ == "__main__":
    unittest.main()
