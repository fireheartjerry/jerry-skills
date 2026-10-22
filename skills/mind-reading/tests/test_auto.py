import datetime as dt
import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

import helpers
import synth_transcripts as T
from mr import auto, incremental
from mr.config import read_json, write_json
from mr.corpus import run_extract
from mr.mine import latest_mined_run, pending_units, run_mine
from mr.prep import chunk_files, run_prep
from mr.synthesize import run_synthesize


def built(no_test=False, **cfg_overrides):
    """A home built through synthesize from copied fixture transcripts, so a test can add sessions."""
    tmp = helpers.tmpdir()
    claude, codex = helpers.copy_fixture_sources(tmp / "src")
    over = {"sources": {"claude_projects_dir": str(claude), "codex_sessions_dir": str(codex)}}
    over.update(cfg_overrides)
    paths, cfg = helpers.init_home(tmp / "home", cfg_overrides=over)
    run_extract(paths, cfg, log=helpers.quiet)
    if no_test:
        run_prep(paths, cfg, no_test=True, train_before="2026-02-01T00:00:00Z", log=helpers.quiet)
    else:
        run_prep(paths, cfg, log=helpers.quiet)
    run_mine(paths, cfg, "fake", fake_dir=helpers.FAKE, log=helpers.quiet)
    run_synthesize(paths, cfg, "fake", fake_dir=helpers.FAKE, log=helpers.quiet)
    return paths, cfg, claude, codex, tmp


def fake_engine(tmp, light_extra=None):
    """The fixture fake engine, with a line added to the light.md the synthesize stage returns (a new model hash)."""
    root = Path(tmp) / "fake-root"
    shutil.copytree(helpers.FIX / "fake-engine", root / "fake-engine")
    shutil.copytree(helpers.FIX / "model", root / "model")
    shutil.copytree(helpers.FIX / "model-refreshed", root / "model-refreshed")
    shutil.copytree(helpers.FIX / "mined", root / "mined")
    if light_extra:
        lp = root / "model" / "light.md"
        lp.write_text(lp.read_text(encoding="utf-8").replace("## Quality bar\n", "## Quality bar\n" + light_extra + "\n", 1),
                      encoding="utf-8")
    return root / "fake-engine"


def add_session(claude, sid, start="2026-04-20T10:00", n=3, words=40):
    """A new session; n=8 with words=600 is about 30k characters of new material, above auto.min_new_chars."""
    lines = [T.claude_user("%s-%d" % (sid, k), "%s:%02dZ" % (start, k), ("rework the billing export step %d " % k) + "detail " * words, session=sid)
             for k in range(n)]
    return T.write_claude(claude, "-home-user-Code-webshop", sid, lines)


