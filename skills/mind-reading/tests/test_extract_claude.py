import re
import unittest

import helpers
from mr import extract_claude
from mr.corpus import build_candidates

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(#\d+)?$")


def kept():
    raws = list(extract_claude.iter_records(helpers.FIX / "claude"))
    recs = list(build_candidates(raws, [], {}, {}))
    return raws, recs


class ClaudeReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raws, cls.recs = kept()
        cls.by_session = {}
        for r in cls.recs:
            cls.by_session.setdefault(r["session"], []).append(r)

    def texts(self, sid):
        return [r["text"] for r in self.by_session.get(sid, [])]

    def test_user_records_kept_and_filters_applied(self):
        t = self.texts("cs01")
        self.assertIn("fix the export button it is broken again", t)
        self.assertIn("run the tests before you report", t)  # system-reminder stripped
        self.assertIn("this screenshot shows the bug", t)  # image stub stripped
        self.assertNotIn("meta record that must be dropped", t)
        self.assertFalse(any("[Request interrupted" in x for x in t))
        self.assertFalse(any("plain tool output" in x for x in t))

    def test_sdk_entrypoint_sidechain_usertype_dropped(self):
        self.assertNotIn("cs03", self.by_session)
        t = self.texts("cs04")
        self.assertNotIn("sidechain text", t)
        self.assertNotIn("agent user type text", t)
        self.assertIn("refactor the cart module and keep the tests green", t)

    def test_queued_command_human_kept_other_origins_dropped(self):
        recs = self.by_session["cs02"]
        signed = [r for r in recs if r["text"] == "signed in on chrome"]
        self.assertEqual(len(signed), 2)  # queued attachment plus a later typed repeat 3h50 apart (prep keeps both)
        self.assertEqual(signed[0]["time"], "2026-01-05T09:10:00.000Z")  # attachment timestamp
        t = self.texts("cs02")
        self.assertEqual(t.count("also make the header logo smaller"), 2)  # queued + user; prep dedups within 10 minutes
        self.assertNotIn("peer message from another session", t)
        self.assertNotIn("origin-less queued text", t)
        self.assertNotIn("peer typed text", t)
        self.assertFalse(any(x.startswith("<task-notification>") for x in t))

    def test_legacy_wrapper_extracted(self):
        self.assertIn("also rename the helper", self.texts("cs04"))

    def test_wrapper_quoted_inside_other_text_never_becomes_user_text(self):
        everything = " ".join(r["text"] for r in self.recs)
        self.assertNotIn("injected instruction", everything)
        self.assertNotIn("wrapper with no suffix", everything)
        self.assertNotIn("Fetched page", everything)
        # a typed message that mentions the sentence stays the user's own text, unchanged
        typed = {"type": "user", "userType": "external", "entrypoint": "cli",
                 "message": {"role": "user", "content": "why does the log say The user sent a new message while you were working: twice?"}}
        self.assertEqual(extract_claude._user_record(typed), [typed["message"]["content"]])

    def test_grant_message_is_a_plain_typed_record(self):
        t = self.texts("cs01")
        self.assertIn("btw you can push the working branch whenever its green, no need to ask me", t)

    def test_subagents_directory_not_read(self):
        self.assertFalse(any("subagent text" in r["text"] for r in self.recs))
        self.assertFalse(any(r["session"] == "agent-a1" for r in self.raws))

    def test_record_ids_are_uuids_and_keys_unique(self):
        for r in self.recs:
            self.assertRegex(r["record_id"], UUID_RE)
        keys = [r["key"] for r in self.recs]
        self.assertEqual(len(keys), len(set(keys)))

    def test_project_from_cwd_is_scrubbed(self):
        self.assertTrue(all(r["project"] == "~/Code/webshop" for r in self.recs), {r["project"] for r in self.recs})

    def test_skip_prefix_session_keeps_only_typed(self):
        t = self.texts("cs05")
        self.assertIn("write the release notes for this sprint", t)
        self.assertEqual(len([x for x in t if x.startswith("<")]), 0)


if __name__ == "__main__":
    unittest.main()
