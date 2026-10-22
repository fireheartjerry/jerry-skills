#!/usr/bin/env python3
"""Shape checks for evals/: the trigger set, the scenarios, the rubric, the results file and
the README. Offline and token-free; they prove the files are well-formed and tied to the
SKILL.md description, not that the skill behaves as the scenarios describe."""

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.abspath(os.path.join(HERE, ".."))
SKILL_MD = os.path.join(SKILL, "SKILL.md")
EVALS = os.path.join(SKILL, "evals")

# One source for the sweep patterns: python3 -I drops the script directory from sys.path,
# so add it before importing the sibling lint module.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from test_lint import EM_DASH, MACHINE_PATHS  # noqa: E402

EXCLUSION_PHRASES = {
    "debugging": "debugging one defect",
    "definite-done": "definite done state",
    "oracle": "linter or metric measures, even if asked to iterate until it passes",
    "out-of-domain": "explaining, planning or deploying",
    "scratch": "building from scratch",
    "tiny-edit": "a one-line edit",
}
# Neighbour prompts belong to another skill in this repository; the description need not
# name them, but the owner must exist.
NEIGHBOUR_OWNERS = {"agent-sessions", "high-agency", "competitive-systems", "code-review"}
# Every domain word must be in the description itself: a router never sees the body.
DOMAIN_WORDS = {"design": "designs", "page": "pages", "ui": "UI", "code": "code", "docs": "docs", "copy": "copy",
                "architecture": "architecture"}
DOMAINS = set(DOMAIN_WORDS)
MIN_PER_DOMAIN = 2
MIN_PER_EXCLUSION = 4
REQUIRED_PHRASINGS = ("improve this", "polish this", "world class", "next level", "this is good but",
                      "audit and fix", "iterate on this", "rounds", "keep going until", "make our readme better",
                      "tighten this", "can you make this component nicer")
REQUIRED_ORACLE = ("iterate on the parser until pytest passes", "keep going until the benchmark hits 95",
                   "make the lint pass")