def hashes(paths):
    return {f.stem: hashlib.sha256(f.read_bytes()).hexdigest() for f in chunk_files(paths)}


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg, self.claude, self.codex, self.tmp = built()

    def pin(self, **kw):
        return run_prep(self.paths, self.cfg, keep_split=True, pin=True, log=helpers.quiet, **kw)["report"]["incremental"]

    def test_bootstrap_keeps_bytes_and_new_sessions_get_new_chunks_only(self):
        before = hashes(self.paths)
        inc = self.pin()
        self.assertEqual((inc["bootstrapped"], inc["bootstrap_unmatched"], inc["changed_chunks"]), (True, 0, []))
        after = hashes(self.paths)
        self.assertEqual({k: after[k] for k in before}, before)
        pins = read_json(self.paths.chunk_pins)
        self.assertEqual(len(pins["chunks"]), len(after))
        add_session(self.claude, "cs95")
        run_extract(self.paths, self.cfg, log=helpers.quiet)
        inc2 = self.pin()
        self.assertEqual(inc2["changed_chunks"], [])
        self.assertEqual(len(inc2["new_chunks"]), 1)
        final = hashes(self.paths)
        self.assertEqual({k: final[k] for k in after}, after)
        new_blocks = [json.loads(l) for l in (self.paths.chunks / (inc2["new_chunks"][0] + ".jsonl")).read_text().splitlines()]
        self.assertEqual([b["session"] for b in new_blocks], ["cs95"])
        self.assertEqual(self.pin()["new_chunks"], [])  # stable once pinned

    def test_continuation_of_a_pinned_session(self):
        self.pin()
        sess = [json.loads(l) for l in self.paths.sessions.read_text().splitlines()]
        target = next(s for s in sess if s["split"] == "train" and s["source"] == "claude-code" and len(s["messages"]) >= 3)
        path = next(self.claude.glob("*/%s.jsonl" % target["session"]))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(T.claude_user("cont-1", "2026-04-25T10:00:00Z", "and now split the invoice export into two steps please",
                                   session=target["session"]) + "\n")
        run_extract(self.paths, self.cfg, log=helpers.quiet)
        before = hashes(self.paths)
        inc = self.pin()
        self.assertEqual(inc["changed_chunks"], [])
        blocks = [json.loads(l) for l in (self.paths.chunks / (inc["new_chunks"][0] + ".jsonl")).read_text().splitlines()]
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        k, n = map(int, b["part"].split("/"))
        self.assertGreater(k, 1)
        self.assertEqual(k, n)
        self.assertTrue(b["first_messages"])
        self.assertEqual([m["text"] for m in b["messages"]], ["and now split the invoice export into two steps please"])
        self.assertEqual({c: h for c, h in hashes(self.paths).items() if c in before}, before)

    def test_small_material_waits_and_full_prep_discards_pins(self):
        inc = self.pin(min_new_chars=10 ** 6)
        self.assertEqual(inc["new_chunks"], [])
        self.assertGreater(inc["deferred_chars"], 0)
        self.assertTrue(self.paths.chunk_pins.exists())
        run_prep(self.paths, self.cfg, keep_split=True, log=helpers.quiet)
        self.assertFalse(self.paths.chunk_pins.exists())

    def test_pending_units(self):
        lenses = 7
        self.assertEqual(pending_units(self.paths, self.cfg, latest_mined_run(self.paths)), [])
        self.assertEqual(len(pending_units(self.paths, self.cfg, None)), len(chunk_files(self.paths)) * lenses)
        add_session(self.claude, "cs95")
        run_extract(self.paths, self.cfg, log=helpers.quiet)
        inc = self.pin(min_new_chars=0)
        todo = pending_units(self.paths, self.cfg, latest_mined_run(self.paths))
        self.assertEqual(sorted({c for c, _ in todo}), sorted(inc["new_chunks"]))
        self.assertEqual(len(todo), len(inc["new_chunks"]) * lenses)

    def test_match_block_needs_the_exact_run_of_messages(self):
        sess = {"messages": [{"key": "k1", "time": "t1", "origin": "typed", "text": "one"},
                             {"key": "k2", "time": "t2", "origin": "typed", "text": "two"}]}
        p = self.cfg["prep"]
        self.assertEqual(incremental.match_block(sess, {"messages": [{"time": "t2", "origin": "typed", "text": "two"}]}, p), ["k2"])
        self.assertIsNone(incremental.match_block(sess, {"messages": [{"time": "t2", "origin": "typed", "text": "changed"}]}, p))


