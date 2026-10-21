import hashlib
import json
import unittest

import helpers
from mr.config import read_json
from mr.prep import run_prep


class PrepSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="prep")
        cls.split = read_json(cls.paths.split)
        cls.report = read_json(cls.paths.prep_report)

    def test_auto_cutoff_takes_latest_sessions(self):
        c = self.split["counts"]
        # 17 eligible reliable sessions: max(ceil(0.2*17)=4, min(10, 17//2=8)) = 8 test sessions
        self.assertEqual(c["test"], 8)
        self.assertEqual(c["dev"], 1)
        self.assertTrue(all(sid > "" for sid in self.split["test_ids"]))
        self.assertIn("codex:rollout-2026-03-28T11-00-00-cx09", self.split["test_ids"])
        self.assertIn("claude-code:cs12", self.split["test_ids"])

    def test_explicit_cutoff_respected(self):
        paths, cfg = helpers.build_home(through="extract")
        out = run_prep(paths, cfg, cutoff="2026-03-01", log=helpers.quiet)["split"]
        self.assertEqual(sorted(out["test_ids"]), ["claude-code:cs10", "claude-code:cs11", "claude-code:cs12", "claude-code:cs13",
                                                   "codex:rollout-2026-03-28T11-00-00-cx09"])
        self.assertIn("claude-code:cs08", out["train_ids"])  # ineligible pre-cutoff session goes to train
        self.assertNotEqual(out["split_id"], self.split["split_id"])

    def test_dev_from_tail_of_pre_cutoff_and_unreliable_never_held_out(self):
        self.assertEqual(self.split["dev_ids"], ["claude-code:cs14"])
        held = set(self.split["test_ids"]) | set(self.split["dev_ids"])
        for sid in ("codex:rollout-2026-03-15T20-12-00-cx06", "codex:rollout-2026-04-02T08-00-00-cx11", "codex:rollout-2026-04-05T09-00-00-cx12"):
            self.assertNotIn(sid, held)  # import artifacts, however late their fake stamps, are never test or dev
            self.assertIn(sid, self.split["train_ids"])
        self.assertLess(self.split["cutoff"], "2026-04-01")  # the auto cutoff ignores the fake April stamps

    def test_keep_split_freezes_test_and_dev_ids(self):
        tmp = helpers.tmpdir()
        claude, codex = helpers.copy_fixture_sources(tmp / "src")
        paths, cfg = helpers.init_home(tmp / "home")
        from mr.corpus import run_extract
        run_extract(paths, cfg, claude_dir=claude, codex_dir=codex, log=helpers.quiet)
        first = run_prep(paths, cfg, cutoff="auto", log=helpers.quiet)["split"]
        # a new real session, later than every test session, arrives before the monthly refresh
        rows = []
        for k, text in enumerate(["add rate limiting to the public api", "start with the search endpoint", "then the export endpoint", "report when the tests pass"]):
            rows.append(json.dumps({"type": "user", "uuid": "00000000-0000-0000-0000-0000000100%02d" % k, "timestamp": "2026-04-20T10:%02d:00.000Z" % (k * 5),
                                    "userType": "external", "entrypoint": "cli", "cwd": "/home/user/Code/webshop", "sessionId": "cs90",
                                    "message": {"role": "user", "content": text}}))
        (claude / "-home-user-Code-webshop" / "cs90.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        run_extract(paths, cfg, claude_dir=claude, codex_dir=codex, log=helpers.quiet)
        kept = run_prep(paths, cfg, cutoff="auto", keep_split=True, log=helpers.quiet)
        ks = kept["split"]
        self.assertEqual(ks["split_id"], first["split_id"])
        self.assertEqual(ks["test_ids"], first["test_ids"])
        self.assertEqual(ks["dev_ids"], first["dev_ids"])
        self.assertEqual(ks["cutoff"], first["cutoff"])
        self.assertTrue(ks["frozen"])
        self.assertIn("claude-code:cs90", ks["train_ids"])
        # the new session and the short post-cutoff session (dropped from a fresh split) both join train under a kept split
        self.assertEqual(kept["report"]["post_cutoff_in_train"], ["claude-code:cs08", "claude-code:cs90"])
        self.assertEqual(ks["counts"]["post_cutoff_in_train"], 2)
        self.assertEqual(kept["report"]["ineligible_post_cutoff_dropped"], [])
        fresh = run_prep(paths, cfg, cutoff="auto", log=helpers.quiet)["split"]  # without keep_split the new session joins test
        self.assertIn("claude-code:cs90", fresh["test_ids"])
        self.assertNotEqual(fresh["split_id"], first["split_id"])
        with self.assertRaises(Exception):
            run_prep(helpers.init_home(tmp / "home2")[0], cfg, keep_split=True, log=helpers.quiet)

    def test_ineligible_post_cutoff_dropped(self):
        self.assertEqual(self.report["ineligible_post_cutoff_dropped"], ["claude-code:cs08"])
        self.assertNotIn("claude-code:cs08", self.split["train_ids"] + self.split["test_ids"])

    def test_split_id_stable_and_truth_suffix_derived(self):
        paths, cfg = helpers.build_home(through="prep")
        again = read_json(paths.split)
        self.assertEqual(again["split_id"], self.split["split_id"])
        self.assertEqual(again["cutoff"], self.split["cutoff"])
        expected = hashlib.sha256((self.split["cutoff"] + "\n" + "\n".join(sorted(self.split["test_ids"]))).encode()).hexdigest()[:8]
        self.assertEqual(self.split["split_id"], expected)
        self.assertEqual(self.split["truth_suffix"], hashlib.sha256(self.split["split_id"].encode()).hexdigest()[:4])
        self.assertTrue((self.paths.eval / self.split["split_id"] / ("truth-" + self.split["truth_suffix"])).is_dir())

    def test_seeds_skipped_for_non_typed_first_message(self):
        skipped = self.report["seeds_skipped_non_typed_first"]["test"]
        self.assertIn("claude-code:cs13", skipped)
        self.assertIn("codex:rollout-2026-01-30T16-00-00-cx10", skipped)
        self.assertEqual(self.split["counts"]["test_seeds"], 6)
        seeds_dir = self.paths.eval / self.split["split_id"] / "seeds"
        seeds = [json.loads(p.read_text()) for p in sorted(seeds_dir.glob("s*.json"))]
        self.assertEqual([s["id"] for s in seeds], ["s%02d" % k for k in range(6)])
        self.assertTrue(all(set(s) >= {"id", "source", "time", "seed", "project"} for s in seeds))

    def test_truth_caps(self):
        paths, cfg = helpers.build_home(through="extract", prep_overrides={"truth_max_messages": 3, "truth_max_chars": 20})
        out = run_prep(paths, cfg, cutoff="auto", log=helpers.quiet)["split"]
        tdir = paths.eval / out["split_id"] / ("truth-" + out["truth_suffix"])
        for p in tdir.glob("s*.json"):
            later = json.loads(p.read_text())["later"]
            self.assertLessEqual(len(later), 2)
            self.assertTrue(all(len(m["text"]) <= 20 for m in later))
            self.assertTrue(all(set(m) == {"time", "origin", "text"} for m in later))

    def test_eval_dir_mode(self):
        import os
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.eval).st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()


class NoTestSplitBoundaryTests(unittest.TestCase):
    def test_train_before_trims_messages_after_the_boundary(self):
        import datetime as _dt
        from mr.prep import no_test_split
        utc = _dt.timezone.utc
        straddle = {"id": "s1", "time_reliable": True, "start": _dt.datetime(2026, 1, 1, 10, tzinfo=utc),
                    "messages": [{"time": "2026-01-01T10:00:00Z", "text": "before"},
                                 {"time": "2026-01-03T09:00:00Z", "text": "after"},
                                 {"time": None, "text": "unknown time"}]}
        later = {"id": "s2", "time_reliable": True, "start": _dt.datetime(2026, 1, 4, tzinfo=utc),
                 "messages": [{"time": "2026-01-04T00:00:00Z", "text": "x"}]}
        split = no_test_split([straddle, later], {}, "2026-01-02T00:00:00Z")
        self.assertEqual([s["id"] for s in split["train"]], ["s1"])
        self.assertEqual([m["text"] for m in split["train"][0]["messages"]], ["before"])
        self.assertEqual(split["trimmed_after_train_before"], 2)
        self.assertEqual(split["dropped_post_cutoff"], ["s2"])
        self.assertEqual(len(straddle["messages"]), 3)  # input not mutated
