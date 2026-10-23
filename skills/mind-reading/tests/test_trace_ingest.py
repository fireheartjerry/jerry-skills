"""trace_ingest: provenance-first observations from Claude Code, Codex (plain and zstd), Kimi and Grok session stores.
All fixtures are fictional and built on the fly in scratch directories."""
import json
import io
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path

import helpers
from mr import trace_ingest as ti

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "66666666-7777-8888-9999-000000000000"
THINK = "PRIVATE-REASONING-SENTINEL"
FAKE_KEY = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2"


def _write(path, rows, raw_tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + raw_tail, encoding="utf-8")
    return path


def _events(path, source, **kw):
    return list(ti.iter_events(path, source, **kw))


def _summary(events):
    last = events[-1]
    assert last["action"]["kind"] == "source_summary"
    return json.loads(last["action"]["summary"])


def _bodies(events):
    return [e for e in events if e["action"] is None or e["action"]["kind"] != "source_summary"]


def cc_user(uid, text, ts="2026-03-01T10:00:00.000Z", **extra):
    r = {"type": "user", "uuid": uid, "sessionId": SID, "timestamp": ts, "cwd": "/work/app",
         "message": {"role": "user", "content": text}}
    r.update(extra)
    return r


def cc_asst(uid, content, ts="2026-03-01T10:00:05.000Z", **extra):
    r = {"type": "assistant", "uuid": uid, "sessionId": SID, "timestamp": ts, "cwd": "/work/app",
         "message": {"role": "assistant", "content": content}}
    r.update(extra)
    return r


