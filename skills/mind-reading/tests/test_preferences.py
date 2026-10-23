import json
import stat
import unittest

import helpers
from mr.config import read_json, write_json
from mr.preferences import PreferenceError, build_review, import_answers, pairs_from_results, _private_json


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.pairs = [{"id": "s%02d" % k, "seed": "fix the export %d" % k,
                       "context": ["earlier context"], "baseline": "First frozen brief %d" % k,
                       "candidate": "Second frozen brief %d" % k} for k in range(30)]

    def build(self, sub="review", **kwargs):
        return build_review(self.pairs, self.tmp / sub, **kwargs)

    def test_stable_balanced_hidden_pairing_and_escaped_text(self):
        self.pairs[0]["seed"] = "</script><script>alert('unsafe')</script>"
        one = self.build(seed=71)
        two = self.build("same", seed=71)
        self.assertEqual(one["review_id"], two["review_id"])
        self.assertEqual(read_json(one["key"]), read_json(two["key"]))
        self.assertEqual(one["count"], 24)
        self.assertEqual(one["human_verdict"], "unreviewed")
        page = __import__("pathlib").Path(one["page"]).read_text()
        self.assertNotIn('"A": "baseline"', page)
        self.assertNotIn('"human_verdict"', page)
        self.assertNotIn("</script><script>alert", page)
        self.assertIn("Neither", page)
        self.assertIn("localStorage", page)
        self.assertEqual(stat.S_IMODE(__import__("os").stat(one["key"]).st_mode), 0o600)
        key = read_json(one["key"])
        self.assertEqual(len({v["pair_id"] for v in key["pairs"].values()}), 24)
        self.assertEqual({v["A"] for v in key["pairs"].values()}, {"baseline", "candidate"})
        self.assertNotEqual(key["review_id"], self.build("different", seed=72)["review_id"])

    def test_missing_human_answers_are_unknown_not_losses(self):
        result = self.build(limit=4)
        key = read_json(result["key"])
        ids = list(key["pairs"])
        answers = {ids[0]: {"pick": next(s for s in ("A", "B") if key["pairs"][ids[0]][s] == "candidate")},
                   ids[1]: {"pick": "tie"}, ids[2]: {"pick": "neither"}}
        path = self.tmp / "answers.json"
        write_json(path, {"review_id": key["review_id"], "answers": answers})
        report = import_answers(result["key"], path)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["counts"], {"baseline": 0, "candidate": 1, "tie": 1, "neither": 1})
        self.assertFalse(report["positive_human_verdict"])
        self.assertEqual(report["verdict"], "incomplete")
        answers[ids[3]] = {"pick": "tie"}
        write_json(path, {"review_id": key["review_id"], "answers": answers})
        report = import_answers(result["key"], path)
        self.assertTrue(report["positive_human_verdict"])
        self.assertEqual(report["promotion"], "never automatic")

    def test_reject_mismatched_unknown_invalid_and_duplicate_answers(self):
        r = self.build(limit=1)
        key = read_json(r["key"])
        sid = next(iter(key["pairs"]))
        p = self.tmp / "answers.json"
        for envelope in ({"review_id": "wrong", "answers": {}},
                         {"review_id": key["review_id"], "answers": {"unknown": {"pick": "A"}}},
                         {"review_id": key["review_id"], "answers": {sid: {"pick": "both"}}}):
            write_json(p, envelope)
            with self.assertRaises(PreferenceError):
                import_answers(r["key"], p)
        p.write_text('{"review_id":%s,"answers":{%s:{"pick":"A"},%s:{"pick":"B"}}}' % (
            json.dumps(key["review_id"]), json.dumps(sid), json.dumps(sid)))
        with self.assertRaisesRegex(PreferenceError, "duplicate JSON"):
            import_answers(r["key"], p)

    def test_duplicate_pairs_and_overwriting_a_different_comparison_refused(self):
        self.build(limit=2)
        with self.assertRaises(PreferenceError):
            self.build(limit=3)
        with self.assertRaises(PreferenceError):
            build_review(self.pairs + [self.pairs[0]], self.tmp / "dup")

    def test_corrupt_key_is_refused(self):
        result = self.build(limit=1)
        key = read_json(result["key"])
        sid = next(iter(key["pairs"]))
        answers = self.tmp / "answers.json"
        write_json(answers, {"review_id": key["review_id"], "answers": {sid: {"pick": "A"}}})
        key["pairs"][sid] = "corrupt"
        write_json(result["key"], key)
        with self.assertRaises(PreferenceError):
            import_answers(result["key"], answers)

    def test_join_requires_both_expansions_and_seed(self):
        left, right, seeds = [self.tmp / n for n in ("left", "right", "seeds")]
        write_json(left / "expansions" / "s01-full.json", {"prompt": "one"})
        with self.assertRaisesRegex(PreferenceError, "IDs differ"):
            pairs_from_results(left, right, seeds_dir=seeds)
        write_json(right / "expansions" / "s01-full.json", {"prompt": "two"})
        with self.assertRaisesRegex(PreferenceError, "missing seed"):
            pairs_from_results(left, right, seeds_dir=seeds)
        write_json(seeds / "s01.json", {"seed": "fix it"})
        self.assertEqual(pairs_from_results(left, right, seeds_dir=seeds)[0]["candidate"], "two")

    def test_join_rejects_same_ids_from_different_seed_context_or_source(self):
        roots = [self.tmp / split / "results" / "model" for split in ("old", "new")]
        seed = {"seed": "fix the export", "context": ["invoice page"], "source": "fictional", "session": "one", "time": "2026-01-01"}
        for root in roots:
            write_json(root / "expansions" / "s01-full.json", {"prompt": "frozen expansion"})
            write_json(root.parent.parent / "seeds" / "s01.json", seed)
        self.assertEqual(len(pairs_from_results(*roots)), 1)
        for field, value in (("seed", "draft a message"), ("context", ["different earlier context"]), ("session", "other-session")):
            write_json(roots[1].parent.parent / "seeds" / "s01.json", dict(seed, **{field: value}))
            with self.assertRaisesRegex(PreferenceError, "identity differs"):
                pairs_from_results(*roots)
            with self.assertRaisesRegex(PreferenceError, "identity differs"):
                pairs_from_results(*roots, seeds_dir=roots[0].parent.parent / "seeds")
            with self.assertRaisesRegex(PreferenceError, "identity differs"):
                pairs_from_results(*roots, seeds_dir=roots[1].parent.parent / "seeds")

    def test_review_preserves_unrelated_file_and_rejects_symlink_before_any_write(self):
        from pathlib import Path
        for linked in (False, True):
            out = self.tmp / ("linked-review" if linked else "regular-review")
            out.mkdir()
            outside = self.tmp / (out.name + "-personal.md")
            outside.write_text("personal work")
            target = out / "review.html"
            if linked:
                target.symlink_to(outside)
            else:
                target.write_text("personal work")
            with self.assertRaises(PreferenceError):
                build_review(self.pairs, out)
            self.assertEqual(outside.read_text(), "personal work")
            self.assertEqual(target.read_text(), "personal work")
            self.assertFalse((out / "private").exists())
        original = self.tmp / "actual-directory"
        original.mkdir()
        linked = self.tmp / "linked-parent"
        linked.symlink_to(original, target_is_directory=True)
        with self.assertRaises(PreferenceError):
            build_review(self.pairs, linked / "review")
        self.assertEqual(list(original.iterdir()), [])

    def test_private_json_rejects_predictable_tmp_symlink_and_unrelated_report(self):
        outside = self.tmp / "personal.txt"
        outside.write_text("personal work")
        target = self.tmp / "report.json"
        target.with_name("report.json.tmp").symlink_to(outside)
        with self.assertRaises(PreferenceError):
            _private_json(target, {"status": "new"})
        self.assertFalse(target.exists())
        self.assertEqual(outside.read_text(), "personal work")
        target.with_name("report.json.tmp").unlink()
        target.write_text("personal work")
        with self.assertRaises(PreferenceError):
            _private_json(target, {"status": "new"}, replace=True)
        self.assertEqual(target.read_text(), "personal work")

    def test_repeated_generated_review_is_idempotent_and_report_updates_keep_identity(self):
        first = self.build(limit=1)
        self.assertEqual(first, self.build(limit=1))
        key = read_json(first["key"])
        answer = self.tmp / "answer.json"
        report = self.tmp / "report.json"
        write_json(answer, {"review_id": key["review_id"], "answers": {}})
        import_answers(first["key"], answer, report)
        sid = next(iter(key["pairs"]))
        write_json(answer, {"review_id": key["review_id"], "answers": {sid: {"pick": "tie"}}})
        self.assertTrue(import_answers(first["key"], answer, report)["completed"])


if __name__ == "__main__":
    unittest.main()
