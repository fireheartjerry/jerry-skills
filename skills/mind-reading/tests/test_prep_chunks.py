import json
import unittest

import helpers
from mr.config import read_json


class PrepChunkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="prep")
        cls.chunks = []
        for p in sorted(cls.paths.chunks.glob("chunk-*.jsonl")):
            cls.chunks.append([json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()])
        cls.report = read_json(cls.paths.prep_report)

    def test_chunk_count_and_sizes(self):
        self.assertEqual(len(self.chunks), 2)
        limit = self.cfg["prep"]["chunk_chars"]
        for c in self.chunks:
            size = sum(len(json.dumps(b, ensure_ascii=False)) + 1 for b in c)
            self.assertTrue(size <= limit or len(c) == 1, size)

    def test_multi_part_session_carries_first_messages(self):
        parts = [b for c in self.chunks for b in c if b["session"] == "cs09"]
        self.assertEqual(len(parts), 2)
        self.assertEqual([b["part"] for b in parts], ["1/2", "2/2"])
        self.assertEqual(parts[0]["first_messages"], [])
        for b in parts[1:]:
            self.assertEqual(len(b["first_messages"]), 2)
            self.assertEqual(set(b["first_messages"][0]), {"time", "text"})
        part_limit = self.cfg["prep"]["part_chars"]
        for b in parts:
            self.assertLessEqual(len(json.dumps(b, ensure_ascii=False)), part_limit)
        self.assertEqual(sum(len(b["messages"]) for b in parts), 60)

    def test_order_by_session_start_and_header_fields(self):
        blocks = [b for c in self.chunks for b in c]
        starts = [b["start"] for b in blocks if b["time_reliable"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(blocks[0]["session"], "cs01")
        for b in blocks:
            self.assertEqual(set(b), {"source", "session", "part", "project", "start", "time_reliable", "first_messages", "messages"})
            for m in b["messages"]:
                self.assertEqual(set(m), {"time", "origin", "text"})
        unreliable = [b for b in blocks if not b["time_reliable"]]
        self.assertEqual(sorted(b["session"][-4:] for b in unreliable), ["cx06", "cx12"])
        for b in unreliable:
            self.assertIsNone(b["start"])  # a fake start never reaches the miner

    def test_bulk_times_null_and_real_continuation_times_kept(self):
        cx12 = next(b for c in self.chunks for b in c if b["session"].endswith("cx12"))
        self.assertEqual([m["text"][:14] for m in cx12["messages"]], ["also check the", "and the cookie", "ok good push i"])
        self.assertTrue(all(m["time"] for m in cx12["messages"]))  # the continuation was typed for real
        for p in self.paths.chunks.glob("chunk-*.jsonl"):
            for line in p.read_text(encoding="utf-8").splitlines():
                b = json.loads(line)
                if not b["time_reliable"]:
                    continue
                self.assertTrue(all(m["time"] for m in b["messages"]), b["session"])

    def test_caps_cut_long_and_nontyped_text_and_report_omitted(self):
        caps = self.cfg["prep"]
        blocks = [b for c in self.chunks for b in c]
        cs09 = [m for b in blocks if b["session"] == "cs09" for m in b["messages"]]
        self.assertEqual(len(cs09), 60)
        for m in cs09:
            self.assertLessEqual(len(m["text"]), caps["chunk_message_max_chars"] + 40)
            self.assertRegex(m["text"], r"\[\d+ chars omitted\]$")
        # the fixture's drafted mandate is a planted test/train leak and is removed from train, so cap it directly
        from mr.prep import chunk_msg
        omitted = {}
        for origin in ("drafted", "pasted", "goal_reminder"):
            m = chunk_msg({"time": "2026-01-01T00:00:00Z", "origin": origin, "text": "x" * 6000, "time_reliable": True}, caps, omitted)
            self.assertEqual(m["text"], "x" * caps["chunk_nontyped_head_chars"] + " [%d chars omitted]" % (6000 - caps["chunk_nontyped_head_chars"]))
            self.assertEqual(omitted[origin], 6000 - caps["chunk_nontyped_head_chars"])
        m = chunk_msg({"time": "2026-01-01T00:00:00Z", "origin": "typed", "text": "y" * 6000, "time_reliable": False}, caps, omitted)
        self.assertEqual(len(m["text"]), caps["chunk_message_max_chars"] + len(" [2000 chars omitted]"))
        self.assertIsNone(m["time"])  # a bulk-stamped time never reaches the miner
        short = chunk_msg({"time": "t", "origin": "drafted", "text": "short mandate", "time_reliable": True}, caps, omitted)
        self.assertEqual(short["text"], "short mandate")
        omitted = self.report["chunk_chars_omitted"]
        self.assertGreater(omitted["typed"], 300000)
        self.assertEqual(self.report["chunk_caps"], {"message_max_chars": 4000, "nontyped_head_chars": 1500})
        sessions = {s["session"]: s for s in helpers.read_jsonl(self.paths.sessions)}
        self.assertGreater(len(sessions["cs09"]["messages"][0]["text"]), 7000)  # sessions.jsonl keeps the full text

    def test_report_chunk_sizes(self):
        self.assertEqual(len(self.report["chunk_chars"]), 2)


if __name__ == "__main__":
    unittest.main()
