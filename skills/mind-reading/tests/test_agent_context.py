"""Agent context: extraction (Claude Code and Codex), corpus storage, scrub and backfill, chunk budgets, leak
stripping, lens routing, and the guarantee that it never becomes the user's voice."""
import json
import unittest
from pathlib import Path

import helpers
from mr import agent_context as _ac
from mr import extract_claude, extract_codex, prep as _prep
from mr.corpus import load_corpus, run_extract
from mr.engine import load_schema
from mr.mine import LEGACY_DEFAULT_LENSES, build_units, resolve_lenses, strip_agent_context

LONG_REPLY = ("I traced the failure to the cache layer and rewrote the invalidation so every write clears the key. " * 8).strip()


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def cc_user(k, text, **extra):
    r = {"type": "user", "uuid": "u-%d" % k, "timestamp": "2026-02-01T10:%02d:00.000Z" % k, "userType": "external",
         "entrypoint": "cli", "cwd": "/home/user/Code/app", "message": {"role": "user", "content": text}}
    r.update(extra)
    return r


def cc_assistant(k, content, **extra):
    r = {"type": "assistant", "uuid": "a-%d" % k, "timestamp": "2026-02-01T10:%02d:30.000Z" % k,
         "message": {"role": "assistant", "content": content}}
    r.update(extra)
    return r


def cc_result(k, tool_id, body, is_error=None, **extra):
    item = {"type": "tool_result", "tool_use_id": tool_id, "content": body}
    if is_error is not None:
        item["is_error"] = is_error
    return cc_user(k, [item], **extra)


