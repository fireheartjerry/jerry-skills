"""help, doctor, validation, list without tmux."""

import json
import os
import shutil
import sys
import time

from helpers import FAKE_DIR, SessionTest

SUBCOMMANDS = ["spawn", "list", "wait", "result", "send", "turn", "log", "peek", "stop", "kill", "reap", "doctor"]


class CliTests(SessionTest):
    def restricted_path(self, tmux=False, engines=("claude", "codex")):
        """A PATH with python3, ps, git, bash and the chosen fake engines."""
        bindir = os.path.join(self.box, "bin-%s-%s" % ("tmux" if tmux else "notmux", "-".join(engines) or "none"))
        if os.path.isdir(bindir):
            return bindir
        os.makedirs(bindir)
        tools = ["ps", "git", "sh", "sleep", "uname"] + (["tmux"] if tmux else [])
        for tool in tools:
            found = shutil.which(tool)
            if found:
                os.symlink(found, os.path.join(bindir, tool))
        # find_binary falls back to `bash -lc 'command -v ...'`, and a login shell
        # would restore the developer's PATH. This stand-in keeps the test PATH.
        with open(os.path.join(bindir, "bash"), "w") as fh:
            fh.write('#!/bin/sh\nif [ "$1" = -lc ]; then shift; exec /bin/sh -c "$@"; fi\nexec /bin/bash "$@"\n')
        os.chmod(os.path.join(bindir, "bash"), 0o755)
        os.symlink(sys.executable, os.path.join(bindir, "python3"))
        for engine in engines:
            os.symlink(os.path.join(FAKE_DIR, engine), os.path.join(bindir, engine))
        return bindir

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
        cp = self.run_as("doctor", env={"PATH": self.no_tmux_path()})
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        fail_lines = [l for l in cp.stdout.splitlines() if l.startswith("FAIL")]
        self.assertTrue(fail_lines, cp.stdout)
        tmux_line = [l for l in fail_lines if "tmux" in l]
        self.assertTrue(tmux_line, cp.stdout)
        self.assertTrue("brew install tmux" in tmux_line[0] or "apt install tmux" in tmux_line[0], tmux_line[0])

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
            (["Codex", "x", self.work, "hi"], "engine must be claude or codex"),
            (["claude", "a.b", self.work, "hi"], "name must match"),
            (["claude", "x", self.work, "hi", "--tier", "high"], "unknown tier 'high'"),
            (["claude", "x", self.work, "hi", "--timeout", "abc"], "--timeout"),
            (["claude", "x", self.work, "hi", "--timeout", "2"], "at least 5"),
            (["claude", "x", self.work, "hi", "--bogus"], "unknown option"),
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
            self.assertLess(elapsed, 2.0, args)
