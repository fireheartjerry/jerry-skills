"""The trigger rule, as a case set the description is checked against. Positive messages fire with the expected seed;
negative ones do not fire, for the stated reason."""
import json
import re
import unittest

import helpers
from mr import trigger
from mr.lint import _frontmatter

CASES = json.loads((helpers.TESTS / "trigger_cases.json").read_text(encoding="utf-8"))


class TriggerTests(unittest.TestCase):
    def test_positive_cases_fire_with_seed(self):
        for case in CASES["positive"]:
            with self.subTest(message=case["message"]):
                got = trigger.classify(case["message"])
                self.assertTrue(got["fire"], got)
                self.assertEqual(got["seed"], case["seed"])

    def test_negative_cases_do_not_fire(self):
        for case in CASES["negative"]:
            with self.subTest(message=case["message"]):
                got = trigger.classify(case["message"])
                self.assertFalse(got["fire"], got)
                self.assertIn(case["reason"].split()[0], got["reason"])

    def test_case_set_is_balanced_enough(self):
        self.assertGreaterEqual(len(CASES["positive"]), 10)
        self.assertGreaterEqual(len(CASES["negative"]), 15)
        reasons = {c["reason"] for c in CASES["negative"]}
        self.assertEqual(reasons, {"no invocation phrase", "quoted or reported phrase", "no seed after the phrase", "about the skill"})

    def test_description_states_the_same_rule(self):
        fm = _frontmatter((helpers.SKILL / "SKILL.md").read_text(encoding="utf-8"))
        desc = fm["description"]
        low = desc.lower()
        for phrase in ("read my mind", "mind-read this", "/mind-reading", "$mind-reading"):
            self.assertIn(phrase, low, phrase)
        self.assertIn("not for", low)
        for exclusion in ("quoted or reported", "already complete", "talk about the skill", "build"):
            self.assertIn(exclusion, low, exclusion)
        self.assertLessEqual(len(desc), 600)
        # every negative-case reason is named in the description or SKILL.md section 0
        body = (helpers.SKILL / "SKILL.md").read_text(encoding="utf-8").lower()
        for needle in ("quoted or reported", "bare words", "no seed", "about the skill"):
            self.assertIn(needle, body, needle)

    def test_invocation_phrases_match_the_description_list(self):
        self.assertEqual(sorted(trigger.INVOCATION_PHRASES), sorted(["read my mind", "mind-read this", "mind read this", "/mind-reading", "$mind-reading"]))
        self.assertIsNotNone(re.match(r"\$mind-reading", "$mind-reading x"))


if __name__ == "__main__":
    unittest.main()
