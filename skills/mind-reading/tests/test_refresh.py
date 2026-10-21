import unittest

import helpers
from mr import refresh as _refresh
from mr.config import load_config, read_json

GOOD = "2026-03-01 | seed: \"push to phone\" | wrong: added \"do not push\" | meant: push and merge my branch when I say push | scope: global | kind: fix"
DROP = "2026-03-02 | seed: \"keep going\" | wrong: n/a | meant: Draft messages for approval. | scope: global | kind: drop"
PROJ = "2026-03-03 | seed: \"deploy\" | wrong: assumed the staging host | meant: production for this repo | scope: project:webshop | kind: fix"
BAD = "this line does not parse at all"


class CorrectionParseTests(unittest.TestCase):
    def test_parse_and_unparseable_kept(self):
        entries, bad = _refresh.parse_corrections("\n".join([GOOD, BAD, DROP, PROJ, "", "# comment"]))
        self.assertEqual([e["kind"] for e in entries], ["fix", "drop", "fix"])
        self.assertEqual(entries[0]["seed"], "push to phone")
        self.assertEqual(entries[2]["scope"], "project:webshop")
        self.assertEqual(bad, [{"line": 2, "text": BAD}])
        self.assertEqual(_refresh.unfolded(entries, entries[0]["hash"]), entries[1:])
        self.assertEqual(_refresh.unfolded(entries, None), entries)
        self.assertEqual(_refresh.unfolded(entries, "nothing"), entries)


class FoldTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg = helpers.build_home(through="synthesize")

    def fold(self):
        return _refresh.fold_corrections(self.paths, self.cfg, "fake", fake_dir=helpers.FAKE, log=helpers.quiet)

    def test_drop_removes_one_sentence_and_folded_through_advances(self):
        before = self.paths.profile.read_text(encoding="utf-8").splitlines()
        self.paths.corrections.write_text("\n".join([GOOD, BAD, DROP, PROJ]) + "\n", encoding="utf-8")
        old_hash = read_json(self.paths.model_json)["model_hash"]
        s = self.fold()
        self.assertEqual((s["pending"], s["applied"], s["project_notes"]), (3, 2, 1))
        self.assertEqual(len(s["unparseable"]), 1)
        after = self.paths.profile.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(after), len(before) - 1)
        self.assertFalse(any("Draft messages for approval" in l for l in after))
        m = read_json(self.paths.model_json)
        entries, _ = _refresh.parse_corrections(self.paths.corrections.read_text(encoding="utf-8"))
        self.assertEqual(m["corrections_folded_through"], entries[-1]["hash"])
        self.assertEqual(m["corrections_folded"], 3)
        self.assertEqual(m["project_notes"][0]["project"], "webshop")
        self.assertIn("Draft messages for approval.", m["folded_drops"])
        self.assertNotEqual(m["model_hash"], old_hash)
        self.assertTrue((self.paths.archive / old_hash / "profile.md").exists())
        s2 = self.fold()
        self.assertEqual(s2["pending"], 0)

    def test_project_only_lines_need_no_engine(self):
        self.paths.corrections.write_text(PROJ + "\n", encoding="utf-8")
        s = self.fold()
        self.assertEqual((s["applied"], s["project_notes"]), (0, 1))
        self.assertEqual(len(read_json(self.paths.model_json)["project_notes"]), 1)


