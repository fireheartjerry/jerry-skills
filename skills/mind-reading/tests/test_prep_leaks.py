import unittest

import helpers
from mr.config import read_json
from mr.corpus import normalize

LEAK_TEXT = "please also update the changelog and bump the version before you tag the release"
SHORT_SHARED = "keep going"


class PrepLeakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="prep")
        cls.sessions = helpers.read_jsonl(cls.paths.sessions)
        cls.report = read_json(cls.paths.prep_report)

    def texts(self, split):
        return [normalize(m["text"]) for s in self.sessions if s["split"] == split for m in s["messages"]]

    def test_long_text_removed_from_train_and_dev_kept_in_test(self):
        leak = normalize(LEAK_TEXT)
        self.assertGreaterEqual(len(leak), self.cfg["prep"]["leak_min_chars"])
        self.assertNotIn(leak, self.texts("train"))
        self.assertNotIn(leak, self.texts("dev"))
        self.assertIn(leak, self.texts("test"))
        self.assertGreaterEqual(self.report["leak_removed_train"], 1)

    def test_short_text_exempt_and_counted(self):
        short = normalize(SHORT_SHARED)
        self.assertLess(len(short), self.cfg["prep"]["leak_min_chars"])
        self.assertIn(short, self.texts("train"))
        self.assertIn(short, self.texts("test"))
        self.assertGreaterEqual(self.report["leak_exempt_short"], 1)
        self.assertIn("leak_exemption", self.report)

    def test_chunks_do_not_contain_leak(self):
        import json
        for p in self.paths.chunks.glob("chunk-*.jsonl"):
            for line in p.read_text(encoding="utf-8").splitlines():
                for m in json.loads(line)["messages"]:
                    self.assertNotEqual(normalize(m["text"]), normalize(LEAK_TEXT))


if __name__ == "__main__":
    unittest.main()
