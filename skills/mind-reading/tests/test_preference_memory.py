import copy
import json
import unittest
from pathlib import Path

import helpers
from mr.config import read_json, write_json
from mr.preferences import build_review
from mr.preference_memory import PreferenceMemoryError, compile_review, load_memory, render, retrieve
from mr import preference_memory as memory
from mr.refresh import prepare_candidate
from mr.select import files_hash, select_slice

SEEDS = ["rotate the orchid vault credentials", "fix the glacier ledger export", "check the lantern harbor tide chart",
         "fix the bug", "polish the quartz kiln dashboard", "reply to the ferry timetable thread"]


class PreferenceMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.pairs = [{"id": "p%d" % i, "seed": s, "context": ["earlier note %d" % i],
                       "baseline": "Plan alpha for case %d" % i, "candidate": "Plan beta for case %d" % i}
                      for i, s in enumerate(SEEDS)]
        self.info = build_review(self.pairs, self.tmp / "review", seed=5)
        self.key = read_json(self.info["key"])
        self.by_pair = {v["pair_id"]: (k, v) for k, v in self.key["pairs"].items()}

    def answer(self, picks, drop=(), name="answers.json", notes=None):
        answers = {}
        for pid, (opaque, _) in self.by_pair.items():
            if pid in drop:
                continue
            answers[opaque] = {"pick": picks.get(pid, "tie"), "note": (notes or {}).get(pid, "")}
        path = self.tmp / name
        write_json(path, {"review_id": self.key["review_id"], "answers": answers})
        return path

    def side(self, pid, outcome):
        _, m = self.by_pair[pid]
        return "A" if m["A"] == outcome else "B"

    def compile(self, picks, **kw):
        return compile_review(self.pairs, self.info["key"], self.answer(picks, **kw))

    def store(self, picks=None, name="model"):
        picks = picks or {}
        for pid in ("p0", "p1", "p2", "p3", "p4", "p5"):
            picks.setdefault(pid, self.side(pid, "candidate"))
        data = self.compile(picks)
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        write_json(d / "preference_memory.json", data)
        return d

    def test_compile_maps_random_sides_and_preserves_ties_and_notes(self):
        sides = {m["A"] for _, m in self.by_pair.values()}
        self.assertEqual(sides, {"baseline", "candidate"})  # genuinely randomised
        picks = {pid: self.side(pid, "baseline") for pid in self.by_pair}
        picks["p1"], picks["p2"] = "tie", "neither"
        note = "  odd\nnote <b>x</b>  "
        out = self.compile(picks, notes={"p0": note})
        by = {r["id"]: r for r in out["pairs"]}
        self.assertEqual(by["p0"]["outcome"], "baseline")
        self.assertEqual(by["p0"]["note"], note)
        self.assertEqual((by["p1"]["outcome"], by["p2"]["outcome"]), ("tie", "neither"))
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["reviews"], [{"review_id": self.key["review_id"], "total": 6, "reviewed": 6}])
        self.assertEqual([r["id"] for r in out["pairs"]], sorted(by))

    def test_compile_writes_and_mutates_nothing(self):
        answers = self.answer({})
        before = sorted(p.name for p in self.tmp.rglob("*"))
        key_bytes, ans_bytes = Path(self.info["key"]).read_bytes(), answers.read_bytes()
        pairs = copy.deepcopy(self.pairs)
        compile_review(self.pairs, self.info["key"], answers)
        self.assertEqual(before, sorted(p.name for p in self.tmp.rglob("*")))
        self.assertEqual(pairs, self.pairs)
        self.assertEqual(key_bytes, Path(self.info["key"]).read_bytes())
        self.assertEqual(ans_bytes, answers.read_bytes())

    def test_extra_unreviewed_pairs_are_refused(self):
        extra = dict(self.pairs[0], id="unreviewed")
        with self.assertRaises(PreferenceMemoryError):
            compile_review(self.pairs + [extra], self.info["key"], self.answer({}))

    def test_store_must_retain_the_complete_review_and_strict_version(self):
        d = self.store()
        original = read_json(d / "preference_memory.json")
        for change in (lambda x: x["pairs"].pop(), lambda x: x.update(version=True)):
            bad = copy.deepcopy(original)
            change(bad)
            write_json(d / "preference_memory.json", bad)
            with self.assertRaises(PreferenceMemoryError):
                load_memory(d)

    def test_render_keeps_the_finish_line_instead_of_clipping_the_preferred_brief(self):
        rows = retrieve(self.store(), "orchid vault")
        rows[0]["candidate"] = "Useful detail. " * 90 + "Verify the resulting credential actually works."
        self.assertIn(rows[0]["candidate"], render(rows))

    def test_learning_is_candidate_only_and_changes_model_lineage(self):
        paths, cfg = helpers.build_home(through="synthesize")
        answers = self.answer({pid: self.side(pid, "candidate") for pid in self.by_pair})
        before = files_hash(paths.model)
        with self.assertRaises(PreferenceMemoryError):
            memory.learn(paths, cfg, self.pairs, self.info["key"], answers)
        self.assertEqual(files_hash(paths.model), before)
        candidate, candidate_cfg, _ = prepare_candidate(paths, cfg, self.tmp / "candidate")
        result = memory.learn(candidate, candidate_cfg, self.pairs, self.info["key"], answers)
        self.assertTrue(result["rebuilt"])
        self.assertEqual(files_hash(paths.model), before)
        self.assertNotEqual(files_hash(candidate.model), before)
        self.assertTrue((candidate.archive / before).is_dir())
        self.assertEqual(read_json(candidate.model_json)["model_hash"], files_hash(candidate.model))
        self.assertEqual(len(load_memory(candidate.model)), 6)
        same = memory.learn(candidate, candidate_cfg, self.pairs, self.info["key"], answers)
        self.assertFalse(same["rebuilt"])
        marker = read_json(candidate.home / "CANDIDATE.json")
        marker["status"] = "activated"
        write_json(candidate.home / "CANDIDATE.json", marker)
        with self.assertRaises(PreferenceMemoryError):
            memory.learn(candidate, candidate_cfg, self.pairs, self.info["key"], answers)

    def test_two_distinct_completed_reviews_can_be_learned_without_losing_the_first(self):
        paths, cfg = helpers.build_home(through="synthesize")
        candidate, candidate_cfg, _ = prepare_candidate(paths, cfg, self.tmp / "multi")
        memory.learn(candidate, candidate_cfg, self.pairs, self.info["key"], self.answer({}))
        before = files_hash(candidate.model)
        second = build_review(self.pairs, self.tmp / "second-review", seed="different")
        key = read_json(second["key"])
        answers = self.tmp / "second-answers.json"
        write_json(answers, {"review_id": key["review_id"], "answers": {k: {"pick": "A", "note": ""} for k in key["pairs"]}})
        result = memory.learn(candidate, candidate_cfg, self.pairs, second["key"], answers)
        self.assertEqual(result["reviews"], 2)
        self.assertEqual(len(load_memory(candidate.model)), 12)
        self.assertTrue((candidate.archive / before / "preference_memory.json").exists())

    def test_optional_preference_memory_is_selected_and_corrupt_memory_falls_back(self):
        paths, cfg = helpers.build_home(through="synthesize")
        write_json(paths.model / "preference_memory.json", self.compile({pid: self.side(pid, "candidate") for pid in self.by_pair}))
        text, decision = select_slice(paths.model, "ready", cfg["select"], context={"goal": "rotate orchid vault"})
        self.assertTrue(decision["preference_pairs"])
        self.assertIn("Past A/B choices", text)
        self.assertIn("unknown reasons", text)
        (paths.model / "preference_memory.json").write_text('{"version":')
        text, decision = select_slice(paths.model, "ready", cfg["select"], context={"goal": "rotate orchid vault"})
        self.assertIn("Standing rules", text)
        self.assertEqual(decision["preference_pairs"], [])
        self.assertTrue(decision["preference_warning"])

    def test_tampering_incomplete_duplicate_and_unknown_refused(self):
        answers = self.answer({})
        for field, value in (("baseline", "Edited"), ("seed", "other"), ("context", ["changed"]), ("candidate", "x")):
            bad = copy.deepcopy(self.pairs)
            bad[0][field] = value
            with self.assertRaises(PreferenceMemoryError):
                compile_review(bad, self.info["key"], answers)
        with self.assertRaises(PreferenceMemoryError):
            compile_review(self.pairs[1:], self.info["key"], answers)
        with self.assertRaises(PreferenceMemoryError):
            compile_review(self.pairs + [dict(self.pairs[0])], self.info["key"], answers)
        bad = copy.deepcopy(self.pairs)
        bad[0]["context"] = "text"
        with self.assertRaises(PreferenceMemoryError):
            compile_review(bad, self.info["key"], answers)
        with self.assertRaises(PreferenceMemoryError):
            self.compile({}, drop=("p3",), name="partial.json")
        raw = json.loads(answers.read_text())
        raw["answers"][next(iter(raw["answers"]))]["pick"] = "C"
        write_json(self.tmp / "unk.json", raw)
        with self.assertRaises(PreferenceMemoryError):
            compile_review(self.pairs, self.info["key"], self.tmp / "unk.json")
        raw = json.loads(answers.read_text())
        raw["answers"]["ffff"] = {"pick": "A"}
        write_json(self.tmp / "unk2.json", raw)
        with self.assertRaises(PreferenceMemoryError):
            compile_review(self.pairs, self.info["key"], self.tmp / "unk2.json")

    def test_missing_and_corrupt_store(self):
        d = self.tmp / "none"
        d.mkdir()
        self.assertEqual(load_memory(d), [])
        self.assertEqual(retrieve(d, "rotate the orchid vault"), [])
        for text in ("{not json", "[]", json.dumps({"version": 2, "reviews": [], "pairs": []})):
            (d / "preference_memory.json").write_text(text)
            with self.assertRaises(PreferenceMemoryError):
                load_memory(d)
        good = self.store()
        data = read_json(good / "preference_memory.json")
        for mutate in (lambda x: x["pairs"][0].update(outcome="win"), lambda x: x["pairs"][0].update(seed="changed"),
                       lambda x: x["pairs"].append(dict(x["pairs"][0])), lambda x: x["pairs"][0].pop("note"),
                       lambda x: x["pairs"][0].update(review_id="zzz")):
            bad = copy.deepcopy(data)
            mutate(bad)
            write_json(d / "preference_memory.json", bad)
            with self.assertRaises(PreferenceMemoryError):
                load_memory(d)

    def test_retrieve_matches_human_seed_with_provenance(self):
        d = self.store()
        got = retrieve(d, "please rotate the orchid vault now")
        self.assertEqual([r["id"] for r in got], ["p0"])
        self.assertEqual(got[0]["review_id"], self.key["review_id"])
        self.assertIn("orchid", got[0]["matched"])
        self.assertGreater(got[0]["relevance"], 0)

    def test_context_rescues_terse_seed(self):
        d = self.store()
        self.assertEqual(retrieve(d, "fix it"), [])
        got = retrieve(d, "fix it", "we were working on the glacier ledger export")
        self.assertEqual(got[0]["id"], "p1")

    def test_generic_and_assistant_words_miss(self):
        d = self.store()
        self.assertEqual(retrieve(d, "fix the bug and verify it works"), [])
        self.assertEqual(retrieve(d, "plan alpha beta"), [])  # words only in the assistant briefs
        self.assertEqual(retrieve(d, "fix", "check the build"), [])

    def test_only_decisive_examples_returned(self):
        d = self.store({"p0": "tie", "p1": "neither"})
        self.assertEqual(retrieve(d, "rotate the orchid vault"), [])
        self.assertEqual(retrieve(d, "glacier ledger export"), [])

    def test_order_limits_and_stability(self):
        d = self.store()
        q = "orchid vault glacier ledger lantern harbor"
        one = retrieve(d, q, limit=5)
        self.assertEqual(one, retrieve(d, q, limit=5))
        self.assertEqual(len(one), 3)
        self.assertEqual([r["relevance"] for r in one], sorted((r["relevance"] for r in one), reverse=True))
        self.assertEqual(len(retrieve(d, q)), 2)
        self.assertEqual(retrieve(d, q, limit=0), [])
        for bad in (-1, True, 1.5, "2"):
            with self.assertRaises(PreferenceMemoryError):
                retrieve(d, q, limit=bad)

    def test_render_is_source_linked_literal_and_non_authoritative(self):
        self.assertEqual(render([]), "")
        d = self.store()
        text = render(retrieve(d, "rotate the orchid vault"))
        rec = retrieve(d, "rotate the orchid vault")[0]
        self.assertIn("Choice %s / p0" % self.key["review_id"], text)
        self.assertIn(rec["candidate"], text)
        self.assertIn("Chose:", text)
        self.assertIn("Over:", text)
        for phrase in ("unknown reasons", "not current instructions", "authorization"):
            self.assertIn(phrase, text)
        data = copy.deepcopy(self.pairs)
        data[0]["candidate"] = "<script>alert(1)</script> & more"
        info = build_review(data, self.tmp / "r2", seed=5)
        key = read_json(info["key"])
        ans = {k: {"pick": "A" if v["A"] == "candidate" else "B"} for k, v in key["pairs"].items()}
        write_json(self.tmp / "a2.json", {"review_id": key["review_id"], "answers": ans})
        d2 = self.tmp / "m2"
        d2.mkdir()
        write_json(d2 / "preference_memory.json", compile_review(data, info["key"], self.tmp / "a2.json"))
        self.assertIn("<script>alert(1)</script> & more", render(retrieve(d2, "rotate the orchid vault")))


if __name__ == "__main__":
    unittest.main()