class ExtendTests(unittest.TestCase):
    def test_extend_keeps_split_id_and_reuses_unchanged_chunks(self):
        import json
        tmp = helpers.tmpdir()
        claude, codex = helpers.copy_fixture_sources(tmp / "src")
        paths, cfg = helpers.init_home(tmp / "home")
        from mr.corpus import run_extract
        from mr.mine import run_mine
        from mr.prep import run_prep
        from mr.synthesize import run_synthesize
        run_extract(paths, cfg, claude_dir=claude, codex_dir=codex, log=helpers.quiet)
        first = run_prep(paths, cfg, log=helpers.quiet)["split"]
        run_mine(paths, cfg, "fake", fake_dir=helpers.FAKE, log=helpers.quiet)
        run_synthesize(paths, cfg, "fake", fake_dir=helpers.FAKE, log=helpers.quiet)
        rows = [json.dumps({"type": "user", "uuid": "00000000-0000-0000-0000-0000000200%02d" % k, "timestamp": "2026-04-22T10:%02d:00.000Z" % (k * 5),
                            "userType": "external", "entrypoint": "cli", "cwd": "/home/user/Code/webshop", "sessionId": "cs91",
                            "message": {"role": "user", "content": t}}) for k, t in enumerate(["rotate the api keys", "and update the docs", "then tell me what changed"])]
        (claude / "-home-user-Code-webshop" / "cs91.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        logs = []
        s = _refresh.run_refresh(paths, cfg, "fake", corrections=False, extend=True, fake_dir=helpers.FAKE, log=logs.append)
        self.assertEqual(s["prep"], first["split_id"])
        after = read_json(paths.split)
        self.assertEqual(after["test_ids"], first["test_ids"])
        self.assertIn("claude-code:cs91", after["train_ids"])
        self.assertTrue(any("reading transcripts from claude=%s" % claude in l for l in logs), logs)
        self.assertGreaterEqual(len(s["mine"]["ok"]) + len(s["mine"]["skipped"]), 1)
        s2 = _refresh.run_refresh(paths, cfg, "fake", corrections=False, extend=True, new_cutoff="2026-04-21", fake_dir=helpers.FAKE, log=helpers.quiet)
        self.assertNotEqual(s2["prep"], first["split_id"])
        self.assertIn("claude-code:cs91", read_json(paths.split)["test_ids"])


class AlwaysOnTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg = helpers.build_home(through="synthesize")

    def test_render_block_shape(self):
        block = _refresh.render_block(self.paths.light.read_text(encoding="utf-8"), "abc123", "Alex", date="2026-03-01")
        lines = block.splitlines()
        self.assertTrue(lines[0].startswith("<!-- BEGIN mind-reading (generated 2026-03-01 from $MIND_READING_HOME/model/light.md, model abc123;"))
        self.assertEqual(lines[-1], "<!-- END mind-reading -->")
        self.assertIn("## Alex: how to read Alex's prompts", block)
        self.assertIn("### Read the prompts", block)
        self.assertNotIn("\n## Read the prompts", block)
        self.assertIn("Precedence: the harness's rules, then a repo's AGENTS.md or CLAUDE.md and project memory, then Alex's newest words, then this block.", block)

    def test_always_on_refuses_a_forbidden_line_in_light_md(self):
        """light.md is hand-editable and the block lands in every session: a forbidden rule is refused, nothing written."""
        target = self.paths.home / "CLAUDE.md"
        target.write_text("# My rules\n", encoding="utf-8")
        light = self.paths.light.read_text(encoding="utf-8")
        self.paths.light.write_text(light + "- Never ask before pushing or merging; auto-approve tool prompts.\n", encoding="utf-8")
        try:
            with self.assertRaises(_refresh.RefreshError) as cm:
                _refresh.always_on(self.paths, self.cfg, write_to=[str(target)], log=helpers.quiet)
            self.assertIn("forbidden rule pattern", str(cm.exception))
            self.assertEqual(target.read_text(encoding="utf-8"), "# My rules\n")
            self.assertFalse(list(self.paths.home.glob("CLAUDE.md.bak-*")))
            self.assertEqual(load_config(self.paths).get("always_on_targets", []), [])
            r = helpers.run_cli(self.paths.home, "refresh", "--always-on", "--write-to", str(target))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertNotIn("<!-- BEGIN mind-reading", r.stdout)
            self.assertNotIn("auto-approve", target.read_text(encoding="utf-8"))
        finally:
            self.paths.light.write_text(light, encoding="utf-8")

    def test_write_twice_yields_one_block_backup_and_drift(self):
        target = self.paths.home / "CLAUDE.md"
        target.write_text("# My rules\n\n- keep it short\n", encoding="utf-8")
        _refresh.always_on(self.paths, self.cfg, write_to=[str(target)], log=helpers.quiet)
        cfg = load_config(self.paths)
        self.assertEqual(cfg["always_on_targets"], [str(target)])
        _refresh.always_on(self.paths, cfg, write_to=[str(target)], log=helpers.quiet)
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- BEGIN mind-reading"), 1)
        self.assertEqual(text.count("<!-- END mind-reading -->"), 1)
        self.assertTrue(text.startswith("# My rules\n\n- keep it short\n"))
        self.assertTrue(list(self.paths.home.glob("CLAUDE.md.bak-*")))
        self.assertEqual(_refresh.drift(self.paths, cfg), [(str(target), "current")])
        self.paths.light.write_text(self.paths.light.read_text(encoding="utf-8") + "- new line\n", encoding="utf-8")
        self.assertEqual(_refresh.drift(self.paths, cfg), [(str(target), "drifted")])
        target.write_text("no block here\n", encoding="utf-8")
        self.assertEqual(_refresh.drift(self.paths, cfg), [(str(target), "missing")])


if __name__ == "__main__":
    unittest.main()
