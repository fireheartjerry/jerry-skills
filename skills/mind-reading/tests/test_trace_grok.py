"""Grok raw-session parser tests. Fictional fixtures built at runtime; secrets are assembled from fragments."""
import json
import shutil
import sqlite3
import unittest
from pathlib import Path

import helpers  # noqa: F401
from mr import trace_grok, trace_ingest

SID = "11111111-2222-3333-4444-555555555555"
KEY = "sk-" + "a" * 24


def write(path, rows, raw=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((r if raw else json.dumps(r)) + "\n" for r in rows), encoding="utf-8")
    return path


def evs(path, **kw):
    return list(trace_grok.iter_events(path, **kw))


def body(events):
    return [e for e in events if e["action"] is None or e["action"]["kind"] != "source_summary"]


def summary(events):
    return json.loads(events[-1]["action"]["summary"])


class GrokTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.tmpdir("mr-grok-")
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.sess = self.root / "sessions" / "%2Fsrv%2Fdemo%2Fmy%20proj" / SID

    def chat(self, rows, name="chat_history.jsonl"):
        return write(self.sess / name, rows)

    def test_discover_known_files_only(self):
        keep = [self.chat([{"type": "user", "content": "x"}]), write(self.sess / "events.jsonl", [{"type": "a"}]),
                write(self.sess.parent / "prompt_history.jsonl", [{"prompt": "p"}])]
        db = self.root / "session_search.sqlite"
        sqlite3.connect(str(db)).close()
        keep.append(db)
        write(self.root / "auth.json", [{"token": KEY}])
        write(self.root / "config.json", [{"a": 1}])
        write(self.root / "sessions" / "x" / "notes" / "chat_history.jsonl", [{"type": "user"}])  # non-UUID parent
        write(self.sess / "other.jsonl", [{"a": 1}])
        self.assertEqual(trace_grok.discover(self.root), sorted(keep))
        self.assertEqual(trace_grok.discover(self.root / "missing"), [])
        self.assertEqual(trace_grok.discover(self.root / "auth.json"), [])

    def test_raw_prompt_is_unknown_not_human(self):
        p = write(self.sess.parent / "prompt_history.jsonl", [
            {"timestamp": "2026-03-04T05:06:07-05:00", "session_id": SID, "prompt": "do it", "is_bash": False},
            {"timestamp": "2026-03-04T05:06:08-05:00", "session_id": SID, "prompt": "ls", "is_bash": True},
            {"timestamp": "bad", "prompt": "orphan"}])
        got = body(evs(p))
        self.assertEqual({e["actor"] for e in got}, {"unknown"})
        self.assertEqual({e["human_signal"] for e in got}, {"grok.unverified_prompt"})
        self.assertEqual(got[0]["time"], "2026-03-04T10:06:07.000Z")
        self.assertEqual(got[1]["content_kinds"], ["bash"])
        self.assertEqual(got[0]["project"], "/srv/demo/my proj")
        self.assertIsNone(got[2]["time"])
        self.assertTrue(got[2]["session"].startswith("document:"))

    def test_only_explicit_origin_is_human(self):
        p = self.chat([
            {"type": "user", "content": "plain", "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "user", "content": "mine", "origin": {"kind": "human"}},
            {"type": "user", "content": "bot", "origin": {"kind": "task-notification"}},
            {"type": "user", "content": "side", "isSidechain": True},
            {"type": "user", "content": "<system-reminder>x</system-reminder>"}])
        got = body(evs(p))
        self.assertEqual([e["actor"] for e in got], ["unknown", "human", "agent", "agent", "injected"])
        self.assertEqual(got[0]["human_signal"], "grok.unverified_prompt")
        self.assertEqual(got[4]["text"], "")
        self.assertEqual(got[0]["session"], SID)

    def test_time_normalized_or_none(self):
        p = self.chat([{"type": "assistant", "content": "a", "ts": 1767225600},
                       {"type": "assistant", "content": "b"}])
        got = body(evs(p))
        self.assertEqual(got[0]["time"], "2026-01-01T00:00:00.000Z")
        self.assertIsNone(got[1]["time"])
        self.assertIsNone(got[1]["evidence_key"])

    def test_no_hidden_reasoning_and_tools_are_observations(self):
        p = self.chat([
            {"type": "system", "content": "secret system prompt"},
            {"type": "assistant", "uuid": "m1", "content": [
                {"type": "thinking", "thinking": "private chain"},
                {"type": "text", "text": "visible"},
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "ls"}}]},
            {"type": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out", "is_error": False}]},
            {"type": "tool", "tool_call_id": "t2", "content": "boom", "is_error": True}])
        got = body(evs(p))
        blob = json.dumps(got)
        self.assertNotIn("private chain", blob)
        self.assertNotIn("secret system prompt", blob)
        self.assertEqual([e["role"] for e in got], ["meta", "tool", "assistant", "tool", "tool"])
        self.assertEqual(got[2]["text"], "visible")
        self.assertEqual(got[3]["actor"], "agent")
        self.assertEqual(got[3]["action"]["name"], "bash")
        self.assertTrue(got[3]["action"]["success"])
        self.assertFalse(got[4]["action"]["success"])

    def test_events_keep_names_not_values(self):
        p = write(self.sess / "events.jsonl", [
            {"ts": "2026-02-02T00:00:00Z", "type": "model_call", "metadata": {"token_note": KEY, "n": 3}}])
        got = body(evs(p))
        self.assertEqual(len(got), 1)
        blob = json.dumps(got)
        self.assertNotIn(KEY, blob)
        info = json.loads(got[0]["action"]["summary"])
        self.assertEqual(info["type"], "model_call")
        self.assertEqual(info["metadata_keys"], ["n", "token_note"])
        self.assertEqual(got[0]["time"], "2026-02-02T00:00:00.000Z")

    def test_native_tool_result_rows_are_not_user_voice(self):
        p = self.chat([{"type": "tool_result", "tool_call_id": "result-1", "content": "observed output", "exit_code": 0}])
        got = body(evs(p))
        self.assertEqual(got[0]["role"], "tool")
        self.assertEqual(got[0]["actor"], "agent")
        self.assertTrue(got[0]["action"]["success"])
        self.assertEqual(got[0]["action"]["call_id"], "result-1")

    def test_secrets_scrubbed(self):
        p = self.chat([{"type": "assistant", "content": "key is " + KEY},
                       {"type": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {"k": KEY}}]}])
        blob = json.dumps(evs(p))
        self.assertNotIn(KEY, blob)
        self.assertIn("[REDACTED:key]", blob)

    def test_copied_files_have_stable_ids(self):
        rows = [{"type": "assistant", "content": "same", "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "user", "content": "q", "timestamp": "2026-01-01T00:00:01Z"}]
        a = self.chat(rows)
        b = write(self.root / "copy" / "sessions" / "%2Fsrv%2Fdemo%2Fmy%20proj" / SID / "chat_history.jsonl", rows)
        ids = lambda p: [e["id"] for e in body(evs(p))]
        self.assertEqual(ids(a), ids(b))
        self.assertEqual(len(set(ids(a))), 2)

    def test_diagnostics_and_limit_prefix(self):
        rows = [json.dumps({"type": "assistant", "content": "n%d" % i}) for i in range(5)]
        p = write(self.sess / "chat_history.jsonl", ["{bad json"] + rows, raw=True)
        full = evs(p)
        s = summary(full)
        self.assertEqual(s["malformed"], 1)
        self.assertEqual(len(body(full)), 5)
        size = len(("{bad json\n" + "\n".join(rows[:2]) + "\n").encode())
        cut = evs(p, limit_bytes=size + 5)
        self.assertEqual(len(body(cut)), 2)
        self.assertTrue(summary(cut)["truncated_tail"])
        self.assertEqual(cut[-1]["action"]["kind"], "source_summary")

    def test_sqlite_delegates_and_rejects_unknown(self):
        db = self.root / "session_search.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE session_docs (session_id, cwd, updated_at, title, content, content_hash)")
        con.execute("INSERT INTO session_docs VALUES ('s','/w','2026-01-01T00:00:00Z','t','',?)", ("h",))
        con.commit()
        con.close()
        got = body(evs(db))
        self.assertEqual(got[0]["human_signal"], "grok.derived_document")
        with self.assertRaises(trace_ingest.TraceError):
            evs(self.root / "config.json")
        with self.assertRaises(trace_ingest.TraceError):
            evs(db, source="codex")


if __name__ == "__main__":
    unittest.main()
