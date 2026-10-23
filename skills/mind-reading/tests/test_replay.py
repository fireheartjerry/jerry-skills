import json
import hashlib
import unittest
from pathlib import Path

import helpers
from mr.replay import ReplayError, freeze_cases


def messages(session="one"):
    return [{"source": "test", "session": session, "key": str(i), "ordinal": i, "origin": "typed",
             "time": "2026-03-01T12:0%d:00Z" % i, "project": "billing",
             "text": text} for i, text in enumerate([
                 "the invoice export is broken", "fix it please", "also verify the FUTUREWORD downloaded file",
                 "then inspect the receipt for accuracy", "good enough for today"])]


class ReplayTests(unittest.TestCase):
    def test_prefix_and_reveal_are_physically_separate_and_source_bound(self):
        rows = messages()
        result = freeze_cases(rows, helpers.tmpdir() / "replay", after="2026-03-01T12:00:00Z", limit=1)
        inputs = json.loads(Path(result["inputs"]).read_text())
        reveals = json.loads(Path(result["reveals"]).read_text())
        case = inputs["cases"][0]
        self.assertEqual(case["seed"], "fix it please")
        self.assertEqual(case["context"], [rows[0]["text"]])
        self.assertNotIn("FUTUREWORD", json.dumps(inputs))
        self.assertIn("FUTUREWORD", json.dumps(reveals))
        self.assertEqual(case["source_ref"], "test:one:1")
        self.assertEqual(reveals["cases"][0]["id"], case["id"])
        self.assertIn("retrospective", result["evidence"])
        self.assertEqual(result["inputs_sha256"], hashlib.sha256(Path(result["inputs"]).read_bytes()).hexdigest())

    def test_excluded_sessions_and_non_typed_turns_cannot_enter_a_fold(self):
        rows = messages() + messages("excluded")
        rows[0]["origin"] = "pasted"
        result = freeze_cases(rows, helpers.tmpdir() / "replay", after="2026-03-01T12:00:00Z", excluded_sessions={"test:excluded"})
        inputs = json.loads(Path(result["inputs"]).read_text())
        self.assertNotIn("test:excluded", json.dumps(inputs))
        self.assertNotIn("the invoice export is broken", json.dumps(inputs))

    def test_freeze_is_deterministic_and_refuses_overwriting_a_different_packet(self):
        out = helpers.tmpdir() / "replay"
        one = freeze_cases(messages(), out, after="2026-03-01T12:00:00Z")
        self.assertEqual(one, freeze_cases(messages(), out, after="2026-03-01T12:00:00Z"))
        changed = messages()
        changed[1]["text"] = "another seed entirely"
        with self.assertRaises(ReplayError):
            freeze_cases(changed, out, after="2026-03-01T12:00:00Z")

    def test_conflicting_source_identity_and_non_chronological_future_are_refused(self):
        rows = messages()
        with self.assertRaises(ReplayError):
            freeze_cases(rows + [dict(rows[1], text="different")], helpers.tmpdir() / "bad", after="2026-03-01T12:00:00Z")
        rows[2]["time"] = "2026-03-01T11:00:00Z"
        result = freeze_cases(rows, helpers.tmpdir() / "safe", after="2026-03-01T12:00:00Z", limit=1)
        reveal = json.loads(Path(result["reveals"]).read_text())["cases"][0]
        self.assertNotIn("FUTUREWORD", json.dumps(reveal))

    def test_small_corpus_is_reported_without_padding_and_limits_are_strict(self):
        result = freeze_cases(messages(), helpers.tmpdir() / "replay", after="2026-03-01T12:00:00Z", limit=24)
        self.assertLess(result["count"], 24)
        for bad in (0, -1, True):
            with self.assertRaises(ReplayError):
                freeze_cases(messages(), helpers.tmpdir() / "bad", after="2026-03-01T12:00:00Z", limit=bad)

    def test_identical_copied_rows_do_not_multiply_evidence(self):
        first = freeze_cases(messages(), helpers.tmpdir() / "one", after="2026-03-01T12:00:00Z")
        copied = freeze_cases(messages() * 2, helpers.tmpdir() / "two", after="2026-03-01T12:00:00Z")
        self.assertEqual(first["inputs_sha256"], copied["inputs_sha256"])

    def test_previous_seed_exclusion_keeps_its_preceding_context_available(self):
        result = freeze_cases(messages(), helpers.tmpdir() / "one", after="2026-03-01T12:00:00Z", excluded_seeds={"test:one:1"})
        inputs = json.loads(Path(result["inputs"]).read_text())["cases"]
        self.assertNotIn("test:one:1", [c["source_ref"] for c in inputs])
        self.assertTrue(any("fix it please" in c["context"] for c in inputs))

    def test_mirrored_threads_cannot_pose_as_unseen_sessions(self):
        goal = " ".join("specific-project-goal-%d" % i for i in range(35))
        reserved, copied = messages("reserved"), messages("new-id")
        reserved[0]["text"] = copied[0]["text"] = goal
        result = freeze_cases(reserved + copied, helpers.tmpdir() / "clean", after="2026-03-01T12:00:00Z",
                              excluded_sessions={"test:reserved"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["mirrored_excluded_sessions"], ["test:new-id"])


if __name__ == "__main__":
    unittest.main()
