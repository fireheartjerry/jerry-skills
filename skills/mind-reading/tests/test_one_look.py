import unittest

import helpers
from mr.config import read_json
from mr.evaluate import EvalRefused, run_eval
from mr.synthesize import run_synthesize


class OneLookTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg = helpers.build_home(through="synthesize")

    def run_eval(self, **kw):
        return run_eval(self.paths, self.cfg, "fake", fake_dir=helpers.FAKE, log=helpers.quiet, **kw)

    def test_first_look_recorded_second_refused_force_tuned(self):
        s1 = self.run_eval()
        ledger = read_json(self.paths.ledger)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["model_hash"], s1["model_hash"])
        self.assertFalse(ledger[0]["forced"])
        with self.assertRaises(EvalRefused) as cm:
            self.run_eval()
        self.assertIn("one-look rule", str(cm.exception))
        s3 = self.run_eval(force=True)
        self.assertTrue(s3["forced"])
        ledger = read_json(self.paths.ledger)
        self.assertEqual(len(ledger), 2)
        self.assertTrue(ledger[1]["forced"])
        from pathlib import Path
        md = Path(s3["report_path"]).read_text(encoding="utf-8")
        self.assertIn("TUNED", md.splitlines()[0])
        self.assertTrue(read_json(self.paths.eval / s3["split_id"] / "results" / s3["model_hash"] / "report.json")["forced"])

    def test_changed_model_gets_clean_look(self):
        s1 = self.run_eval()
        self.paths.light.write_text(self.paths.light.read_text(encoding="utf-8") + "- A new rule.\n", encoding="utf-8")
        run_synthesize(self.paths, self.cfg, "fake", substeps=["index", "manifest"], log=helpers.quiet)
        s2 = self.run_eval()
        self.assertNotEqual(s1["model_hash"], s2["model_hash"])
        self.assertFalse(s2["forced"])
        self.assertEqual(len(read_json(self.paths.ledger)), 2)

    def test_cli_exit_code_two_on_refusal(self):
        self.run_eval()
        r = helpers.run_cli(self.paths.home, "eval", "--engine", "fake", "--fake-dir", helpers.FAKE)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("one-look rule", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
