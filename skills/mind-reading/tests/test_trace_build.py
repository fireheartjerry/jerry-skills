import unittest
import helpers
from mr.trace_build import project_identity, nearby_outcomes


class BuildTests(unittest.TestCase):
    def test_project_aliases_need_verified_prefixes(self):
        aliases = {"c:/work/app": "verified-app"}
        self.assertEqual(project_identity("C:\\work\\app\\src", aliases), "verified-app")
        self.assertNotEqual(project_identity("C:/other/app", aliases), "verified-app")
        self.assertNotEqual(project_identity("C:/work/application", aliases), "verified-app")

    def test_nearby_commits_never_use_future_revert_or_retention(self):
        ex = {"project": "p", "seed_time": "2026-01-01T00:00:00Z", "target_time": "2026-01-02T00:00:00Z"}
        repo = {"project": "p", "commits": [
            {"id": "aaaa", "source_ref": "git:a", "subject": "A change", "committed_at": "2026-01-01T01:00:00Z", "reachable_at_snapshot": True},
            {"id": "bbbb", "source_ref": "git:b", "subject": "Revert", "committed_at": "2026-02-01T00:00:00Z", "explicit_revert_targets": ["aaaa"]}]}
        observed = nearby_outcomes(ex, [repo], "2026-01-03T00:00:00Z")
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0]["explicit_revert_observed_by_training_cutoff"])
        self.assertNotIn("reachable_at_snapshot", observed[0])
        self.assertTrue(nearby_outcomes(ex, [repo], "2026-03-01T00:00:00Z")[0]["explicit_revert_observed_by_training_cutoff"])


if __name__ == "__main__": unittest.main()
