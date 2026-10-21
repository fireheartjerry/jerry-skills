"""The committed fixtures must equal the generator's output, so the two cannot drift."""
import importlib.util
import unittest

import helpers


class FixtureGeneratorTests(unittest.TestCase):
    def test_committed_fixtures_match_generator(self):
        spec = importlib.util.spec_from_file_location("make_fixtures", helpers.TESTS / "make_fixtures.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        files = mod.generate()
        self.assertGreater(len(files), 30)
        self.assertEqual(mod.check(files), [])

    def test_fixtures_carry_no_absolute_user_paths(self):
        marker = "/" + "Users/"
        for p in helpers.FIX.rglob("*"):
            if p.is_file():
                self.assertNotIn(marker, p.read_text(encoding="utf-8"), p)


if __name__ == "__main__":
    unittest.main()
