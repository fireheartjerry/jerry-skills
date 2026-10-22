import json
import unittest

import helpers
from mr import select as _select
from mr.config import read_json
from mr.mine import DEFAULT_LENSES, latest_mined_run, mined_chunks, run_mine
from mr.synthesize import consolidate, jaccard, merge_items, run_synthesize


class MineSynthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="mine")
        cls.run_id = latest_mined_run(cls.paths)

    def test_mine_outputs_per_chunk_per_lens_with_meta(self):
        files = sorted((self.paths.mined / self.run_id).glob("chunk-*.json"))
        self.assertEqual(len(files), 2 * len(DEFAULT_LENSES))  # one output per chunk per lens
        chunks = mined_chunks(self.paths, self.run_id)
        self.assertEqual(sorted(chunks), ["chunk-00", "chunk-01"])
        self.assertEqual(sorted(chunks["chunk-00"]), sorted(DEFAULT_LENSES))
        meta = read_json(chunks["chunk-00"]["defaults"])["_meta"]
        self.assertEqual(meta["engine"], "fake")
        self.assertEqual(meta["lens"], "defaults")
        self.assertEqual(len(meta["chunk_hash"]), 16)
        # each lens file holds only its own fields (the fake engine projects the canned chunk onto the lens schema)
        self.assertEqual(sorted(k for k in read_json(chunks["chunk-00"]["defaults"]) if k != "_meta"), ["unsaid_defaults"])

    def test_mine_resume_and_reuse(self):
        units = 2 * len(DEFAULT_LENSES)
        res = run_mine(self.paths, self.cfg, "fake", run_id=self.run_id, fake_dir=helpers.FAKE, log=helpers.quiet)
        self.assertEqual(len(res["skipped"]), units)
        res2 = run_mine(self.paths, self.cfg, "fake", fake_dir=helpers.FAKE, reuse_from=self.run_id, log=helpers.quiet)
        self.assertEqual(len(res2["skipped"]), units)  # reused from the previous run by chunk hash, no calls
        self.assertEqual(res2["calls"], 0)

    def test_single_pass_writes_one_file_per_chunk(self):
        paths, cfg = helpers.build_home(through="prep")
        res = run_mine(paths, cfg, "fake", fake_dir=helpers.FAKE, lenses=["all"], log=helpers.quiet)
        self.assertEqual((len(res["ok"]), res["lenses"]), (2, ["all"]))
        run_id = latest_mined_run(paths)
        self.assertEqual(sorted(p.name for p in (paths.mined / run_id).glob("chunk-*.json")), ["chunk-00.json", "chunk-01.json"])
        c = consolidate(paths, run_id, log=helpers.quiet)
        self.assertEqual(c["chunks"], 2)

    def test_consolidate_moves_project_facts_and_trims_evidence(self):
        c = consolidate(self.paths, self.run_id, log=helpers.quiet)
        self.assertEqual(c["chunks"], 2)  # distinct chunks, not lens files
        self.assertEqual(c["session_stats"]["sessions_read"], 6 + 6)  # once per chunk, not once per lens (both chunks are the canned chunk-00)
        # 2 per chunk (one trait, one unsaid default): the copies the followthrough lens reported are merged in; plus one
        # project-scoped lesson, merged across the two chunks
        self.assertEqual(len(c["project_facts"]), 5)
        self.assertEqual(sum(1 for f in c["project_facts"] if f.get("kind") == "lesson"), 1)
        self.assertTrue(all(t["scope"] == "project" for t in c["project_facts"]))
        for layer, traits in c["traits"].items():
            for t in traits:
                self.assertNotEqual(t["scope"], "project")
                self.assertLessEqual(len(t["evidence"]), 2)
        # "ship it" and "Ship it" merged by lowercase phrase; "rq" never occurs in typed fixture text, so it is dropped
        self.assertEqual([v["phrase"] for v in c["vocabulary"]], ["ship it"])
        self.assertEqual(len(c["seed_expansions"]), 6)
        self.assertEqual(len(c["preference_changes"]), 2)  # core and reactions report the same change: one per chunk
        self.assertEqual(c["trimmed"], [])
        for t in [t for ts in c["traits"].values() for t in ts]:
            self.assertIn(t["chunk"], ("chunk-00", "chunk-01"))  # the chunk id, never a lens file stem
        verify = next(t for t in c["traits"]["quality_bar"] if t["chunk"] == "chunk-00")
        self.assertEqual(verify["lenses"], ["core", "followthrough"])
        self.assertEqual(verify["count"], 3)  # the same two quotes from two lenses are not new observations
        self.assertEqual(sum(len(c["traits"][l]) for l in c["traits"]), 2 * 5)  # 5 person traits per chunk, no duplicates
        drafted = next(t for t in c["traits"]["building"] if t["chunk"] == "chunk-00")
        self.assertNotIn("endorsed", drafted)  # one typed quote beside the drafted one: not endorsed-only
        self.assertTrue((self.paths.mined / self.run_id / "consolidated.json").exists())

    def test_consolidate_budget_trims(self):
        c = consolidate(self.paths, self.run_id, budget_chars=2000, log=helpers.quiet)
        self.assertIn("dropped weak traits with count 1", c["trimmed"])
        self.assertIn("trimmed evidence to one quote", c["trimmed"])
        for traits in c["traits"].values():
            for t in traits:
                self.assertFalse(t["strength"] == "weak" and t["count"] == 1)
                self.assertLessEqual(len(t["evidence"]), 1)

    def test_synthesize_writes_model_and_manifest(self):
        paths, cfg = helpers.build_home(through="synthesize")
        for n in ("light.md", "profile.md", "examples.md", "index.json", "MODEL.json"):
            self.assertTrue((paths.model / n).exists(), n)
        m = read_json(paths.model_json)
        self.assertEqual(m["model_hash"], _select.files_hash(paths.model))
        self.assertEqual(m["name_token"], "Alex")
        self.assertEqual(m["split_id"], read_json(paths.split)["split_id"])
        idx = read_json(paths.index)
        self.assertEqual(len(idx["groups"]), 8)
        self.assertEqual(len(idx["examples"]), 22)  # 20 plus the two upgrade pairs
        self.assertEqual([g["id"] for g in idx["lessons"]], ["W1", "W2", "W3"])
        light = paths.light.read_text(encoding="utf-8")
        self.assertTrue(light.startswith("# Alex: how to read Alex's prompts"))
        self.assertIn("Scope:", light)

    def test_resume_skips_then_force_archives(self):
        paths, cfg = helpers.build_home(through="synthesize")
        first_hash = read_json(paths.model_json)["model_hash"]
        s = run_synthesize(paths, cfg, "fake", fake_dir=helpers.FAKE, log=helpers.quiet)
        self.assertTrue(all("skipped" in v for k, v in s["substeps"].items() if k in ("profile", "light", "examples")))
        paths.light.write_text(paths.light.read_text(encoding="utf-8") + "- An extra hand edit.\n", encoding="utf-8")
        run_synthesize(paths, cfg, "fake", substeps=["index", "manifest"], log=helpers.quiet)
        second_hash = read_json(paths.model_json)["model_hash"]
        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(read_json(paths.model_json)["parents"], [first_hash])
        run_synthesize(paths, cfg, "fake", force=True, fake_dir=helpers.FAKE, log=helpers.quiet)
        self.assertTrue((paths.archive / second_hash / "light.md").exists())

    def test_index_fails_with_line_number_when_when_missing(self):
        paths, cfg = helpers.build_home(through="synthesize")
        lines = paths.profile.read_text(encoding="utf-8").splitlines()
        k = next(i for i, l in enumerate(lines) if l.startswith("### E."))
        self.assertTrue(lines[k + 1].startswith("When:"))
        del lines[k + 1]
        paths.profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(_select.ModelFormatError) as cm:
            _select.build_index(paths.model)
        self.assertIn("profile.md:%d" % (k + 1), str(cm.exception))
        self.assertIn("no `When:` line", str(cm.exception))

    def test_index_line_ranges_point_at_content(self):
        paths, cfg = helpers.build_home(through="synthesize")
        idx = read_json(paths.index)
        lines = paths.profile.read_text(encoding="utf-8").splitlines()
        g = next(g for g in idx["groups"] if g["id"] == "E")
        self.assertTrue(lines[g["lines"][0] - 1].startswith("### E."))
        self.assertIn("export", g["when"])
        sr = idx["standing_rules"]["lines"]
        self.assertTrue(lines[sr[0] - 1].startswith("## 6."))
        ex = paths.examples.read_text(encoding="utf-8").splitlines()
        e5 = next(e for e in idx["examples"] if e["n"] == 5)
        self.assertTrue(ex[e5["lines"][0] - 1].startswith("### 5."))
        self.assertEqual(e5["tags"], ["bug", "export"])
        self.assertTrue(any(v["phrase"].startswith("export") for v in idx["vocabulary"]))

    def test_examples_numbering_gap_rejected(self):
        text = "# Seed to full prompt: examples\n\n## T\n### 1. a\nSituation: s.\nTags: bug\n> p\n### 3. b\nSituation: s.\nTags: bug\n> p\n"
        with self.assertRaises(_select.ModelFormatError) as cm:
            _select.parse_examples(text.splitlines())
        self.assertIn("expected 2", str(cm.exception))

    def test_light_header_prepended_when_missing(self):
        body = "## Read the prompts\n- x\n"
        out = _select.ensure_light_header(body, "Alex")
        self.assertTrue(out.startswith("# Alex: how to read Alex's prompts\n\nScope:"))
        self.assertEqual(_select.ensure_light_header(out, "Alex"), out)


