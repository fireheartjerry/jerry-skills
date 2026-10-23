import json
import unittest

import helpers
from mr.config import read_json, write_json, write_jsonl
from mr.preferences import PreferenceError
from mr.tracking import (TrackingError, build_baseline_review, discover_candidates, import_baseline_answers,
                         record_outcome, scan_transcripts, summarize, validate_decision)

BRIEF = "Reading: fix the export fully.\n\n> Fix the export and check its siblings. [S]"
REF = {"source": "claude-code", "session": "abc", "turn": 10}


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.paths, self.cfg = helpers.init_home(self.tmp / "home")

    def test_missing_baseline_is_unknown_and_corrections_need_human_source(self):
        self.assertIsNone(summarize(self.paths)["corrections_per_100_observed"])
        decisions = [validate_decision({"decision_id": "d1"}), validate_decision({"decision_id": "d2"}), {"seed_hash": "legacy"}]
        write_jsonl(self.paths.decisions, decisions)
        with self.assertRaises(TrackingError):
            record_outcome(self.paths, {"decision_id": "d1", "outcome": "corrected", "source_ref": REF})
        with self.assertRaises(TrackingError):
            record_outcome(self.paths, {"decision_id": "missing", "reviewer": "human", "outcome": "corrected", "source_ref": REF})
        record_outcome(self.paths, {"decision_id": "d1", "reviewer": "human", "outcome": "corrected", "source_ref": REF})
        s = summarize(self.paths)
        self.assertEqual((s["human_reviewed_observed"], s["unreviewed_or_unobserved"], s["legacy_unlinked"]), (1, 1, 1))
        self.assertEqual(s["corrections_per_100_observed"], 100.0)
        record_outcome(self.paths, {"decision_id": "d1", "reviewer": "human", "outcome": "no_correction_observed", "source_ref": REF})
        self.assertEqual(summarize(self.paths)["corrections_per_100_observed"], 0.0)
        self.assertEqual(summarize(self.paths)["outcome_revisions"], 1)

    def test_decision_count_validation(self):
        for value in (-1, 0.5, True, "2"):
            with self.assertRaises(TrackingError):
                validate_decision({"s": value})
        self.assertNotEqual(validate_decision({})["decision_id"], validate_decision({})["decision_id"])

    def test_detector_requires_displayed_brief_and_next_message_not_generic_complaints(self):
        events = discover_candidates([
            {"seed": "fix it", "brief": "ordinary reply", "source_ref": REF, "followup": "no i meant the PDF"},
            {"seed": "fix it", "brief": BRIEF, "source_ref": REF, "followup": "no i meant the PDF"},
            {"seed": "fix it", "brief": BRIEF, "source_ref": REF, "followup": "no i meant the PDF"},
            {"seed": "fix it", "brief": BRIEF, "source_ref": dict(REF, turn=20), "followup": None}])
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["correction_candidate"])
        self.assertIsNone(events[0]["verified_correction"])
        self.assertFalse(events[1]["followup_observed"])
        review = build_baseline_review(events, self.tmp / "baseline")
        key = read_json(review["key"])
        answers = self.tmp / "answers.json"
        write_json(answers, {"review_id": key["review_id"], "answers": {events[0]["id"]: {"pick": "uncertain"}}})
        r = import_baseline_answers(review["key"], answers)
        self.assertEqual(r["eligible_reviewed_denominator"], 0)
        self.assertIsNone(r["corrections_per_100_observed"])
        write_json(answers, {"review_id": key["review_id"], "answers": {events[0]["id"]: {"pick": "corrected"}}})
        self.assertEqual(import_baseline_answers(review["key"], answers)["corrections_per_100_observed"], 100.0)

    def test_raw_scanner_uses_actual_assistant_output_skips_child_and_harness(self):
        claude = self.tmp / "claude" / "project"
        claude.mkdir(parents=True)
        rows = [{"type": "user", "message": {"content": "read my mind: fix export"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": BRIEF}]}},
                {"type": "user", "isMeta": True, "message": {"content": "no i meant harness"}},
                {"type": "user", "message": {"content": "no i meant the PDF export"}}]
        (claude / "parent.jsonl").write_text("\n".join(json.dumps(x) for x in rows))
        (claude / "child.jsonl").write_text("\n".join(json.dumps(dict(x, isSidechain=True)) for x in rows))
        sdk = [dict(rows[0], entrypoint="sdk-cli")] + rows[1:]
        (claude / "sdk.jsonl").write_text("\n".join(json.dumps(x) for x in sdk))
        self.cfg["sources"] = {"claude_projects_dir": str(claude.parent), "codex_sessions_dir": str(self.tmp / "codex")}
        result = scan_transcripts(self.cfg)
        self.assertEqual((result["eligible_displayed_briefs"], result["observed_followups"], result["correction_candidates"]), (1, 1, 1))
        self.assertIsNone(result["confirmed_correction_rate"])
        self.assertEqual(result["events"][0]["followup_ref"]["turn"], 4)

    def test_baseline_outputs_preserve_unrelated_pages_and_symlink_targets(self):
        events = discover_candidates([{"seed": "fix it", "brief": BRIEF, "source_ref": REF, "followup": "no i meant PDF"}])
        outside = self.paths.profile
        outside.write_text("live model")
        for linked in (False, True):
            out = self.tmp / ("linked-baseline" if linked else "regular-baseline")
            out.mkdir()
            page = out / "baseline.html"
            if linked:
                page.symlink_to(outside)
            else:
                page.write_text("personal notes")
            with self.assertRaises(PreferenceError):
                build_baseline_review(events, out)
            self.assertEqual(outside.read_text(), "live model")
            self.assertFalse((out / "private").exists())

    def test_outcome_append_rejects_symlinked_target(self):
        write_jsonl(self.paths.decisions, [validate_decision({"decision_id": "d1"})])
        outside = self.paths.profile
        outside.write_text("live model")
        target = self.paths.home / "telemetry" / "outcomes.jsonl"
        target.parent.mkdir()
        target.symlink_to(outside)
        with self.assertRaises(PreferenceError):
            record_outcome(self.paths, {"decision_id": "d1", "reviewer": "human", "outcome": "corrected", "source_ref": REF})
        self.assertEqual(outside.read_text(), "live model")

    def test_baseline_same_review_can_update_generated_report(self):
        events = discover_candidates([{"seed": "fix it", "brief": BRIEF, "source_ref": REF, "followup": "no i meant PDF"}])
        review = build_baseline_review(events, self.tmp / "baseline")
        self.assertEqual(review, build_baseline_review(events, self.tmp / "baseline"))
        answers, report = self.tmp / "answers.json", self.tmp / "report.json"
        write_json(answers, {"review_id": review["review_id"], "answers": {}})
        self.assertFalse(import_baseline_answers(review["key"], answers, report)["completed"])
        write_json(answers, {"review_id": review["review_id"], "answers": {events[0]["id"]: {"pick": "corrected"}}})
        self.assertTrue(import_baseline_answers(review["key"], answers, report)["completed"])


if __name__ == "__main__":
    unittest.main()