class AutoRunTests(unittest.TestCase):
    def setUp(self):
        self.paths, self.cfg, self.claude, self.codex, self.tmp = built()
        self.fake = fake_engine(self.tmp, light_extra="- Verify on the real path before reporting.")

    def run_auto(self, **kw):
        kw.setdefault("fake_dir", self.fake)
        return auto.run_auto(self.paths, self.cfg, kind="fake", log=helpers.quiet, **kw)

    def test_small_new_material_waits_and_spends_nothing(self):
        runs_before = sorted(p.name for p in self.paths.mined.iterdir())
        rec = self.run_auto()
        self.assertEqual(rec["outcome"], "deferred")
        self.assertEqual(rec["budget"]["calls"], 0)
        self.assertEqual(sorted(p.name for p in self.paths.mined.iterdir()), runs_before)
        self.assertEqual(read_json(self.paths.auto / "last.json")["outcome"], "deferred")
        self.assertFalse((self.paths.auto / "lock").exists())

    def test_new_material_is_mined_alone_then_swapped_in(self):
        old = read_json(self.paths.model_json)
        old_chunks = len(chunk_files(self.paths))
        add_session(self.claude, "cs95", n=8, words=600)
        rec = self.run_auto()
        self.assertEqual(rec["outcome"], "swapped", rec)
        new_chunks = rec["steps"]["prep"]["new_chunks"]
        self.assertEqual(rec["steps"]["mine"]["units"], len(new_chunks) * 7)
        self.assertEqual(rec["steps"]["mine"]["calls"], len(new_chunks) * 7)  # unchanged chunks reused, not re-mined
        self.assertEqual(len(chunk_files(self.paths)), old_chunks + len(new_chunks))
        new = read_json(self.paths.model_json)
        self.assertNotEqual(new["model_hash"], old["model_hash"])
        self.assertEqual(new["parents"], [old["model_hash"]])
        self.assertEqual(new["mined_run"], latest_mined_run(self.paths))
        self.assertIn("Verify on the real path", self.paths.light.read_text(encoding="utf-8"))
        self.assertTrue((self.paths.archive / old["model_hash"] / "profile.md").exists())
        hist = helpers.read_jsonl(self.paths.history)
        self.assertEqual((hist[-1]["event"], hist[-1]["model_hash"], hist[-1]["previous"], hist[-1]["archived"]),
                         ("auto", new["model_hash"], old["model_hash"], old["model_hash"]))
        self.assertFalse((self.paths.auto / "staging").exists())
        self.assertEqual(self.run_auto()["outcome"], "nothing-new")

    def test_budget_stop_is_incomplete_and_the_next_run_resumes(self):
        add_session(self.claude, "cs95", n=8, words=600)
        rec = self.run_auto(max_calls=3)
        self.assertEqual(rec["outcome"], "incomplete")
        self.assertEqual((rec["steps"]["mine"]["ok"], rec["steps"]["mine"]["left"]), (3, 4))
        rec2 = self.run_auto()
        self.assertEqual(rec2["outcome"], "swapped")
        self.assertEqual(rec2["steps"]["mine"]["calls"], 4)

    def test_lint_gate_and_hand_edit_gate_hold_the_swap(self):
        bad = fake_engine(helpers.tmpdir(), light_extra="- Push without asking.")
        add_session(self.claude, "cs95", n=8, words=600)
        live = read_json(self.paths.model_json)["model_hash"]
        rec = self.run_auto(fake_dir=bad)
        self.assertEqual(rec["outcome"], "held")
        self.assertIn("forbidden", rec["reason"])
        self.assertEqual(read_json(self.paths.model_json)["model_hash"], live)
        self.assertTrue((self.paths.auto / "staging" / "model" / "light.md").exists())  # kept for inspection

        def edit_during_run(paths, sp, cfg):
            paths.light.write_text(paths.light.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            return None
        with mock.patch("mr.auto._lint_gate", side_effect=edit_during_run):
            rec2 = self.run_auto()
        self.assertEqual(rec2["outcome"], "held")
        self.assertIn("changed during the run", rec2["reason"])

    def test_lint_gate_refuses_new_findings_only(self):
        sp, _ = auto.stage(self.paths, self.tmp / "st")
        self.assertIsNone(auto._lint_gate(self.paths, sp, self.cfg))
        sp.profile.write_text(sp.profile.read_text(encoding="utf-8") + "- reach me at someone@example.com\n", encoding="utf-8")
        self.assertIn("lint findings", auto._lint_gate(self.paths, sp, self.cfg))

    def test_reviewed_corrections_are_folded_and_pending_drafts_are_not(self):
        self.paths.corrections.write_text("# pending d0000001 | 2026-04-01 | seed: \"x\" | wrong: n/a | meant: something | "
                                          "scope: global | kind: fix\n", encoding="utf-8")
        self.cfg["auto"]["min_new_chars"] = 10 ** 6
        self.assertEqual(self.run_auto()["outcome"], "deferred")
        with open(self.paths.corrections, "a", encoding="utf-8") as fh:
            fh.write("2026-04-02 | seed: \"push to phone\" | wrong: added a ban | meant: push and merge my branch when I say push | "
                     "scope: global | kind: fix\n")
        rec = self.run_auto()
        self.assertEqual(rec["outcome"], "swapped", rec)
        self.assertEqual(rec["steps"]["corrections"]["applied"], 1)
        self.assertNotIn("synthesize", rec["steps"])
        m = read_json(self.paths.model_json)
        self.assertEqual(m["corrections_folded"], 1)
        self.assertTrue(self.paths.corrections.read_text(encoding="utf-8").startswith("# pending"))  # never rewritten

    def test_regression_rolls_back_to_the_parent(self):
        add_session(self.claude, "cs95", n=8, words=600)
        self.assertEqual(self.run_auto()["outcome"], "swapped")
        m = read_json(self.paths.model_json)
        live, parent = m["model_hash"], m["parents"][0]
        from mr.stats import rate_block
        summary = {"invocations": 24, "classified": 24, "correction_rate": rate_block(10, 24), "refinement_rate": rate_block(0, 24),
                   "by_model": {live: {"correction": rate_block(9, 12)}, parent: {"correction": rate_block(1, 12)}}}
        with mock.patch("mr.track.run_track", return_value=summary):
            rec = self.run_auto()
        self.assertEqual(rec["outcome"], "rolled-back", rec)
        self.assertEqual(read_json(self.paths.model_json)["model_hash"], parent)
        self.assertEqual(helpers.read_jsonl(self.paths.history)[-1]["event"], "auto-rollback")
        self.cfg["auto"]["rollback_on_regression"] = False
        summary["by_model"] = {parent: {"correction": rate_block(9, 12)}, live: {"correction": rate_block(1, 12)}}
        read_json(self.paths.model_json)  # parent is live now, its parent is the version left behind
        with mock.patch("mr.track.run_track", return_value=summary):
            rec2 = self.run_auto()
        self.assertEqual(rec2["outcome"], "held")

    def test_lock(self):
        self.paths.auto.mkdir(parents=True, exist_ok=True)
        write_json(self.paths.auto / "lock", {"pid": os.getpid(), "started": "now", "epoch": __import__("time").time()})
        self.assertEqual(self.run_auto()["outcome"], "locked")
        write_json(self.paths.auto / "lock", {"pid": 2 ** 22 + 12345, "started": "then", "epoch": 0})
        self.assertNotEqual(self.run_auto()["outcome"], "locked")

    def test_no_test_split_advances_its_boundary(self):
        paths, cfg, claude, codex, tmp = built(no_test=True)
        rec = auto.run_auto(paths, cfg, kind="fake", fake_dir=fake_engine(tmp, "- Verify first."), log=helpers.quiet,
                            force=True, now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(read_json(paths.split)["train_before"], "2026-09-30T22:00:00Z")
        self.assertEqual(rec["outcome"], "swapped", rec)
        self.assertTrue(rec["steps"]["prep"]["new_chunks"])


class RollbackTests(unittest.TestCase):
    def test_rollback_restores_the_previous_version_and_keeps_grants(self):
        paths, cfg, claude, codex, tmp = built()
        old = read_json(paths.model_json)["model_hash"]
        add_session(claude, "cs95", n=8, words=600)
        self.assertEqual(auto.run_auto(paths, cfg, kind="fake", fake_dir=fake_engine(tmp, "- Verify first."), log=helpers.quiet)["outcome"], "swapped")
        new = read_json(paths.model_json)["model_hash"]
        grants = paths.grants.read_text(encoding="utf-8").replace("- Status: active", "- Status: revoked 2026-04-30", 1)
        paths.grants.write_text(grants, encoding="utf-8")
        r = helpers.run_cli(paths.home, "refresh", "--list")
        self.assertIn(old, r.stdout)
        self.assertIn("previous", r.stdout)
        r = helpers.run_cli(paths.home, "refresh", "--rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Verify first", paths.light.read_text(encoding="utf-8"))
        self.assertIn("revoked 2026-04-30", paths.grants.read_text(encoding="utf-8"))
        m = read_json(paths.model_json)
        self.assertEqual((m["rolled_back_from"], m["rolled_back_to"]), (new, old))
        self.assertTrue((paths.archive / new / "light.md").exists())  # roll forward stays possible
        self.assertEqual(helpers.run_cli(paths.home, "refresh", "--rollback", "--to", "ffffffff").returncode, 4)
        r = helpers.run_cli(paths.home, "refresh", "--rollback", "--to", new[:8])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Verify first", paths.light.read_text(encoding="utf-8"))

    def test_prune_keeps_the_newest_auto_versions_only(self):
        paths, cfg = helpers.init_home(helpers.tmpdir() / "home")
        rows = []
        for k in range(5):
            h = "%016d" % k
            (paths.archive / h).mkdir(parents=True)
            rows.append({"time": "2026-04-0%dT00:00:00Z" % (k + 1), "event": "auto", "model_hash": "%016d" % (k + 1), "archived": h})
        (paths.archive / "handmade").mkdir()
        paths.history.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        removed = auto.prune_versions(paths, 2, log=helpers.quiet)
        self.assertEqual(sorted(removed), ["%016d" % k for k in range(3)])
        self.assertEqual(sorted(p.name for p in paths.archive.iterdir()), ["0000000000000003", "0000000000000004", "handmade"])


class AutoCliTests(unittest.TestCase):
    def test_cli_logs_and_status(self):
        paths, cfg, claude, codex, tmp = built()
        add_session(claude, "cs95", n=8, words=600)
        r = helpers.run_cli(paths.home, "refresh", "--auto", "--engine", "fake", "--fake-dir", fake_engine(tmp, "- Verify first."))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("refresh --auto: swapped", r.stdout)
        log = (paths.logs / "refresh-auto.log").read_text(encoding="utf-8")
        self.assertIn("refresh --auto: swapped", log)
        st = helpers.run_cli(paths.home, "status")
        self.assertIn("auto refresh: last run", st.stdout)
        self.assertIn("swapped", st.stdout)
        paths.auto.mkdir(exist_ok=True)
        write_json(paths.auto / "lock", {"pid": os.getpid(), "started": "now", "epoch": __import__("time").time()})
        self.assertEqual(helpers.run_cli(paths.home, "refresh", "--auto", "--engine", "fake").returncode, 2)


if __name__ == "__main__":
    unittest.main()
