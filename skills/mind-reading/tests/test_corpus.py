import json
import unittest
from pathlib import Path

import helpers
from mr.config import read_json
from mr.corpus import load_corpus, run_extract


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.claude, self.codex = helpers.copy_fixture_sources(self.tmp / "src")
        self.paths, self.cfg = helpers.init_home(self.tmp / "home")

    def extract(self):
        return run_extract(self.paths, self.cfg, claude_dir=self.claude, codex_dir=self.codex, log=helpers.quiet)

    def test_first_run_writes_second_appends_nothing(self):
        s1 = self.extract()
        n = s1["total"]
        self.assertGreater(n, 100)
        self.assertEqual(sum(s1["added"].values()), n)
        s2 = self.extract()
        self.assertEqual(sum(s2["added"].values()), 0)
        self.assertEqual(s2["total"], n)
        self.assertEqual(len(load_corpus(self.paths)), n)

    def test_truncated_source_keeps_records_and_manifest_reports(self):
        n = self.extract()["total"]
        target = next(self.codex.rglob("*cx09.jsonl"))
        lines = target.read_text(encoding="utf-8").splitlines()
        target.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
        s2 = self.extract()
        self.assertEqual(s2["total"], n)
        entry = read_json(self.paths.manifest)["files"][str(target)]
        self.assertLess(entry["records_seen"], entry["records_first_seen"])

    def test_new_record_appends_exactly_one(self):
        n = self.extract()["total"]
        target = self.claude / "-home-user-Code-webshop" / "cs12.jsonl"
        row = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
        row["uuid"] = "00000000-0000-0000-0000-00000000abcd"
        row["message"] = {"role": "user", "content": "a brand new typed message"}
        row["timestamp"] = "2026-03-20T17:00:00.000Z"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        s2 = self.extract()
        self.assertEqual(s2["total"], n + 1)
        self.assertEqual(s2["added"]["claude-code"], 1)

    def test_explicit_dirs_saved_to_config_and_logged(self):
        logs = []
        run_extract(self.paths, self.cfg, claude_dir=self.claude, codex_dir=self.codex, log=logs.append)
        from mr.config import load_config
        cfg = load_config(self.paths)
        self.assertEqual(cfg["sources"]["claude_projects_dir"], str(self.claude))
        self.assertEqual(cfg["sources"]["codex_sessions_dir"], str(self.codex))
        self.assertTrue(any("sources claude=%s" % self.claude in l for l in logs), logs)
        s2 = run_extract(self.paths, cfg, log=helpers.quiet)  # no dirs passed: the saved ones are read
        self.assertEqual(sum(s2["added"].values()), 0)
        self.assertGreater(sum(s2["seen"].values()), 100)

    def test_codex_rollout_rewritten_in_place_adds_nothing(self):
        n = self.extract()["total"]
        target = next(self.codex.rglob("*cx09.jsonl"))
        lines = target.read_text(encoding="utf-8").splitlines()
        filler = json.dumps({"timestamp": "2026-03-28T11:00:30.000Z", "type": "event_msg", "payload": {"type": "token_count"}})
        target.write_text("\n".join([lines[0], filler, filler] + lines[1:]) + "\n", encoding="utf-8")  # every ordinal shifts by two
        s2 = self.extract()
        self.assertEqual(s2["total"], n)
        self.assertEqual(sum(s2["added"].values()), 0)

    def test_keys_stable_across_homes(self):
        self.extract()
        keys_a = sorted(r["key"] for r in load_corpus(self.paths))
        paths_b, cfg_b = helpers.init_home(self.tmp / "home-b")
        run_extract(paths_b, cfg_b, claude_dir=self.claude, codex_dir=self.codex, log=helpers.quiet)
        keys_b = sorted(r["key"] for r in load_corpus(paths_b))
        self.assertEqual(keys_a, keys_b)

    def test_rescrub_bumps_version_keeps_keys(self):
        self.extract()
        rows = load_corpus(self.paths)
        rows[0]["scrub_version"] = 0
        with open(self.paths.messages, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        before = [r["key"] for r in rows]
        run_extract(self.paths, self.cfg, rescrub=True, log=helpers.quiet)
        after = load_corpus(self.paths)
        self.assertEqual([r["key"] for r in after], before)
        self.assertTrue(all(r["scrub_version"] >= 1 for r in after))

    def test_unknown_tags_and_scrub_report_written(self):
        self.extract()
        self.assertEqual(read_json(self.paths.unknown_tags).get("weird-tag"), 2)  # cs11 and its import copy cx12
        report = read_json(self.paths.scrub_report)
        counts = report["runs"][-1]["counts"]
        self.assertGreaterEqual(counts.get("email", 0), 1)
        self.assertGreaterEqual(counts.get("person:<teammate>", 0), 1)
        texts = " ".join(r["text"] for r in load_corpus(self.paths))
        self.assertNotIn("Sam Taylor", texts)
        self.assertNotIn("example.com", texts)
        self.assertNotIn("/home/user", texts)
        self.assertIn("<teammate>", texts)


if __name__ == "__main__":
    unittest.main()