def _ev(quote, origin="typed", time="2026-01-03T10:00:00Z", session="cs01"):
    return {"quote": quote, "time": time, "session": session, "origin": origin}


class MergeTests(unittest.TestCase):
    """The multi-lens merge inside one chunk, deterministic and engine-free."""

    def trait(self, claim, quotes, count=1, strength="moderate", layer="quality_bar", lens="core", scope="person", origin="typed"):
        return {"layer": layer, "claim": claim, "scope": scope, "strength": strength, "count": count,
                "evidence": [_ev(q, origin) for q in quotes], "lenses": [lens]}

    def test_jaccard_ignores_stop_words_and_case(self):
        self.assertEqual(jaccard("Verifies on the real path", "verifies real path"), 1.0)
        self.assertEqual(jaccard("pushes finished work", "pushes finished work to the branch"), 0.75)
        self.assertEqual(jaccard("", ""), 1.0)
        self.assertEqual(jaccard("deploys to staging", "writes short messages"), 0.0)

    def test_traits_merge_by_layer_and_overlap(self):
        a = self.trait("Verifies on the real path before calling anything done", ["run the tests", "show me a screenshot"], count=3)
        b = self.trait("Verifies on the real path before calling done", ["run the tests", "check it in the browser"], count=2,
                       strength="strong", lens="followthrough")
        c = self.trait("Verifies on the real path before calling done", ["x"], layer="workflow", lens="followthrough")  # other layer
        d = self.trait("Prefers dark themes in every app", ["dark mode pls"], lens="followthrough")  # no overlap
        out = merge_items([a, b, c, d], "trait")
        self.assertEqual(len(out), 3)
        m = out[0]
        self.assertEqual(m["claim"], a["claim"])  # the first lens owns the wording
        self.assertEqual(m["count"], 3 + 2 - 1)  # "run the tests" is one observation, not two
        self.assertEqual(m["strength"], "strong")
        self.assertEqual(m["lenses"], ["core", "followthrough"])
        self.assertEqual([e["quote"] for e in m["evidence"]], ["run the tests", "show me a screenshot"])  # capped at 2

    def test_identical_reports_do_not_inflate_the_count(self):
        a = self.trait("Pushes finished work", ["push it when green"], count=2)
        out = merge_items([a, dict(a, lenses=["followthrough"])], "trait")
        self.assertEqual((len(out), out[0]["count"], len(out[0]["evidence"])), (1, 2, 1))

    def test_typed_evidence_is_kept_before_endorsed_when_capping(self):
        a = self.trait("Narrows a drafted mandate", ["Draft one", "Draft two"], origin="drafted")
        b = self.trait("Narrows a drafted mandate", ["start with the keys"], lens="followthrough")
        out = merge_items([a, b], "trait")
        self.assertEqual([e["origin"] for e in out[0]["evidence"]], ["typed", "drafted"])

    def test_defaults_merge_by_kind_and_overlap(self):
        def d(when, default, kind=None, lens="defaults", first="fix the button", later="also the others"):
            out = {"when": when, "default": default, "scope": "person", "lenses": [lens],
                   "evidence": [{"first": first, "later": later, "time": None, "session": "cs01"}]}
            if kind:
                out["kind"] = kind
            return out
        a = d("bug report naming one instance", "fix every sibling of the reported instance", kind="scope")
        b = d("bug report naming one instance", "fix every sibling of the reported instance too", lens="followthrough")
        c = d("bug report naming one instance", "fix every sibling of the reported instance", kind="reporting", lens="followthrough")
        out = merge_items([a, b, c], "default")
        self.assertEqual(len(out), 2)  # a missing kind matches any; a different kind does not
        self.assertEqual(out[0]["lenses"], ["defaults", "followthrough"])
        self.assertEqual(len(out[0]["evidence"]), 1)  # the same first -> later pair once

    def test_endorsed_only_traits_are_capped_and_untyped_seeds_dropped(self):
        paths, cfg = helpers.build_home(through="mine")
        run_id = latest_mined_run(paths)
        f = mined_chunks(paths, run_id)["chunk-00"]["core"]
        data = read_json(f)
        data["traits"].append({"layer": "building", "claim": "Wants resilient checkout flows", "scope": "person", "strength": "strong",
                               "count": 2, "evidence": [_ev("Make the checkout flow resilient to payment provider timeouts", "drafted")]})
        data["seed_expansions"].append({"seed": "Make the checkout flow resilient to payment provider timeouts", "context": "drafted",
                                        "full_prompt": "x", "tags": []})
        f.write_text(json.dumps(data), encoding="utf-8")
        voc = mined_chunks(paths, run_id)["chunk-00"]["vocabulary"]
        vdata = read_json(voc)
        vdata["vocabulary"].append({"phrase": "payment provider timeouts", "meaning": "only in drafted text"})
        voc.write_text(json.dumps(vdata), encoding="utf-8")
        c = consolidate(paths, run_id, log=helpers.quiet)
        t = next(t for t in c["traits"]["building"] if t["claim"] == "Wants resilient checkout flows")
        self.assertEqual((t["strength"], t["endorsed"]), ("weak", True))
        self.assertNotIn("Make the checkout flow resilient to payment provider timeouts", [s["seed"] for s in c["seed_expansions"]])
        self.assertNotIn("payment provider timeouts", [v["phrase"] for v in c["vocabulary"]])
        self.assertEqual((c["origin_filter"]["seeds"], c["origin_filter"]["vocabulary"], c["origin_filter"]["endorsed_capped"]), (1, 3, 1))  # "rq" twice


if __name__ == "__main__":
    unittest.main()
