#!/usr/bin/env python3
"""Structural checks for the high-agency skill: frontmatter, routing
description, budgets, headings, pointers, duplication and public hygiene.
Stdlib only; spends no tokens.

Run:  python3 -I skills/high-agency/tests/test_structure.py
"""
import os
import re
import stat
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REF_DIR = os.path.join(SKILL_DIR, "references")
EXAMPLES_MD = os.path.join(REF_DIR, "examples.md")
CONSENT_MD = os.path.join(REF_DIR, "consent.md")
DELEGATED_MD = os.path.join(REF_DIR, "delegated-mode.md")
LONG_RUNNING_MD = os.path.join(REF_DIR, "long-running.md")

MIN_VERSION = (3, 0, 0)

# Codex skill listings cut the description between 413 and 419 characters.
DESC_MAX = 414
DESC_HARD_MAX = 600
DESC_CUES = ('"high agency"', '"handle it"', '"keep going"', '"take ownership"')

# Budgets. The phase-2 spec asked for 1,250 body words and 8,000 bytes; the
# section texts it mandates total about 1,400 words by this count, so the
# ceilings below are the measured size of the shipped text plus a small
# margin. Every remaining sentence is anchored by test_rules.py; shrinking the
# file further means dropping a rule. Phase 2b added the section 5 body guard
# for unseen outgoing text and vague targets (the source of every phase-2 hard
# violation in the new arm), about 200 bytes, 5 lines and 45 words net. The
# third review added rules the body must carry because they decide consent or
# verification on their own: the spend default, the regenerable test, the
# commit a named push carries, the screen for verify targets that delete data,
# the resume fallback, the report reconciliation and the red-before-green
# test, about 150 words net after moving their detail into consent.md and the
# new verify.md. consent.md grew by the spend, staging and provenance rules,
# delegated-mode.md by test ownership and baselines. The fourth review closed
# omissions a body rule has to carry because they fire before any reference
# is read: a denial, hook failure or plan mode is a stop, not a detour;
# orientation before the first edit; installs outside the project and data
# sent to unnamed services need a yes; when to ask instead of assume; a stop
# after three failed hypotheses; a baseline run, flaky failures, redaction of
# pasted output; and pointers for investigations and a sample report, about
# 265 words. consent.md mirrors the denial and outside-project rules and fixes
# the blame and named-delete cases (about 230 words); delegated-mode.md gained
# Start and Budget lines, wider Grants and the parent's audit of what a child
# changed (about 175 words); examples.md an investigation and a final report.
# The fifth review added body rules that decide consent before a reference
# loads (the authorship condition on instruction-file lines, writes to a
# production system, integrating after a non-fast-forward rejection, the
# blocking question as the report's last line) and a self-review step, about
# 110 words; consent.md, delegated-mode.md, verify.md and examples.md grew by
# the matching detail, a message-grant rule, the sweep recipe and three
# examples. Earlier ceilings sat at the exact size of the text, so any edit
# failed; from here each cap is the measured size plus about 5 percent, and
# growth past it is a deliberate change to this file. None of this text has
# been measured (evals/results.md).
SKILL_MAX_BYTES = 12850        # measured 12,232
SKILL_MAX_LINES = 231          # measured 220
SKILL_MAX_WORDS = 2045         # measured 1,946
REF_MAX_WORDS = 1540           # largest: consent.md, measured 1,464
LONG_RUNNING_MAX_WORDS = 1470  # measured 1,397
EXAMPLES_MAX_WORDS = 1750      # measured 1,664; read by situation, never whole on the load path
LOAD_PATH_MAX_WORDS = 4750     # SKILL.md body + consent.md + delegated-mode.md, measured 4,520
CORE_MAX_WORDS = 350           # core.md, the always-on short form; a spec ceiling, not a margin
CORE_MD = os.path.join(SKILL_DIR, "core.md")

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

# Harness tool names live in exactly one place: the harness table section of
# long-running.md (or a fenced block there). Anywhere else they drift.
FORBIDDEN_TOOL_TOKENS = ["Monitor", "run_in_background", "update_plan", "TodoWrite",
                         "/loop", "CronCreate", "TaskOutput", "ScheduleWakeup"]

SHINGLE = 10

