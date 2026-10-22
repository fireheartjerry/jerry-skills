"""help, doctor, validation, list without tmux."""

import json
import os
import shutil
import subprocess
import sys
import time

from helpers import FAKE_DIR, FAST, SCRIPT, SessionTest, script_module

SUBCOMMANDS = ["spawn", "list", "status", "wait", "result", "events", "send", "turn", "log", "peek", "stop",
               "kill", "reap", "engines", "schema", "doctor"]


class CliTests(SessionTest):
    def no_tmux_path(self):
        return self.restricted_path(tmux=False)

    def test_suite_path_is_minimal(self):
        """tests/run.sh promises a PATH without Homebrew's GNU coreutils."""
        bindir = os.environ.get("AGENT_SESSIONS_TEST_BIN")
        if not bindir:
            self.skipTest("not running under tests/run.sh")
        entries = os.environ["PATH"].split(os.pathsep)
        self.assertEqual(entries, [FAKE_DIR, bindir, "/usr/bin", "/bin"], entries)
        self.assertEqual(sorted(os.listdir(bindir)), sorted(t for t in ("git", "ps", "python3", "tmux")
                                                             if os.path.exists(os.path.join(bindir, t))))
        if sys.platform == "darwin":
            # macOS ships no `timeout`; on Linux it is base coreutils, so the
            # guard that matters (no GNU-only tools) can only be asserted here.
            self.assertIsNone(shutil.which("timeout"))
            self.assertIsNone(shutil.which("gtimeout"))

    def test_doctor_passes_with_one_engine(self):
        cp = self.run_as("doctor", env={"PATH": self.restricted_path(tmux=True, engines=("claude",))})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        warn_lines = [l for l in cp.stdout.splitlines() if l.startswith("warn") and "codex" in l]
        self.assertTrue(warn_lines, cp.stdout)
        self.assertIn("fine if you only use claude", warn_lines[0])
        cp = self.run_as("doctor", "--engine", "codex", env={"PATH": self.restricted_path(tmux=True, engines=("claude",))})
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertTrue([l for l in cp.stdout.splitlines() if l.startswith("FAIL") and "codex" in l], cp.stdout)
        cp = self.run_as("doctor", env={"PATH": self.restricted_path(tmux=True, engines=())})
        self.assertEqual(cp.returncode, 1, cp.stdout)
        fails = [l for l in cp.stdout.splitlines() if l.startswith("FAIL")]
        self.assertEqual(len(fails), 2, cp.stdout)
        gemini = [l for l in cp.stdout.splitlines() if "gemini" in l]
        self.assertTrue(gemini[0].startswith("warn"), gemini)

    def test_doctor_gemini_is_experimental(self):
        """A missing gemini is a warning, never a FAIL, unless asked for by name."""
        path = self.restricted_path(tmux=True, engines=("claude", "codex"))
        cp = self.run_as("doctor", env={"PATH": path})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        line = [l for l in cp.stdout.splitlines() if l.split(":")[0].endswith("gemini")][0]
        self.assertTrue(line.startswith("warn"), line)
        self.assertIn("experimental", line)
        self.assertIn("fine if you only use claude or codex", line)
        cp = self.run_as("doctor", "--engine", "gemini", env={"PATH": path})
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertTrue([l for l in cp.stdout.splitlines() if l.startswith("FAIL") and "gemini" in l], cp.stdout)
        cp = self.run_as("doctor", "--json")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("experimental", json.loads(cp.stdout)["gemini"]["detail"])
        cp = self.run_as("doctor", "--engine", "bogus")
        self.assertEqual(cp.returncode, 2)

    def test_module_split_reexports(self):
        mod = script_module()
        for name in ("ps_parent", "ps_lstart", "engine_alive", "ClaudeParser", "CodexParser", "GeminiParser",
                     "validate", "RESULT_SCHEMA", "EVENT_SCHEMA", "JUDGE_SCHEMA", "engine_for", "build_result",
                     "state_lock", "Runner", "COMMANDS", "HELP", "main", "cmd_spawn", "worktree_summary"):
            self.assertTrue(hasattr(mod, name), name)
        self.assertEqual(mod.VERSION, "3.1.0")
        self.assertEqual(self.run_as("--version").stdout.strip(), "3.1.0")
        for sub in SUBCOMMANDS:
            self.assertIn(sub, mod.COMMANDS)
        cp = subprocess.run([sys.executable, "-I", SCRIPT, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, env=self.environ())
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("agent-session 3.1.0", cp.stdout)
        with open(SCRIPT, encoding="utf-8") as fh:
            entry = fh.read()
        self.assertLess(len(entry.splitlines()), 40, "the entry script must stay thin")
        self.assertIn("agent_sessions.cli", entry)
        pkg = os.path.join(os.path.dirname(SCRIPT), "agent_sessions")
        for module in ("__init__", "common", "procs", "backend", "state", "engines", "schema", "gitops", "runner",
                       "commands", "cli"):
            self.assertTrue(os.path.exists(os.path.join(pkg, module + ".py")), module)

    def test_entry_writes_no_bytecode(self):
        """The entry sets dont_write_bytecode before it imports the package.
        Checked on a clean copy, so nothing else that imported the package
        counts, under -I (PYTHONDONTWRITEBYTECODE ignored) with a private
        pycache prefix; a plain import of the copy is the control and must
        write there."""
        copy = os.path.join(self.box, "scripts")
        shutil.copytree(os.path.dirname(SCRIPT), copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        prefix = os.path.join(self.box, "pycache")
        python = [sys.executable, "-I", "-X", "pycache_prefix=" + prefix]
        # The prefix mirrors absolute source paths; the stdlib's own startup
        # imports land there too and are not the entry's doing.
        mirror = os.path.join(prefix, os.path.realpath(copy).lstrip(os.sep))

        def bytecode():
            found = [os.path.join(d, f) for top in (copy, mirror) for d, _dirs, files in os.walk(top)
                     for f in files if f.endswith(".pyc")]
            return found + [d for d, dirs, _files in os.walk(copy) if "__pycache__" in dirs]

        cp = subprocess.run(python + [os.path.join(copy, "agent-session"), "--help"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, env=self.environ())
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("agent-session 3.1.0", cp.stdout)
        self.assertEqual(bytecode(), [], "the entry sets dont_write_bytecode")
        cp = subprocess.run(python + ["-c", "import sys; sys.path.insert(0, sys.argv[1]); import agent_sessions.cli",
                                      copy], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, env=self.environ())
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue([p for p in bytecode() if p.endswith(".pyc")], "control import wrote no bytecode")

    def test_fast_subset(self):
        """tests/run.sh --fast runs only the quick classes and marks the run."""
        with open(os.path.join(os.path.dirname(SCRIPT), "..", "tests", "run.sh"), encoding="utf-8") as fh:
            runner = fh.read()
        self.assertIn("-k Cli -k Contract -k Parser -k Schema", runner)
        self.assertIn("export AGENT_SESSIONS_TEST_FAST=1", runner)
        self.assertIn('if [ "${1:-}" = "--fast" ]; then', runner, "run.sh must accept --fast first")
        if FAST:
            self.assertTrue(any(marker in self.__class__.__name__ for marker in ("Cli", "Contract", "Parser", "Schema")))

    def test_help_without_tmux(self):
        cp = self.run_as("--help", env={"PATH": self.no_tmux_path()})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        for sub in SUBCOMMANDS:
            self.assertIn(sub, cp.stdout)
        self.assertEqual(self.run_as("help").returncode, 0)
        self.assertEqual(self.run_as().returncode, 0)

    def test_list_without_tmux(self):
        self.fake_meta("ghost")
        cp = self.run_as("list", env={"PATH": self.no_tmux_path()})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("ghost", cp.stdout)
        self.assertIn("lost", cp.stdout)
        self.assertIn("tmux not found", cp.stderr)

    def test_doctor_reports_missing_tmux(self):
        # Without tmux the process backend is used: a warning, not a failure.
        cp = self.run_as("doctor", env={"PATH": self.no_tmux_path()})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        tmux_line = [l for l in cp.stdout.splitlines() if l.startswith("warn  tmux")]
        self.assertTrue(tmux_line, cp.stdout)
        self.assertTrue("brew install tmux" in tmux_line[0] or "apt install tmux" in tmux_line[0], tmux_line[0])
        self.assertIn("backend: process (tmux not found)", cp.stdout)
        # Forcing tmux makes its absence a failure.
        cp = self.run_as("doctor", "--backend", "tmux", env={"PATH": self.no_tmux_path()})
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        fail_lines = [l for l in cp.stdout.splitlines() if l.startswith("FAIL") and "tmux" in l]
        self.assertTrue(fail_lines, cp.stdout)

    def test_doctor_passes_with_stubs(self):
        cp = self.run_as("doctor")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        cp = self.run_as("doctor", "--json")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        report = json.loads(cp.stdout)
        for check, entry in report.items():
            self.assertIn(entry["status"], ("ok", "warn"), "%s: %r" % (check, entry))
        self.assertIn("claude", report)
        self.assertIn("codex", report)
        self.assertIn("tmux-session", report)
        self.assertEqual(self.run_as("doctor", "--engine", "claude").returncode, 0)

    def test_spawn_rejects_bad_engine_name_tier_timeout(self):
        cases = [
            (["Codex", "x", self.work, "hi"], "engine must be claude, codex or gemini"),
            (["claude", "a.b", self.work, "hi"], "name must match"),
            (["claude", "x", self.work, "hi", "--tier", "high"], "unknown tier 'high'"),
            (["claude", "x", self.work, "hi", "--timeout", "abc"], "--timeout"),
            (["claude", "x", self.work, "hi", "--timeout", "2"], "at least 5"),
            (["claude", "x", self.work, "hi", "--bogus"], "unknown option"),
            (["claude", "x", self.work, "hi", "--budget-usd", "nan"], "finite"),
            (["claude", "x", self.work, "hi", "--budget-usd", "inf"], "finite"),
            (["claude", "x", self.work, "hi", "--tree-budget-usd", "-inf"], "finite"),
        ]
        for args, message in cases:
            cp = self.run_as("spawn", *args)
            self.assertEqual(cp.returncode, 2, "%r: %s%s" % (args, cp.stdout, cp.stderr))
            self.assertIn(message, cp.stderr, args)
        dirs = [d for d in os.listdir(self.state) if not d.startswith(".")]
        self.assertEqual(dirs, [])

    def test_spawn_rejects_depth(self):
        cp = self.run_as("spawn", "claude", "x", self.work, "hi", env={"AGENT_SESSION_DEPTH": "2"})
        self.assertEqual(cp.returncode, 2)
        self.assertIn("depth", cp.stderr)
        self.assertFalse(os.path.exists(self.sdir("x")))

    def test_spawn_rejects_missing_dir_and_empty_prompt(self):
        cp = self.run_as("spawn", "claude", "x", os.path.join(self.work, "missing"), "hi")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no such directory", cp.stderr)
        cp = self.run_as("spawn", "claude", "x", self.work, "   \n")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("empty", cp.stderr)
        cp = self.run_as("spawn", "claude", "x", self.work)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("no prompt", cp.stderr)

    def test_spawn_timeout_suffixes(self):
        self.spawn("claude", "t1", "hi @@sleep:0", "--timeout", "30m")
        self.assertEqual(self.meta("t1")["timeout_s"], 1800)
        self.spawn("claude", "t2", "hi @@sleep:0", "--timeout", "2h")
        self.assertEqual(self.meta("t2")["timeout_s"], 7200)
        self.spawn("claude", "t3", "hi @@sleep:0", "--timeout", "45s")
        self.assertEqual(self.meta("t3")["timeout_s"], 45)

    def test_spawn_requires_one_prompt_source(self):
        pfile = os.path.join(self.box, "prompt.txt")
        with open(pfile, "w", encoding="utf-8") as fh:
            fh.write("from file @@sleep:0 with unicode: café\n")
        cp = self.run_as("spawn", "claude", "x", self.work, "positional", "--prompt-file", pfile)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("once", cp.stderr)
        self.spawn("claude", "f", "--prompt-file", pfile)
        with open(os.path.join(self.sdir("f"), "prompt.txt"), encoding="utf-8") as fh:
            self.assertIn("café", fh.read())
        self.ok("spawn", "claude", "s", self.work, "-", input="from stdin @@sleep:0\n")
        with open(os.path.join(self.sdir("s"), "prompt.txt"), encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith("from stdin"))

    def test_unknown_name_everywhere(self):
        commands = [["wait", "nope"], ["result", "nope"], ["send", "nope", "hi"], ["turn", "nope"],
                    ["stop", "nope"], ["kill", "nope"], ["log", "nope"], ["peek", "nope"], ["reap", "nope"],
                    ["wait", "nope", "--timeout", "30"]]
        for args in commands:
            start = time.time()
            cp = self.run_as(*args)
            elapsed = time.time() - start
            self.assertEqual(cp.returncode, 2, "%r: %s%s" % (args, cp.stdout, cp.stderr))
            self.assertIn("no session 'nope'", cp.stderr, args)
            # Fails fast: never the 240 s budget. The bound is loose because the
            # host may be running other suites at the same time.
            self.assertLess(elapsed, 5.0, args)
