import datetime as dt
import json
import unittest
from unittest.mock import patch

import helpers
from mr import foresight


class ForesightTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg = helpers.build_home(through="synthesize")
        self.now = dt.datetime(2026, 3, 1, 12, tzinfo=dt.timezone.utc)
        self.clock = patch.object(foresight, "now_utc", lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.data = {"seed": "fix this export", "source_ref": {"source": "test", "session": "one", "turn": "seed"},
                     "predictions": [{"text": "The real downloaded file needs verification.", "support": ["episode:export"], "confidence": "L"}]}

    def test_registration_is_timestamped_hash_bound_and_private(self):
        row = foresight.record(self.paths, self.data)
        self.assertEqual(row["created_at"], "2026-03-01T12:00:00Z")
        self.assertNotIn("seed", row)
        self.assertEqual(len(row["prediction_hash"]), 64)
        self.assertEqual(len(row["model_hash"]), 16)
        self.assertEqual(len(row["runtime_sha256"]), 64)
        file = self.paths.home / "telemetry/forecasts.jsonl"
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(foresight.report(self.paths)["pending_predictions"], 1)
        self.assertIsNone(foresight.report(self.paths)["wanted_per_100_scored_predictions"])

    def observe(self, row, outcome="wanted", **changes):
        self.now += dt.timedelta(minutes=1)
        data = {"forecast_id": row["forecast_id"], "reviewer": "human", "observed_at": self.now.isoformat(),
                "source_ref": {"source": "test", "session": "one", "turn": "later"},
                "labels": [{"prediction_id": row["predictions"][0]["id"], "outcome": outcome}]}
        data.update(changes)
        return foresight.observe(self.paths, data)

    def test_only_reviewed_actual_later_turns_enter_the_precision_denominator(self):
        row = foresight.record(self.paths, self.data)
        self.observe(row)
        report = foresight.report(self.paths)
        self.assertEqual(report["scored_predictions"], 1)
        self.assertEqual(report["wanted_per_100_scored_predictions"], 100.0)
        self.observe(row, "unresolved")
        report = foresight.report(self.paths)
        self.assertEqual(report["unresolved_predictions"], 1)
        self.assertIsNone(report["wanted_per_100_scored_predictions"])
        self.observe(row, "unwanted")
        self.assertEqual(foresight.report(self.paths)["wanted_per_100_scored_predictions"], 0.0)

    def test_post_hoc_or_future_or_nonhuman_observations_are_refused(self):
        row = foresight.record(self.paths, self.data)
        for changes in ({"observed_at": "2026-03-01T11:59:00Z"}, {"observed_at": "2027-03-01T12:00:00Z"},
                        {"reviewer": "detector"}, {"source_ref": self.data["source_ref"]}):
            with self.subTest(changes=changes), self.assertRaises(foresight.ForesightError):
                self.observe(row, **changes)

    def test_unknown_prediction_and_duplicate_labels_are_refused(self):
        row = foresight.record(self.paths, self.data)
        label = {"prediction_id": row["predictions"][0]["id"], "outcome": "wanted"}
        for labels in ([dict(label, prediction_id="unknown")], [label, label]):
            with self.assertRaises(foresight.ForesightError):
                self.observe(row, labels=labels)

    def test_abstentions_are_not_silently_called_success(self):
        foresight.record(self.paths, dict(self.data, predictions=[]))
        result = foresight.report(self.paths)
        self.assertEqual(result["abstentions"], 1)
        self.assertEqual(result["scored_predictions"], 0)
        self.assertIsNone(result["wanted_per_100_scored_predictions"])

    def test_future_target_fields_cannot_be_registered(self):
        for bad in (dict(self.data, next_user_turn="the actual future"), dict(self.data, created_at="2025-01-01")):
            with self.assertRaises(foresight.ForesightError):
                foresight.record(self.paths, bad)
        with self.assertRaises(foresight.ForesightError):
            foresight.record(self.paths, dict(self.data, decision_id="invented"))

    def test_cli_registration_and_unknown_outcomes_are_observable(self):
        result = helpers.run_cli(self.paths.home, "foresight", "record", "--json", stdin=json.dumps(self.data))
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertTrue(row["forecast_id"])
        result = helpers.run_cli(self.paths.home, "foresight", "report", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["pending_predictions"], 1)
        self.assertIsNone(report["wanted_per_100_scored_predictions"])

    def test_malformed_ids_raise_the_declared_error(self):
        with self.assertRaises(foresight.ForesightError):
            foresight.observe(self.paths, {"forecast_id": []})
        row = foresight.record(self.paths, self.data)
        with self.assertRaises(foresight.ForesightError):
            self.observe(row, labels=[{"prediction_id": [], "outcome": "wanted"}])

    def test_linked_decision_cannot_be_attributed_to_a_different_model(self):
        self.paths.decisions.write_text(json.dumps({"decision_id": "old", "model_hash": "0" * 16}) + "\n")
        with self.assertRaises(foresight.ForesightError):
            foresight.record(self.paths, dict(self.data, decision_id="old"))

    def test_tampering_after_registration_is_visible_and_cannot_be_scored(self):
        row = foresight.record(self.paths, self.data)
        file = self.paths.home / "telemetry/forecasts.jsonl"
        changed = json.loads(file.read_text())
        changed["predictions"][0]["text"] = "A different prediction after seeing the future."
        file.write_text(json.dumps(changed) + "\n")
        self.assertEqual(foresight.report(self.paths)["invalid_forecasts"], 1)
        with self.assertRaises(foresight.ForesightError):
            self.observe(row)

    def test_partial_and_invalid_utf8_rows_cannot_eat_a_later_valid_forecast(self):
        path = self.paths.home / "telemetry/forecasts.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'\xff\n{"interrupted":')
        foresight.record(self.paths, self.data)
        report = foresight.report(self.paths)
        self.assertEqual(report["forecasts"], 1)
        self.assertEqual(report["invalid_forecasts"], 2)
        self.assertEqual(report["pending_predictions"], 1)


if __name__ == "__main__":
    unittest.main()