class DiscoverTests(unittest.TestCase):
    def test_python_decoder_checks_final_frame_and_concatenated_frames(self):
        try:
            import zstandard as z
        except ImportError:
            self.skipTest("optional Python zstandard not installed")
        a=z.ZstdCompressor().compress(b"first\n"); b=z.ZstdCompressor().compress(b"second\n")
        with io.BufferedReader(ti._StrictZstdReader(io.BytesIO(a+b),z)) as reader:
            self.assertEqual(reader.read(),b"first\nsecond\n")
        for value in (a[:-1],a+b[:-1],b"x",b""):
            with io.BufferedReader(ti._StrictZstdReader(io.BytesIO(value),z)) as reader:
                with self.assertRaises((EOFError,z.ZstdError)): reader.read()
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def test_claude_only_session_files_and_subagents(self):
        proj = self.tmp / "-work-app"
        _write(proj / (SID + ".jsonl"), [{}])
        _write(proj / SID / "subagents" / "agent-a1.jsonl", [{}])
        _write(proj / "eval-artifact.jsonl", [{}])
        _write(proj / "memory" / "note.md", [])
        _write(proj / SID / "tool-results" / "x.jsonl", [{}])
        got = ti.discover(self.tmp, "claude")
        self.assertEqual(sorted(p.name for p in got), sorted([SID + ".jsonl", "agent-a1.jsonl"]))

    def test_codex_kimi_grok_memory(self):
        _write(self.tmp / "c" / ("rollout-1-%s.jsonl" % SID), [{}])
        (self.tmp / "c" / ("rollout-2-%s.jsonl.zst" % SID2)).write_bytes(b"")
        (self.tmp / "c" / "other.jsonl").write_text("{}")
        _write(self.tmp / "k" / "wd" / "session_x" / "agents" / "main" / "wire.jsonl", [{}])
        _write(self.tmp / "k" / "wd" / "session_x" / "agents" / "agent-1" / "wire.jsonl", [{}])
        _write(self.tmp / "k" / "wd" / "session_x" / "other" / "wire.jsonl", [{}])
        (self.tmp / "g" / "sessions").mkdir(parents=True)
        (self.tmp / "g" / "sessions" / "session_search.sqlite").write_bytes(b"")
        (self.tmp / "m").mkdir()
        (self.tmp / "m" / "a.md").write_text("x")
        self.assertEqual(len(ti.discover(self.tmp / "c", "codex")), 2)
        self.assertEqual(len(ti.discover(self.tmp / "k", "kimi")), 2)
        self.assertEqual(len(ti.discover(self.tmp / "g", "grok")), 1)
        self.assertEqual(len(ti.discover(self.tmp / "m", "memory")), 1)
        self.assertEqual(ti.discover(self.tmp / "missing", "codex"), [])

    def test_unknown_source_raises(self):
        with self.assertRaises(ti.TraceError):
            ti.discover(self.tmp, "nope")
        with self.assertRaises(ti.TraceError):
            ti.iter_events(self.tmp / "x.jsonl", "claude")
        self.assertTrue(issubclass(ti.TraceError, ValueError))


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def run_claude(self, rows, name=None, **kw):
        path = _write(self.tmp / "-work-app" / (name or (SID + ".jsonl")), rows)
        return _events(path, "claude", **kw)

    def test_native_ids_are_scoped_to_session_and_stable_across_copies(self):
        a = self.run_claude([cc_user("shared", "first request", origin={"kind": "human"})])
        b = self.run_claude([cc_user("shared", "first request", origin={"kind": "human"})], name=SID2+".jsonl")
        c = self.run_claude([cc_user("shared", "second request", sessionId=SID2, origin={"kind": "human"})])
        self.assertEqual(_bodies(a)[0]["id"], _bodies(b)[0]["id"])
        self.assertNotEqual(_bodies(a)[0]["id"], _bodies(c)[0]["id"])

    def test_nonnative_ids_are_path_free_and_repeats_stay_distinct(self):
        rows = [dict(kimi_prompt("hello", "2026-03-01T10:00:00Z"), type="context.append_message",
                     message={"role": "user", "origin": {"kind": "x"}, "content": "note"}),
                dict(kimi_prompt("hello", "2026-03-01T10:00:01Z"), type="context.append_message",
                     message={"role": "user", "origin": {"kind": "x"}, "content": "note"})]
        a = _events(_write(self.tmp / "one" / "sess" / "agents" / "main" / "wire.jsonl", rows), "kimi")
        b = _events(_write(self.tmp / "two" / "sess" / "agents" / "main" / "wire.jsonl", rows), "kimi")
        self.assertEqual(len(_bodies(a)), 2)
        self.assertEqual([e["id"] for e in _bodies(a)], [e["id"] for e in _bodies(b)])
        self.assertEqual(len({e["id"] for e in _bodies(a)}), 2)

    def test_evidence_key_needs_time_and_ignores_path_and_session(self):
        r1 = cc_user("u1", "same  words", origin={"kind": "human"})
        a = self.run_claude([r1])
        b = self.run_claude([dict(r1, sessionId=SID2)], name=SID2 + ".jsonl")
        k = _bodies(a)[0]["evidence_key"]
        self.assertTrue(k)
        self.assertEqual(k, _bodies(b)[0]["evidence_key"])
        self.assertNotEqual(k, _bodies(self.run_claude([dict(r1, uuid="other")]))[0]["evidence_key"])
        self.assertNotIn("same", k)
        nt = self.run_claude([cc_user("u1", "same  words", ts=None, origin={"kind": "human"})])
        self.assertIsNone(_bodies(nt)[0]["evidence_key"])
        self.assertIsNone(a[-1]["evidence_key"])

    def test_explicit_user_provenance_survives_cli_originator(self):
        self.assertEqual(ti._codex_class({"thread_source": "user", "originator": "codex_exec", "source": "exec"})[0], "human")
        self.assertEqual(ti._codex_class({"originator": "codex_exec", "source": "exec"})[0], "unknown")

    def test_human_vs_sdk_unknown_vs_legacy(self):
        ev = self.run_claude([
            cc_user("u1", "fix  the typo teh", origin={"kind": "human"}, turnOrigin="human"),
            cc_user("u2", "sdk driven prompt", turnOrigin="sdk"),
            cc_user("u3", "turn origin only", turnOrigin="human"),
            cc_user("u4", "old session prompt"),
        ])
        by = {e["source_ref"].split(":")[1]: e for e in _bodies(ev)}
        self.assertEqual(by["u1"]["actor"], "human")
        self.assertEqual(by["u1"]["text"], "fix  the typo teh")  # spelling and spacing preserved
        self.assertEqual(by["u2"]["actor"], "unknown")
        self.assertIn("unknown_origin", by["u2"]["human_signal"])
        self.assertEqual(by["u3"]["actor"], "human")
        self.assertEqual(by["u4"]["actor"], "unknown")
        self.assertEqual(by["u4"]["human_signal"], "claude.legacy_no_origin")
        self.assertEqual(by["u1"]["time"], "2026-03-01T10:00:00.000Z")
        self.assertEqual(by["u1"]["project"], "/work/app")

    def test_sidechain_and_subagent_path_are_agent(self):
        sub = self.run_claude([cc_user("s1", "brief for the helper", origin={"kind": "human"}, isSidechain=True,
                                       agentId="a1")])
        self.assertEqual(_bodies(sub)[0]["actor"], "agent")
        self.assertEqual(_bodies(sub)[0]["parent_session"], SID)
        path = _write(self.tmp / "-work-app" / SID / "subagents" / "agent-a2.jsonl",
                      [cc_user("s2", "delegated brief", origin={"kind": "human"})])
        ev = _events(path, "claude")
        self.assertEqual(_bodies(ev)[0]["actor"], "agent")
        self.assertEqual(_bodies(ev)[0]["parent_session"], SID)

    def test_injected_blocks_are_blank_meta(self):
        ev = self.run_claude([
            cc_user("i1", "<system-reminder>be careful</system-reminder>", origin={"kind": "human"}),
            cc_user("i2", "hello", isMeta=True),
        ])
        for e in _bodies(ev):
            self.assertEqual((e["role"], e["actor"], e["text"]), ("meta", "injected", ""))
        self.assertGreater(_bodies(ev)[0]["text_chars"], 0)

    def test_tool_linkage_success_and_no_thinking(self):
        ev = self.run_claude([
            cc_user("u1", "run it", origin={"kind": "human"}),
            cc_asst("a1", [{"type": "thinking", "thinking": THINK}, {"type": "text", "text": "running"},
                           {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make test"}},
                           {"type": "tool_use", "id": "t2", "name": "Read", "input": {"path": "x"}}]),
            cc_user("r1", [{"type": "tool_result", "tool_use_id": "t1", "content": "all good: error budget fine",
                            "is_error": False}], sourceToolAssistantUUID="a1"),
            cc_user("r2", [{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "ok"}],
                            "is_error": True}]),
            cc_user("r3", [{"type": "tool_result", "tool_use_id": "t9", "content": "no flag"}]),
        ])
        acts = {e["action"]["call_id"]: e["action"] for e in ev if e["action"] and e["action"]["kind"] == "tool_result"}
        self.assertEqual(acts["t1"]["success"], True)  # not downgraded by the word "error" in the body
        self.assertEqual(acts["t1"]["name"], "Bash")
        self.assertEqual(acts["t2"]["success"], False)
        self.assertIsNone(acts["t9"]["success"])
        calls = [e for e in ev if e["action"] and e["action"]["kind"] == "tool_call"]
        self.assertEqual([c["action"]["name"] for c in calls], ["Bash", "Read"])
        self.assertTrue(all(e["role"] == "tool" and e["text"] == "" for e in calls))
        self.assertNotIn(THINK, json.dumps(ev))
        self.assertEqual(_summary(ev)["ignored"]["thinking_block"], 1)

    def test_uuid_copy_dedup_and_stable_ids(self):
        rows = [cc_user("u1", "same prompt", origin={"kind": "human"}), cc_asst("a1", "answer")]
        a = self.run_claude(rows)
        b = self.run_claude(rows + rows, name=SID2 + ".jsonl")
        self.assertEqual([e["id"] for e in _bodies(a)], [e["id"] for e in _bodies(b)])
        self.assertEqual(len(_bodies(b)), 2)
        self.assertEqual(_summary(b)["duplicates"], 2)
        self.assertNotEqual(a[0]["source_ref"], b[0]["source_ref"])  # path hash differs, native id stays

    def test_missing_timestamps_and_meta_records(self):
        ev = self.run_claude([{"type": "last-prompt", "lastPrompt": "x"},
                              cc_user("u1", "no stamp", ts=None, origin={"kind": "human"})])
        e = _bodies(ev)[0]
        self.assertIsNone(e["time"])
        self.assertEqual(_summary(ev)["ignored"]["type:last-prompt"], 1)

    def test_secret_scrub_and_truncation(self):
        long_text = "word " * 5000
        ev = self.run_claude([cc_user("u1", "token is %s ok" % FAKE_KEY, origin={"kind": "human"}),
                              cc_user("u2", long_text, origin={"kind": "human"})])
        first, second = _bodies(ev)
        self.assertNotIn(FAKE_KEY, first["text"])
        self.assertIn("[REDACTED:", first["text"])
        self.assertTrue(second["text_truncated"])
        self.assertEqual(len(second["text"]), 16000)
        self.assertEqual(second["text_chars"], len(long_text))

    def test_malformed_and_truncated_source(self):
        path = _write(self.tmp / "-work-app" / (SID + ".jsonl"),
                      [cc_user("u1", "ok", origin={"kind": "human"}), {}],
                      raw_tail='{"type": "user", "message": {"con\n[1,2]\n')
        before = path.read_bytes()
        ev = _events(path, "claude")
        s = _summary(ev)
        self.assertEqual(s["malformed"], 2)
        self.assertEqual(len(_bodies(ev)), 1)
        self.assertFalse(ev[-1]["action"]["success"])
        self.assertEqual(path.read_bytes(), before)

    def test_limit_bytes_freezes_prefix(self):
        rows = [cc_user("u%d" % i, "prompt %d" % i, origin={"kind": "human"}) for i in range(4)]
        path = _write(self.tmp / "-work-app" / (SID + ".jsonl"), rows)
        first_two = len(json.dumps(rows[0]).encode()) + len(json.dumps(rows[1]).encode()) + 2
        ev = _events(path, "claude", limit_bytes=first_two + 7)  # cuts row three mid-line
        s = _summary(ev)
        self.assertEqual(len(_bodies(ev)), 2)
        self.assertTrue(s["truncated_tail"])
        self.assertEqual(s["malformed"], 0)
        self.assertEqual(len(_bodies(_events(path, "claude"))), 4)
        with self.assertRaises(ti.TraceError):
            ti.iter_events(path, "claude", limit_bytes=-1)


