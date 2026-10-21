#!/usr/bin/env python3
"""Tests for install.sh, run against a fake HOME and a synthetic skills tree in a
temp directory. Nothing outside the temp directory is touched.

Run: python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

import glob
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(REPO, "install.sh")
TARGET_DIRS = {"claude": ".claude/skills", "codex": ".codex/skills", "agents": ".agents/skills"}


def write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if mode is not None:
        os.chmod(path, mode)


def is_exec(path):
    return bool(os.stat(path).st_mode & stat.S_IXUSR)


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="install-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        shutil.copy(INSTALL_SH, os.path.join(self.repo, "install.sh"))
        os.chmod(os.path.join(self.repo, "install.sh"), 0o755)
        self.skills = os.path.join(self.repo, "skills")
        write(os.path.join(self.skills, "alpha", "SKILL.md"), "---\nname: alpha\ndescription: a\n---\n# alpha\n")
        write(os.path.join(self.skills, "alpha", "scripts", "tool"), "#!/usr/bin/env bash\necho tool\n", 0o755)
        write(os.path.join(self.skills, "beta", "SKILL.md"), "---\nname: beta\ndescription: b\n---\n# beta\n")
        os.makedirs(os.path.join(self.skills, "notaskill"))  # no SKILL.md: must be ignored

    def run_install(self, *args, expect=0):
        env = dict(os.environ, HOME=self.home)
        proc = subprocess.run(["bash", os.path.join(self.repo, "install.sh")] + list(args),
                              cwd=self.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)
        if expect is not None:
            self.assertEqual(proc.returncode, expect, proc.stdout)
        return proc.stdout

    def dest(self, target, name):
        return os.path.join(self.home, TARGET_DIRS[target], name)

    def src(self, name):
        return os.path.join(self.skills, name)

    def assert_linked(self, target, name):
        d = self.dest(target, name)
        self.assertTrue(os.path.islink(d), "%s should be a symlink" % d)
        self.assertEqual(os.path.realpath(d), os.path.realpath(self.src(name)))

    def backups(self, target, name):
        return sorted(glob.glob(self.dest(target, name) + ".bak-*"))

    def tree(self, root):
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                out.append(os.path.relpath(os.path.join(dirpath, n), root))
        return sorted(out)


class SyntaxTests(unittest.TestCase):
    def test_bash_syntax(self):
        subprocess.run(["bash", "-n", INSTALL_SH], check=True)

    def test_help(self):
        out = subprocess.run(["bash", INSTALL_SH, "--help"], stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
        self.assertIn("--uninstall", out)
        self.assertIn("--dry-run", out)
        self.assertNotIn("#!/usr/bin", out)


class InstallTests(InstallCase):
    def test_install_links_every_skill_into_every_target(self):
        out = self.run_install()
        for target in TARGET_DIRS:
            self.assert_linked(target, "alpha")
            self.assert_linked(target, "beta")
            self.assertFalse(os.path.exists(self.dest(target, "notaskill")))
        self.assertIn("[claude] link     alpha -> %s" % self.src("alpha"), out)
        self.assertIn("done: 6 linked, 0 copied, 0 unchanged, 0 backed up", out)

    def test_reinstall_is_idempotent(self):
        self.run_install()
        before = self.tree(self.home)
        out = self.run_install()
        self.assertEqual(self.tree(self.home), before)
        self.assertIn("already linked", out)
        self.assertIn("done: 0 linked, 0 copied, 6 unchanged, 0 backed up", out)
        for target in TARGET_DIRS:
            self.assertEqual(self.backups(target, "alpha"), [])

    def test_existing_directory_is_backed_up_not_overwritten(self):
        old = self.dest("claude", "alpha")
        write(os.path.join(old, "old.txt"), "keep me\n")
        write(os.path.join(old, "SKILL.md"), "stale\n")
        out = self.run_install("--target", "claude")
        self.assert_linked("claude", "alpha")
        baks = self.backups("claude", "alpha")
        self.assertEqual(len(baks), 1, baks)
        self.assertFalse(os.path.islink(baks[0]))
        with open(os.path.join(baks[0], "old.txt")) as fh:
            self.assertEqual(fh.read(), "keep me\n")
        self.assertIn("backup", out)
        self.assertIn(baks[0], out)
        self.assertIn("1 backed up", out)
        # second run: no further backups
        self.run_install("--target", "claude")
        self.assertEqual(self.backups("claude", "alpha"), baks)

    def test_existing_file_and_foreign_symlink_are_backed_up(self):
        write(self.dest("codex", "alpha"), "a file in the way\n")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(os.path.dirname(self.dest("codex", "beta")), exist_ok=True)
        os.symlink(elsewhere, self.dest("codex", "beta"))
        out = self.run_install("--target", "codex")
        self.assert_linked("codex", "alpha")
        self.assert_linked("codex", "beta")
        self.assertEqual(len(self.backups("codex", "alpha")), 1)
        self.assertTrue(os.path.isfile(self.backups("codex", "alpha")[0]))
        self.assertEqual(len(self.backups("codex", "beta")), 1)
        self.assertTrue(os.path.islink(self.backups("codex", "beta")[0]))
        self.assertIn("2 backed up", out)

    def test_broken_symlink_is_replaced(self):
        os.makedirs(os.path.dirname(self.dest("agents", "alpha")))
        os.symlink(os.path.join(self.tmp, "gone"), self.dest("agents", "alpha"))
        out = self.run_install("--target", "agents", "--skill", "alpha")
        self.assert_linked("agents", "alpha")
        self.assertEqual(self.backups("agents", "alpha"), [])
        self.assertIn("broken symlink", out)

    def test_skill_filter(self):
        out = self.run_install("--skill", "alpha")
        for target in TARGET_DIRS:
            self.assert_linked(target, "alpha")
            self.assertFalse(os.path.lexists(self.dest(target, "beta")))
        self.assertIn("done: 3 linked", out)
        out = self.run_install("--skill", "nope", expect=2)
        self.assertIn("unknown skill: nope", out)
        self.assertIn("alpha", out)
        self.assertEqual(self.tree(self.home).count("skills/nope"), 0)

    def test_target_selection(self):
        self.run_install("--target", "codex")
        self.assertTrue(os.path.islink(self.dest("codex", "alpha")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".agents")))
        self.run_install("--target", "claude,agents", "--skill", "beta")
        self.assert_linked("claude", "beta")
        self.assert_linked("agents", "beta")
        self.assertFalse(os.path.lexists(self.dest("claude", "alpha")))
        self.run_install("--target=claude", "--target", "codex", "--skill=alpha")
        self.assert_linked("claude", "alpha")
        out = self.run_install("--target", "cursor", expect=2)
        self.assertIn("unknown target: cursor", out)

    def test_dry_run_changes_nothing(self):
        out = self.run_install("--dry-run")
        self.assertEqual(os.listdir(self.home), [])
        self.assertIn("(dry-run) [claude] link     alpha", out)
        self.assertIn("(dry-run) done: 6 linked", out)
        write(os.path.join(self.dest("claude", "alpha"), "x.txt"), "x\n")
        out = self.run_install("--dry-run", "--target", "claude")
        self.assertIn("(dry-run) [claude] backup", out)
        self.assertTrue(os.path.isdir(self.dest("claude", "alpha")))
        self.assertEqual(self.backups("claude", "alpha"), [])
        self.assertFalse(os.path.lexists(self.dest("claude", "beta")))

    def test_unknown_argument(self):
        out = self.run_install("--bogus", expect=2)
        self.assertIn("unknown argument: --bogus", out)
        self.assertEqual(os.listdir(self.home), [])


class CopyTests(InstallCase):
    def test_copy_mode(self):
        out = self.run_install("--copy", "--target", "claude")
        d = self.dest("claude", "alpha")
        self.assertTrue(os.path.isdir(d) and not os.path.islink(d))
        with open(os.path.join(d, "SKILL.md")) as fh:
            self.assertIn("name: alpha", fh.read())
        self.assertTrue(is_exec(os.path.join(d, "scripts", "tool")))
        with open(os.path.join(d, ".install-source")) as fh:
            self.assertEqual(fh.read().strip(), self.src("alpha"))
        self.assertIn("done: 0 linked, 2 copied", out)

    def test_copy_is_idempotent_and_refreshes_without_backups(self):
        self.run_install("--copy", "--target", "claude")
        out = self.run_install("--copy", "--target", "claude")
        self.assertIn("already copied and unchanged", out)
        self.assertIn("2 unchanged", out)
        self.assertEqual(self.backups("claude", "alpha"), [])
        write(os.path.join(self.src("alpha"), "SKILL.md"), "---\nname: alpha\ndescription: changed\n---\n")
        out = self.run_install("--copy", "--target", "claude")
        self.assertIn("replace", out)
        with open(os.path.join(self.dest("claude", "alpha"), "SKILL.md")) as fh:
            self.assertIn("changed", fh.read())
        self.assertEqual(self.backups("claude", "alpha"), [])

    def test_link_over_own_copy_replaces_and_copy_over_link_backs_up_nothing_foreign(self):
        self.run_install("--copy", "--target", "claude", "--skill", "alpha")
        self.run_install("--target", "claude", "--skill", "alpha")
        self.assert_linked("claude", "alpha")
        self.assertEqual(self.backups("claude", "alpha"), [])
        out = self.run_install("--copy", "--target", "claude", "--skill", "alpha")
        # a symlink to our own source is still moved aside, never silently deleted
        self.assertEqual(len(self.backups("claude", "alpha")), 1)
        self.assertTrue(os.path.islink(self.backups("claude", "alpha")[0]))
        self.assertIn("backup", out)

    def test_foreign_directory_backed_up_in_copy_mode(self):
        write(os.path.join(self.dest("claude", "alpha"), "mine.txt"), "mine\n")
        self.run_install("--copy", "--target", "claude", "--skill", "alpha")
        baks = self.backups("claude", "alpha")
        self.assertEqual(len(baks), 1)
        self.assertTrue(os.path.isfile(os.path.join(baks[0], "mine.txt")))


class UninstallTests(InstallCase):
    def test_uninstall_removes_links_and_leaves_foreign_things(self):
        self.run_install()
        foreign_dir = self.dest("codex", "gamma")
        write(os.path.join(foreign_dir, "SKILL.md"), "foreign\n")
        foreign_link_target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(foreign_link_target)
        os.remove(self.dest("codex", "beta"))
        os.symlink(foreign_link_target, self.dest("codex", "beta"))
        out = self.run_install("--uninstall")
        for target in TARGET_DIRS:
            self.assertFalse(os.path.lexists(self.dest(target, "alpha")))
            self.assertTrue(os.path.isdir(os.path.join(self.home, TARGET_DIRS[target])))
        self.assertFalse(os.path.lexists(self.dest("claude", "beta")))
        self.assertTrue(os.path.islink(self.dest("codex", "beta")), "foreign symlink must stay")
        self.assertTrue(os.path.isdir(foreign_dir), "foreign directory must stay")
        self.assertIn("symlink points elsewhere", out)
        self.assertIn("done: 5 removed, 1 skipped", out)

    def test_uninstall_when_nothing_installed(self):
        out = self.run_install("--uninstall")
        self.assertIn("not installed", out)
        self.assertIn("done: 0 removed, 0 skipped", out)
        self.assertEqual(os.listdir(self.home), [])

    def test_uninstall_removes_own_copies_but_not_foreign_directories(self):
        self.run_install("--copy", "--target", "agents")
        write(os.path.join(self.dest("claude", "alpha"), "SKILL.md"), "not ours\n")
        out = self.run_install("--uninstall", "--target", "agents,claude")
        self.assertFalse(os.path.exists(self.dest("agents", "alpha")))
        self.assertFalse(os.path.exists(self.dest("agents", "beta")))
        self.assertTrue(os.path.isdir(self.dest("claude", "alpha")))
        self.assertIn("was not installed by this script", out)
        self.assertIn("done: 2 removed, 1 skipped", out)

    def test_uninstall_dry_run_and_backup_note(self):
        write(os.path.join(self.dest("claude", "alpha"), "old.txt"), "old\n")
        self.run_install("--target", "claude", "--skill", "alpha")
        before = self.tree(self.home)
        out = self.run_install("--uninstall", "--dry-run", "--target", "claude", "--skill", "alpha")
        self.assertEqual(self.tree(self.home), before)
        self.assertIn("(dry-run) [claude] unlink", out)
        self.assertIn("backups present", out)
        self.run_install("--uninstall", "--target", "claude", "--skill", "alpha")
        self.assertFalse(os.path.lexists(self.dest("claude", "alpha")))
        self.assertEqual(len(self.backups("claude", "alpha")), 1, "uninstall must not delete backups")


if __name__ == "__main__":
    unittest.main()
