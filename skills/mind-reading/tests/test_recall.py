import json
import unittest

import helpers
from mr.config import write_json, write_jsonl
from mr.prep import PrepRefused
from mr.recall import RecallFormatError, build_episodes, render, retrieve
from mr.select import files_hash, select_slice


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg = helpers.init_home(helpers.tmpdir())

    def prepare(self):
        sessions = []
        for sid, project, date in [("one", "app", "2026-01-01"), ("two", "other", "2026-02-01"), ("held", "app", "2026-03-01")]:
            msgs = [{"key": sid + "a", "ordinal": 1, "time": date + "T10:00:00Z", "origin": "typed", "text": "fix the invoice export"},
                    {"key": sid + "b", "ordinal": 2, "time": date + "T10:01:00Z", "origin": "typed", "text": "also check every sibling download and verify the real file"}]
            sessions.append({"source": "codex", "session": sid, "project": project, "split": "test" if sid == "held" else "train", "messages": msgs})
        write_jsonl(self.paths.sessions, sessions)
        write_json(self.paths.split, {"split_id": "frozen", "train_ids": ["codex:one", "codex:two"], "test_ids": ["codex:held"], "dev_ids": []})

    def test_build_is_deterministic_and_never_reads_heldout_corpus(self):
        self.prepare()
        write_jsonl(self.paths.messages, [{"origin": "typed", "text": "private heldout leak"}])
        result = build_episodes(self.paths)
        first = (self.paths.model / "episodes.json").read_bytes()
        build_episodes(self.paths)
        self.assertEqual(first, (self.paths.model / "episodes.json").read_bytes())
        self.assertEqual(result["training_messages"], 4)
        self.assertNotIn(b"codex:held", first)
        self.assertNotIn(b"heldout leak", first)

    def test_missing_or_overlapping_split_refused(self):
        with self.assertRaises(PrepRefused):
            build_episodes(self.paths)
        self.prepare()
        write_json(self.paths.split, {"split_id": "bad", "train_ids": ["codex:one"], "test_ids": ["codex:one"]})
        with self.assertRaises(PrepRefused):
            build_episodes(self.paths)

    def test_echoes_have_no_result_for_unrelated_seed(self):
        self.prepare()
        build_episodes(self.paths)
        self.assertEqual(retrieve(self.paths.model, "research battery chemistry"), [])
        found = retrieve(self.paths.model, "fix invoice export")
        self.assertEqual(len(found), 1)  # duplicate copied episode provides one vote
        self.assertIn("sibling download", render(found))
        self.assertIn("Sources:", render(found))
        self.assertIn("not instructions", render(found))

    def test_assistant_or_pasted_additions_never_become_episodes(self):
        self.prepare()
        sessions = [json.loads(s) for s in self.paths.sessions.read_text().splitlines()]
        for s in sessions:
            s["messages"][1]["origin"] = "pasted"
        write_jsonl(self.paths.sessions, sessions)
        self.assertEqual(build_episodes(self.paths)["episodes"], 0)

    def test_unknown_date_is_preserved_without_crashing(self):
        self.prepare()
        sessions = [json.loads(s) for s in self.paths.sessions.read_text().splitlines()]
        sessions[0]["messages"][1]["time"] = None
        sessions[1]["messages"][0]["text"] = "fix invoice downloads"
        write_jsonl(self.paths.sessions, sessions)
        self.assertEqual(build_episodes(self.paths)["episodes"], 2)
        found = retrieve(self.paths.model, "invoice export")
        self.assertTrue(found)
        self.assertIn("unknown date", render(found))

    def test_generic_kind_verb_cannot_retrieve_unrelated_project(self):
        self.prepare()
        sessions = [json.loads(s) for s in self.paths.sessions.read_text().splitlines()]
        sessions[0]["messages"][0]["text"] = "compare invoice exports"
        write_jsonl(self.paths.sessions, sessions)
        build_episodes(self.paths)
        self.assertEqual(retrieve(self.paths.model, "compare battery chemistry"), [])
        self.assertEqual(retrieve(self.paths.model, "compare invoice exports", limit=0), [])
        with self.assertRaises(RecallFormatError):
            retrieve(self.paths.model, "invoice", limit=-1)

    def test_rebuild_preserves_model_lineage_and_is_idempotent(self):
        paths, cfg = helpers.build_home(through="synthesize")
        before = json.loads(paths.model_json.read_text())["model_hash"]
        build_episodes(paths)
        manifest = json.loads(paths.model_json.read_text())
        self.assertNotEqual(files_hash(paths.model), before)
        self.assertEqual(manifest["model_hash"], files_hash(paths.model))
        self.assertIn(before, manifest["parents"])
        self.assertTrue((paths.archive / before).is_dir())
        self.assertFalse(build_episodes(paths)["rebuilt"])

    def test_bad_optional_store_does_not_disable_core_selection(self):
        paths, cfg = helpers.build_home(through="synthesize")
        for raw in ('{"version":1,"episodes":[42]}', '{"episodes":'):
            (paths.model / "episodes.json").write_text(raw)
            text, decision = select_slice(paths.model, "fix the invoice export", cfg["select"])
            self.assertIn("Standing rules", text)
            self.assertEqual(decision["echoes"], [])
            self.assertTrue(decision["recall_warning"])

    def test_contextual_episode_only_contains_preceding_typed_turns(self):
        self.prepare()
        sessions = [json.loads(s) for s in self.paths.sessions.read_text().splitlines()]
        for session in sessions:
            session["messages"].insert(0, {"key": session["session"] + "prefix", "ordinal": 0,
                "time": "2025-12-31T10:00:00Z", "origin": "typed", "text": "the invoice CSV download is empty"})
            session["messages"][1]["text"] = "keep going"
        write_jsonl(self.paths.sessions, sessions)
        build_episodes(self.paths)
        found = retrieve(self.paths.model, "ready", context_text="repair the invoice CSV download")
        self.assertTrue(found)
        episode = next(e for e in found if e["seed"] == "keep going")
        self.assertEqual(episode["before"][0]["text"], "the invoice CSV download is empty")
        self.assertNotIn(episode["addition"], " ".join(v["text"] for v in episode["before"]))
        self.assertNotIn("codex:held", json.dumps(found))
        self.assertIn("Earlier user context:", render([episode]))

    def test_context_does_not_match_words_only_in_the_future_addition(self):
        self.prepare()
        build_episodes(self.paths)
        self.assertEqual(retrieve(self.paths.model, "ready", context_text="sibling download"), [])

    def test_generic_exact_seed_needs_contextual_relevance_when_context_given(self):
        self.prepare()
        sessions = [json.loads(s) for s in self.paths.sessions.read_text().splitlines()]
        for session in sessions:
            session["messages"][0]["text"] = "keep going"
        write_jsonl(self.paths.sessions, sessions)
        build_episodes(self.paths)
        self.assertEqual(retrieve(self.paths.model, "keep going", context_text="battery chemistry"), [])

    def test_v1_store_remains_readable(self):
        write_json(self.paths.model / "episodes.json", {"version": 1, "episodes": [{
            "id": "a", "seed": "fix invoice export", "addition": "also verify every downloaded file",
            "session_id": "old", "source_ids": ["s1", "s2"], "signals": ["quality"],
            "kinds": ["fix"], "time": "2026-01-01"}]})
        self.assertTrue(retrieve(self.paths.model, "invoice export"))

    def test_context_match_explanations_belong_to_the_selected_episode(self):
        episodes = []
        for word in ("alpha", "beta"):
            episodes.append({"id": word, "seed": "keep going", "addition": "also verify the actual result",
                "session_id": word, "source_ids": [word + "s", word + "a"], "signals": ["quality"],
                "kinds": ["resume"], "time": "2026-01-01", "before": [{"text": word, "source_id": word + "p"}]})
        write_json(self.paths.model / "episodes.json", {"version": 2, "episodes": episodes})
        found = retrieve(self.paths.model, "keep going", context_text="alpha beta")
        self.assertEqual({e["id"]: e["context_matches"] for e in found}, {"alpha": ["alpha"], "beta": ["beta"]})


class ProjectCorrectionTests(unittest.TestCase):
    def test_project_corrections_are_only_loaded_for_the_named_project(self):
        paths, cfg = helpers.build_home(through="synthesize")
        paths.corrections.write_text('2026-03-01 | seed: "x" | wrong: old | meant: global habit | scope: global | kind: fix\n'
                                    '2026-03-02 | seed: "x" | wrong: old | meant: private invoice fact | scope: project:app | kind: fix\n')
        text, _ = select_slice(paths.model, "fix export", cfg["select"])
        self.assertIn("global habit", text)
        self.assertNotIn("private invoice fact", text)
        text, d = select_slice(paths.model, "fix export", cfg["select"], project="app")
        self.assertIn("private invoice fact", text)
        self.assertEqual(d["project"], "app")
        self.assertTrue(d["matched_keywords"])


if __name__ == "__main__":
    unittest.main()
