import unittest

import helpers
from mr.config import read_json


class PrepDedupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="prep")
        cls.sessions = {(s["source"], s["session"]): s for s in helpers.read_jsonl(cls.paths.sessions)}
        cls.report = read_json(cls.paths.prep_report)

    def session(self, part):
        return next(s for k, s in self.sessions.items() if part in k[1])

    def test_within_session_duplicate_dropped_far_repeat_kept(self):
        texts = [m["text"] for m in self.session("cs02")["messages"]]
        self.assertEqual(texts.count("also make the header logo smaller"), 1)
        self.assertEqual(texts.count("signed in on chrome"), 2)  # 3h50 apart
        self.assertEqual(self.report["within_session_duplicates_removed"], 1)

    def test_bulk_stamped_flagged_and_never_canonical(self):
        cx06 = self.session("cx06")
        self.assertFalse(cx06["time_reliable"])
        self.assertIn("codex:rollout-2026-03-15T20-12-00-cx06", self.report["bulk_stamped_sessions"])
        cs14 = self.session("cs14")
        self.assertTrue(cs14["time_reliable"])
        self.assertEqual(len(cs14["messages"]), 40)  # the earlier reliable session keeps everything

    def test_twin_copy_loses_overlap_keeps_novel(self):
        cx06 = self.session("cx06")
        texts = [m["text"] for m in cx06["messages"]]
        self.assertEqual(len(texts), 2)
        self.assertTrue(all("novel" in t for t in texts))
        pair = next(t for t in self.report["twins"] if "cx06" in t["session"])
        self.assertIn("cs14", pair["twin_of"])
        self.assertEqual(pair["dropped"], 38)
        self.assertAlmostEqual(pair["containment"], 0.95, places=2)

    def test_fork_keeps_novel_messages(self):
        cx07 = self.session("cx07")
        texts = [m["text"] for m in cx07["messages"]]
        self.assertEqual(texts, ["ok ship it", "push now", "thx looks good"])
        pair = next(t for t in self.report["twins"] if "cx07" in t["session"])
        self.assertIn("cx08", pair["twin_of"])
        self.assertEqual(pair["dropped"], 6)
        self.assertEqual(len(self.session("cx08")["messages"]), 6)

    def test_three_message_import_copy_is_flagged_and_emptied(self):
        cx11 = self.session("cx11")
        self.assertFalse(cx11["time_reliable"])
        self.assertEqual(cx11["bulk_messages"], 3)
        self.assertEqual(cx11["messages"], [])
        self.assertEqual(cx11["dropped"]["twin"], 3)
        self.assertEqual(cx11["split"], "train")
        pair = next(t for t in self.report["twins"] if "cx11" in t["session"])
        self.assertIn("cs12", pair["twin_of"])
        self.assertIn("codex:rollout-2026-04-02T08-00-00-cx11", self.report["bulk_stamped_sessions"])

    def test_import_plus_continuation_keeps_only_the_continuation(self):
        cx12 = self.session("cx12")
        self.assertFalse(cx12["time_reliable"])  # its first message carries the import stamp
        self.assertEqual(cx12["bulk_messages"], 4)
        self.assertEqual([m["text"] for m in cx12["messages"]], ["also check the footer links on every page", "and the cookie banner on mobile", "ok good push it"])
        self.assertTrue(all(m["time_reliable"] for m in cx12["messages"]))
        self.assertEqual(cx12["dropped"], {"twin": 0, "duplicate": 0, "leak": 0, "bulk_copy": 4})
        self.assertEqual(cx12["split"], "train")
        self.assertEqual(self.report["bulk_copies"], [{"session": "codex:rollout-2026-04-05T09-00-00-cx12", "dropped": 4, "messages_before": 7}])
        self.assertEqual(self.report["bulk_copies_removed"], 4)
        self.assertEqual(self.report["bulk_stamped_messages"], 47)  # 40 + 3 + 4

    def test_minute_spaced_typing_is_not_a_cluster(self):
        for part in ("cs09", "cs14"):
            s = self.session(part)
            self.assertTrue(s["time_reliable"], part)
            self.assertEqual(s["bulk_messages"], 0, part)

    def test_report_lists_pairs_and_no_twins_flag(self):
        self.assertEqual(len(self.report["twins"]), 3)
        paths, cfg = helpers.build_home(through="extract")
        from mr.prep import run_prep
        run_prep(paths, cfg, no_twins=True, log=helpers.quiet)
        rep = read_json(paths.prep_report)
        self.assertTrue(rep["no_twins"])
        self.assertEqual(rep["twins"], [])


if __name__ == "__main__":
    unittest.main()
