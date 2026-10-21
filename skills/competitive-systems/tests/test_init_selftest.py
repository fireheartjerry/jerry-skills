"""`dev.py init` and the tools/dev.py shim it writes: files and marker, resolution order
(DEVKIT_DEV_PY, the recorded path, project copies, home copies), exit 3 with every tried
location, refusal to overwrite a foreign tools/dev.py, --check and --dry-run. Every test
runs in a temporary copy of the Python fixture with HOME pointed at an empty directory.
`dev.py selftest` runs once, from a copy of the skill without tests/, as installed.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
DEV = os.path.join(SCRIPTS, "dev.py")
REPOS = os.path.join(HERE, "fixtures", "repos")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from devkit.common import load_config  # noqa: E402
from devkit.init import MARKER, SHIM_MAX_LINES  # noqa: E402


def listing(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                out.append((os.path.relpath(full, root), f.read()))
    return out


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="devkit-init-")
        self.root = os.path.join(self.tmp, "repo")
        shutil.copytree(os.path.join(REPOS, "py"), self.root)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        env = dict((k, v) for k, v in os.environ.items()
                   if not k.startswith("DEVKIT_") and k != "CLAUDE_PLUGIN_ROOT")
        env["HOME"] = self.home
        env.update(extra)
        return env

    def dev(self, *args, **kw):
        dev = kw.pop("dev", DEV)
        return subprocess.run([sys.executable, "-I", dev] + list(args) + ["--repo", self.root],
                              cwd=self.root, env=self.env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def shim(self, *args, **kw):
        cwd = kw.pop("cwd", self.root)
        shim = kw.pop("shim", os.path.join(self.root, "tools", "dev.py"))
        return subprocess.run([sys.executable, shim] + list(args), cwd=cwd,
                              env=self.env(**kw), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def skill_copy(self, where):
        """A copy of scripts/ at <where>/competitive-systems/scripts; returns its dev.py."""
        dst = os.path.join(where, "competitive-systems", "scripts")
        shutil.copytree(SCRIPTS, dst, ignore=shutil.ignore_patterns("__pycache__"))
        return os.path.join(dst, "dev.py")

    def test_init_writes_shim_config_and_gitignore(self):
        p = self.dev("init")
        self.assertEqual(p.returncode, 0, p.stderr)
        shim = self.read("tools/dev.py")
        self.assertEqual(shim.splitlines()[1], MARKER)
        self.assertLessEqual(len(shim.splitlines()), SHIM_MAX_LINES)
        self.assertTrue(os.stat(os.path.join(self.root, "tools/dev.py")).st_mode & stat.S_IXUSR)
        self.assertEqual(self.read(".devkit/.gitignore"), "*\n")
        data = json.loads(self.read("tools/dev.json"))
        self.assertEqual(data["languages"], ["python"])
        self.assertEqual(data["commands"], {"python": {"lint": None, "typecheck": None,
                                                       "test": None, "test_report": None}})
        self.assertIn("tools/dev.py", data["ignore"])
        for key in ("sensitive", "exclude_dirs", "test_globs", "test_map", "always_run",
                    "benchmarks", "size_audit", "budget_seconds", "_doc"):
            self.assertIn(key, data)
        cfg = load_config(self.root, warn_unknown=True)           # the file it wrote is valid
        self.assertEqual(cfg["path"], "tools/dev.json")
        self.assertIn("init: wrote tools/dev.py", p.stdout)
        self.assertIn("init: wrote tools/dev.json (languages: python)", p.stdout)

    def test_shim_runs_the_bundled_copy_from_anywhere(self):
        self.assertEqual(self.dev("init").returncode, 0)
        p = self.shim("detect", DEVKIT_DEV_PY=DEV)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("languages: python", p.stdout)
        self.assertIn("config: tools/dev.json", p.stdout)
        p = self.shim("where", "frobnicate", DEVKIT_DEV_PY=DEV, cwd=os.path.join(self.root, "pkg"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("pkg/core.py:4 "), p.stdout)
        p = self.shim("bound", "amdahl", "--p", "0.12", "--s", "100", DEVKIT_DEV_PY=DEV)
        self.assertIn("1.14x", p.stdout)
        p = self.shim("nosuchcmd", DEVKIT_DEV_PY=DEV)
        self.assertEqual(p.returncode, 2)               # exit codes pass through the shim

    def test_resolution_order_and_exit_3(self):
        copy = self.skill_copy(os.path.join(self.tmp, "installed"))
        p = self.dev("init", dev=copy)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("installed/competitive-systems/scripts/dev.py", self.read("tools/dev.py"))
        self.assertEqual(self.shim("--version").returncode, 0)        # via the recorded path
        fake = os.path.join(self.tmp, "fake", "dev.py")
        os.makedirs(os.path.dirname(fake))
        with open(fake, "w") as f:
            f.write("import os, sys\nprint('fake', os.environ['DEVKIT_REPO'], sys.argv[1:])\n")
        p = self.shim("detect", DEVKIT_DEV_PY=fake)                    # the variable wins
        self.assertEqual(p.stdout.split()[0], "fake")
        self.assertEqual(os.path.realpath(p.stdout.split()[1]), os.path.realpath(self.root))
        shutil.rmtree(os.path.join(self.tmp, "installed"))
        p = self.shim("detect")
        self.assertEqual(p.returncode, 3)
        err = p.stderr
        self.assertIn("cannot find the competitive-systems dev.py; tried:", err)
        for label in ("recorded by init:", "project copy:", "home copy:"):
            self.assertIn(label, err)
        for loc in (os.path.join(".claude", "skills"), os.path.join(".codex", "skills"),
                    os.path.join(".agents", "skills"), "installed"):
            self.assertIn(loc, err)
        self.assertIn("export DEVKIT_DEV_PY=", err)
        self.assertIn("scripts/dev.py init", err)
        self.assertNotIn("Traceback", err)
        self.skill_copy(os.path.join(self.root, ".claude", "skills"))  # a project copy
        self.assertEqual(self.shim("--version").returncode, 0)
        shutil.rmtree(os.path.join(self.root, ".claude"))
        self.skill_copy(os.path.join(self.home, ".agents", "skills"))  # a home copy
        p = self.shim("--version")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_plugin_installs_survive_a_version_update(self):
        cache = os.path.join(self.home, ".claude", "plugins", "cache", "market", "plugin")
        v1 = self.skill_copy(os.path.join(cache, "1.0.0", "skills"))
        p = self.dev("init", dev=v1)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("finds the newest plugin install at run time", p.stdout)
        self.assertIn("RECORDED = None", self.read("tools/dev.py"))
        self.assertEqual(self.shim("--version").returncode, 0)
        shutil.rmtree(os.path.join(cache, "1.0.0"))                    # the plugin updates
        self.skill_copy(os.path.join(cache, "1.1.0", "skills"))
        p = self.shim("--version")
        self.assertEqual(p.returncode, 0, p.stderr)
        shutil.rmtree(os.path.join(self.home, ".claude"))
        p = self.shim("--version")
        self.assertEqual(p.returncode, 3)
        self.assertIn("plugin install:", p.stderr)
        root = os.path.join(self.tmp, "plugin-root")
        self.skill_copy(os.path.join(root, "skills"))
        p = self.shim("--version", CLAUDE_PLUGIN_ROOT=root)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_refuses_a_foreign_tools_dev_py(self):
        os.makedirs(os.path.join(self.root, "tools"))
        with open(os.path.join(self.root, "tools", "dev.py"), "w") as f:
            f.write("print('the project tool')\n")
        before = listing(self.root)
        p = self.dev("init")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr.strip(), "dev.py init: a different tools/dev.py exists; pass "
                                           "--force to replace it or --shim-path to choose another name")
        self.assertEqual(listing(self.root), before)
        self.assertEqual(self.dev("init", "--check").returncode, 1)
        p = self.dev("init", "--shim-path", "tools/devkit.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.read("tools/dev.py"), "print('the project tool')\n")
        p = self.shim("detect", shim=os.path.join(self.root, "tools", "devkit.py"), DEVKIT_DEV_PY=DEV)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.dev("init", "--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("init: replaced tools/dev.py", p.stdout)
        self.assertEqual(self.read("tools/dev.py").splitlines()[1], MARKER)
        for bad in ("/abs/dev.py", "../dev.py", "tools/dev.sh"):
            self.assertEqual(self.dev("init", "--shim-path", bad).returncode, 2, bad)

    def test_rewrites_its_own_shim_and_keeps_the_config(self):
        self.assertEqual(self.dev("init").returncode, 0)
        path = os.path.join(self.root, "tools", "dev.py")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace("RECORDED = ", "RECORDED = 'stale/path.py' or "))
        with open(os.path.join(self.root, "tools", "dev.json"), "w") as f:
            f.write('{"budget_seconds": 60}\n')
        p = self.dev("init")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("init: rewrote tools/dev.py", p.stdout)
        self.assertIn("init: kept tools/dev.json (exists)", p.stdout)
        self.assertEqual(self.read("tools/dev.py"), text)
        self.assertEqual(self.read("tools/dev.json"), '{"budget_seconds": 60}\n')

    def test_check_and_dry_run(self):
        before = listing(self.root)
        p = self.dev("init", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("init: would write tools/dev.py", p.stdout)
        self.assertIn("init: would write tools/dev.json", p.stdout)
        self.assertIn("init: would write .devkit/.gitignore", p.stdout)
        self.assertIn(MARKER, p.stdout)
        self.assertEqual(listing(self.root), before)
        j = json.loads(self.dev("init", "--dry-run", "--json").stdout)
        self.assertEqual([a["action"] for a in j["actions"]], ["write", "write", "write"])
        self.assertEqual(listing(self.root), before)
        self.assertEqual(self.dev("init", "--check").returncode, 1)
        copy = self.skill_copy(os.path.join(self.tmp, "installed"))   # recorded as absolute
        self.assertEqual(self.dev("init", dev=copy).returncode, 0)
        p = self.dev("init", "--check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("tools/dev.py resolves", p.stdout)
        path = os.path.join(self.root, "tools", "dev.py")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace(MARKER, "# devkit-shim v0"))
        p = self.dev("init", "--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("re-run `dev.py init`", p.stdout)
        os.remove(path)
        p = self.dev("init", "--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("does not exist", p.stdout)


class HelpTests(unittest.TestCase):
    """tooling.md promises --json on every command and --help as the source of truth."""

    def test_every_command_documents_json_and_every_option_has_help(self):
        listing = subprocess.run([sys.executable, "-I", DEV, "help"], stdout=subprocess.PIPE,
                                 universal_newlines=True, timeout=60).stdout
        commands = [line.split()[0] for line in listing.splitlines()
                    if line.startswith("  ") and line.split()]
        self.assertIn("report", commands)
        subs = {"bound": ["amdahl", "makespan", "ops"], "perf": ["run", "history"]}
        for cmd in commands:
            for argv in ([[cmd, s] for s in subs[cmd]] if cmd in subs else [[cmd]]):
                p = subprocess.run([sys.executable, "-I", DEV] + argv + ["--help"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, timeout=60)
                self.assertEqual(p.returncode, 0, (argv, p.stderr))
                self.assertIn("--json", p.stdout, argv)
        import ast
        import glob
        files = glob.glob(os.path.join(SCRIPTS, "devkit", "*.py")) + [
            os.path.join(SCRIPTS, "dev.py"), os.path.join(SCRIPTS, "bench.py")]
        bare = []
        for path in sorted(files):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"
                        and not any(k.arg == "help" for k in node.keywords)):
                    bare.append("%s:%d" % (os.path.basename(path), node.lineno))
        self.assertEqual(bare, [], "add_argument without help=")


class SelftestTests(unittest.TestCase):
    def test_selftest_passes_from_an_install_without_tests(self):
        """The long test (about 8 s): the copy holds SKILL.md, scripts/ and references/ only,
        so the selftest cannot lean on tests/fixtures."""
        skill = os.path.dirname(HERE)
        tmp = tempfile.mkdtemp(prefix="devkit-selftest-install-")
        try:
            dst = os.path.join(tmp, "competitive-systems")
            os.makedirs(dst)
            shutil.copy(os.path.join(skill, "SKILL.md"), dst)
            for sub in ("scripts", "references"):
                shutil.copytree(os.path.join(skill, sub), os.path.join(dst, sub),
                                ignore=shutil.ignore_patterns("__pycache__"))
            scratch = os.path.join(tmp, "tmpdir")
            os.makedirs(scratch)
            env = dict((k, v) for k, v in os.environ.items() if not k.startswith("DEVKIT_"))
            env["TMPDIR"] = scratch
            p = subprocess.run([sys.executable, "-I", os.path.join(dst, "scripts", "dev.py"),
                                "selftest"], cwd=tmp, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
            out = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, out)
            self.assertIn("selftest: PASS", p.stdout)
            self.assertNotIn("Traceback", out)
            steps = [line for line in p.stdout.splitlines() if line.startswith("ok ")]
            self.assertGreaterEqual(len(steps), 45, p.stdout)
            self.assertEqual(os.listdir(scratch), [], "temporary directory kept without --keep")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
