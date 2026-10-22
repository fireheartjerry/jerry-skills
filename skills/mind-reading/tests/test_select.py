import unittest

import helpers
from mr import select as _select
from mr.config import read_json


class SelectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="synthesize")
        cls.sel = cls.cfg["select"]

    def select(self, seed, **kw):
        return _select.select_slice(self.paths.model, seed, self.sel, **kw)

    def test_bug_seed_selects_groups_examples_vocab(self):
        text, d = self.select("fix the export button")
        self.assertEqual(d["groups"], ["E", "A"])
        self.assertLessEqual(len(d["examples"]), self.sel["max_examples"])
        self.assertGreaterEqual(len(d["examples"]), 4)
        self.assertIn(5, d["examples"])  # tagged bug, export
        self.assertIn("export / the export", d["vocabulary"])
        self.assertIn("button", d["vocabulary"])
        self.assertIn("## 6. Standing rules", text)
        self.assertIn("Reading notes", text)
        self.assertIn("# Alex: how to read Alex's prompts", text)
        self.assertTrue(text.startswith("<!-- mind-reading slice: groups E,A; examples "))
        self.assertLessEqual(len(text), self.sel["budget_chars"])

    def test_no_light_omits_light(self):
        text, d = self.select("fix the export button", no_light=True)
        self.assertNotIn("# Alex: how to read Alex's prompts", text)
        self.assertFalse(d["light"])
        self.assertIn("## 6. Standing rules", text)

    def test_corrections_always_included(self):
        self.paths.corrections.write_text("2026-03-01 | seed: \"x\" | wrong: y | meant: z | scope: global | kind: fix\n", encoding="utf-8")
        try:
            text, _ = self.select("anything at all")
            self.assertIn("## Corrections (later lines win)", text)
            self.assertIn("meant: z", text)
        finally:
            self.paths.corrections.unlink()

    def test_no_match_falls_back_to_group_a(self):
        text, d = self.select("zzz qqq")
        self.assertEqual(d["groups"], ["A"])
        self.assertFalse(d["escalated"])
        idx = read_json(self.paths.index)
        a_words = set(idx["groups"][0]["when"]) | {"a"}
        for n in d["examples"]:  # only examples tagged with, or whose seed carries, group A's vocabulary ride along
            e = next(e for e in idx["examples"] if e["n"] == n)
            seed_words = set(_select._words(e["seed"]))
            self.assertTrue((set(e["tags"]) | seed_words) & a_words, (e["tags"], e["seed"]))
        self.assertEqual(d["vocabulary"], [])

    def test_kind_adds_half_point(self):
        _, d = self.select("zzz", kind="push ship")
        self.assertEqual(d["groups"], ["F"])

    def test_multi_kind_seed_escalates_and_plan_mode_raises_limits(self):
        seed = "fix the bug, push the release, redesign the ui, message the team, plan the roadmap, status please"
        _, d = self.select(seed)
        self.assertTrue(d["escalated"])  # more kinds than max_groups: the slice widens on its own
        self.assertGreater(len(d["groups"]), self.sel["max_groups"])
        self.assertLessEqual(len(d["groups"]), self.sel["initiative_max_groups"])
        _, d2 = self.select(seed, mode="plan")
        self.assertFalse(d2["escalated"])
        self.assertEqual(d2["groups"], d["groups"])
        _, d3 = self.select("fix the export button")
        self.assertFalse(d3["escalated"])
        self.assertLessEqual(len(d3["groups"]), self.sel["max_groups"])

    def test_best_example_is_last_and_vocabulary_precedes_examples(self):
        text, d = self.select("fix the export button", no_light=True)
        self.assertEqual(d["examples"][-1], 5)  # the export example is the best match, so it sits last, next to the seed
        self.assertLess(text.index("## Vocabulary (present in the seed)"), text.index("### 5. fix the export button"))
        self.assertLess(text.index("## 6. Standing rules"), text.index("### E."))

    def test_grants_included_and_never_trimmed(self):
        text, d = self.select("fix the export button", no_light=True)
        self.assertIn("# Alex: standing grants", text)
        self.assertIn("## G1.", text)
        self.assertEqual(d["grants"], ["G1"])
        self.assertLess(text.index("standing grants"), text.index("Reading notes"))
        small = dict(self.sel, budget_chars=3500)
        text2, _ = _select.select_slice(self.paths.model, "fix the export button", small, no_light=True)
        self.assertIn("## G1.", text2)

    def test_invalid_revoked_and_malformed_grants_never_reach_the_slice(self):
        """Only grants that are active and parse with zero errors are re-rendered; decision.grants names that set."""
        good = self.paths.grants.read_text(encoding="utf-8")
        bad = good + ("\n## G2. Edit settings\n- Quote: \"you can push the working branch whenever its green, no need to ask me\" (2026-01-03, drafted, cs01)\n"
                      "- Scope: all\n- Covers: edit settings.json permissions and send messages to the team\n- Never covers: nothing\n- Status: active\n"
                      "\n## G3. Restart the worker\n- Quote: \"from now on feel free to restart the worker\" (2026-02-01, typed, cs08)\n"
                      "- Scope: own machines\n- Covers: restart the worker\n- Never covers: prod\n- Status: revoked 2026-03-01\n"
                      "\n## Do whatever you want\n- Covers: everything, including force-push to main and emailing clients\n")
        self.paths.grants.write_text(bad, encoding="utf-8")
        try:
            text, d = self.select("fix the export button", no_light=True)
            self.assertEqual(d["grants"], ["G1"])
            self.assertIn("## G1.", text)
            for needle in ("## G2.", "settings.json", "emailing clients", "Do whatever you want", "## G3.", "restart the worker"):
                self.assertNotIn(needle, text, needle)
            self.assertTrue(d["stale_index"])  # grants.md changed after synthesize index
            self.assertIn("index.json stale", text.splitlines()[0])
            # a file whose only grants are invalid contributes nothing, and decision.grants is empty
            self.paths.grants.write_text(bad.replace("- Status: active\n", "- Status: revoked 2026-03-02\n", 1), encoding="utf-8")
            text, d = self.select("fix the export button", no_light=True)
            self.assertEqual(d["grants"], [])
            self.assertNotIn("standing grants", text)
        finally:
            self.paths.grants.write_text(good, encoding="utf-8")
        gtext, ids = _select.runtime_grants(bad)
        self.assertEqual(ids, ["G1"])
        self.assertTrue(gtext.startswith("# Alex: standing grants"))
        self.assertEqual(gtext.count("## G"), 1)
        self.assertEqual(_select.runtime_grants(""), ("", []))

    def test_stale_index_is_rebuilt_in_memory(self):
        """Lines inserted into profile.md after `synthesize index` must not shift the slice onto the wrong group."""
        prof = self.paths.profile.read_text(encoding="utf-8")
        idx_text = self.paths.index.read_text(encoding="utf-8")
        _, before = self.select("fix the bug", no_light=True)
        lines = prof.splitlines()
        first_layer = next(i for i, l in enumerate(lines) if l.startswith("## 1."))
        lines[first_layer + 1:first_layer + 1] = ["- **Padding %d.** A new trait line. [weak] \"x\" (01-0%d)" % (k, k % 9 + 1) for k in range(15)]
        self.paths.profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            self.assertTrue(_select.index_is_stale(self.paths.model))
            text, d = self.select("fix the bug", no_light=True)
            self.assertTrue(d["stale_index"])
            self.assertEqual(d["groups"], before["groups"])
            self.assertFalse(d["legacy"])
            self.assertIn("### E.", text)
            self.assertIn("## 6. Standing rules", text)
            self.assertNotIn("Padding", text)  # the inserted lines sit in layer 1, which the slice never loads
            self.assertIn("(index.json stale, rebuilt in memory: run `synthesize index`)", text.splitlines()[0])
            self.assertEqual(self.paths.index.read_text(encoding="utf-8"), idx_text)  # never written by select
            from mr.status import gather
            st = gather(self.paths, self.cfg)
            self.assertTrue(any("index.json is stale" in w for w in st["warnings"]), st["warnings"])
        finally:
            self.paths.profile.write_text(prof, encoding="utf-8")
        self.assertFalse(_select.index_is_stale(self.paths.model))
        _, d = self.select("fix the bug", no_light=True)
        self.assertFalse(d["stale_index"])

    def test_legacy_title_keywords_match_by_stem_and_header_counts_trimmed_examples(self):
        import shutil
        legacy = helpers.tmpdir() / "model"
        legacy.mkdir()
        prof = [l for l in self.paths.profile.read_text(encoding="utf-8").splitlines() if not l.startswith("When:")]
        (legacy / "profile.md").write_text("\n".join(prof) + "\n", encoding="utf-8")
        ex = [l for l in self.paths.examples.read_text(encoding="utf-8").splitlines() if not l.startswith("Tags:")]
        (legacy / "examples.md").write_text("\n".join(ex) + "\n", encoding="utf-8")
        shutil.copy(self.paths.light, legacy / "light.md")
        _, d = _select.select_slice(legacy, "message the stakeholder about the deadline", self.sel)
        self.assertIn("G", d["groups"])  # "Messages to people" matched through messages/message
        _, d2 = _select.select_slice(legacy, "deploying the release", self.sel)
        self.assertIn("F", d2["groups"])  # "Shipping: git, releases, deploys" matched through releases/release and deploys/deploying
        text, d3 = _select.select_slice(legacy, "fix the export button", dict(self.sel, initiative_budget_chars=9000))
        self.assertLess(len(d3["examples"]), 22)
        self.assertRegex(text.splitlines()[0], r"legacy model, no index: \d+ of 22 examples loaded, the budget trimmed the rest")
        text4, d4 = _select.select_slice(legacy, "fix the export button", self.sel)
        self.assertEqual(len(d4["examples"]), 22)
        self.assertIn("every example loaded", text4.splitlines()[0])

    def test_legacy_model_without_index_reads_every_example(self):
        """A model from before the index format: no index.json, groups without When: lines, examples without Tags:."""
        import shutil
        legacy = helpers.tmpdir() / "model"
        legacy.mkdir()
        prof = self.paths.profile.read_text(encoding="utf-8").splitlines()
        prof = [l for l in prof if not l.startswith("When:")]
        prof[0] = "# Alex: the full Mind Reading model"
        (legacy / "profile.md").write_text("\n".join(prof) + "\n", encoding="utf-8")
        ex = [l for l in self.paths.examples.read_text(encoding="utf-8").splitlines() if not l.startswith("Tags:")]
        (legacy / "examples.md").write_text("\n".join(ex) + "\n", encoding="utf-8")
        shutil.copy(self.paths.light, legacy / "light.md")
        text, d = _select.select_slice(legacy, "fix the export button", self.sel)
        self.assertTrue(d["legacy"])
        self.assertTrue(text.startswith("<!-- mind-reading slice (legacy model, no index: every example loaded)"))
        self.assertEqual(len(d["examples"]), 22)
        self.assertEqual(d["examples"][-1], 5)  # the export example still scores highest by seed overlap
        self.assertEqual(d["groups"], ["A"])  # no When: lines: the title words stand in, and "fix" hits group A's title
        _, d2 = _select.select_slice(legacy, "there is a bug in the reports", self.sel)
        self.assertIn("E", d2["groups"])  # "Bug reports and single-instance feedback" matched through its title
        self.assertIn("## 6. Standing rules", text)
        self.assertIn("# Alex: how to read Alex's prompts", text)
        self.assertIsNotNone(_select.legacy_index(legacy))
        self.assertIsNone(_select.legacy_index(helpers.tmpdir()))

    def test_budget_trims_examples_then_groups_never_standing_rules(self):
        small = dict(self.sel, budget_chars=3500)
        text, d = _select.select_slice(self.paths.model, "fix the export button", small, no_light=True)
        self.assertEqual(d["examples"], [])
        self.assertEqual(d["groups"], ["E"])  # first group kept, second trimmed
        self.assertIn("## 6. Standing rules", text)

    def test_index_schema_valid(self):
        from mr import schema as _schema
        from mr.engine import load_schema
        self.assertEqual(_schema.validate(read_json(self.paths.index), load_schema("index")), [])


if __name__ == "__main__":
    unittest.main()