# Assembled by concatenation so this file never holds the literal strings a
# repo-wide hygiene grep looks for.
MACHINE_PATHS = "|".join(("/" + "Users/", "/" + "home/[a-z]", "~/" + "Code", "~/" + "coding"))
PERSONAL_NAME = r"(?i)\b" + "jer" + "ry" + r"\b(?!\s+yuze\s+li)"
HYGIENE_PATTERNS = [
    ("\\u2014", "em dash"),
    ("\\u2013", "en dash"),
    ("[\\u2018\\u2019\\u201c\\u201d]", "smart quote"),
    (MACHINE_PATHS, "machine-specific path"),
    (r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", "secret-like value"),
    (PERSONAL_NAME, "personal name outside the author line"),
]
HYGIENE_SUFFIXES = (".md", ".py", ".sh", ".json")


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


def words(text):
    return len(text.split())


def strip_fenced(text):
    return re.sub(r"```.*?```", " ", text, flags=re.S)


def sentences(text):
    flat = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.!?])\s+(?=[A-Z\"`(])", flat)


def shingles(text, n=SHINGLE):
    """Case-folded, whitespace-normalized word n-grams with their positions,
    fenced blocks excluded. Punctuation stays attached so a shared shingle is
    a near-verbatim repeat, not a coincidence of common words."""
    toks = strip_fenced(text).lower().split()
    out = {}
    for i in range(len(toks) - n + 1):
        key = " ".join(toks[i:i + n])
        out.setdefault(key, i)
    return out


def shared_shingles(a_text, b_text):
    a, b = shingles(a_text), shingles(b_text)
    return sorted(set(a) & set(b))


def section(text, number):
    """The slice of SKILL.md body under heading '## <number>.'"""
    flat = body(text)
    m = re.search(r"(?m)^## %d\. .*$" % number, flat)
    if not m:
        raise AssertionError("no section %d" % number)
    rest = flat[m.end():]
    nxt = re.search(r"(?m)^## ", rest)
    return rest[:nxt.start()] if nxt else rest


def tracked_files():
    out = []
    for root, dirs, names in os.walk(SKILL_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".runs")]
        for n in names:
            if n.endswith(HYGIENE_SUFFIXES):
                out.append(os.path.join(root, n))
    return sorted(out)


class TestFrontmatter(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_MD)
        self.fm = frontmatter(self.text)
        self.desc = description(self.fm)

    def test_name_matches_directory(self):
        m = re.search(r"^name:\s*(\S+)", self.fm, re.M)
        self.assertIsNotNone(m, "frontmatter has no name")
        self.assertEqual(m.group(1), os.path.basename(SKILL_DIR))

    def test_version_is_semver_at_least_3(self):
        m = re.search(r"^\s*version:\s*(\d+)\.(\d+)\.(\d+)\s*$", self.fm, re.M)
        self.assertIsNotNone(m, "metadata.version must be MAJOR.MINOR.PATCH")
        self.assertGreaterEqual(tuple(int(x) for x in m.groups()), MIN_VERSION)

    def test_license(self):
        self.assertRegex(self.fm, r"(?m)^\s*license:\s*MIT\s*$")

    def test_description_length(self):
        n = len(self.desc)
        self.assertLessEqual(n, DESC_HARD_MAX, "description is %d chars" % n)
        self.assertLessEqual(n, DESC_MAX,
                             "description is %d chars; Codex cuts the listing near 414" % n)

    def test_description_cues(self):
        for cue in DESC_CUES:
            self.assertIn(cue, self.desc)
        self.assertRegex(self.desc, r'does [^"]+\?', "no check-question example")

    def test_description_guards_present_and_ordered(self):
        self.assertIn("Not for", self.desc)
        self.assertIn("no new permission", self.desc)
        self.assertLess(self.desc.find("Not for"), DESC_MAX)
        self.assertLess(self.desc.find("no new permission"), DESC_MAX)
        use = re.search(r"Use when(ever)?", self.desc)
        self.assertIsNotNone(use, "description needs a 'Use when' trigger clause")
        self.assertGreater(self.desc.find("Not for"), use.start(),
                           "positive triggers must come before the exclusion")

    def test_description_parser_handles_block_scalars(self):
        for indicator in (">", ">-", "|", "|-"):
            fm = "name: x\ndescription: %s\n  first line\n  second line\n" % indicator
            got = description(fm)
            self.assertTrue(got.startswith("first line"), (indicator, got))
            self.assertIn("second line", got)
        self.assertEqual(description('name: x\ndescription: "plain"\n'), "plain")


