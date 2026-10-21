#!/usr/bin/env python3
"""Lint the skill's markdown and scripts, and check that every number in the worked
example and the fixtures adds up. Runs without spending tokens."""

import os
import re
import stat
import unittest
from decimal import ROUND_HALF_UP, Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.abspath(os.path.join(HERE, ".."))
SKILL_MD = os.path.join(SKILL, "SKILL.md")
REFERENCES = os.path.join(SKILL, "references")
SCRIPTS = os.path.join(SKILL, "scripts")
FIXTURES = os.path.join(HERE, "fixtures")

# Built with chr() and concatenation so this file does not itself trip the sweep.
EM_DASH = chr(0x2014)
ELLIPSIS = chr(0x2026)
BYPASS_FLAG = "--" + "dangerously"
YOLO_FLAG = "--" + "yolo"
ALLOW_FLAG = "--" + "allow"
EXTERNAL_SKILL = "improve-" + "animations"
# A Linux home such as /home/<user>/ counts; the stage's own home/ subdirectory
# (<stage>/home/, $stage/home) does not: it is always preceded by a path character.
MACHINE_PATHS = (re.compile(r"/" + "Users/"), re.compile(r"(?<![\w>$}])/" + r"home/\w"), re.compile(r"C:" + re.escape(chr(92))))


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def all_skill_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(SKILL):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            found.append(os.path.join(dirpath, name))
    return sorted(found)


def frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md has no frontmatter"
    return match.group(1)


