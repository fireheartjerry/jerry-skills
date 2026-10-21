#!/usr/bin/env python3
"""Lint the skill's markdown and scripts, and check that every number in the worked
example and the fixtures adds up. Runs without spending tokens."""

import json
import os
import re
import stat
import unittest
from decimal import ROUND_HALF_UP, Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.abspath(os.path.join(HERE, ".."))
SKILL_MD = os.path.join(SKILL, "SKILL.md")
REFERENCES = os.path.join(SKILL, "references")
FULL_PROTOCOL = os.path.join(REFERENCES, "full-protocol.md")
SCRIPTS = os.path.join(SKILL, "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
EVALS = os.path.join(SKILL, "evals")

# Built with chr() and concatenation so this file does not itself trip the sweep.
EM_DASH = chr(0x2014)
ELLIPSIS = chr(0x2026)
BYPASS_FLAG = "--" + "dangerously"
YOLO_FLAG = "--" + "yolo"
ALLOW_FLAG = "--" + "allow"
EXTERNAL_SKILL = "improve-" + "animations"
# A Linux home such as /home/<user>/ counts; a placeholder or variable path such as
# <stage>/x/home/ or $dir/home does not: it is always preceded by a path character.
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


def lite_card(text):
    """The SKILL.md text between '## 2. Lite card' and the next top-level section."""
    start = text.index("## 2. Lite card")
    end = text.index("\n## 3.", start)
    return text[start:end]


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
        self.assertLessEqual(len(description), 560, "description is longer than 560 characters")
        self.assertIn("Not for", description)
        self.assertLess(description.index("Not for"), 150, "exclusions must appear before index 150")
        for phrase in ("world class", "next level", "this is good but", "iterate on this", "audit and fix",
                       "iterate until", "designs", "pages", "UI", "code", "docs", "copy"):
            self.assertIn(phrase, description, "description lacks phrase %r" % phrase)
        self.assertNotIn("lite mode", description)
        self.assertNotIn(EXTERNAL_SKILL, self.text)
        self.assertNotIn(ELLIPSIS, description)

    def test_size(self):
        self.assertLessEqual(os.path.getsize(SKILL_MD), 9500)

    def test_full_protocol_size(self):
        self.assertLessEqual(os.path.getsize(FULL_PROTOCOL), 14000)

    def test_required_phrases(self):
        for phrase in ("## 2. Lite card", "reply ok or edit", "T = 3", "2 rounds max", "full-protocol.md",
                       "PRESERVE", "ACHIEVE", "REJECT (repeat)", "git stash", "--preflight", "--same-engine",
                       "--evidence", "restore --dry-run", "UNKNOWN", "not measured", "--rounds-used",
                       "HALT", "CONFIRM", "BLOCKED", "--path", "ROOT", "LOOP", "cheapest change", "--unsandboxed"):
            self.assertIn(phrase, self.text, "SKILL.md lacks %r" % phrase)

    def test_lite_card_is_self_sufficient(self):
        """The lite card runs with only the files it names; full-only mechanics stay out."""
        card = lite_card(self.text)
        for phrase in ("spawn_child.sh", "snapshot.py", "strip_criteria.py", "score_math.py decide",
                       "reviewer-brief.md", "scorer-brief.md", "cross-agent.md", "troubleshooting.md",
                       "--threshold 3", "--max-rounds 2", "--rounds-used 1", "PRIOR FINDINGS", "DERIVED",
                       "reply ok or edit", "[PRESERVE]", "[ACHIEVE]"):
            self.assertIn(phrase, card, "lite card lacks %r" % phrase)
        for phrase in ("T = max", "third-run", "twice"):
            self.assertNotIn(phrase, card, "lite card carries full-only mechanics: %r" % phrase)

    def test_full_mode_pointer_precedes_lite_card(self):
        self.assertLess(self.text.index("full-protocol.md"), self.text.index("## 2. Lite card"))

    def test_section_numbering_is_consistent(self):
        """The protocol subsections live in full-protocol.md as 4a to 4e under '## 4. Phase 3';
        nothing cites the phase-1 SKILL.md subsections any more, and every full-protocol
        citation names a subsection that exists."""
        protocol = read(FULL_PROTOCOL)
        subsections = re.findall(r"^### (\d)([a-e])\. ", protocol, re.M)
        self.assertEqual([s[0] + s[1] for s in subsections], ["4a", "4b", "4c", "4d", "4e"])
        self.assertRegex(protocol, re.compile(r"^## 4\. Phase 3", re.M))
        existing = set(s[0] + s[1] for s in subsections)
        stale = re.compile(r"SKILL\.md,? (?:section )?[3-6][a-e]\b")
        citation = re.compile(r"full-protocol\.md,? (?:section )?(4[a-e])")
        paths = [SKILL_MD]
        for folder in (REFERENCES, EVALS):
            paths.extend(os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n.endswith((".md", ".json")))
        for path in paths:
            text = read(path)
            rel = os.path.relpath(path, SKILL)
            self.assertIsNone(stale.search(text), "%s cites a phase-1 SKILL.md subsection" % rel)
            for cited in citation.findall(text):
                self.assertIn(cited, existing, "%s cites full-protocol.md %s, which does not exist" % (rel, cited))

    def test_full_protocol_phrases(self):
        text = read(FULL_PROTOCOL)
        for phrase in ("T = max(3, 2n)", "third-run=needed", "zero N/A", "--rounds-used", "HALT", "STOP plateau",
                       "CONFIRM", "BLOCKED", "CONTINUE", "score_math.py budget", "rounds-affordable=0",
                       "Never start a round", "ceiling, not a goal", "--scores 58,73 --rounds-used 2",
                       "## 5. Amending", "## 6. Cost and time"):
            self.assertIn(phrase, text, "full-protocol.md lacks %r" % phrase)
        self.assertIn("rescore the previous", text.lower())


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

    def test_evals_json_parses(self):
        """Every evals/*.json parses; the shape checks live in test_evals.py."""
        names = sorted(n for n in os.listdir(EVALS) if n.endswith(".json"))
        self.assertTrue(names, "evals/ holds no JSON files")
        for name in names:
            with open(os.path.join(EVALS, name), "r", encoding="utf-8") as handle:
                try:
                    json.load(handle)
                except ValueError as exc:
                    self.fail("evals/%s does not parse: %s" % (name, exc))

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
        for name in sorted(present):
            cited = set(re.findall(r"references/([\w-]+\.md)", read(os.path.join(REFERENCES, name))))
            self.assertEqual(cited - present, set(), "references/%s points at missing references" % name)

    def test_evals_dir_mentioned(self):
        self.assertIn("`evals/`", read(SKILL_MD))
        self.assertTrue(os.path.isdir(EVALS))

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

    def test_no_phantom_script_paths(self):
        """Every `scripts/<name>` token in SKILL.md, the references, the evals and the
        fixtures names a script the skill ships (the repo validator scans the same files).
        The worked example's fictional helpers live under tools/ and are never written as
        scripts/."""
        paths = [SKILL_MD]
        for directory in (REFERENCES, os.path.join(SKILL, "evals"), FIXTURES):
            for dirpath, _dirnames, filenames in os.walk(directory):
                paths.extend(os.path.join(dirpath, n) for n in filenames if n.endswith((".md", ".json")))
        self.assertTrue(paths)
        shipped = set(os.listdir(SCRIPTS))
        # The lookbehind skips tokens inside a longer path (x/scripts/, .scripts/) or a
        # longer word (test_scripts.py); a leading backtick does not exempt a token, so
        # inline code is checked too, as the validator does.
        token = re.compile(r"(?<![\w/.-])scripts/([\w.-]+)")
        for path in paths:
            text = read(path)
            rel = os.path.relpath(path, SKILL)
            for name in token.findall(text):
                self.assertIn(name, shipped, "%s names scripts/%s, which the skill does not ship" % (rel, name))
            for helper in ("frames.mjs", "check-overflow.mjs"):
                stray = re.search(r"(?<!tools/)" + re.escape(helper), text)
                self.assertIsNone(stray, "%s mentions %s without the tools/ prefix" % (rel, helper))


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
                       "--model", "--unsandboxed", "keyring", "Verified", "# duration", "byte-identical",
                       "Read(//<stage>/**)", "## What each engine enforces", "tokens used"):
            self.assertIn(phrase, text, phrase)
        for stale in ("--no-bash", "Bash(cat:*)\" ", "stricter variant"):
            self.assertNotIn(stale, text, stale)

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
        self.assertIn("--rounds-used 3", text, "the budget worked line must show a mid-loop call")
        self.assertIn("lists no files", text)
        for phrase in ("third-run=needed", "## Budget in money or time", "rounds-affordable", "not measured",
                       "full-protocol.md", "Budget overrun", "STOP plateau", "STOP regression",
                       "CONTINUE slow real trend"):
            self.assertIn(phrase, text, phrase)

    def test_example_loop(self):
        path = os.path.join(REFERENCES, "example-loop.md")
        self.assertTrue(os.path.isfile(path))
        self.assertLessEqual(os.path.getsize(path), 24576)
        text = read(path)
        for phrase in ("rubric v2", "STOP plateau", "REJECT (repeat)", "Unscoreable", "0a", "0b", "--rounds-used", "lapse",
                       "third-run=no", "tools/frames.mjs", "tools/check-overflow.mjs", "not shipped with this skill",
                       "12 child runs", "`.log` footers"):
            self.assertIn(phrase, text, phrase)
        self.assertNotIn("49/74", text)

    def test_example_child_run_count_matches_score_log(self):
        """The report's child-run figure is derived from the fixture's score log: one scorer
        run per a/b row, one per plain round row that has no a/b rows under the same rubric
        (mean rows are not runs), and one reviewer run per numbered round."""
        rows = table_rows(read(os.path.join(FIXTURES, "criteria-full.md")), "| Round | Rubric |")
        ids = [(r[0], r[1]) for r in rows]
        sub_runs = [(rnd, rubric) for rnd, rubric in ids if re.fullmatch(r"\d+[a-z]", rnd)]
        means = set((rnd[:-1], rubric) for rnd, rubric in sub_runs)
        single_runs = [(rnd, rubric) for rnd, rubric in ids if rnd.isdigit() and (rnd, rubric) not in means]
        scorer_runs = len(sub_runs) + len(single_runs)
        reviewer_runs = len(set(rnd for rnd, _rubric in ids if rnd.isdigit() and int(rnd) >= 1))
        self.assertEqual((scorer_runs, reviewer_runs), (8, 4))
        text = read(os.path.join(REFERENCES, "example-loop.md"))
        self.assertIn("%d child runs (%d scorer, %d reviewer)" % (scorer_runs + reviewer_runs, scorer_runs, reviewer_runs), text)


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