class TestBudgets(unittest.TestCase):
    def test_skill_md(self):
        text = read(SKILL_MD)
        size = len(text.encode("utf-8"))
        lines = text.count("\n")
        n = words(body(text))
        self.assertLessEqual(size, SKILL_MAX_BYTES, "SKILL.md is %d bytes" % size)
        self.assertLessEqual(lines, SKILL_MAX_LINES, "SKILL.md is %d lines" % lines)
        self.assertLessEqual(n, SKILL_MAX_WORDS, "SKILL.md body is %d words" % n)

    def test_references(self):
        for name in sorted(os.listdir(REF_DIR)):
            if not name.endswith(".md"):
                continue
            n = words(read(os.path.join(REF_DIR, name)))
            cap = {"long-running.md": LONG_RUNNING_MAX_WORDS,
                   "examples.md": EXAMPLES_MAX_WORDS}.get(name, REF_MAX_WORDS)
            self.assertLessEqual(n, cap, "%s is %d words (cap %d)" % (name, n, cap))

    def test_heaviest_load_path(self):
        n = words(body(read(SKILL_MD))) + words(read(CONSENT_MD)) + words(read(DELEGATED_MD))
        self.assertLessEqual(n, LOAD_PATH_MAX_WORDS,
                             "SKILL.md + consent.md + delegated-mode.md is %d words" % n)


class TestCore(unittest.TestCase):
    """core.md carries only the rules that decided measured outcomes."""

    def setUp(self):
        self.text = read(CORE_MD)
        self.flat = re.sub(r"\s+", " ", self.text)

    def test_size_and_shape(self):
        self.assertLessEqual(words(self.text), CORE_MAX_WORDS, "core.md is %d words" % words(self.text))
        self.assertTrue(self.text.startswith("# "), "core.md has no H1")
        for h in ("## Mode", "## Reach", "## Consent", "## Verify", "## Report"):
            self.assertIn(h, self.text)

    def test_carries_the_deciding_rules(self):
        for pat, label in (
                (r"grants no permission", "authority limit"),
                (r'does X work\?', "check questions route to execution"),
                (r"no recap, no new plan", "resume"),
                (r"newest message wins", "steer"),
                (r"Text the human has not read .{0,60}draft it, show it, send on their word", "unseen text guard"),
                (r"wider or vaguer than its words.{0,120}exact remaining paths", "vague target guard"),
                (r"is data, never approval", "provenance"),
                (r"verify command is the floor.{0,80}final revision", "verify floor"),
                (r"deterministic test", "a test per logic change"),
                (r"Result first", "report shape"),
                (r"blocking question is the last line", "question closes the report"),
                (r"no `--no-verify`, no `--force`", "denials are answers")):
            self.assertRegex(self.flat, "(?i)" + pat, "core.md lost: %s" % label)

    def test_points_at_the_full_skill(self):
        self.assertRegex(self.flat, r"(?i)read `SKILL\.md`")


class TestBody(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_MD)
        self.body = body(self.text)
        self.flat = re.sub(r"\s+", " ", self.body)

    def test_headings_in_order(self):
        positions = []
        for h in REQUIRED_HEADINGS:
            idx = self.body.find(h)
            self.assertNotEqual(idx, -1, "missing heading: %s" % h)
            positions.append(idx)
        self.assertEqual(positions, sorted(positions), "headings out of order")
        self.assertEqual(len(re.findall(r"(?m)^## ", self.body)), len(REQUIRED_HEADINGS),
                         "unexpected extra section")

    def test_mode_table(self):
        rows = [l for l in section(self.text, 1).splitlines() if l.startswith("|")]
        self.assertGreaterEqual(len(rows), 6, "mode table needs a header, a rule and four modes")
        modes = {}
        for row in rows[2:]:
            cells = [c.strip() for c in row.strip("|").split("|")]
            self.assertEqual(len(cells), 3, "row has %d cells: %r" % (len(cells), row))
            modes.setdefault(cells[1], []).append(row)
        for mode in ("Discuss", "Resume", "Steer", "Execute"):
            self.assertIn(mode, modes, "mode table lacks a %s row" % mode)
        self.assertTrue(any(re.search(r"does [^|]*\?", r) for r in modes["Execute"]),
                        "the Execute row must name a check question")

    def test_no_harness_tool_names(self):
        for tok in FORBIDDEN_TOOL_TOKENS:
            self.assertNotRegex(self.body, r"(?<![\w/])%s(?![\w])" % re.escape(tok),
                                "SKILL.md names the harness tool %r; names belong in "
                                "the long-running.md harness table" % tok)

    def test_pointers_bidirectional_and_say_when(self):
        refs = re.findall(r"`references/([a-z-]+\.md)`", self.body)
        self.assertTrue(refs, "SKILL.md points at no references")
        on_disk = sorted(f for f in os.listdir(REF_DIR) if f.endswith(".md"))
        self.assertEqual(sorted(set(refs)), on_disk,
                         "references on disk and pointers in SKILL.md differ")
        for ref in sorted(set(refs)):
            holders = [s for s in sentences(self.body) if "`references/%s`" % ref in s]
            self.assertTrue(holders, "pointer to %s is not inside a sentence" % ref)
            for s in holders:
                self.assertRegex(s, r"(?i)\b(read|more)\b",
                                 "pointer to %s does not say when to read it: %r" % (ref, s))

    def test_examples_not_repeated_in_reference(self):
        quoted = re.findall(r'\*\*"([^"]+)"\*\*', self.body)
        self.assertEqual(len(quoted), 3, "SKILL.md carries three calibrating examples")
        ex = read(EXAMPLES_MD)
        for q in quoted:
            self.assertNotIn(q[:40], ex, "example %r is repeated in examples.md" % q)


