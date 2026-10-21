import json
import random
import shutil
import unittest

import helpers
from mr.config import read_json
from mr.evaluate import run_eval


class EvalFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="eval")
        cls.split = read_json(cls.paths.split)
        cls.model_hash = read_json(cls.paths.model_json)["model_hash"]
        cls.results = cls.paths.eval / cls.split["split_id"] / "results" / cls.model_hash

    def test_requirements_once_per_session_shared_by_arms(self):
        n = self.split["counts"]["test_seeds"]
        self.assertEqual(len(list((self.results / "requirements").glob("*.json"))), n)
        self.assertEqual(len(list((self.results / "expansions").glob("*.json"))), n * 3)
        self.assertEqual(len(list((self.results / "judgments").glob("*.json"))), n * 3)
        report = read_json(self.results / "report.json")
        self.assertEqual(report["sessions"], n)
        self.assertEqual(report["requirements"], n * 3)
        self.assertEqual(report["arms"], ["none", "light", "full"])
        self.assertEqual(report["rubric_version"], 3)
        self.assertTrue((self.results / "report.md").exists())
        self.assertIn("No arm saw project memory", report["limitation"])

    def test_arm_order_shuffled_deterministically(self):
        sid = "s00"
        order = ["none", "light", "full"]
        random.Random("%s:%s" % (self.split["split_id"], sid)).shuffle(order)
        order2 = ["none", "light", "full"]
        random.Random("%s:%s" % (self.split["split_id"], sid)).shuffle(order2)
        self.assertEqual(order, order2)
        orders = set()
        for k in range(20):
            o = ["none", "light", "full"]
            random.Random("%s:s%02d" % (self.split["split_id"], k)).shuffle(o)
            orders.add(tuple(o))
        self.assertGreater(len(orders), 1)

    def test_id_mismatch_retries_then_fails(self):
        paths, cfg = helpers.build_home(through="synthesize")
        fake = paths.home / "fake"
        shutil.copytree(helpers.FAKE, fake)
        bad = {"default": {"items": [{"id": 1, "verdict": "full"}, {"id": 2, "verdict": "full"}], "contradictions": []}}
        (fake / "eval-score.json").write_text(json.dumps(bad), encoding="utf-8")
        s = run_eval(paths, cfg, "fake", fake_dir=fake, arms=["none"], log=helpers.quiet)
        self.assertEqual(s["stages"]["score"]["failed"], read_json(paths.split)["counts"]["test_seeds"])
        self.assertEqual(s["stages"]["score"]["ok"], 0)
        run_dir = next((paths.eval / read_json(paths.split)["split_id"] / "runs").glob("*-score"))
        self.assertFalse(list(paths.runs.glob("*-score")))
        calls = [json.loads(l) for l in (run_dir / "calls.jsonl").read_text().splitlines()]
        per_unit = {}
        for c in calls:
            per_unit.setdefault(c["unit"], []).append(c["status"])
        self.assertTrue(all(v == ["invalid", "invalid"] for v in per_unit.values()), per_unit)

    def test_dev_writes_no_ledger_and_is_repeatable(self):
        paths, cfg = helpers.build_home(through="synthesize")
        s1 = run_eval(paths, cfg, "fake", dev=True, fake_dir=helpers.FAKE, log=helpers.quiet)
        s2 = run_eval(paths, cfg, "fake", dev=True, fake_dir=helpers.FAKE, log=helpers.quiet)
        self.assertFalse(paths.ledger.exists())
        self.assertTrue(s1["dev"] and s2["dev"])
        self.assertTrue((paths.dev / "results" / s1["model_hash"] / "report.md").exists())
        self.assertEqual(s2["stages"]["expand"]["skipped"], read_json(paths.split)["counts"]["dev_seeds"] * 3)

    def test_print_mode_end_to_end_inside_eval_dir(self):
        """Three print/collect cycles land a report; every prompt and output stays under eval/<split_id>/ (mode 0700)."""
        import os
        import stat
        paths, cfg = helpers.build_home(through="synthesize")
        split_id = read_json(paths.split)["split_id"]
        runs = paths.eval / split_id / "runs"
        fake = {k: json.loads((helpers.FAKE / (k + ".json")).read_text())["default"] for k in ("eval-requirements", "eval-expand", "eval-score")}
        arms = ["none", "full"]
        s = run_eval(paths, cfg, "print", arms=arms, max_sessions=2, log=helpers.quiet)
        base = s["run_id"]
        self.assertIn("mind_reading.py eval --run %s --engine print --collect" % base, s["instructions"])
        self.assertNotIn("-req", s["instructions"].split("--run ")[1].split()[0])
        for stage, suffix in (("eval-requirements", "req"), ("eval-expand", "expand"), ("eval-score", "score")):
            prompts = sorted((runs / ("%s-%s" % (base, suffix)) / "prompts").glob("%s-*.md" % stage))
            self.assertTrue(prompts, stage)
            out = runs / ("%s-%s" % (base, suffix)) / "out"
            out.mkdir(exist_ok=True)
            for p in prompts:
                (out / (p.stem + ".json")).write_text(json.dumps(fake[stage]), encoding="utf-8")
            s = run_eval(paths, cfg, "print", arms=arms, max_sessions=2, run_id=base, collect=True, log=helpers.quiet)
        self.assertIn("report_path", s)
        self.assertEqual(s["report"]["sessions"], 2)
        for suffix in ("req", "expand", "score"):
            self.assertFalse(list(paths.runs.glob("*-" + suffix)), "no eval prompt may land under runs/")
        for d in (paths.eval, paths.eval / split_id, runs):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        req_prompt = next((runs / (base + "-req") / "prompts").glob("eval-requirements-s00.md")).read_text(encoding="utf-8")
        self.assertIn("<later_messages>", req_prompt)
        self.assertEqual(len(read_json(paths.ledger)), 1)


if __name__ == "__main__":
    unittest.main()
