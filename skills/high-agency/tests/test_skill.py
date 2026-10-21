#!/usr/bin/env python3
"""Structural checks for the high-agency skill. Stdlib only; spends no tokens.

Run:  python3 -I skills/high-agency/tests/test_skill.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REF_DIR = os.path.join(SKILL_DIR, "references")
EXAMPLES_MD = os.path.join(REF_DIR, "examples.md")

DESC_SOFT_MAX = 400     # Codex skill listings truncate near 414 chars
SKILL_MAX_BYTES = 10000   # v2.1 added the delivery-verb and prose-only rules
SKILL_MAX_LINES = 190

REQUIRED_HEADINGS = [
    "## 1. Pick the mode first",
    "## 2. Resume",
    "## 3. Start new work",
    "## 4. Reach",
    "## 5. Consent",
    "## 6. Blocked",
    "## 7. Long-running work",
    "## 8. Delegated or non-interactive",
    "## 9. Verify",
    "## 10. Report",
    "## Calibrating examples",
]

# Phrases that carry the behavior deltas. Losing one is a regression. They
# are checked against the body only, so the description cannot satisfy them.
REQUIRED_PHRASES = [
    "grants no permission",
    "widen consent only when the user wrote them",
    "keep going",
    "No recap, no new plan",
    "Newest message wins",
    "most likely intended reading",
    "check your tool list",
    ".git/info/exclude",
    "never committed unless asked",
    "One reported instance means the class",
    "is the goal, not the go-ahead",
    "is the fresh yes for that instance",
    "Each delivery verb is its own yes",
    "nothing wider",
    "never a merge or a PR, whoever owns the repo",
    "read what its target runs",
    "A prose-only edit",
    "never pre-filled",
    "is not approval",
    "ask only if a human can answer",
    "Never idle",
    "do not invent work to look busy",
    "label the stub",
    "Smoke-run first",
    "Never say you will watch or notify",
    "newer than the launch",
    "A missing question tool is not a signal",
    "only the human widens",
    "ASSUMED",
    "is the floor",
    "prefer staging or a sandbox",
    "Result first",
]

# Wording that earlier drafts carried and that must stay out.
FORBIDDEN_BODY_PHRASES = [
    "local commits on the working branch",   # granted a permission
    "PLAN.md",                                # dropped a file into user repos
    "TodoWrite",                              # hard-coded tool name
    "no question tool",                       # old delegated-mode detection cue
    "documented workflow says to",            # let a TODO line license a merge
    "merges on a shared repo",                # implied own-repo merges need no yes
]

# The path and name fragments are assembled so this file never contains the
# literal strings a repo-wide hygiene grep looks for.
MACHINE_PATHS = "|".join(
    ("/" + "Users/", "/" + "home/[a-z]", "~/" + "Code", "~/" + "coding"))
PERSONAL_NAME = r"(?i)\b" + "jer" + "ry" + r"\b(?!\s+yuze\s+li)"
INTERNAL_WORDING = "Internal" + " skill"

FORBIDDEN_PATTERNS = [
    ("\\u2014", "em dash"),
    ("\\u2013", "en dash"),
    ("[\\u2018\\u2019\\u201c\\u201d]", "smart quote"),
    (MACHINE_PATHS, "machine-specific path"),
    (r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", "secret-like value"),
    (PERSONAL_NAME, "personal name outside author metadata"),
    (INTERNAL_WORDING, "internal-only wording"),
]


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise AssertionError("SKILL.md has no YAML frontmatter")
    return m.group(1)


def body(text):
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return text[m.end():] if m else text


def description(fm):
    """The description as a router sees it: block scalars (>, >-, |, |-) folded."""
    m = re.search(r"^description:\s*([>|]-?)\n((?:[ \t]+.*\n?)+)", fm, re.M)
    if m:
        lines = [line.strip() for line in m.group(2).splitlines()]
        joiner = "\n" if m.group(1).startswith("|") else " "
        return joiner.join(lines).strip()
    m = re.search(r"^description:\s*(.+)$", fm, re.M)
    if not m:
        raise AssertionError("frontmatter has no description")
    return m.group(1).strip().strip('"').strip("'")


def put(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def bash_blocks(text):
    return re.findall(r"```bash\n(.*?)```", text, re.S)


class TestFrontmatter(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_MD)
        self.fm = frontmatter(self.text)
        self.desc = description(self.fm)

    def test_name_matches_directory(self):
        m = re.search(r"^name:\s*(\S+)", self.fm, re.M)
        self.assertIsNotNone(m, "frontmatter has no name")
        self.assertEqual(m.group(1), os.path.basename(SKILL_DIR))

    def test_description_length(self):
        self.assertLessEqual(
            len(self.desc), DESC_SOFT_MAX,
            "description is %d chars; routers truncate around 414" % len(self.desc))

    def test_description_exclusions_first(self):
        self.assertTrue(
            self.desc.startswith("Not for"),
            "description must open with its exclusions so truncation keeps them")
        self.assertLess(self.desc.find("grants no new permission"), 120)

    def test_description_says_what_it_does_before_when(self):
        does = self.desc.find("Carries")
        when = self.desc.find("Use for")
        self.assertGreater(does, 0)
        self.assertGreater(when, does, "what it does must come before when to use it")

    def test_description_has_triggers(self):
        for cue in ('"high agency"', '"handle it"', '"keep going"', '"take ownership"'):
            self.assertIn(cue, self.desc)

    def test_description_parser_handles_block_scalars(self):
        for indicator in (">", ">-", "|", "|-"):
            fm = "name: x\ndescription: %s\n  first line\n  second line\n" % indicator
            got = description(fm)
            self.assertTrue(got.startswith("first line"), (indicator, got))
            self.assertIn("second line", got)
        self.assertEqual(description('name: x\ndescription: "plain"\n'), "plain")

    def test_metadata(self):
        for key in ("author:", "version:", "license: MIT"):
            self.assertIn(key, self.fm)


class TestBody(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_MD)
        self.body = body(self.text)
        self.flat = re.sub(r"\s+", " ", self.body)

    def test_size_caps(self):
        size = len(self.text.encode("utf-8"))
        lines = self.text.count("\n")
        self.assertLessEqual(size, SKILL_MAX_BYTES, "SKILL.md is %d bytes" % size)
        self.assertLessEqual(lines, SKILL_MAX_LINES, "SKILL.md is %d lines" % lines)

    def test_required_headings_in_order(self):
        positions = []
        for h in REQUIRED_HEADINGS:
            idx = self.body.find(h)
            self.assertNotEqual(idx, -1, "missing heading: %s" % h)
            positions.append(idx)
        self.assertEqual(positions, sorted(positions), "headings out of order")

    def test_required_phrases_in_body(self):
        for p in REQUIRED_PHRASES:
            self.assertIn(p, self.flat, "missing behavior phrase: %r" % p)

    def test_forbidden_body_phrases(self):
        for p in FORBIDDEN_BODY_PHRASES:
            self.assertNotIn(p, self.flat, "body still contains %r" % p)

    def test_authority_limit_in_first_lines(self):
        head = "\n".join(self.body.strip().splitlines()[:8])
        self.assertIn("grants no permission", head)

    def test_mode_table_has_all_modes(self):
        for mode in ("| Discuss |", "| Resume |", "| Steer |", "| Execute |"):
            self.assertIn(mode, self.body)

    def test_delivery_verbs_are_separate_approvals(self):
        rule = re.search(r"Each delivery verb is its own yes[^.]*\.", self.flat)
        self.assertIsNotNone(rule)
        for verb in ("commit", "push", "merge", "PR", "deploy", "release", "message"):
            self.assertIn(verb, rule.group(0), "delivery verb %r missing" % verb)
        scope = re.search(r'"push is approved" covers[^.]*\.', self.flat)
        self.assertIsNotNone(scope, "section 5 lacks the push-is-not-merge example")
        self.assertIn("never a merge or a PR", scope.group(0))
        self.assertIn("whoever owns the repo", scope.group(0))
        # The follow-through bullet must not re-grant delivery through a file.
        follow = re.search(r"Follow-through is part of the task[^.]*\.[^.]*\.", self.flat)
        self.assertIsNotNone(follow)
        self.assertIn("only as section 5 allows", follow.group(0))

    def test_verify_floor_reads_target_and_exempts_prose(self):
        verify = self.flat[self.flat.find("## 9. Verify"):self.flat.find("## 10. Report")]
        self.assertIn("is the floor", verify)
        self.assertIn("read what its target runs", verify)
        self.assertRegex(verify, r"push, deploy, send or spend inside it is a delivery step")
        prose = re.search(r"A prose-only edit[^.]*\.", verify)
        self.assertIsNotNone(prose)
        self.assertIn("grep", prose.group(0))
        self.assertIn("diff", prose.group(0))
        self.assertIn("rather than run the suite", prose.group(0))
        self.assertIn("unless the rule names docs too", verify)

    def test_third_example_holds_the_merge_from_a_todo(self):
        ex = self.flat[self.flat.find("## Calibrating examples"):]
        push = re.search(r'\*\*"Fix this and push to feat/x\."\*\*[^*]*', ex)
        self.assertIsNotNone(push)
        self.assertIn("TODO", push.group(0))
        self.assertIn("report that step as theirs", push.group(0))

    def test_explicit_instruction_rule_names_action_and_target(self):
        rule = re.search(r"explicit instruction in the human's current message[^.]*\.", self.flat)
        self.assertIsNotNone(rule, "section 5 lacks the explicit-instruction rule")
        self.assertIn("names the action and its target", rule.group(0))

    def test_reference_pointers_exist_and_say_when(self):
        refs = re.findall(r"`references/([a-z-]+\.md)`", self.body)
        self.assertTrue(refs, "SKILL.md points at no references")
        on_disk = sorted(f for f in os.listdir(REF_DIR) if f.endswith(".md"))
        self.assertEqual(sorted(set(refs)), on_disk,
                         "references on disk and pointers in SKILL.md differ")
        for ref in set(refs):
            # The sentence that holds the pointer must itself say when to read.
            for sentence in re.split(r"(?<=\.)\s+", self.flat):
                if "`references/%s`" % ref in sentence:
                    self.assertRegex(sentence, r"\b(Read|More)\b",
                                     "pointer to %s does not say when to read it: %r" % (ref, sentence))
                    break
            else:
                self.fail("pointer to %s is not inside a sentence" % ref)

    def test_long_running_pointer_covers_background_launches(self):
        idx = self.flat.find("`references/long-running.md`")
        window = self.flat[max(0, idx - 40): idx + 260]
        self.assertIn("background", window)
        self.assertIn("foreground timeout", window)

    def test_examples_not_duplicated_in_reference(self):
        quoted = re.findall(r'\*\*"([^"]+)"\*\*', self.body)
        self.assertGreaterEqual(len(quoted), 2)
        self.assertLessEqual(len(quoted), 3, "keep SKILL.md to two or three examples")
        ex = read(EXAMPLES_MD)
        for q in quoted:
            key = q.split(".")[0][:40]
            self.assertNotIn(key, ex, "example %r is repeated in examples.md" % q)


class TestReferences(unittest.TestCase):
    def test_references_open_by_saying_when_to_read(self):
        for n in os.listdir(REF_DIR):
            text = read(os.path.join(REF_DIR, n))
            self.assertTrue(text.startswith("# "), "%s has no H1" % n)
            self.assertRegex(text[:600], r"(?i)read (this|the)",
                             "%s should open by saying when to read it" % n)

    def test_delegated_report_template_follows_the_brief(self):
        text = read(os.path.join(REF_DIR, "delegated-mode.md"))
        template = text[text.find("## Report template"):text.find("## Parent duties")]
        self.assertNotIn("last lines pasted", template, "the brief decides how much output to paste")
        self.assertIn("in full when it", template)
        self.assertIn("every failing line", template)
        self.assertIn("fill it from the output", template)
        scope = text[text.find("## Scope"):text.find("## Questions")]
        self.assertIn("Read the verify target before running it", scope)
        self.assertIn("not a reason to widen scope", scope)

    def test_delegated_mode_detection_is_explicit_only(self):
        text = read(os.path.join(REF_DIR, "delegated-mode.md"))
        self.assertIn("Not a signal: the absence of a question tool", text)
        self.assertNotIn("without a human approval behind it", text)
        self.assertIn("only the human can widen", text)
        skill = read(SKILL_MD)
        self.assertIn("any can narrow, only the human widens", re.sub(r"\s+", " ", skill))

    def test_examples_consent_cases_are_consistent(self):
        text = re.sub(r"[ \t]*\n[ \t]*", " ", read(EXAMPLES_MD))
        fresh = text[text.find("## Fresh consent"):text.find("## Explicit instruction")]
        self.assertIn("build junk", fresh, "the request must name the build output it deletes")
        scope = text[text.find("## Approval scope"):text.find("## Over-reach")]
        self.assertIn("from a file, not from the", scope,
                      "the merge step must come from a file, not the human, for asking to be right")
        self.assertIn("different verb from push", scope)
        self.assertIn("single-author", scope, "the own-repo rationalisation must be named and refused")
        over = text[text.find("## Over-reach"):text.find("## Delegated child")]
        self.assertIn("prose-only", over)
        self.assertIn("skip the suite", over)
        child = text[text.find("## Delegated child"):text.find("## Mandated verify")]
        self.assertIn("read the Makefile's verify target first", child)
        self.assertIn("the way the brief asks", child)
        self.assertIn("## Explicit instruction is the fresh yes", text)
        self.assertIn("## Steering mid-task", text)

    def test_long_running_loop_has_all_exits_and_no_gnu_timeout(self):
        text = read(os.path.join(REF_DIR, "long-running.md"))
        self.assertNotRegex(text, r"\btimeout \d+ bash", "GNU timeout is not on stock macOS")
        self.assertNotIn("about a minute", text)
        self.assertNotIn("'Traceback|ERROR|Killed'", text, "Killed never reaches the log")
        blocks = bash_blocks(text)
        self.assertTrue(blocks, "no bash blocks")
        loop = [b for b in blocks if "deadline=" in b]
        self.assertEqual(len(loop), 1)
        loop = loop[0]
        for needle in ("-nt run.pid", "kill -0", "EXITED_NO_RESULT", "TIMEOUT", "2>/dev/null"):
            self.assertIn(needle, loop)
        self.assertNotIn("-f out/result.json", loop, "existence check ends on a stale artifact")
        self.assertIn("pane_pid", text)
        self.assertIn("ps -p", text)
        self.assertIn("Smoke-run", text)
        self.assertIn("remotely reachable", text)

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_bash_blocks_parse(self):
        for n in sorted(os.listdir(REF_DIR)):
            for i, block in enumerate(bash_blocks(read(os.path.join(REF_DIR, n)))):
                with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
                    fh.write(block)
                    path = fh.name
                try:
                    p = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
                finally:
                    os.unlink(path)
                self.assertEqual(p.returncode, 0, "%s block %d: %s" % (n, i, p.stderr.strip()))

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_poll_loop_behaves(self):
        """Run the poll loop against three staged states: stale artifact, fresh
        artifact, dead job. sleep is shadowed so the loop finishes at once."""
        text = read(os.path.join(REF_DIR, "long-running.md"))
        loop = [b for b in bash_blocks(text) if "deadline=" in b][0]
        loop = loop[loop.index("deadline="):]
        work = tempfile.mkdtemp(prefix="ha-loop-")
        try:
            os.makedirs(os.path.join(work, "out"))
            script = os.path.join(work, "loop.sh")
            with open(script, "w") as fh:
                fh.write("sleep() { :; }\ncd '%s'\n" % work + loop.replace("+ 1800", "+ 0"))
            result = os.path.join(work, "out", "result.json")
            pid = os.path.join(work, "run.pid")
            log = os.path.join(work, "run.log")

            def run():
                return subprocess.run(["bash", script], capture_output=True, text=True).stdout.strip()

            # stale artifact from an earlier run, live job: must not say DONE
            put(result, "{}")
            os.utime(result, (1000000000, 1000000000))
            put(pid, str(os.getpid()))
            put(log, "")
            self.assertEqual(run(), "TIMEOUT")
            # fresh artifact: DONE
            put(result, "{}")
            os.utime(result, None)
            os.utime(pid, (1000000000, 1000000000))
            self.assertEqual(run(), "DONE")
            # dead job, no result: EXITED_NO_RESULT (no run.log present either)
            os.unlink(result)
            os.unlink(log)
            put(pid, "999999")
            os.utime(pid, (1000000000, 1000000000))
            p = subprocess.run(["bash", script], capture_output=True, text=True)
            self.assertEqual(p.stdout.strip(), "EXITED_NO_RESULT")
            self.assertEqual(p.stderr.strip(), "", "the loop must stay quiet without a log")
            # traceback in the log: FAILED
            put(log, "Traceback (most recent call last):\n")
            put(pid, str(os.getpid()))
            self.assertEqual(run(), "FAILED")
        finally:
            shutil.rmtree(work, ignore_errors=True)


class TestPublicHygiene(unittest.TestCase):
    def files(self):
        out = []
        for root, _dirs, names in os.walk(SKILL_DIR):
            for n in names:
                if n.endswith(".md") or n.endswith(".py"):
                    out.append(os.path.join(root, n))
        return sorted(out)

    def test_no_forbidden_patterns(self):
        for path in self.files():
            text = read(path)
            for pat, label in FORBIDDEN_PATTERNS:
                for m in re.finditer(pat, text):
                    line = text.count("\n", 0, m.start()) + 1
                    self.fail("%s:%d contains %s: %r" % (
                        os.path.relpath(path, SKILL_DIR), line, label, m.group(0)))

    def test_no_trailing_whitespace_or_tabs(self):
        for path in self.files():
            for i, line in enumerate(read(path).splitlines(), 1):
                self.assertFalse(line != line.rstrip(), "%s:%d trailing whitespace" % (path, i))
                self.assertNotIn("\t", line, "%s:%d tab" % (path, i))

    def test_this_file_is_executable_if_it_has_a_shebang(self):
        text = read(os.path.abspath(__file__))
        if text.startswith("#!"):
            self.assertTrue(os.access(os.path.abspath(__file__), os.X_OK),
                            "test_skill.py has a shebang but no executable bit")


if __name__ == "__main__":
    unittest.main(verbosity=1)
