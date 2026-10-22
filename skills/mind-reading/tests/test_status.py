import json
import shutil
import unittest
from pathlib import Path

import helpers
from mr.config import read_json, write_json
from mr.status import estimate, format_text, gather


class StatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="eval")

    def test_gather_on_full_home(self):
        st = gather(self.paths, self.cfg)
        self.assertEqual(st["corpus"]["records"], 229)
        self.assertEqual(st["corpus"]["by_source"], {"claude-code": 149, "codex": 80})
        self.assertEqual(st["unknown_tags"], {"weird-tag": 2})
        self.assertEqual(st["chunks"], 2)
        self.assertEqual((st["mined"]["done"], st["mined"]["started"], len(st["mined"]["lenses"])), (2, 2, 7))  # chunks, not lens files
        self.assertEqual(len(st["ledger"]), 1)
        self.assertIsNotNone(st["model"])
        self.assertIsNone(st["eval_guard"])  # no settings file configured in tests
        self.assertEqual(st["blocked"], [])
        self.assertEqual((st["grants"]["total"], st["grants"]["active"], st["grants"]["errors"]), (1, 1, []))
        self.assertEqual(st["spool_pending"], 0)
        text = format_text(st)
        self.assertIn("corpus: 229 records", text)
        self.assertIn("mined: 2 of 2 chunk(s) x 7 lens(es)", text)
        self.assertIn("eval look: model", text)
        self.assertIn("grants: 1 total, 1 active", text)

    def test_invalid_grant_blocks_and_legacy_model_warns(self):
        good = self.paths.grants.read_text(encoding="utf-8")
        self.paths.grants.write_text(good + "\n## G9. Bypass\n- Quote: \"go ahead\" (2026-01-01, typed, x)\n- Scope: all\n- Covers: bypass permissions\n- Never covers: nothing\n- Status: active\n", encoding="utf-8")
        try:
            st = gather(self.paths, self.cfg)
            self.assertTrue(any("grants.md has" in b for b in st["blocked"]), st["blocked"])
            self.assertEqual((st["grants"]["total"], st["grants"]["active"], st["grants"]["invalid"]), (2, 1, 1))  # active means valid and active
            self.assertIn("grants: 2 total, 1 active, 1 invalid (never applied)", format_text(st))
            self.assertTrue(any("index.json is stale" in w for w in st["warnings"]), st["warnings"])  # grants.md changed since synthesize index
        finally:
            self.paths.grants.write_text(good, encoding="utf-8")
        import shutil
        idx_backup = self.paths.index.read_text(encoding="utf-8")
        manifest_backup = self.paths.model_json.read_text(encoding="utf-8")
        self.paths.index.unlink()
        self.paths.model_json.unlink()
        try:
            st = gather(self.paths, self.cfg)
            self.assertTrue(st["model"]["legacy"])
            self.assertFalse(any("no model" in b for b in st["blocked"]))
            self.assertTrue(any("legacy fallback" in w for w in st["warnings"]), st["warnings"])
            self.assertIn("model: legacy format", format_text(st))
        finally:
            self.paths.index.write_text(idx_backup, encoding="utf-8")
            self.paths.model_json.write_text(manifest_backup, encoding="utf-8")
        del shutil

    def test_loose_directory_modes_warn(self):
        import os
        os.chmod(self.paths.corpus_dir, 0o755)
        try:
            st = gather(self.paths, self.cfg)
            self.assertTrue(any("not mode 0700" in w and "corpus" in w for w in st["warnings"]), st["warnings"])
        finally:
            os.chmod(self.paths.corpus_dir, 0o700)

    def test_blocked_items_first(self):
        write_json(self.paths.unknown_tags, {"weird-tag": 50})
        try:
            st = gather(self.paths, self.cfg)
            self.assertTrue(any("unknown harness tags" in b for b in st["blocked"]))
            text = format_text(st)
            self.assertTrue(text.startswith("BLOCKED:"))
        finally:
            write_json(self.paths.unknown_tags, {"weird-tag": 2})

    def test_sources_gone_reported(self):
        manifest = read_json(self.paths.manifest)
        manifest["files"]["/nonexistent/transcript.jsonl"] = {"records_first_seen": 7, "records_seen": 7}
        write_json(self.paths.manifest, manifest)
        try:
            st = gather(self.paths, self.cfg)
            self.assertIn({"path": "/nonexistent/transcript.jsonl", "records_retained": 7}, st["sources_gone"])
            self.assertIn("source gone: /nonexistent/transcript.jsonl (7 records retained)", format_text(st))
        finally:
            del manifest["files"]["/nonexistent/transcript.jsonl"]
            write_json(self.paths.manifest, manifest)

    def test_calibration_from_decisions(self):
        shutil.copy(helpers.FIX / "decisions.jsonl", self.paths.decisions)
        try:
            st = gather(self.paths, self.cfg)
            cal = st["calibration"]
            self.assertEqual(cal["invocations"], 3)
            self.assertAlmostEqual(cal["mean_s"], 10 / 3, places=2)
            self.assertAlmostEqual(cal["mean_g"], 1.0)
            self.assertAlmostEqual(cal["held_rate"], 1 / 3, places=3)
            self.assertEqual(cal["modes"], {"execute": 1, "status": 1, "discuss": 1})
            self.assertIn("calibration: 3 invocations", format_text(st))
        finally:
            self.paths.decisions.unlink()

    def test_eval_guard_check(self):
        settings = self.paths.home / "settings.json"
        settings.write_text(json.dumps({"permissions": {"deny": []}}), encoding="utf-8")
        cfg = dict(self.cfg, claude_settings_path=str(settings))
        st = gather(self.paths, cfg)
        self.assertFalse(st["eval_guard"])
        self.assertTrue(any("permissions.deny guard" in b for b in st["blocked"]))
        settings.write_text(json.dumps({"permissions": {"deny": ["Read(%s/eval/**)" % self.paths.home]}}), encoding="utf-8")
        st = gather(self.paths, cfg)
        self.assertFalse(st["eval_guard"])  # a single leading slash is relative to the settings file: present but broken
        self.assertIn("single leading slash", st["eval_guard_note"])
        self.assertTrue(any("single leading slash" in b for b in st["blocked"]))
        self.assertIn("MISSING (deny rule", format_text(st))
        settings.write_text(json.dumps({"permissions": {"deny": ["Read(//%s/eval/**)" % str(self.paths.home).lstrip("/")]}}), encoding="utf-8")
        st = gather(self.paths, cfg)
        self.assertTrue(st["eval_guard"])
        self.assertIsNone(st["eval_guard_note"])

    def test_guard_rule_forms(self):
        from unittest import mock
        from mr.status import guard_rule_status
        home = Path("/home/user/.local/share/mind-reading")
        with mock.patch.object(Path, "home", return_value=Path("/home/user")):
            self.assertEqual(guard_rule_status("Read(~/.local/share/mind-reading/eval/**)", home), "ok")
            self.assertEqual(guard_rule_status("Read(//home/user/.local/share/mind-reading/eval/**)", home), "ok")
            self.assertEqual(guard_rule_status("Read(/home/user/.local/share/mind-reading/eval/**)", home), "broken")
            self.assertEqual(guard_rule_status("Read(~/.local/share/mind-reading/eval/*)", home), "ok")
            self.assertIsNone(guard_rule_status("Read(~/.local/share/other/eval/**)", home))
            self.assertIsNone(guard_rule_status("Bash(rm:*)", home))
            self.assertIsNone(guard_rule_status("Grep(~/.local/share/mind-reading/eval/**)", home))

    def test_codex_always_on_target_without_isolation_blocks(self):
        cfg = dict(self.cfg, always_on_targets=[str(self.paths.home / ".codex" / "AGENTS.md")], codex_isolate_home=False)
        st = gather(self.paths, cfg)
        self.assertTrue(any("codex_isolate_home is false" in b for b in st["blocked"]), st["blocked"])
        cfg = dict(self.cfg, always_on_targets=[str(self.paths.home / ".codex" / "AGENTS.md")], codex_isolate_home=True)
        st = gather(self.paths, cfg)
        self.assertFalse(any("codex_isolate_home" in b for b in st["blocked"]))

    def test_corrections_pending_and_unparseable(self):
        self.paths.corrections.write_text("garbage line\n2026-03-01 | seed: \"x\" | wrong: y | meant: z | scope: global | kind: fix\n", encoding="utf-8")
        try:
            st = gather(self.paths, self.cfg)
            self.assertEqual(st["corrections"]["total"], 1)
            self.assertEqual(st["corrections"]["pending"], 1)
            self.assertEqual(len(st["corrections"]["unparseable"]), 1)
            self.assertIn("unparseable line 1", format_text(st))
        finally:
            self.paths.corrections.unlink()

    def test_no_test_split_has_no_security_guard_requirement(self):
        split = read_json(self.paths.split)
        original = dict(split)
        split["no_test"] = True
        write_json(self.paths.split, split)
        settings = self.paths.home / "unguarded.json"
        write_json(settings, {"permissions": {"deny": []}})
        try:
            st = gather(self.paths, dict(self.cfg, claude_settings_path=str(settings)))
            self.assertIsNone(st["eval_guard"])
            self.assertFalse(st["eval_guard_applicable"])
            self.assertFalse(any("permissions.deny guard" in b for b in st["blocked"]))
            self.assertIn("not applicable: this split holds no test data", format_text(st))
        finally:
            write_json(self.paths.split, original)

    def test_estimate_derives_from_chunk_characters(self):
        st = gather(self.paths, self.cfg)
        est = estimate(self.paths, self.cfg, st)
        self.assertEqual(est["chunks"], 2)
        self.assertEqual((est["lenses"], est["mine_calls"]), (7, 14))  # one call per chunk per default lens
        self.assertEqual(est["eval_calls"], est["eval_sessions"] * 7)
        total_chars = sum(read_json(self.paths.prep_report)["chunk_chars"])
        lo, hi = (t // est["lenses"] for t in est["mine_tokens"])  # per lens: every lens rereads the whole chunk
        self.assertLess(lo, total_chars // 4 + 2 * 20000)  # a 200k-character chunk is tens of thousands of tokens, not hundreds
        self.assertGreater(hi, total_chars // 4)
        self.assertLess(hi, total_chars)  # never the inflated multi-turn figures
        self.assertIn("estimates", est["basis"])
        self.assertLess(est["eval_tokens"][1], est["eval_sessions"] * 7 * 60000)


if __name__ == "__main__":
    unittest.main()
