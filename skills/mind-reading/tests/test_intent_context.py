import json
import unittest

import helpers
from mr.config import write_json
from mr.intent_context import ContextFormatError, normalize, retrieval_text, render
from mr.select import select_slice


class IntentContextTests(unittest.TestCase):
    def test_empty_context_has_no_query_or_rendering(self):
        for data in (None, {}, {"version": 1}, {"goal": "", "recent_user_turns": []}):
            self.assertEqual(normalize(data), {})
            self.assertEqual(retrieval_text(normalize(data)), "")
            self.assertEqual(render(normalize(data)), "")

    def test_only_preceding_context_fields_are_accepted(self):
        for data in ({"future": "also deploy"}, {"next_user_turn": "ship it"},
                     {"version": 2}, {"goal": []}, {"recent_user_turns": "a"},
                     {"recent_user_turns": [1]}, {"verified_facts": [None]}):
            with self.subTest(data=data), self.assertRaises(ContextFormatError):
                normalize(data)

    def test_context_has_bounded_size_without_silent_truncation(self):
        for data in ({"goal": "x" * 2001}, {"recent_user_turns": ["x"] * 9},
                     {"recent_user_turns": ["x" * 2001]}, {"verified_facts": ["x" * 501]}):
            with self.subTest(data=data), self.assertRaises(ContextFormatError):
                normalize(data)

    def test_query_uses_goal_and_recent_turns_and_preserves_order(self):
        data = {"version": 1, "goal": "repair invoice downloads", "phase": "repair",
                "recent_user_turns": ["export is broken", "the CSV is empty"],
                "verified_facts": ["download count is zero"], "project": "billing"}
        original = json.dumps(data, sort_keys=True)
        frame = normalize(data)
        text = retrieval_text(frame)
        self.assertIn("repair invoice downloads", text)
        self.assertLess(text.index("export is broken"), text.index("the CSV is empty"))
        self.assertEqual(json.dumps(data, sort_keys=True), original)
        shown = render(frame)
        self.assertIn("current invocation", shown)
        self.assertIn("not historical evidence", shown)

    def test_empty_context_preserves_exact_selector_oracle(self):
        paths, cfg = helpers.build_home(through="synthesize")
        for seed in ("fix export", "keep going", "zzz qqq", "push the release"):
            expected = select_slice(paths.model, seed, cfg["select"])
            self.assertEqual(select_slice(paths.model, seed, cfg["select"], context={}), expected)

    def test_context_rescues_terse_seed_without_overriding_explicit_seed(self):
        paths, cfg = helpers.build_home(through="synthesize")
        frame = {"goal": "fix the invoice export button", "recent_user_turns": ["the download is still broken"]}
        _, contextual = select_slice(paths.model, "ready", cfg["select"], context=frame)
        self.assertIn("E", contextual["groups"])
        self.assertTrue(contextual["context_routed"])
        _, direct = select_slice(paths.model, "push the release", cfg["select"], context=frame)
        ordinary = select_slice(paths.model, "push the release", cfg["select"])[1]
        self.assertEqual(direct["groups"], ordinary["groups"])
        self.assertFalse(direct["context_routed"])

    def test_conflicting_project_context_is_refused(self):
        paths, cfg = helpers.build_home(through="synthesize")
        with self.assertRaises(ContextFormatError):
            select_slice(paths.model, "ready", cfg["select"], project="one", context={"project": "two"})

    def test_cli_context_and_stdin_paths_are_literal_and_invalid_shapes_fail(self):
        paths, cfg = helpers.build_home(through="synthesize")
        frame = paths.home / "context.json"
        write_json(frame, {"goal": "fix invoice export"})
        result = helpers.run_cli(paths.home, "select", "--seed-file", "-", "--context-file", frame, "--json", stdin="ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["decision"]["context_routed"])
        result = helpers.run_cli(paths.home, "select", "--seed-file", "-", "--context-file", "-", stdin="{}")
        self.assertEqual(result.returncode, 1)
        frame.write_text('{"goal":"one","goal":"two"}')
        result = helpers.run_cli(paths.home, "select", "--seed", "ready", "--context-file", frame)
        self.assertEqual(result.returncode, 1)
        frame.write_text('{"next_user_turn":"future"}')
        result = helpers.run_cli(paths.home, "select", "--seed", "ready", "--context-file", frame)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