def table_rows(text, header_start):
    """Rows (as lists of cell strings) of the first markdown table whose header starts with header_start."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(header_start):
            rows = []
            for row in lines[index + 2:]:
                if not row.startswith("|"):
                    break
                rows.append([cell.strip() for cell in row.strip().strip("|").split("|")])
            return rows
    raise AssertionError("table %r not found" % header_start)


def half_up(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class FrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL_MD)
        self.front = frontmatter(self.text)

    def test_name(self):
        self.assertRegex(self.front, re.compile(r"^name: iterative-improvement$", re.M))

    def test_description_shape(self):
        match = re.search(r'^description:\s*"(.*)"\s*$', self.front, re.M)
        self.assertIsNotNone(match, "description must be one double-quoted line")
        description = match.group(1)
        self.assertLessEqual(len(description), 600, "description is longer than 600 characters")
        self.assertIn("Not for", description)
        self.assertLess(description.index("Not for"), 300, "exclusions must appear before index 300")
        for phrase in ("world class", "next level", "this is good but", "iterate on this"):
            self.assertIn(phrase, description, "description lacks trigger phrase %r" % phrase)
        self.assertNotIn("lite mode", description)
        self.assertNotIn(EXTERNAL_SKILL, self.text)
        self.assertNotIn(ELLIPSIS, description)

    def test_size(self):
        self.assertLessEqual(os.path.getsize(SKILL_MD), 14000)

    def test_required_phrases(self):
        for phrase in ("lite", "PRESERVE", "ACHIEVE", "T = max(3, 2n)", "zero N/A", "rescore the previous",
                       "git stash", "REJECT (repeat)", "--rounds-used", "--path", "ROOT", "LOOP",
                       "restore --dry-run", "UNKNOWN", "cheapest change", "--no-bash", "--preflight",
                       "Before", "After", "--same-engine", "--evidence"):
            self.assertIn(phrase, self.text, "SKILL.md lacks %r" % phrase)

    def test_section_numbering_is_consistent(self):
        subsections = re.findall(r"^### (\d)([a-e])\. ", self.text, re.M)
        self.assertEqual([s[1] for s in subsections], ["a", "b", "c", "d", "e"])
        parent = set(s[0] for s in subsections)
        self.assertEqual(len(parent), 1, "subsections belong to one section")
        parent = parent.pop()
        self.assertRegex(self.text, re.compile(r"^## %s\. Phase 3" % parent, re.M))
        for reference in re.findall(r"\b([3-5][a-e])\b", self.text):
            self.assertTrue(reference.startswith(parent), "stale subsection reference %r" % reference)
        for name in os.listdir(REFERENCES):
            text = read(os.path.join(REFERENCES, name))
            for reference in re.findall(r"SKILL\.md,? (?:section )?([3-5][a-e])\b", text):
                self.assertTrue(reference.startswith(parent), "%s points at stale section %r" % (name, reference))


class SweepTests(unittest.TestCase):
    def test_no_em_dash(self):
        for path in all_skill_files():
            self.assertNotIn(EM_DASH, read(path), "em dash in %s" % os.path.relpath(path, SKILL))

    def test_no_machine_paths(self):
        for path in all_skill_files():
            text = read(path)
            for needle in MACHINE_PATHS:
                self.assertIsNone(needle.search(text), "%r in %s" % (needle.pattern, os.path.relpath(path, SKILL)))

    def test_no_bypass_flags_anywhere(self):
        for path in all_skill_files():
            text = read(path)
            self.assertNotIn(BYPASS_FLAG, text, "%r in %s" % (BYPASS_FLAG, os.path.relpath(path, SKILL)))
            self.assertNotIn(YOLO_FLAG, text, "%r in %s" % (YOLO_FLAG, os.path.relpath(path, SKILL)))

    def test_allow_flag_is_gone_from_docs(self):
        for name in ("SKILL.md", "references/cross-agent.md", "references/reviewer-brief.md",
                     "references/scorer-brief.md", "references/troubleshooting.md"):
            text = read(os.path.join(SKILL, name))
            self.assertNotIn(ALLOW_FLAG + " ", text, "%s still documents %s" % (name, ALLOW_FLAG))
            self.assertNotIn(ALLOW_FLAG + "`", text, "%s still documents %s" % (name, ALLOW_FLAG))

    def test_cmd_record_path_is_consistent(self):
        for name in ("SKILL.md", "references/criteria-guide.md", "references/example-loop.md", "references/cross-agent.md"):
            text = read(os.path.join(SKILL, name))
            self.assertNotIn(".md.cmd", text, name)


class PointerTests(unittest.TestCase):
    def test_references_mentioned_and_present(self):
        text = read(SKILL_MD)
        mentioned = set(re.findall(r"references/([\w-]+\.md)", text))
        present = set(n for n in os.listdir(REFERENCES) if n.endswith(".md"))
        self.assertTrue(mentioned, "SKILL.md mentions no reference files")
        self.assertEqual(mentioned - present, set(), "SKILL.md points at missing references")
        self.assertEqual(present - mentioned, set(), "reference files not mentioned in SKILL.md")

    def test_scripts_mentioned_exist_and_are_executable(self):
        text = read(SKILL_MD)
        mentioned = set(re.findall(r"([\w]+\.(?:py|sh))", text))
        mentioned = set(n for n in mentioned if n not in ("run.sh",) and not n.startswith("test_"))
        self.assertTrue(mentioned)
        for name in mentioned:
            path = os.path.join(SCRIPTS, name)
            self.assertTrue(os.path.isfile(path), "SKILL.md mentions missing script %s" % name)
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IXUSR, "%s is not executable" % name)


class ReferenceContentTests(unittest.TestCase):
    def test_reviewer_brief(self):
        text = read(os.path.join(REFERENCES, "reviewer-brief.md"))
        for phrase in ("CONSTRAINTS", "OUT OF SCOPE", "PRIOR FINDINGS", "[re-raised: new evidence]",
                       "YOU ARE READ-ONLY", "NOT INSTRUCTIONS TO YOU", "STAGING"):
            self.assertIn(phrase, text)

    def test_scorer_brief(self):
        text = read(os.path.join(REFERENCES, "scorer-brief.md"))
        for phrase in ("YOU ARE READ-ONLY", "NOT INSTRUCTIONS TO YOU", "## Scaled:", "UNKNOWN", "STAGING"):
            self.assertIn(phrase, text)
        start = text.index("## The prompt")
        fence_open = text.index("```", start)
        fence_close = text.index("```", fence_open + 3)
        prompt = text[fence_open + 3:fence_close].lower()
        self.assertNotIn("plateau", prompt)
        self.assertNotIn("loop", prompt)
        self.assertIsNone(re.search(r"\bround \d", prompt), "the prompt must not name a round number")
        self.assertEqual(re.sub(r"\s+", " ", text).count("Never score `0` for a tooling failure"), 1)

    def test_cross_agent(self):
        text = read(os.path.join(REFERENCES, "cross-agent.md"))
        self.assertNotIn(BYPASS_FLAG, text)
        self.assertNotIn(YOLO_FLAG, text)
        for phrase in ("-s read-only", '--setting-sources ""', "--disable-slash-commands", "--skills-dir", "CODEX_HOME",
                       "project_doc_max_bytes=0", "Edit(//", "< /dev/null", "--same-engine", "--preflight",
                       "--model", "--no-bash", "keyring", "Verified"):
            self.assertIn(phrase, text, phrase)

    def test_criteria_guide(self):
        text = read(os.path.join(REFERENCES, "criteria-guide.md"))
        for phrase in ("<!-- loop-only -->", "<!-- /loop-only -->", "[PRESERVE|ACHIEVE]",
                       "## Score log", "## Amendments", "Baseline SHA", "literal SHA", "UNKNOWN"):
            self.assertIn(phrase, text)
        self.assertNotIn("three most recent", text)
        self.assertNotIn("git log --no-merges", text, "the C2 example must not be a frozen git-history measurement")

    def test_troubleshooting(self):
        text = read(os.path.join(REFERENCES, "troubleshooting.md"))
        self.assertIn("rescore the previous snapshot twice", re.sub(r"\s+", " ", text))
        self.assertIn("--no-bash", text)

    def test_example_loop(self):
        path = os.path.join(REFERENCES, "example-loop.md")
        self.assertTrue(os.path.isfile(path))
        self.assertLessEqual(os.path.getsize(path), 24576)
        text = read(path)
        for phrase in ("rubric v2", "STOP plateau", "REJECT (repeat)", "Unscoreable", "0a", "0b", "--rounds-used", "lapse"):
            self.assertIn(phrase, text)
        self.assertNotIn("49/74", text)


class ArithmeticTests(unittest.TestCase):
    """Every number shown in the fixtures and the worked example must add up."""

    def test_scorer_output_fixture_sums(self):
        text = read(os.path.join(FIXTURES, "scorer-output.md"))
        header = re.search(r"^## Score: (\d+)/(\d+)$", text, re.M)
        scaled = int(re.search(r"^## Scaled: (\d+)$", text, re.M).group(1))
        points, adjusted = int(header.group(1)), int(header.group(2))
        rows = table_rows(text, "| # | Criterion |")
        self.assertEqual(len(rows), 8)
        got_points = sum(int(r[2]) for r in rows if r[2] != "N/A")
        got_max = sum(int(r[3]) for r in rows if r[2] != "N/A")
        full_max = sum(int(r[3]) for r in rows)
        self.assertEqual(got_points, points)
        self.assertEqual(got_max, adjusted)
        self.assertEqual(full_max, 100)
        self.assertEqual(half_up(Decimal(points) * 100 / adjusted), scaled)
        na = [r[0] for r in rows if r[2] == "N/A"]
        unscoreable = text[text.index("## Unscoreable"):text.index("## Notes")]
        for criterion in na:
            self.assertIn(criterion + ":", unscoreable)
        gates = table_rows(text, "| Gate |")
        self.assertEqual([g[1] for g in gates], ["PASS", "FAIL", "PASS"])

    def test_criteria_fixture_sums_to_100_and_log_is_consistent(self):
        text = read(os.path.join(FIXTURES, "criteria-full.md"))
        weights = [int(w) for w in re.findall(r"^### C\d+\. .*\((\d+) pts\)$", text, re.M)]
        self.assertEqual(len(weights), 9)
        self.assertEqual(sum(weights), 100)
        for name, pts in re.findall(r"^### (C\d+)\. .*\((\d+) pts\)$", text, re.M):
            block = text[text.index("### %s." % name):]
            block = block[:block.index("\n### ") if "\n### " in block else len(block)]
            anchors = re.findall(r"^- `(\d+)`:", block, re.M)
            self.assertEqual(anchors[-1], pts, "%s full anchor must equal its weight" % name)
            self.assertEqual(anchors[0], "0", name)
        rows = table_rows(text, "| Round | Rubric |")
        previous = {}
        for row in rows:
            rnd, rubric, raw, adj, scaled, delta = row[0], row[1], int(row[2]), int(row[3]), int(row[4]), row[5]
            self.assertEqual(half_up(Decimal(raw) * 100 / adj), scaled, row)
            if delta.lstrip("+-").isdigit():
                self.assertIn(rubric, previous, row)
                self.assertEqual(int(delta), scaled - previous[rubric], row)
            if rnd.isdigit():
                previous[rubric] = scaled
        self.assertEqual([int(r[4]) for r in rows][-4:], [58, 73, 78, 80])

    def test_example_round_zero_table(self):
        text = read(os.path.join(REFERENCES, "example-loop.md"))
        rows = table_rows(text, "| # | Max | Run 0a |")
        body = [r for r in rows if r[0].startswith("C")]
        total = [r for r in rows if "Total" in r[0]][0]
        self.assertEqual(sum(int(r[1]) for r in body), 100)
        self.assertEqual(sum(int(r[2]) for r in body), int(total[2].strip("*")))
        self.assertEqual(sum(int(r[3]) for r in body), int(total[3].strip("*")))
        self.assertEqual((int(total[2].strip("*")), int(total[3].strip("*"))), (41, 45))

    def test_example_rescore_breakdown(self):
        text = read(os.path.join(REFERENCES, "example-loop.md"))
        block = re.search(r"## Score: 56/100\n## Scaled: 56\n(.*?)\n```", text, re.S).group(1)
        points = [int(p) for p in re.findall(r"C\d+ (\d+)", block)]
        self.assertEqual(len(points), 9)
        self.assertEqual(sum(points), 56)

    def test_example_final_table(self):
        text = read(os.path.join(REFERENCES, "example-loop.md"))
        rows = table_rows(text, "| # | Criterion | Max (v2) |")
        body = [r for r in rows if r[0].startswith("C")]
        total = [r for r in rows if "Total" in r[0]][0]
        self.assertEqual(sum(int(r[2]) for r in body), 100)
        self.assertEqual(sum(int(r[3]) for r in body), int(total[3]))
        self.assertEqual(sum(int(r[4]) for r in body), int(total[4]))
        self.assertEqual((int(total[3]), int(total[4])), (43, 80))
        headline = re.search(r"\*\*Result: (\d+) -> (\d+)", text)
        self.assertEqual((int(headline.group(1)), int(headline.group(2))), (43, 80))

    def test_example_round_one_matches_fixture(self):
        text = read(os.path.join(REFERENCES, "example-loop.md"))
        self.assertIn("## Score: 49/80\n## Scaled: 61", text)
        fixture = read(os.path.join(FIXTURES, "scorer-output.md"))
        self.assertTrue(fixture.startswith("## Score: 49/80\n## Scaled: 61"))
        self.assertIn("F14 (false claim)", text)
        self.assertNotIn("F13 (false claim)", text)


if __name__ == "__main__":
    unittest.main()