SCENARIO_KEYS = ("id", "title", "phase", "mode", "situation", "user_message", "ideal", "failure_modes")
MODES = {"full", "lite", "none"}
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load(name):
    with open(os.path.join(EVALS, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def description():
    match = re.search(r'^description:\s*"(.*)"\s*$', read(SKILL_MD), re.M)
    assert match, "SKILL.md description must be one double-quoted line"
    return match.group(1)


def normalise(message):
    return re.sub(r"\s+", " ", message.strip().lower())


def section(text, heading):
    """Text from a '## ' heading that starts with `heading` to the next '## ' heading."""
    match = re.search(r"^## " + re.escape(heading) + r".*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, "section %r not found" % heading
    return match.group(1)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.data = load("triggers.json")
        self.should = self.data["should_trigger"]
        self.should_not = self.data["should_not_trigger"]

    def test_top_level_shape(self):
        self.assertEqual(set(self.data), {"description_under_test", "should_trigger", "should_not_trigger"})
        self.assertIn("SKILL.md", self.data["description_under_test"])

    def test_counts(self):
        self.assertGreaterEqual(len(self.should), 24)
        self.assertGreaterEqual(len(self.should_not), 20)

    def test_entry_keys(self):
        for entry in self.should:
            self.assertEqual(set(entry), {"message", "domain", "note"}, entry)
        for entry in self.should_not:
            keys = {"message", "exclusion", "note"} | ({"owner"} if entry.get("exclusion") == "neighbour" else set())
            self.assertEqual(set(entry), keys, entry)
        for entry in self.should + self.should_not:
            self.assertTrue(entry["message"].strip() and entry["note"].strip(), entry)

    def test_domains(self):
        counts = {}
        for entry in self.should:
            self.assertIn(entry["domain"], DOMAINS, entry)
            counts[entry["domain"]] = counts.get(entry["domain"], 0) + 1
        for domain in sorted(DOMAINS):
            self.assertGreaterEqual(counts.get(domain, 0), MIN_PER_DOMAIN, "domain %r under-covered" % domain)

    def test_exclusions(self):
        counts = {}
        for entry in self.should_not:
            self.assertIn(entry["exclusion"], set(EXCLUSION_PHRASES) | {"neighbour"}, entry)
            counts[entry["exclusion"]] = counts.get(entry["exclusion"], 0) + 1
        for exclusion in sorted(EXCLUSION_PHRASES) + ["neighbour"]:
            self.assertGreaterEqual(counts.get(exclusion, 0), MIN_PER_EXCLUSION, "exclusion %r under-covered" % exclusion)

    def test_neighbours_name_a_real_skill(self):
        skills = set(os.listdir(os.path.dirname(SKILL)))
        for entry in self.should_not:
            if entry["exclusion"] == "neighbour":
                self.assertIn(entry["owner"], NEIGHBOUR_OWNERS, entry)
                if entry["owner"] != "code-review":  # a host built-in, not a skill in this repo
                    self.assertIn(entry["owner"], skills, entry)

    def test_hard_negatives_are_present(self):
        """The near misses the description most often lost: creation, an eval or scanner
        as the oracle, a tiny edit, a critique with no edits."""
        messages = [normalise(e["message"]) for e in self.should_not]
        for needle in ("from scratch", "eval pass rate", "known vulnerabilities", "run axe", "commit message",
                       "don't change anything"):
            self.assertTrue(any(needle in m for m in messages), needle)

    def test_no_duplicates_within_or_across_lists(self):
        seen = {}
        for kind, entries in (("should_trigger", self.should), ("should_not_trigger", self.should_not)):
            for entry in entries:
                key = normalise(entry["message"])
                self.assertNotIn(key, seen, "%r appears in %s and %s" % (entry["message"], seen.get(key), kind))
                seen[key] = kind

    def test_required_phrasings_are_covered(self):
        messages = [normalise(e["message"]) for e in self.should]
        for phrase in REQUIRED_PHRASINGS:
            self.assertTrue(any(phrase in m for m in messages), "no should-trigger message covers %r" % phrase)

    def test_required_oracle_cases(self):
        oracle = [normalise(e["message"]) for e in self.should_not if e["exclusion"] == "oracle"]
        for message in REQUIRED_ORACLE:
            self.assertIn(message, oracle)

    def test_out_of_domain_covers_explanations_plans_deploys_and_typos(self):
        messages = " | ".join(normalise(e["message"]) for e in self.should_not if e["exclusion"] == "out-of-domain")
        for word in ("explain", "plan", "deploy"):
            self.assertIn(word, messages)
        tiny = " | ".join(normalise(e["message"]) for e in self.should_not if e["exclusion"] == "tiny-edit")
        self.assertIn("typo", tiny)

    def test_exclusions_map_to_description(self):
        text = description()
        for exclusion in set(e["exclusion"] for e in self.should_not) - {"neighbour"}:
            self.assertIn(EXCLUSION_PHRASES[exclusion], text, "description lost the wording for %r" % exclusion)

    def test_domains_map_to_description(self):
        text = description()
        for domain in set(e["domain"] for e in self.should):
            self.assertRegex(text, r"\b%s\b" % re.escape(DOMAIN_WORDS[domain]), "description lacks %r" % domain)

    def test_description_carries_the_main_triggers(self):
        text = description().lower()
        for phrase in ("improve this", "polish this", "audit and fix", "iterate on this", "run n rounds",
                       "keep going until it is great", "two competent people"):
            self.assertIn(phrase, text, phrase)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        data = load("scenarios.json")
        self.assertEqual(set(data), {"scenarios"})
        self.scenarios = data["scenarios"]
        self.by_id = dict((s["id"], s) for s in self.scenarios)

    def test_ids_unique(self):
        ids = [s["id"] for s in self.scenarios]
        self.assertEqual(len(ids), len(set(ids)))

    def test_phase_one_set_is_complete(self):
        for i in range(1, 13):
            sid = "S%d" % i
            self.assertIn(sid, self.by_id)
            self.assertEqual(self.by_id[sid]["phase"], "1", sid)

    def test_phase_two_count(self):
        self.assertGreaterEqual(len([s for s in self.scenarios if s["phase"] == "2"]), 10)

    def test_every_scenario_is_complete(self):
        for s in self.scenarios:
            self.assertEqual(tuple(sorted(s)), tuple(sorted(SCENARIO_KEYS)), s.get("id"))
            for key in ("id", "title", "situation", "user_message"):
                self.assertIsInstance(s[key], str)
                self.assertTrue(s[key].strip(), "%s has an empty %s" % (s["id"], key))
            self.assertIn(s["phase"], ("1", "2"), s["id"])
            self.assertIn(s["mode"], MODES, s["id"])
            self.assertRegex(s["id"], r"^%s\d+$" % ("S" if s["phase"] == "1" else "P"))
            for key in ("ideal", "failure_modes"):
                self.assertIsInstance(s[key], list)
                self.assertGreaterEqual(len(s[key]), 2, "%s needs two %s items" % (s["id"], key))
                for item in s[key]:
                    self.assertTrue(isinstance(item, str) and item.strip(), s["id"])

    def test_modes_of_anchor_scenarios(self):
        for sid in ("S2", "S3"):
            self.assertEqual(self.by_id[sid]["mode"], "none", sid)
        for sid in ("S4", "P1"):
            self.assertEqual(self.by_id[sid]["mode"], "lite", sid)

    def test_quoted_score_math_results_still_hold(self):
        """Replay every score_math result a scenario quotes, so a rule change in the
        script cannot leave an ideal or failure mode describing the old behaviour."""
        import subprocess
        script = os.path.join(SKILL, "scripts", "score_math.py")
        # (scenario, argv, substrings of the scenario text, substrings of the output)
        cases = [
            ("P5", ["baseline", "40", "56", "44"], ["40 and 56", "returns 44", "56 as the outlier",
                                                    "baseline 42, n = 4, T = 8",
                                                    "uses T = 32"],
             ["baseline=42 noise=4 threshold=8", "outlier=56"]),
            ("P5", ["baseline", "40", "56", "48"], ["returns 48", "soft=true"],
             ["soft=true", "outlier=none"]),
            ("P4", ["baseline", "52", "52"], ["52 and 52", "third-run=needed"], ["third-run=needed"]),
            ("S9", ["baseline", "41", "45"], ["41 and 45 (n = 4, T = 8)"], ["noise=4 threshold=8"]),
            ("S9", ["baseline", "56", "60"], ["56 and 60 (reference 58, n = 4, T = 8)"],
             ["baseline=58 noise=4 threshold=8"]),
            ("S9", ["decide", "--threshold", "8", "--target", "85", "--max-rounds", "5", "--scores",
                    "58,71,76,79", "--gates", "PASS", "--rounds-used", "4"],
             ["58, 71, 76, 79", "target 85", "plateau"], ["STOP plateau"]),
        ]
        for sid, argv, quoted, expected in cases:
            text = json.dumps(self.by_id[sid])
            for snippet in quoted:
                self.assertIn(snippet, text, "%s no longer quotes %r" % (sid, snippet))
            run = subprocess.run([sys.executable, "-I", script] + argv, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            for snippet in expected:
                self.assertIn(snippet, run.stdout, "%s: %s" % (sid, " ".join(argv)))

    def test_s4_runs_lite_without_the_full_protocol(self):
        ideal = " ".join(self.by_id["S4"]["ideal"])
        self.assertIn("full-protocol.md", ideal)
        self.assertIn("without loading", ideal)


class RubricTests(unittest.TestCase):
    def test_phrases(self):
        text = read(os.path.join(EVALS, "rubric.md"))
        for phrase in ("0 to 3", "9 to 10", "precision", "recall", "failure mode"):
            self.assertIn(phrase, text.lower() if phrase in ("precision", "recall") else text, phrase)

    def test_size(self):
        self.assertLess(os.path.getsize(os.path.join(EVALS, "rubric.md")), 3072)


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.text = read(os.path.join(EVALS, "results.md"))

    def test_every_scenario_id_appears(self):
        for s in load("scenarios.json")["scenarios"]:
            self.assertRegex(self.text, r"\b%s\b" % s["id"], "results.md lacks %s" % s["id"])

    def test_required_content(self):
        self.assertIn("n = 12", self.text)

    def test_live_section_states_what_ran(self):
        live = section(self.text, "3. Live")
        self.assertTrue("not run" in live or ISO_DATE.search(live), "live section names neither a date nor 'not run'")


class ReadmeAndSweepTests(unittest.TestCase):
    def test_readme_names_every_file(self):
        text = read(os.path.join(EVALS, "README.md"))
        for name in sorted(os.listdir(EVALS)):
            if name != "README.md":
                self.assertIn(name, text, "README.md does not mention %s" % name)

    def test_readme_size(self):
        self.assertLess(os.path.getsize(os.path.join(EVALS, "README.md")), 4096)

    def test_markdown_hygiene(self):
        names = [n for n in os.listdir(EVALS) if n.endswith(".md")]
        self.assertTrue({"README.md", "results.md", "rubric.md"} <= set(names), names)
        for name in names:
            path = os.path.join(EVALS, name)
            text = read(path)
            self.assertLess(os.path.getsize(path), 8192, name)
            self.assertNotIn(EM_DASH, text, name)
            for needle in MACHINE_PATHS:
                self.assertIsNone(needle.search(text), "%r in %s" % (needle.pattern, name))


if __name__ == "__main__":
    unittest.main()
