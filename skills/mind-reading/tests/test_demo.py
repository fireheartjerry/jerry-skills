"""The public demo exercises real selector behavior without private histories."""
import os
import tempfile
import unittest
from pathlib import Path

from helpers import quiet
from mr.config import DEFAULT_HOME, Paths, load_config
from mr.demo import DemoRefused, run_demo
from mr.select import select_slice


class DemoTests(unittest.TestCase):
    def test_demo_retrieves_specific_supported_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "demo")
            result = run_demo(paths, load_config(paths), log=quiet)
            cfg = load_config(paths)
            self.assertEqual(result["engine_calls"], 0)
            self.assertEqual(result["transcripts_read"], 0)
            self.assertEqual(len(result["scenarios"]), 3)
            self.assertEqual(cfg["engine"], "print")
            self.assertTrue(all(str(paths.home) in root for root in cfg["sources"].values()))
            sliced, decision = select_slice(paths.model, "add the migration", cfg["select"], kind="sqlite")
            self.assertIn("rollback path", sliced)
            self.assertIn("preserve existing rows", sliced)
            self.assertTrue(decision["legacy"])
            self.assertIn("B", decision["groups"])
            self.assertNotIn("C", decision["groups"])
            self.assertEqual(decision["grants"], [])
            text = Path(result["output"]).read_text()
            self.assertIn("authored illustrations", text)
            self.assertIn("not accuracy", text)
            self.assertFalse(paths.messages.exists())
            self.assertFalse(paths.index.exists())
            if os.name == "posix":
                self.assertEqual(paths.profile.stat().st_mode & 0o777, 0o600)

    def test_demo_refuses_real_or_unknown_files_even_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "private")
            paths.model.mkdir(parents=True)
            paths.profile.write_text("private model")
            for force in (False, True):
                with self.assertRaises(DemoRefused):
                    run_demo(paths, load_config(paths), force=force, log=quiet)
            self.assertEqual(paths.profile.read_text(), "private model")

    def test_demo_refuses_canonical_live_home_before_touching_it(self):
        paths = Paths(Path(DEFAULT_HOME).expanduser())
        with self.assertRaises(DemoRefused):
            run_demo(paths, {}, force=True, log=quiet)

    def test_demo_refuses_symlink_home_before_writing(self):
        if os.name != "posix":
            self.skipTest("symlink semantics require POSIX")
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original"
            original.mkdir()
            linked = Path(tmp) / "linked"
            linked.symlink_to(original, target_is_directory=True)
            paths = Paths(linked)
            with self.assertRaises(DemoRefused):
                run_demo(paths, load_config(paths), log=quiet)
            self.assertEqual(list(original.iterdir()), [])

    def test_force_only_resets_existing_fictional_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "demo")
            run_demo(paths, load_config(paths), log=quiet)
            with self.assertRaises(DemoRefused):
                run_demo(paths, load_config(paths), log=quiet)
            run_demo(paths, load_config(paths), force=True, log=quiet)
            paths.messages.write_text("not demo data")
            with self.assertRaises(DemoRefused):
                run_demo(paths, load_config(paths), force=True, log=quiet)
            self.assertEqual(paths.messages.read_text(), "not demo data")

    def test_demo_refuses_symlink_inside_existing_demo(self):
        if os.name != "posix":
            self.skipTest("symlink semantics require POSIX")
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "demo")
            run_demo(paths, load_config(paths), log=quiet)
            paths.profile.unlink()
            outside = Path(tmp) / "outside.md"
            outside.write_text("real private profile")
            paths.profile.symlink_to(outside)
            with self.assertRaises(DemoRefused):
                run_demo(paths, load_config(paths), force=True, log=quiet)
            self.assertEqual(outside.read_text(), "real private profile")


if __name__ == "__main__":
    unittest.main()