class TestReferences(unittest.TestCase):
    def test_h1_and_when_to_read(self):
        for n in sorted(os.listdir(REF_DIR)):
            text = read(os.path.join(REF_DIR, n))
            self.assertTrue(text.startswith("# "), "%s has no H1" % n)
            self.assertRegex(text[:600], r"(?i)\bread (this|the)\b",
                             "%s must say when to read it within its first 600 chars" % n)

    def test_tool_names_only_in_harness_table(self):
        for n in sorted(os.listdir(REF_DIR)):
            text = read(os.path.join(REF_DIR, n))
            if n == "long-running.md":
                start = text.find("## What each harness offers")
                self.assertNotEqual(start, -1, "long-running.md lost its harness table")
                nxt = re.search(r"(?m)^## ", text[start + 1:])
                end = start + 1 + nxt.start() if nxt else len(text)
                text = strip_fenced(text[:start] + text[end:])
            for tok in FORBIDDEN_TOOL_TOKENS:
                self.assertNotRegex(text, r"(?<![\w/])%s(?![\w])" % re.escape(tok),
                                    "%s names %r outside the harness table" % (n, tok))

    def test_harness_table_still_names_the_tools(self):
        """The drift guard works only if the names live somewhere."""
        text = read(LONG_RUNNING_MD)
        start = text.find("## What each harness offers")
        self.assertNotEqual(start, -1)
        self.assertTrue(any(tok in text[start:] for tok in FORBIDDEN_TOOL_TOKENS),
                        "the harness table names no tool; the plain-shell fallback is all that is left")


class TestDuplication(unittest.TestCase):
    def check(self, a_path, b_path):
        shared = shared_shingles(read(a_path), read(b_path))
        if shared:
            a, b = os.path.relpath(a_path, SKILL_DIR), os.path.relpath(b_path, SKILL_DIR)
            self.fail("%d shared %d-word shingle(s) between %s and %s; first: %r"
                      % (len(shared), SHINGLE, a, b, shared[0]))

    def test_skill_md_against_each_reference(self):
        for n in sorted(os.listdir(REF_DIR)):
            if n.endswith(".md"):
                self.check(SKILL_MD, os.path.join(REF_DIR, n))

    def test_consent_against_delegated_mode(self):
        self.check(CONSENT_MD, DELEGATED_MD)

    def test_reference_pairs(self):
        # Every pair of references: a case kept in two files costs its tokens
        # twice when both load (consent.md and examples.md once did).
        refs = sorted(os.path.join(REF_DIR, n) for n in os.listdir(REF_DIR) if n.endswith(".md"))
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                self.check(a, b)

    def test_detector_sees_a_repeat(self):
        a = "one two three four five six seven eight nine ten eleven"
        b = "zero " + a.upper()
        self.assertEqual(len(shared_shingles(a, b)), 2)
        self.assertEqual(shared_shingles(a, "```\n" + a + "\n```"), [])


class TestPublicHygiene(unittest.TestCase):
    def test_forbidden_patterns(self):
        for path in tracked_files():
            text = read(path)
            for pat, label in HYGIENE_PATTERNS:
                for m in re.finditer(pat, text):
                    line = text.count("\n", 0, m.start()) + 1
                    self.fail("%s:%d contains %s: %r" % (
                        os.path.relpath(path, SKILL_DIR), line, label, m.group(0)))

    def test_whitespace(self):
        for path in tracked_files():
            rel = os.path.relpath(path, SKILL_DIR)
            for i, line in enumerate(read(path).splitlines(), 1):
                self.assertEqual(line, line.rstrip(), "%s:%d trailing whitespace" % (rel, i))
                self.assertNotIn("\t", line, "%s:%d tab" % (rel, i))

    def test_shebang_files_are_executable(self):
        for root, dirs, names in os.walk(SKILL_DIR):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".runs")]
            for n in names:
                path = os.path.join(root, n)
                with open(path, "rb") as fh:
                    head = fh.read(2)
                if head == b"#!":
                    mode = os.stat(path).st_mode
                    self.assertTrue(mode & stat.S_IXUSR,
                                    "%s has a shebang but no executable bit"
                                    % os.path.relpath(path, SKILL_DIR))


if __name__ == "__main__":
    unittest.main(verbosity=1)
