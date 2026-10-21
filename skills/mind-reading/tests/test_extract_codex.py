import unittest

import helpers
from mr import extract_codex
from mr.corpus import build_candidates


class CodexReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raws = list(extract_codex.iter_records(helpers.FIX / "codex"))
        cls.recs = list(build_candidates(cls.raws, [], {}, {}))
        cls.sessions = {r["session"] for r in cls.raws}

    def recs_for(self, part):
        return [r for r in self.recs if part in r["session"]]

    def test_subagent_exec_codex_exec_and_automation_files_skipped(self):
        for sid in ("cx02", "cx03", "cx04", "cx05"):
            self.assertFalse(any(sid in s for s in self.sessions), sid)
        self.assertTrue(any("cx01" in s for s in self.sessions))

    def test_input_image_dropped_text_kept(self):
        t = [r["text"] for r in self.recs_for("cx01")]
        self.assertIn("match the existing palette", t)
        self.assertFalse(any("image_url" in x or "base64" in x for x in t))

    def test_goal_reminder_kept_once_with_marker(self):
        goals = [r for r in self.recs_for("cx01") if r["origin"] == "goal_reminder"]
        self.assertEqual(len(goals), 1)
        self.assertTrue(goals[0]["text"].startswith("[goal reminder, kept once] <codex_internal_context"))

    def test_question_reply_unwrapped_to_answers(self):
        qa = [r for r in self.recs_for("cx01") if r["origin"] == "question_answer"]
        self.assertEqual(len(qa), 2)
        self.assertEqual(qa[0]["text"], "staging first, then prod after I look at it")
        self.assertIn("Which environment", qa[0]["meta"]["question"])
        self.assertEqual(qa[1]["text"], "yes keep it\nno flag, just ship")
        self.assertFalse(any("questionItemId" in r["text"] for r in self.recs))

    def test_skill_block_and_environment_context_dropped(self):
        t = [r["text"] for r in self.recs_for("cx01")]
        self.assertFalse(any(x.startswith("<skill>") for x in t))
        self.assertFalse(any(x.startswith("<environment_context>") for x in t))

    def test_ids_from_payload_or_timestamp_digest(self):
        import hashlib
        ids = [r["record_id"] for r in self.raws if "cx01" in r["session"]]
        self.assertTrue(all(i.startswith("msg_") or ":" in i for i in ids))
        last = [r for r in self.raws if "cx01" in r["session"]][-1]
        # no payload id: time plus a text digest, never the line ordinal, so a rollout rewritten in place keeps its keys
        self.assertEqual(last["record_id"], "%s:%s" % (last["time"], hashlib.sha256(last["text"].encode()).hexdigest()[:12]))
        self.assertNotIn(":%d" % last["ordinal"], last["record_id"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_project_is_cwd(self):
        self.assertTrue(all(r["project"] == "~/Code/webshop" for r in self.recs))


if __name__ == "__main__":
    unittest.main()
