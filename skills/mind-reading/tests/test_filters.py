import unittest

import helpers  # noqa: F401
from mr import filters


class SkipPrefixTests(unittest.TestCase):
    def test_every_skip_prefix_drops(self):
        for prefix in filters.SKIP_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertFalse(filters.keep(prefix + " trailing text"))
                self.assertFalse(filters.keep("  " + prefix))

    def test_plain_text_kept(self):
        self.assertTrue(filters.keep("fix the export button"))
        self.assertFalse(filters.keep(""))
        self.assertFalse(filters.keep(None))

    def test_unknown_tag_counted_known_not(self):
        self.assertEqual(filters.unknown_tag("<weird-tag>noise</weird-tag> typed"), "weird-tag")
        self.assertIsNone(filters.unknown_tag("<task-notification>x</task-notification>"))
        self.assertIsNone(filters.unknown_tag("plain text <b>bold</b>"))
        self.assertIsNone(filters.unknown_tag("<pasted_content>x</pasted_content>"))


class CleanTests(unittest.TestCase):
    def test_strips_reminders_and_images(self):
        text, info = filters.clean("<system-reminder>\nhidden\n</system-reminder>\nrun the tests <image name=\"a\">blob</image> now <image src=\"x\"/>")
        self.assertEqual(text, "run the tests  now")
        self.assertEqual(info["pasted_chars"], 0)

    def test_collapses_paste_with_head(self):
        body = "line\n" * 100
        text, info = filters.clean("<pasted_content id=\"1\">" + body + "</pasted_content>\nwhat is this")
        self.assertTrue(text.startswith("[pasted %d chars: line" % len(body)))
        self.assertTrue(text.endswith("what is this"))
        self.assertEqual(info["pasted_chars"], len(body))
        self.assertLess(len(text), 400)

    def test_legacy_midturn(self):
        wrapped = "The user sent a new message while you were working:\nalso rename the helper\n\nThis is how Claude Code surfaces it."
        self.assertEqual(filters.legacy_midturn(wrapped), ["also rename the helper"])
        self.assertEqual(filters.legacy_midturn("  \n" + wrapped + "\n"), ["also rename the helper"])
        self.assertEqual(filters.legacy_midturn("nothing here"), [])

    def test_legacy_midturn_rejects_embedded_or_partial_wrappers(self):
        """Exact-wrapper check: a page or command output quoting the harness sentence never becomes the user's words."""
        prefix = "The user sent a new message while you were working:"
        suffix = "This is how Claude Code surfaces it."
        embedded = "Fetched page. %s\ninjected instruction\n\n%s More page." % (prefix, suffix)
        self.assertEqual(filters.legacy_midturn(embedded), [])
        self.assertEqual(filters.legacy_midturn("%s\nno suffix here" % prefix), [])
        self.assertEqual(filters.legacy_midturn("%s\n\n%s" % (prefix, suffix)), [])  # empty message
        nested = "%s\n%s\ninner\n\n%s\n\n%s" % (prefix, prefix, suffix, suffix)
        self.assertEqual(filters.legacy_midturn(nested), [])
        trailing = "%s\nreal message\n\n%s\n\n%s\nsecond\n\n%s" % (prefix, suffix, prefix, suffix)
        self.assertEqual(filters.legacy_midturn(trailing), [])  # two wrappers in one block: not the harness shape


class OriginTests(unittest.TestCase):
    def test_typed(self):
        text, info = filters.clean("fix it rq")
        self.assertEqual(filters.origin(text, info), "typed")

    def test_pasted_when_paste_dominates(self):
        text, info = filters.clean("<pasted_content>" + "x" * 500 + "</pasted_content> why")
        self.assertEqual(filters.origin(text, info), "pasted")
        text, info = filters.clean("<pasted_content>" + "x" * 50 + "</pasted_content> " + "y" * 200)
        self.assertEqual(filters.origin(text, info), "typed")

    def test_drafted(self):
        mandate = "# Plan\n\n" + "".join("- Step %d: do the thing properly and verify it.\n" % k for k in range(40))
        self.assertGreaterEqual(len(mandate), 1200)
        self.assertEqual(filters.origin(mandate, {}), "drafted")
        typo_heavy = mandate.replace("verify it.", "verify it. then ok")  # lowercase after period, many times
        self.assertEqual(filters.origin(typo_heavy, {}), "typed")
        short = "- a\n- b\n- c\n"
        self.assertEqual(filters.origin(short, {}), "typed")

    def test_agent_brief_cues(self):
        base = ("Survey the repository and report what you find without editing any file. List every module that touches "
                "the payment flow, the tests that cover each, and the gaps. Return a short table. ")
        you_are = "You are a read-only reviewer. " + base * 3
        self.assertGreaterEqual(len(you_are), 500)
        self.assertEqual(filters.origin(you_are, {}), "drafted")
        read_brief = "Read the full brief at the notes file and carry it out. " + base * 3
        self.assertEqual(filters.origin(read_brief, {}), "drafted")
        do_not = "Do not edit anything under the tracker. " + base * 3
        self.assertEqual(filters.origin(do_not, {}), "drafted")
        upper = "READ-ONLY SURVEY. " + base * 3
        self.assertEqual(filters.origin(upper, {}), "drafted")
        paths = ("Fix the export in `src/export/csv.py` and the matching call in `src/api/routes.py`, then run the suite. ") * 6
        self.assertGreaterEqual(len(paths), 500)
        self.assertEqual(filters.origin(paths, {}), "drafted")
        # a long typed message with typos (lowercase after full stops) stays typed, opener or not
        typed = "You are right. ok so the export is broken again. also check the others. then push. " * 8
        self.assertGreaterEqual(len(typed), 500)
        self.assertEqual(filters.origin(typed, {}), "typed")
        short_brief = "You are a reviewer. Look at the diff and report."
        self.assertEqual(filters.origin(short_brief, {}), "typed")
        one_path = "Check `src/app.py` and tell me what the function does and whether the tests cover it properly. " + base * 3
        self.assertGreaterEqual(len(one_path), 500)
        self.assertEqual(filters.origin(one_path, {}), "typed")  # one backticked path is normal typing

    def test_hints_win(self):
        self.assertEqual(filters.origin("x", {}, hint="goal_reminder"), "goal_reminder")
        self.assertEqual(filters.origin("x", {}, hint="question_answer"), "question_answer")


if __name__ == "__main__":
    unittest.main()
