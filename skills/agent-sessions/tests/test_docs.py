"""Documentation contract: help, commands.md and SKILL.md agree with the code;
no em dashes, private paths or GNU-only tools; the eval checker catches drift."""

import glob
import importlib.util
import os
import re
import subprocess
import sys
import unittest

from helpers import SCRIPT, SKILL

sys.path.insert(0, os.path.join(SKILL, "scripts"))
from agent_sessions import cli  # noqa: E402

COMMANDS_MD = os.path.join(SKILL, "references", "commands.md")
INTERNAL = ("_run", "_hook")
EM_DASH = chr(0x2014)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def skill_files():
    out = []
    for path in glob.glob(os.path.join(SKILL, "**", "*"), recursive=True):
        if os.path.isfile(path) and "__pycache__" not in path:
            out.append(path)
    return out


def description():
    text = read(os.path.join(SKILL, "SKILL.md"))
    front = text.split("---", 2)[1]
    m = re.search(r"^description: >\n((?:  .*\n)+)", front, re.M)
    if not m:
        m = re.search(r"^description: (.*)$", front, re.M)
    return " ".join(line.strip() for line in m.group(1).splitlines())


class DocsContractTests(unittest.TestCase):
    def test_help_lists_every_command_and_commands_md_covers_flags(self):
        cp = subprocess.run([sys.executable, "-I", SCRIPT, "--help"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        usage = set(re.findall(r"^\s+agent-session\s+([a-z][a-z-]*)", cp.stdout, re.M))
        listed = set(re.findall(r"^  ([a-z]+)\s+\S", cp.stdout, re.M))
        doc = read(COMMANDS_MD)
        doc_flags = set(re.findall(r"(?<![\w-])--([a-z][a-z0-9-]*)", doc))
        public = [c for c in cli.COMMANDS if c not in INTERNAL]
        self.assertEqual(sorted(public), sorted(cli.FLAGS), "FLAGS must have one entry per public command")
        for cmd in public:
            self.assertIn(cmd, usage, "%s missing from the usage block of --help" % cmd)
            self.assertIn(cmd, listed, "%s missing from the commands list of --help" % cmd)
            self.assertRegex(doc, r"(?m)^agent-session %s\b" % re.escape(cmd), "%s missing from the grammar" % cmd)
            self.assertRegex(doc, r"(?m)^### (?:%s\b|.*\b%s\b)" % (re.escape(cmd), re.escape(cmd)),
                             "%s has no subsection in commands.md" % cmd)
            for flag in cli.FLAGS[cmd]:
                self.assertIn(flag, doc_flags, "--%s (%s) is not documented in commands.md" % (flag, cmd))

    def test_description_length_and_no_em_dash(self):
        desc = description()
        self.assertLess(len(desc), 600, "description is %d characters" % len(desc))
        self.assertIn("Not for", desc[:300], "exclusions belong early in the description")
        text = read(os.path.join(SKILL, "SKILL.md"))
        self.assertIn("version: 3.1.0", text)
        lines = text.count("\n")
        # A ceiling only: every line of SKILL.md is loaded on each use, so a
        # shorter file that keeps the decisions is better, never a failure.
        self.assertLessEqual(lines, 190, "SKILL.md has %d lines; keep it at 190 or fewer" % lines)
        bad = [os.path.relpath(p, SKILL) for p in skill_files() if EM_DASH in read_lenient(p)]
        self.assertEqual(bad, [], "em dash (U+2014) in %s" % bad)

    def test_no_private_paths_or_gnu_tools(self):
        home = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^/\s'\"`]+/")
        leaks = [os.path.relpath(p, SKILL) for p in skill_files() if home.search(read_lenient(p))]
        self.assertEqual(leaks, [], "absolute home paths in %s" % leaks)
        # A GNU-only tool used as a command, not as an option name or a status.
        shell_cmd = re.compile(r"(?:^|[;&|(`]|\$\()\s*(?:g?timeout|setsid|mapfile|readarray)\b|declare -A|date -I")
        argv_cmd = re.compile(r"\[\s*[\"'](?:g?timeout|setsid|gdate|date)[\"']\s*,")
        found = []
        for path in glob.glob(os.path.join(SKILL, "tests", "*.sh")) + [SCRIPT]:
            for n, line in enumerate(read(path).splitlines(), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#!") else ""
                if shell_cmd.search(code):
                    found.append("%s:%d: %s" % (os.path.relpath(path, SKILL), n, line.strip()))
        for path in glob.glob(os.path.join(SKILL, "scripts", "agent_sessions", "*.py")) + [SCRIPT]:
            for n, line in enumerate(read(path).splitlines(), 1):
                if argv_cmd.search(line):
                    found.append("%s:%d: %s" % (os.path.relpath(path, SKILL), n, line.strip()))
        self.assertEqual(found, [])

    def test_check_scenarios_passes_and_catches_drift(self):
        checker = os.path.join(SKILL, "evals", "check_scenarios.py")
        cp = subprocess.run([sys.executable, checker], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        spec = importlib.util.spec_from_file_location("check_scenarios", checker)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        problems = []
        mod.check_references([{"id": "x", "ideal": ["`agent-session frobnicate a --no-such-flag`"]}], problems)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("frobnicate", problems[0])
        self.assertIn("--no-such-flag", problems[1])
        problems = []
        mod.check_scenarios({"scenarios": [{"id": "Bad Id"}]}, problems)
        self.assertTrue(any("expected 24" in p for p in problems), problems)
        self.assertTrue(any("missing keys" in p for p in problems), problems)

    def test_reference_pointers_exist(self):
        text = read(os.path.join(SKILL, "SKILL.md"))
        for ref in sorted(set(re.findall(r"references/[a-z-]+\.md", text))):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, ref)), ref)
        for name in ("commands", "prompts", "schema", "reliability", "review", "pipelines", "isolation", "dialogue",
                     "scope"):
            self.assertIn("references/%s.md" % name, text, "SKILL.md does not point to %s.md" % name)


def read_lenient(path):
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


if __name__ == "__main__":
    unittest.main()