def kimi_prompt(text, ts):
    return {"type": "turn.prompt", "time": ts, "promptId": None, "input": [{"type": "text", "text": text}]}


def codex_rows(thread_source="user", source="vscode", originator="Codex Desktop", with_event_copy=True, spawn=None):
    meta = {"id": SID, "cwd": "/work/app", "originator": originator, "source": source,
            "timestamp": "2026-03-01T10:00:00.000Z", "base_instructions": {"text": "SYSTEM-PROMPT-SENTINEL"}}
    if thread_source is not None:
        meta["thread_source"] = thread_source
    if spawn:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": spawn, "depth": 1}}}

    def msg(i, role, kinds, texts, ts):
        item = "input_text" if role == "user" else "output_text"
        return {"timestamp": ts, "ordinal": i, "type": "response_item", "payload": {
            "type": "message", "id": "m%d" % i, "role": role, "content": [{"type": item, "text": t} for t in texts],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": kinds, "turn_id": "turn1"}}}

    rows = [{"timestamp": "2026-03-01T10:00:00.000Z", "ordinal": 0, "type": "session_meta", "payload": meta}]
    if with_event_copy:
        rows.append({"timestamp": "2026-03-01T10:00:01.000Z", "ordinal": 1, "type": "event_msg", "payload": {
            "type": "item_completed", "item": {"type": "UserMessage", "id": "item1",
                                                "content": [{"type": "text", "text": "please add a retry"}]}}})
    rows += [
        msg(2, "user", ["agents_md.instructions", "environments.environment_context"],
            ["# AGENTS.md instructions for /work/app", "<environment_context>cwd</environment_context>"],
            "2026-03-01T10:00:01.100Z"),
        msg(3, "user", ["user.text"], ["please add a retry"], "2026-03-01T10:00:01.200Z"),
        msg(4, "developer", ["unknown"], ["DEVELOPER-SENTINEL"], "2026-03-01T10:00:01.300Z"),
        {"timestamp": "2026-03-01T10:00:02.000Z", "ordinal": 5, "type": "response_item",
         "payload": {"type": "reasoning", "id": "r1", "summary": [{"text": THINK}], "encrypted_content": "zzz"}},
        msg(6, "assistant", ["unknown"], ["adding it"], "2026-03-01T10:00:03.000Z"),
        {"timestamp": "2026-03-01T10:00:04.000Z", "ordinal": 7, "type": "response_item",
         "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "make test"}},
        {"timestamp": "2026-03-01T10:00:05.000Z", "ordinal": 8, "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                     "output": [{"type": "input_text", "text": "Process exited with code 1\nerror: none really"}]}},
        {"timestamp": "2026-03-01T10:00:06.000Z", "ordinal": 9, "type": "response_item",
         "payload": {"type": "custom_tool_call", "call_id": "c2", "name": "exec", "input": "ls"}},
        {"timestamp": "2026-03-01T10:00:06.500Z", "ordinal": 10, "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "c2",
                     "output": json.dumps({"output": "fine", "metadata": {"exit_code": 0}})}},
    ]
    return rows


def _zstd_compress(src, dst):
    try:
        import zstandard
        dst.write_bytes(zstandard.ZstdCompressor().compress(src.read_bytes()))
        return True
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if not exe:
        return False
    subprocess.run([exe, "-q", "-f", "-o", str(dst), str(src)], check=True)
    return True


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def run_codex(self, rows, name=None):
        path = _write(self.tmp / "2026" / "03" / "01" / (name or ("rollout-2026-03-01T05-00-00-%s.jsonl" % SID)), rows)
        return path, _events(path, "codex")

    def humans(self, ev):
        return [e for e in ev if e["role"] == "user"]

    def test_user_text_vs_injections_and_event_copy(self):
        path, ev = self.run_codex(codex_rows())
        users = self.humans(ev)
        self.assertEqual(len(users), 1)  # the event_msg copy is not a second human turn
        self.assertEqual((users[0]["actor"], users[0]["text"]), ("human", "please add a retry"))
        self.assertEqual(users[0]["human_signal"], "codex.thread_source.user+user.text")
        inj = [e for e in ev if e["actor"] == "injected"]
        self.assertEqual(len(inj), 1)
        self.assertEqual(inj[0]["text"], "")
        self.assertIn("agents_md.instructions", inj[0]["content_kinds"])
        blob = json.dumps(ev)
        for secret in (THINK, "SYSTEM-PROMPT-SENTINEL", "DEVELOPER-SENTINEL"):
            self.assertNotIn(secret, blob)
        self.assertEqual(users[0]["session"], SID)
        self.assertEqual(users[0]["time"], "2026-03-01T10:00:01.200Z")
        self.assertEqual(_summary(ev)["duplicates"], 1)

    def test_event_copy_after_response_item_and_repeated_prompts(self):
        rows = codex_rows(with_event_copy=False)
        copy = {"timestamp": "2026-03-01T10:00:02.000Z", "ordinal": 1, "type": "event_msg", "payload": {
            "type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "please add a retry"}]}}}
        again = json.loads(json.dumps(rows[2]))
        again["payload"]["id"] = "m33"
        _, ev = self.run_codex(rows[:3] + [copy, again] + rows[3:])
        self.assertEqual(len(self.humans(ev)), 2)  # a genuine repeat is kept, only the copy is dropped

    def test_event_msg_only_is_blank_meta_not_a_user_turn(self):
        rows = [r for r in codex_rows() if not (r["type"] == "response_item" and r["payload"].get("role") == "user")]
        _, ev = self.run_codex(rows)
        self.assertEqual(self.humans(ev), [])
        un = [e for e in ev if "unanchored_user_event" in e["content_kinds"]]
        self.assertEqual(len(un), 1)
        self.assertEqual((un[0]["role"], un[0]["actor"], un[0]["text"]), ("meta", "unknown", ""))
        self.assertEqual(un[0]["human_signal"], "codex.event_msg_only_unanchored")
        self.assertEqual(un[0]["time"], "2026-03-01T10:00:01.000Z")
        self.assertEqual(_summary(ev)["unanchored_user_events"], 1)

    def test_misaligned_kinds_mixed_turn_is_unknown(self):
        rows = codex_rows(with_event_copy=False)
        rows[2]["payload"]["content"] = [
            {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
            {"type": "input_text", "text": "looks like a request"},
            {"type": "input_text", "text": "mystery wrapper text"}]
        rows[2]["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] = ["user.text", "x.wrapper"]
        rows[2]["payload"]["id"] = "mix"
        _, ev = self.run_codex(rows)
        self.assertFalse(any(e["actor"] == "human" for e in ev))
        mixed = [e for e in ev if e["human_signal"] == "codex.kinds_misaligned"]
        self.assertEqual(len(mixed), 1)
        self.assertEqual((mixed[0]["role"], mixed[0]["actor"]), ("user", "unknown"))
        self.assertNotIn("environment_context", mixed[0]["text"])
        inj = [e for e in ev if e["actor"] == "injected" and "mix:injected" in e["source_ref"]]
        self.assertEqual([e["text"] for e in inj], [""])

    def test_delegation_and_legacy_and_exec(self):
        _, sub = self.run_codex(codex_rows(thread_source="subagent", spawn="parent-thread-1"), name="rollout-a-%s.jsonl" % SID2)
        user = self.humans(sub)[0]
        self.assertEqual(user["actor"], "agent")
        self.assertEqual(user["parent_session"], "parent-thread-1")
        _, auto = self.run_codex(codex_rows(thread_source="automation"), name="rollout-b-%s.jsonl" % SID2)
        self.assertEqual(self.humans(auto)[0]["actor"], "agent")
        _, legacy = self.run_codex(codex_rows(thread_source=None), name="rollout-c-%s.jsonl" % SID2)
        self.assertEqual(self.humans(legacy)[0]["actor"], "unknown")
        _, execrun = self.run_codex(codex_rows(thread_source="user", source="exec", originator="codex_exec"),
                                    name="rollout-d-%s.jsonl" % SID2)
        self.assertEqual(self.humans(execrun)[0]["actor"], "human")

    def test_turn_trigger_user_does_not_make_human(self):
        rows = codex_rows(thread_source="subagent", spawn="p")
        rows[0]["payload"]["turn_trigger"] = "user"
        _, ev = self.run_codex(rows)
        self.assertEqual(self.humans(ev)[0]["actor"], "agent")

    def test_tool_linkage_and_explicit_success(self):
        _, ev = self.run_codex(codex_rows())
        res = {e["action"]["call_id"]: e["action"] for e in ev if e["action"] and e["action"]["kind"] == "tool_result"}
        self.assertEqual(res["c1"]["success"], False)
        self.assertEqual(res["c1"]["name"], "exec")
        self.assertEqual(res["c2"]["success"], True)

    def test_zstd_plain_parity(self):
        path, plain = self.run_codex(codex_rows())
        z = path.with_name(path.name + ".zst")
        if not _zstd_compress(path, z):
            self.skipTest("no zstandard module or zstd CLI")
        comp = _events(z, "codex")

        self.assertEqual([{k: v for k, v in e.items() if k != "source_ref"} for e in plain[:-1]],
                         [{k: v for k, v in e.items() if k != "source_ref"} for e in comp[:-1]])
        self.assertTrue(_summary(comp)["compressed"])
        self.assertEqual(_summary(comp)["errors"], [])

    def test_corrupt_zstd_is_recorded(self):
        path, _ = self.run_codex(codex_rows())
        z = path.with_name(path.name + ".zst")
        if not _zstd_compress(path, z):
            self.skipTest("no zstandard module or zstd CLI")
        z.write_bytes(z.read_bytes()[:40])
        ev = _events(z, "codex")
        self.assertTrue(any(err.startswith("decompress_failed") for err in _summary(ev)["errors"]))
        self.assertFalse(ev[-1]["action"]["success"])
        z.write_bytes(b"not zstd at all")
        self.assertTrue(_summary(_events(z, "codex"))["errors"])


class KimiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.sess = self.tmp / "sessions" / "wd_demo_abc" / "session_0001"

    def wire(self, agent, rows):
        return _write(self.sess / "agents" / agent / "wire.jsonl", rows)

    def test_prompt_copies_ms_time_and_tool_link(self):
        (self.sess).mkdir(parents=True)
        (self.sess / "state.json").write_text(json.dumps({"cwd": "/work/app", "agents": {
            "main": {"type": "main"}, "agent-1": {"type": "sub", "parentAgentId": "main"}}}))
        rows = [
            {"type": "metadata", "created_at": 1792657014599},
            {"type": "turn.prompt", "agentId": "main", "input": [{"type": "text", "text": "do the thing"}],
             "origin": {"kind": "user"}, "promptId": "p1", "turnId": 0, "time": 1792657015271},
            {"type": "context.append_message", "agentId": "main", "time": 1792657015272,
             "message": {"role": "user", "content": [{"type": "text", "text": "do the thing"}], "origin": {"kind": "user"}}},
            {"type": "agent.message.appended", "time": 1792657015273, "message": {"message": {"role": "user"}}},
            {"type": "context.append_message", "agentId": "main", "time": 1792657015274,
             "message": {"role": "user", "content": [{"type": "text", "text": "hook text"}],
                         "origin": {"kind": "hook_result"}}},
            {"type": "context.append_loop_event", "time": 1792657016000, "event": {
                "type": "content.part", "part": {"type": "think", "think": THINK}}},
            {"type": "context.append_loop_event", "time": 1792657016001, "event": {
                "type": "content.part", "part": {"type": "text", "text": "on it"}}},
            {"type": "context.append_loop_event", "time": 1792657016002, "event": {
                "type": "tool.call", "toolCallId": "tc1", "name": "Read", "args": {"path": "a"}}},
            {"type": "context.append_loop_event", "time": 1792657016003, "event": {
                "type": "tool.result", "toolCallId": "tc1", "result": {"output": "contents", "isError": False}}},
            {"type": "context.append_loop_event", "time": 1792657016004, "event": {
                "type": "tool.result", "toolCallId": "tc2", "result": {"output": "boom", "is_error": True}}},
        ]
        ev = _events(self.wire("main", rows), "kimi")
        humans = [e for e in ev if e["actor"] == "human"]
        self.assertEqual(len(humans), 1)
        self.assertEqual(humans[0]["text"], "do the thing")
        self.assertEqual(humans[0]["time"], "2026-10-22T08:16:55.271Z")
        self.assertEqual(humans[0]["project"], "/work/app")
        inj = [e for e in ev if e["actor"] == "injected"]
        self.assertEqual((len(inj), inj[0]["text"]), (1, ""))
        res = {e["action"]["call_id"]: e["action"] for e in ev if e["action"] and e["action"]["kind"] == "tool_result"}
        self.assertEqual((res["tc1"]["name"], res["tc1"]["success"]), ("Read", True))
        self.assertEqual(res["tc2"]["success"], False)
        self.assertNotIn(THINK, json.dumps(ev))
        s = _summary(ev)
        self.assertEqual(s["duplicates"], 1)
        self.assertEqual(s["ignored"]["type:agent.message.appended"], 1)

    def test_subagent_and_system_trigger_are_agent(self):
        self.sess.mkdir(parents=True)
        (self.sess / "state.json").write_text(json.dumps({"agents": {"agent-1": {"type": "sub"}}}))
        row = {"type": "turn.prompt", "agentId": "agent-1", "input": [{"type": "text", "text": "brief"}],
               "origin": {"kind": "system_trigger", "name": "subagent"}, "promptId": "p9", "time": 1792657015271}
        ev = _events(self.wire("agent-1", [row]), "kimi")
        body = _bodies(ev)[0]
        self.assertEqual(body["actor"], "agent")
        self.assertEqual(body["parent_session"], "session_0001")
        self.assertEqual(body["session"], "session_0001/agent-1")
        trigger = dict(row, origin={"kind": "system_trigger"}, promptId="p10")
        main = _events(self.wire("main", [trigger]), "kimi")
        self.assertEqual(_bodies(main)[0]["actor"], "agent")

    def test_missing_state_json_is_fine(self):
        row = {"type": "turn.prompt", "input": [{"type": "text", "text": "hi"}], "origin": {"kind": "user"}, "time": 5}
        ev = _events(self.wire("main", [row]), "kimi")
        self.assertEqual(_bodies(ev)[0]["actor"], "human")
        self.assertEqual(_bodies(ev)[0]["time"], "1970-01-01T00:00:05.000Z")


class GrokMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def make_db(self, rows):
        path = self.tmp / "session_search.sqlite"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('schema_version', '4')")
        con.execute("CREATE TABLE session_docs (session_id TEXT, cwd TEXT, updated_at TEXT, title TEXT, content TEXT, "
                    "content_hash TEXT)")
        con.executemany("INSERT INTO session_docs VALUES (?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        return path

    def test_empty_db_is_valid(self):
        ev = _events(self.make_db([]), "grok")
        self.assertEqual(len(ev), 1)
        s = _summary(ev)
        self.assertEqual(s["errors"], [])
        self.assertTrue(ev[0]["action"]["success"])

    def test_nonempty_content_is_derived_document_with_diagnostic(self):
        ev = _events(self.make_db([("s1", "/work/app", "2026-03-01T10:00:00Z", "title", "user: hello there", "h1")]), "grok")
        doc = ev[0]
        self.assertEqual((doc["role"], doc["actor"], doc["text"]), ("meta", "unknown", ""))
        self.assertEqual(doc["content_kinds"], ["derived_document"])
        self.assertEqual(doc["session"], "s1")
        self.assertIn("unsupported_content_format", _summary(ev)["errors"])
        self.assertFalse(any(e["actor"] == "human" for e in ev))

    def test_not_a_database(self):
        bad = self.tmp / "session_search.sqlite"
        bad.write_bytes(b"this is not sqlite" * 20)
        ev = _events(bad, "grok")
        self.assertTrue(_summary(ev)["errors"])

    def test_memory_markdown_is_never_human(self):
        md = self.tmp / "rollout_summaries" / "a.md"
        md.parent.mkdir()
        md.write_text("thread_id: %s\nupdated_at: 2026-03-01T10:00:00Z\ncwd: /work/app\n\nThe user asked for %s\n" %
                      (SID, FAKE_KEY), encoding="utf-8")
        ev = _events(md, "memory")
        e = ev[0]
        self.assertEqual((e["role"], e["actor"]), ("meta", "injected"))
        self.assertEqual(e["session"], SID)
        self.assertEqual(e["time"], "2026-03-01T10:00:00.000Z")
        self.assertNotIn(FAKE_KEY, e["text"])
        self.assertFalse(any(x["actor"] == "human" for x in ev))

    def test_memory_without_thread_id_gets_stable_document_session(self):
        a = self.tmp / "a" / "n.md"
        a.parent.mkdir()
        a.write_text("no header here\nupdated by hand\n" + "lorem ipsum " * 25000, encoding="utf-8")
        e = _events(a, "memory")[0]
        self.assertTrue(e["session"].startswith("document:"))
        self.assertEqual(e["session"], _events(a, "memory")[0]["session"])
        self.assertEqual((e["actor"], e["text_chars"] > 250000), ("injected", True))
        self.assertLessEqual(len(e["text"]), 16000)
        self.assertEqual(e["human_signal"], "memory.derivative")


class ShapeTests(unittest.TestCase):
    KEYS = {"id", "source", "machine", "session", "project", "time", "ordinal", "role", "actor", "text", "text_chars",
            "text_truncated", "source_ref", "parent_session", "human_signal", "content_kinds", "action", "evidence_key"}

    def test_every_event_has_contract_keys_and_dense_ordinals(self):
        tmp = helpers.tmpdir()
        self.addCleanup(shutil.rmtree, str(tmp), True)
        path = _write(tmp / ("rollout-x-%s.jsonl" % SID), codex_rows())
        ev = _events(path, "codex", machine="laptop")
        self.assertEqual([e["ordinal"] for e in ev], list(range(len(ev))))
        for e in ev:
            self.assertEqual(set(e), self.KEYS)
            self.assertEqual(e["machine"], "laptop")
            self.assertIn(e["role"], ("user", "assistant", "tool", "meta"))
            self.assertIn(e["actor"], ("human", "agent", "unknown", "injected"))
            self.assertLessEqual(len(e["text"]), 16000)
            if e["action"]:
                self.assertLessEqual(len(e["action"]["summary"]), 2000)
        self.assertEqual(ev[-1]["action"]["kind"], "source_summary")

    def test_iso_normalisation(self):
        self.assertEqual(ti._iso("2026-03-01T10:00:00Z"), "2026-03-01T10:00:00.000Z")
        self.assertEqual(ti._iso("2026-03-01T12:00:00.5+02:00"), "2026-03-01T10:00:00.500Z")
        self.assertEqual(ti._iso(1792657015271), ti._iso(1792657015.271))
        self.assertIsNone(ti._iso("garbage"))
        self.assertIsNone(ti._iso(None))


if __name__ == "__main__":
    unittest.main()