def claude_transcript(root):
    rows = [
        cc_user(1, "fix the cache bug"),
        cc_assistant(1, [{"type": "text", "text": LONG_REPLY}, {"type": "tool_use", "id": "t1", "name": "Bash",
                                                                 "input": {"command": "git reset --hard HEAD~1"}}]),
        cc_result(2, "t1", "Exit code 1\nfatal: ambiguous argument", is_error=True),
        cc_user(3, "why did u reset my branch"),                       # head + command_failed + reverted
        cc_assistant(3, [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "a.py"}}]),
        cc_result(4, "t2", "The user doesn't want to proceed with this tool use. The tool use was rejected", is_error=True),
        cc_user(5, "no, put it back first"),                           # interrupted (rejected tool), no head
        cc_assistant(5, [{"type": "text", "text": "Restored the branch."}]),
        cc_assistant(6, [{"type": "text", "text": "sidechain text never counts"}], isSidechain=True),
        cc_user(6, "[Request interrupted by user]"),
        cc_user(7, "ok now just the cache file"),                     # head (non-sidechain) + interrupted
        cc_assistant(7, [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "npm test"}}]),
        cc_result(8, "t3", "Exit code 1\n1 failing", is_error=True),
        cc_assistant(8, [{"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "npm test"}}]),
        cc_result(9, "t4", "all green", is_error=False),
        cc_user(10, "nice"),                                           # the last command passed: no marker
    ]
    _write(Path(root) / "-home-user-Code-app" / "s1.jsonl", rows)


def codex_transcript(root):
    meta = {"timestamp": "2026-02-02T09:00:00.000Z", "type": "session_meta",
            "payload": {"id": "x1", "cwd": "/home/user/Code/app", "originator": "Codex Desktop", "source": "vscode", "thread_source": "user"}}

    def user(m, text):
        return {"timestamp": "2026-02-02T09:%02d:00.000Z" % m, "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}], "id": "msg-%d" % m}}

    def item(m, payload, kind="response_item"):
        return {"timestamp": "2026-02-02T09:%02d:30.000Z" % m, "type": kind, "payload": payload}

    rows = [meta, user(1, "run the migration"),
            item(1, {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": json.dumps({"command": ["bash", "-lc", "make migrate"]})}),
            item(1, {"type": "function_call_output", "call_id": "c1", "output": "Exit code: 2\nWall time: 1s\nerror"}),
            item(1, {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The migration failed on a lock."}]}),
            user(2, "it broke again?"),                                   # head + command_failed
            item(2, {"type": "function_call", "name": "apply_patch", "call_id": "c2", "arguments": "{}"}),
            item(2, {"type": "function_call_output", "call_id": "c2", "output": "apply_patch verification failed: no match"}),
            user(3, "<codex_internal_context source=\"goal\">\nContinue.\n</codex_internal_context>"),  # goal: takes nothing
            user(4, "try a smaller patch"),                                # tool_error kept for the real message
            item(4, {"type": "function_call", "name": "shell", "call_id": "c3", "arguments": json.dumps({"command": "git checkout -- db.py"})}),
            item(4, {"type": "function_call_output", "call_id": "c3", "output": json.dumps({"output": "", "metadata": {"exit_code": 0}})}),
            item(4, {"type": "turn_aborted"}, kind="event_msg"),
            user(5, "stop, that was my change"),                           # interrupted + reverted, exit 0 is no failure
            # the newer exec tool: a script of exec_command calls, output as content items, the last exit_code counts
            item(5, {"type": "custom_tool_call", "name": "exec", "call_id": "c4", "input": "text(await tools.exec_command({cmd: 'make test'}))"}),
            item(5, {"type": "custom_tool_call_output", "call_id": "c4", "output": [
                {"type": "input_text", "text": "Script completed\nWall time 2 seconds\nOutput:\n{\"exit_code\": 0}\n{\"exit_code\": 1}"}]}),
            user(6, "the tests still fail")]                               # command_failed
    _write(Path(root) / "2026" / "02" / "02" / "rollout-2026-02-02T09-00-00-x1.jsonl", rows)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        claude_transcript(self.tmp / "claude")
        codex_transcript(self.tmp / "codex")

    def by_text(self, records):
        return {r["text"]: r["agent_context"] for r in records}

    def test_claude_turn_markers_and_head(self):
        got = self.by_text(extract_claude.iter_records(self.tmp / "claude", context_chars=300))
        self.assertIsNone(got["fix the cache bug"])
        why = got["why did u reset my branch"]
        self.assertEqual(why["markers"], ["command_failed", "reverted"])
        self.assertTrue(why["head"].startswith("I traced the failure"))
        self.assertLessEqual(len(why["head"]), 304)
        self.assertTrue(why["head"].endswith(" ..."))  # cut at a word boundary, marked
        self.assertEqual(got["no, put it back first"], {"head": "", "markers": ["interrupted"]})
        self.assertIsNone(got["[Request interrupted by user]"])  # the harness record itself takes nothing
        nxt = got["ok now just the cache file"]
        self.assertEqual(nxt, {"head": "Restored the branch.", "markers": ["interrupted"]})  # sidechain text ignored
        self.assertIsNone(got["nice"])  # the last command of that turn passed

    def test_codex_turn_markers_and_goal_reminder(self):
        got = self.by_text(extract_codex.iter_records(self.tmp / "codex", context_chars=300))
        self.assertIsNone(got["run the migration"])
        self.assertEqual(got["it broke again?"], {"head": "The migration failed on a lock.", "markers": ["command_failed"]})
        goal = next(v for k, v in got.items() if "goal reminder" in k)
        self.assertIsNone(goal)  # the harness injected it: the context waits for the user's next message
        self.assertEqual(got["try a smaller patch"], {"head": "", "markers": ["tool_error"]})
        self.assertEqual(got["stop, that was my change"], {"head": "", "markers": ["interrupted", "reverted"]})
        self.assertEqual(got["the tests still fail"], {"head": "", "markers": ["command_failed"]})

    def test_zero_chars_turns_it_off(self):
        for rec in list(extract_claude.iter_records(self.tmp / "claude", context_chars=0)) + \
                list(extract_codex.iter_records(self.tmp / "codex", context_chars=0)):
            self.assertIsNone(rec["agent_context"])

    def test_exit_code_and_output_parsing(self):
        self.assertEqual(_ac.exit_code("Exit code: 3\nWall time"), 3)
        self.assertEqual(_ac.exit_code("Process exited with code 0"), 0)
        self.assertEqual(_ac.exit_code(json.dumps({"output": "x", "metadata": {"exit_code": 1}})), 1)
        self.assertIsNone(_ac.exit_code("plain output"))
        self.assertEqual(_ac.output_failed("error: no such file"), (True, False))
        self.assertEqual(_ac.output_failed("all good"), (False, False))
        self.assertEqual(_ac.output_failed([{"type": "input_text", "text": "Script failed\nWall time 1 seconds"}]), (True, False))
        self.assertEqual(_ac.exit_code('{"exit_code": 0} then {"exit_code": 2}'), 2)  # the command the turn ended on
        self.assertTrue(_ac.REVERT_RE.search("git revert abc123"))
        self.assertFalse(_ac.REVERT_RE.search("git reset --soft HEAD~1"))

    def test_exit_code_comes_from_the_header_not_the_output(self):
        # text the command printed never overrides the tool's own header
        self.assertEqual(_ac.exit_code("Exit code: 0\nWall time: 3s\nOutput:\nnpm ERR! process exited with code 1"), 0)
        self.assertEqual(_ac.output_failed("Exit code: 0\nWall time: 3s\nOutput:\nnpm ERR! process exited with code 1"), (False, True))
        self.assertEqual(_ac.exit_code("Exit code: 1\nWall time: 2s\nOutput:\nsubtask: exit code 0"), 1)
        self.assertEqual(_ac.output_failed("Exit code: 1\nWall time: 2s\nOutput:\nsubtask: exit code 0"), (True, True))
        self.assertEqual(_ac.exit_code("Chunk ID: a1\nWall time: 0.2\nProcess exited with code 2\nOutput:\nexit code 0"), 2)
        self.assertEqual(_ac.exit_code("Exit code 1\n2 failing"), 1)  # Claude Code's Bash error
        self.assertIsNone(_ac.exit_code("build log\nprocess exited with code 1"))  # no header: unknown, not a failure
        self.assertEqual(_ac.exit_code("Script completed\nOutput:\n{\"exit_code\": 0}\n{\"exit_code\": 1}"), 1)

    def test_unstaging_is_not_a_revert(self):
        self.assertTrue(_ac.REVERT_RE.search("git restore src/a.py"))
        self.assertFalse(_ac.REVERT_RE.search("git restore --staged src/a.py"))
        self.assertFalse(_ac.REVERT_RE.search("git restore -S src/a.py"))
        self.assertTrue(_ac.REVERT_RE.search("git restore --staged --worktree src/a.py"))
        self.assertTrue(_ac.REVERT_RE.search("git restore --staged a.py; git restore b.py"))

    def test_codex_revert_check_reads_shell_commands_only(self):
        meta = {"timestamp": "2026-02-03T09:00:00.000Z", "type": "session_meta",
                "payload": {"id": "x2", "cwd": "/home/user/Code/app", "originator": "Codex Desktop", "source": "vscode", "thread_source": "user"}}

        def user(m, text):
            return {"timestamp": "2026-02-03T09:%02d:00.000Z" % m, "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}], "id": "m-%d" % m}}

        def item(m, payload):
            return {"timestamp": "2026-02-03T09:%02d:30.000Z" % m, "type": "response_item", "payload": payload}

        rows = [meta, user(1, "document the undo steps"),
                item(1, {"type": "custom_tool_call", "name": "apply_patch", "call_id": "p1",
                         "input": "*** Begin Patch\n+To undo a change, run git restore <file>.\n*** End Patch"}),
                item(1, {"type": "custom_tool_call_output", "call_id": "p1", "output": "Success. Updated docs/undo.md"}),
                user(2, "good, now unstage the lockfile"),
                item(2, {"type": "function_call", "name": "shell", "call_id": "s1",
                         "arguments": json.dumps({"command": ["bash", "-lc", "git restore --staged package-lock.json"]})}),
                item(2, {"type": "function_call_output", "call_id": "s1", "output": json.dumps({"output": "", "metadata": {"exit_code": 0}})}),
                user(3, "thanks, commit the rest")]
        _write(self.tmp / "codex2" / "2026" / "02" / "03" / "rollout-2026-02-03T09-00-00-x2.jsonl", rows)
        got = self.by_text(extract_codex.iter_records(self.tmp / "codex2", context_chars=300))
        self.assertIsNone(got["good, now unstage the lockfile"])  # a patch that mentions git restore is not a revert
        self.assertIsNone(got["thanks, commit the rest"])  # unstaging is not a revert


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.tmpdir()
        claude_transcript(self.tmp / "claude")
        codex_transcript(self.tmp / "codex")

    def test_stored_labelled_scrubbed_and_apart_from_text(self):
        root = self.tmp / "claude" / "-home-user-Code-app"
        rows = [cc_user(1, "deploy it"),
                cc_assistant(1, [{"type": "text", "text": "Sent the summary to Sam Taylor at sam@example.com from /home/user/Code/app."}]),
                cc_user(2, "thanks")]
        _write(root / "s2.jsonl", rows)
        paths, cfg = helpers.init_home(self.tmp / "home")
        run_extract(paths, cfg, claude_dir=self.tmp / "claude", codex_dir=self.tmp / "codex", log=helpers.quiet)
        recs = {r["text"]: r for r in load_corpus(paths)}
        ctx = recs["thanks"]["agent_context"]
        self.assertEqual(ctx["origin"], "agent_context")
        self.assertNotIn("Sam Taylor", ctx["head"])
        self.assertIn("<teammate>", ctx["head"])
        self.assertNotIn("sam@example.com", ctx["head"])
        self.assertNotIn("/home/user", ctx["head"])
        self.assertNotIn("Sent the summary", recs["thanks"]["text"])  # never the user's text
        self.assertTrue(all("agent_context" in r for r in recs.values()))

    def test_backfill_once_keys_unchanged(self):
        paths, cfg = helpers.init_home(self.tmp / "home", cfg_overrides={"extract": {"agent_context_chars": 0}})
        run_extract(paths, cfg, claude_dir=self.tmp / "claude", codex_dir=self.tmp / "codex", log=helpers.quiet)
        before = load_corpus(paths)
        self.assertTrue(all("agent_context" not in r for r in before))
        cfg["extract"]["agent_context_chars"] = 400
        s = run_extract(paths, cfg, claude_dir=self.tmp / "claude", codex_dir=self.tmp / "codex", log=helpers.quiet)
        after = load_corpus(paths)
        self.assertEqual([r["key"] for r in after], [r["key"] for r in before])
        self.assertEqual([r["text"] for r in after], [r["text"] for r in before])
        self.assertEqual(s["agent_context_backfilled"], len(before))
        self.assertEqual(sum(s["added"].values()), 0)
        self.assertTrue(any(r["agent_context"] for r in after))
        s2 = run_extract(paths, cfg, claude_dir=self.tmp / "claude", codex_dir=self.tmp / "codex", log=helpers.quiet)
        self.assertEqual(s2["agent_context_backfilled"], 0)


class ChunkTests(unittest.TestCase):
    P = {"chunk_agent_context_chars": 300, "chunk_agent_context_ratio": 0.5, "chunk_agent_context_min_chars": 120}

    def test_budget_keeps_markers_and_reactions_first(self):
        msgs = [{"text": "x" * 100}, {"text": "why did that break again"}, {"text": "y" * 100}]
        ctxs = [{"head": "a" * 300}, {"head": "b" * 300, "markers": ["command_failed"]}, {"head": "c" * 300}]
        stats = {}
        out = _prep.fit_context([dict(m) for m in msgs], ctxs, self.P, stats)
        heads = [len((m.get("agent_context") or {}).get("head", "")) for m in out]
        user_chars = sum(len(m["text"]) for m in msgs)
        self.assertLessEqual(sum(heads), max(int(user_chars * 0.5), 124))  # the ratio, or one shortened head at least
        self.assertEqual(heads, [0, 124, 0])  # the head beside a failure survives, shortened; the others go
        self.assertEqual(out[1]["agent_context"]["markers"], ["command_failed"])
        self.assertGreaterEqual(stats.get("heads_dropped", 0), 1)

    def test_markers_always_kept_heads_dropped_past_the_floor(self):
        msgs = [{"text": ""}, {"text": ""}]
        out = _prep.fit_context(msgs, [{"head": "z" * 200, "markers": ["reverted"]}, {"head": "q" * 200, "markers": ["tool_error"]}], self.P)
        self.assertEqual(out[0]["agent_context"], {"head": "z" * 120 + " ...", "markers": ["reverted"]})
        self.assertEqual(out[1]["agent_context"], {"markers": ["tool_error"]})

    def test_chunk_boundaries_ignore_agent_context(self):
        p = dict(helpers.load_config(helpers.Paths(helpers.tmpdir()))["prep"], chunk_chars=1500, part_chars=1200)
        msgs = [{"time": "2026-01-01T00:%02d:00Z" % k, "origin": "typed", "text": "message number %d " % k * 4,
                 "time_reliable": True, "agent_context": {"head": "agent reply %d " % k * 30, "markers": []}} for k in range(40)]
        sess = {"source": "claude-code", "session": "s", "project": None, "start_iso": msgs[0]["time"], "time_reliable": True, "messages": msgs}
        bare = dict(sess, messages=[{k: v for k, v in m.items() if k != "agent_context"} for m in msgs])
        with_ctx = _prep.build_chunks([sess], p)
        without = _prep.build_chunks([bare], p)
        self.assertEqual([[len(b["messages"]) for b in c] for c in with_ctx], [[len(b["messages"]) for b in c] for c in without])
        self.assertTrue(any("agent_context" in m for c in with_ctx for b in c for m in b["messages"]))

    def test_context_quoting_a_held_out_message_is_stripped(self):
        held = "please rotate the staging credentials before friday and tell nobody"
        train = [{"messages": [{"agent_context": {"head": "Sure. You said: please rotate the staging credentials before friday and tell nobody.",
                                                  "markers": []}},
                               {"agent_context": {"head": "Unrelated reply about the build", "markers": []}}]}]
        self.assertEqual(_prep.strip_context_leaks(train, {held}), 1)
        self.assertIsNone(train[0]["messages"][0]["agent_context"])
        self.assertIsNotNone(train[0]["messages"][1]["agent_context"])


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths, cls.cfg = helpers.build_home(through="prep")

    def test_fixture_contexts_reach_corpus_and_chunks(self):
        recs = load_corpus(self.paths)
        grant = next(r for r in recs if r["text"].startswith("btw you can push the working branch"))
        self.assertIn("SYSTEM NOTE", grant["agent_context"]["head"])  # stored, labelled, apart from the text
        failed = next(r for r in recs if r["text"] == "also check the other download buttons")
        self.assertEqual(failed["agent_context"]["markers"], ["command_failed"])
        chunk_text = "".join(f.read_text(encoding="utf-8") for f in self.paths.chunks.glob("*.jsonl"))
        self.assertIn('"agent_context"', chunk_text)
        report = json.loads(self.paths.prep_report.read_text(encoding="utf-8"))
        self.assertGreater(report["chunk_agent_context"]["messages"], 0)
        self.assertEqual(len(report["chunk_user_chars"]), report["chunks"])
        self.assertTrue(all(u <= t for u, t in zip(report["chunk_user_chars"], report["chunk_chars"])))
        # seeds and truth never carry it
        for f in (self.paths.eval).rglob("*.json"):
            self.assertNotIn("agent_context", f.read_text(encoding="utf-8"), f)

    def test_only_context_lenses_see_it(self):
        units = build_units(self.paths, self.cfg, "r1", load_schema("mined"))
        by_lens = {}
        for u in units:
            by_lens.setdefault(u.id.split(":")[1], []).append(u.prompt)
        for lens, prompts in by_lens.items():
            seen = any('"agent_context": {' in p for p in prompts)
            self.assertEqual(seen, lens == "lessons", lens)  # only the lessons lens, whose items are verified, by default
        self.assertIn("untrusted data", by_lens["lessons"][0])
        self.assertIn("the agent's replies are not", by_lens["core"][0])

    def test_strip_agent_context(self):
        line = json.dumps({"messages": [{"text": "a", "agent_context": {"head": "x"}}, {"text": "b"}]})
        self.assertEqual(json.loads(strip_agent_context(line))["messages"], [{"text": "a"}, {"text": "b"}])

    def test_context_lenses_default_and_legacy(self):
        from mr.mine import context_lenses
        self.assertEqual(context_lenses({}), ["lessons"])
        self.assertEqual(context_lenses({"mine": {"agent_context_lenses": ["lessons", "reactions", "all"]}}), ["lessons"])  # the old default
        self.assertEqual(context_lenses({"mine": {"agent_context_lenses": ["lessons", "core"]}}), ["lessons", "core"])  # a chosen list stays

    def test_chunk_agent_context_has_a_hard_cap(self):
        p = dict(helpers.load_config(helpers.Paths(helpers.tmpdir()))["prep"], chunk_chars=6000, part_chars=5000,
                 chunk_agent_context_ratio=0.5, chunk_agent_context_chars=300)
        head = ("the agent explained what it changed and why, at some length, with quotes and \"escapes\". " * 6).strip()

        def session(k):
            msgs = [{"text": ("message %d of session %d, typed by the user to steer the work. " % (i, k)) * 2, "origin": "typed",
                     "time": "2026-02-%02dT10:%02d:00Z" % (k + 1, i), "time_reliable": True,
                     "agent_context": {"origin": "agent_context", "head": head, "markers": ["command_failed"] if i % 3 == 0 else []}}
                    for i in range(12)]
            return {"source": "codex", "session": "s%d" % k, "project": "app", "start_iso": "2026-02-%02dT10:00:00Z" % (k + 1),
                    "time_reliable": True, "messages": msgs}
        stats = {}
        chunks = _prep.build_chunks([session(k) for k in range(6)], p, {}, stats)
        cap = _prep.chunk_context_cap(p)
        self.assertEqual(cap, 3000)
        for c in chunks:
            self.assertLessEqual(sum(_prep.context_chars(b) for b in c), cap)
            self.assertLessEqual(sum(len(json.dumps(_prep.without_context(b), ensure_ascii=False)) + 1 for b in c), p["chunk_chars"])
        tight = dict(p, chunk_agent_context_max_chars=400)
        chunks = _prep.build_chunks([session(0)], tight, {}, stats)
        self.assertTrue(all(sum(_prep.context_chars(b) for b in c) <= 400 for c in chunks))
        self.assertGreater(stats.get("heads_dropped_chunk_cap", 0), 0)
        kept = [m for c in chunks for b in c for m in b["messages"] if m.get("agent_context")]
        self.assertTrue(all("markers" in m["agent_context"] for m in kept))  # markers outlast heads

    def test_legacy_default_lens_list_gains_lessons(self):
        self.assertIn("lessons", resolve_lenses({"mine": {"lenses": list(LEGACY_DEFAULT_LENSES)}}))
        self.assertEqual(resolve_lenses({"mine": {"lenses": ["core", "grants"]}}), ["core", "grants"])  # a chosen list stays


if __name__ == "__main__":
    unittest.main()
