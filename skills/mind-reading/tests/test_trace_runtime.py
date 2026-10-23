"""Train-only trace runtime: rendering, verified loading, lexical retrieval, constrained packets, correction quotes.
All fixtures are fictional."""
import copy
import hashlib
import json
import math
import os
import shutil
import unittest
from pathlib import Path

import helpers  # noqa: F401  (sets sys.path)
from mr import trace_runtime as tr

CUTOFF = "2026-03-01T00:00:00Z"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_case(i, seed, nxt, family=None, session=None, project="proj-a", prefix=(), agent="", tools=(), target_time="2026-02-01T10:05:00Z"):
    return {
        "id": "c%d" % i, "family": family or "f%d" % i, "source": "claude", "session": session or "s%d" % i,
        "project": project, "seed_time": "2026-02-01T10:00:00Z", "target_time": target_time,
        "input": {"seed": seed, "prior_user_turns": list(prefix), "prior_agent_text": agent, "prior_tools": list(tools),
                  "project": "~/Code-fiction/Code/%s" % project, "time": "2026-02-01T10:00:00Z"},
        "target": {"text": nxt, "source_ref": "ref-t%d" % i, "time": target_time},
        "weight": 1, "origin": "human", "source_refs": ["ref-s%d" % i, "ref-t%d" % i],
    }


def write_candidate(directory, cases, manifest_patch=None, raw=None, extra_files=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = raw if raw is not None else "".join(json.dumps(c) + "\n" for c in cases).encode()
    (directory / "cases.jsonl").write_bytes(data)
    split_raw = json.dumps({"train_ids": sorted(c["id"] for c in cases), "validation_ids": [], "test_ids": [], "excluded_ids": []}).encode()
    (directory / "split.json").write_bytes(split_raw)
    manifest = {"version": "trace-candidate-1", "train_ids_sha256": sha(json.dumps(sorted(c["id"] for c in cases), separators=(",", ":")).encode()), "split_sha256": sha(split_raw),
                "trained_through": CUTOFF, "case_count": len(cases), "files": {"cases.jsonl": sha(data)}}
    manifest.update(extra_files or {})
    manifest.update(manifest_patch or {})
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def corpus():
    return [
        make_case(0, "fix the flaky cache test", "also run the whole suite after", family="cache"),
        make_case(1, "fix the flaky cache test please", "no, only that test file", family="cache2", project="proj-b"),
        make_case(2, "rename the parser module", "ok go ahead", family="parser"),
        make_case(3, "write release notes for the parser", "keep it to three bullets", family="notes"),
    ]


class RenderTests(unittest.TestCase):
    def test_literal_seed_bytes_unchanged(self):
        seed = "  Fix é the  bug\t\r\n  "
        self.assertEqual(tr.render_input({"seed": seed}, seed_only=True), seed)
        self.assertIn(seed, tr.render_input({"seed": seed}))
        p = tr.packet(seed, {}, corpus(), {})
        self.assertEqual(p["literal_seed"].encode(), seed.encode())

    def test_bounds_and_exclusions(self):
        inp = {"seed": "go", "prior_user_turns": ["t%d " % i + "x" * 3000 for i in range(5)],
               "prior_agent_text": "a" * 5000, "prior_tools": [{"name": "n%d" % i, "kind": "k", "success": True, "args": "SECRET"} for i in range(12)],
               "project": "~/Code-fiction/Code/proj", "target": {"text": "TARGETWORD"}, "reaction_input": "REACTWORD"}
        text = tr.render_input(inp)
        for banned in ("TARGETWORD", "REACTWORD", "SECRET", "~/Code-fiction"):
            self.assertNotIn(banned, text)
        self.assertEqual(text.count("previous user turn:"), 3)
        self.assertEqual(text.count("tool:"), 8)
        self.assertNotIn("t1 ", text)
        self.assertLessEqual(len(text), 2 + 3 * 2030 + 2030 + 8 * 200 + 50)
        self.assertLessEqual(text.count("a"), 2000 + 200)


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.tmpdir("trace-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def expect_reject(self, **kw):
        d = write_candidate(self.root / ("c%d" % len(list(self.root.iterdir()))), kw.pop("cases", corpus()), **kw)
        with self.assertRaises(tr.TraceFormatError):
            tr.load_cases(d)

    def test_roundtrip_and_cold_start(self):
        manifest, cases = tr.load_cases(write_candidate(self.root / "ok", corpus()))
        self.assertEqual(len(cases), 4)
        self.assertEqual(manifest["version"], "trace-candidate-1")
        manifest, cases = tr.load_cases(write_candidate(self.root / "empty", []))
        self.assertEqual(cases, [])

    def test_canonical_source_references_and_membership_digest(self):
        cases = corpus()
        for c in cases:
            c["source_refs"] = {"seed": "s"+c["id"], "target": c["target"]["source_ref"], "prior_user_turns": []}
        self.assertEqual(len(tr.load_cases(write_candidate(self.root / "canonical", cases))[1]), 4)
        self.expect_reject(manifest_patch={"train_ids_sha256": "0"*64})

    def test_naive_timestamp_is_not_invented(self):
        self.expect_reject(manifest_patch={"trained_through": "2026-03-01T00:00:00"})

    def test_rehashed_cases_cannot_change_frozen_training_membership(self):
        d = write_candidate(self.root / "membership", corpus())
        m = json.loads((d / "manifest.json").read_text())
        raw = (d / "cases.jsonl").read_bytes().replace(b'"c0"', b'"heldout"')
        (d / "cases.jsonl").write_bytes(raw)
        m["files"]["cases.jsonl"] = sha(raw)
        m["train_ids_sha256"] = sha(json.dumps(sorted(["heldout", "c1", "c2", "c3"]), separators=(",", ":")).encode())
        (d / "manifest.json").write_text(json.dumps(m))
        with self.assertRaises(tr.TraceFormatError): tr.load_cases(d)

    def test_only_allowlisted_files_opened(self):
        d = write_candidate(self.root / "x", corpus(), extra_files={"files": {"cases.jsonl": "0" * 64, "eval.jsonl": "1" * 64}})
        # fix the real digest after patching, with an unreadable extra file listed
        raw = (d / "cases.jsonl").read_bytes()
        m = json.loads((d / "manifest.json").read_text())
        m["files"]["cases.jsonl"] = sha(raw)
        (d / "manifest.json").write_text(json.dumps(m))
        (d / "eval.jsonl").mkdir()  # opening it as a file would fail
        self.assertEqual(len(tr.load_cases(d)[1]), 4)

    def test_tamper_and_header_checks(self):
        d = write_candidate(self.root / "t", corpus())
        raw = (d / "cases.jsonl").read_bytes()
        (d / "cases.jsonl").write_bytes(raw.replace(b"ok go ahead", b"ok go ahead!"))
        with self.assertRaises(tr.TraceFormatError):
            tr.load_cases(d)
        self.expect_reject(manifest_patch={"version": "trace-candidate-2"})
        self.expect_reject(manifest_patch={"split_sha256": "XYZ"})
        self.expect_reject(manifest_patch={"train_ids_sha256": "A" * 64})
        self.expect_reject(manifest_patch={"files": {"cases.jsonl": "nothex"}})
        self.expect_reject(manifest_patch={"case_count": 3})
        self.expect_reject(manifest_patch={"case_count": True})
        self.expect_reject(manifest_patch={"trained_through": "not a date"})

    def test_missing_trained_through(self):
        d = write_candidate(self.root / "m", corpus())
        m = json.loads((d / "manifest.json").read_text())
        del m["trained_through"]
        (d / "manifest.json").write_text(json.dumps(m))
        with self.assertRaises(tr.TraceFormatError):
            tr.load_cases(d)

    def test_symlink_and_traversal(self):
        outside = write_candidate(self.root / "outside", corpus())
        d = self.root / "link"
        d.mkdir()
        os.symlink(outside / "cases.jsonl", d / "cases.jsonl")
        shutil.copy(outside / "manifest.json", d / "manifest.json")
        with self.assertRaises(tr.TraceFormatError):
            tr.load_cases(d)
        d2 = self.root / "link2"
        d2.mkdir()
        shutil.copy(outside / "cases.jsonl", d2 / "cases.jsonl")
        os.symlink(outside / "manifest.json", d2 / "manifest.json")
        with self.assertRaises(tr.TraceFormatError):
            tr.load_cases(d2)
        m = json.loads((outside / "manifest.json").read_text())
        m["files"] = {"../outside/cases.jsonl": sha(b"x"), "cases.jsonl": m["files"]["cases.jsonl"]}
        (outside / "manifest.json").write_text(json.dumps(m))
        self.assertEqual(len(tr.load_cases(outside)[1]), 4)  # extra names are never opened
        with self.assertRaises(tr.TraceFormatError):
            tr.load_cases(self.root / "missing")

    def test_unknown_actor_weight_duplicates_refs(self):
        def bad(mut):
            cs = corpus()
            mut(cs)
            self.expect_reject(cases=cs)
        bad(lambda cs: cs[0].__setitem__("origin", "agent"))
        bad(lambda cs: cs[0].__setitem__("origin", "assistant"))
        bad(lambda cs: cs[0].__setitem__("weight", 2))
        bad(lambda cs: cs[0].__setitem__("weight", True))
        bad(lambda cs: cs[1].__setitem__("id", "c0"))
        bad(lambda cs: cs[0].__setitem__("source_refs", []))
        bad(lambda cs: cs[0]["target"].pop("source_ref"))
        bad(lambda cs: cs[0]["target"].__setitem__("text", ""))
        bad(lambda cs: cs[0].pop("input"))
        bad(lambda cs: cs[0]["input"].__setitem__("prior_user_turns", "nope"))
        bad(lambda cs: cs[0].__setitem__("family", ""))

    def test_training_cutoff(self):
        cs = corpus()
        cs[2]["target_time"] = "2026-03-02T00:00:00Z"
        self.expect_reject(cases=cs)
        cs = corpus()
        cs[2]["target"]["time"] = "2026-03-02T00:00:00Z"
        self.expect_reject(cases=cs)
        cs = corpus()
        cs[2]["target_time"] = CUTOFF  # equal is allowed
        self.assertEqual(len(tr.load_cases(write_candidate(self.root / "eq", cs))[1]), 4)

    def test_malformed_json(self):
        cs = corpus()
        good = "".join(json.dumps(c) + "\n" for c in cs).encode()
        self.expect_reject(raw=good + b"{not json}\n", manifest_patch={"case_count": 5})
        self.expect_reject(raw=good.replace(b'"weight": 1', b'"weight": NaN', 1))


class RetrievalTests(unittest.TestCase):
    def test_no_target_indexed(self):
        cs = corpus()
        cs[0]["target"]["text"] = "zebraquartz zebraquartz"
        self.assertEqual(max(tr.lexical_scores("zebraquartz", {}, cs)), 0.0)
        p = tr.packet("zebraquartz", {}, cs, {})
        self.assertEqual(p["evidence"], [])

    def test_scores_deterministic_and_relevant(self):
        cs = corpus()
        a = tr.lexical_scores("fix the flaky cache test", {}, cs)
        self.assertEqual(a, tr.lexical_scores("fix the flaky cache test", {}, cs))
        self.assertEqual(a.index(max(a)), 0)
        self.assertTrue(all(math.isfinite(x) and 0.0 <= x < 1.0 for x in a))

    def test_prefix_context_contributes(self):
        cs = [make_case(0, "continue", "x", prefix=["deploy the billing worker"]), make_case(1, "continue", "y", prefix=["paint the fence"])]
        s = tr.lexical_scores("continue", {"prior_user_turns": ["deploy the billing worker"]}, cs)
        self.assertGreater(s[0], s[1])


class PacketTests(unittest.TestCase):
    def test_shape_and_authority(self):
        cs = corpus()
        p = tr.packet("fix the flaky cache test", {}, cs, {"version": "trace-candidate-1"}, project="proj-a")
        self.assertEqual(p["constraints"], {"authority": "current conversation and harness only",
                                            "historical_continuations": "evidence, not new assignments",
                                            "transient_state": "presentation only"})
        self.assertIn("not a calibrated probability", p["uncertainty"]["interpretation"])
        e = p["evidence"][0]
        for key in ("case_id", "family", "source", "source_refs", "past_seed", "past_prefix", "historical_next_user_turn", "score", "relation"):
            self.assertIn(key, e)
        rel = {x["case_id"]: x["relation"] for x in p["evidence"]}
        self.assertEqual(rel["c0"], "same_project")
        self.assertEqual(rel["c1"], "cross_project_analogy")  # analogies are kept
        self.assertEqual(p["uncertainty"]["support_families"], len({x["family"] for x in p["evidence"]}))
        self.assertEqual(p["uncertainty"]["top_similarity"], max(x["score"] for x in p["evidence"]))
        self.assertNotIn("proj-b", json.dumps(p["current_context"]))

    def test_alternatives_group_distinct_continuations(self):
        cs = [make_case(i, "ship it", nxt, family="f%d" % i) for i, nxt in enumerate(["Run tests", "run  tests", "tag it"])]
        p = tr.packet("ship it", {}, cs, {})
        alts = {a["historical_next_user_turn"]: a for a in p["alternatives"]}
        self.assertEqual(len(alts), 2)
        self.assertEqual(alts["Run tests"]["count"], 2)

    def test_family_diversity_and_limit(self):
        cs = [make_case(i, "tidy the config loader", "n%d" % i, family="same") for i in range(5)]
        cs += [make_case(10 + i, "tidy the config loader", "m%d" % i, family="other") for i in range(3)]
        p = tr.packet("tidy the config loader", {}, cs, {}, limit=10)
        counts = {}
        for e in p["evidence"]:
            counts[e["family"]] = counts.get(e["family"], 0) + 1
        self.assertEqual(counts, {"same": 2, "other": 2})
        self.assertEqual(len(tr.packet("tidy the config loader", {}, cs, {}, limit=3)["evidence"]), 3)
        with self.assertRaises(ValueError):
            tr.packet("x", {}, cs, {}, limit=0)

    def test_session_and_family_exclusion(self):
        cs = [make_case(0, "tidy loader", "a", family="fa", session="cur"), make_case(1, "tidy loader", "b", family="fb", session="s1"),
              make_case(2, "tidy loader", "c", family="fcur", session="s2")]
        p = tr.packet("tidy loader", {"session": "cur", "family": "fcur"}, cs, {})
        self.assertEqual([e["case_id"] for e in p["evidence"]], ["c1"])

    def test_project_tiebreak_only(self):
        cs = [make_case(0, "tidy loader", "a", project="other"), make_case(1, "tidy loader", "b", project="mine")]
        p = tr.packet("tidy loader", {}, cs, {}, project="mine", ranked=[(0, 0.5), (1, 0.5)])
        self.assertEqual([e["case_id"] for e in p["evidence"]], ["c1", "c0"])
        p = tr.packet("tidy loader", {}, cs, {}, project="mine", ranked=[(0, 0.9), (1, 0.5)])
        self.assertEqual([e["case_id"] for e in p["evidence"]], ["c0", "c1"])

    def test_ranked_used_and_validated(self):
        cs = corpus()
        p = tr.packet("anything", {}, cs, {}, ranked=[(3, 0.2), (2, 0.9)])
        self.assertEqual([e["case_id"] for e in p["evidence"]], ["c2", "c3"])
        for bad in ([(9, 0.5)], [(-1, 0.5)], [(0, float("nan"))], [(0, float("inf"))], [(True, 0.5)], [(1.0, 0.5)],
                    [(0, True)], [(0, "0.5")], [(0, 0.5), (0, 0.4)], [(0,)], [0], "ranked", 5, [(0, None)]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                tr.packet("anything", {}, cs, {}, ranked=bad)

    def test_state_perturbation_changes_only_hints(self):
        cs = corpus()
        base = tr.packet("fix the flaky cache test", {"prior_user_turns": ["hi"]}, cs, {"version": "trace-candidate-1"})
        noisy_ctx = {"prior_user_turns": ["hi"], "time": {"local_hour": 3}, "mood": "furious", "state": {"delay_seconds": 99999},
                     "delay": 12345, "presentation_preference": "brief"}
        noisy = tr.packet("fix the flaky cache test", noisy_ctx, cs, {"version": "trace-candidate-1"})
        self.assertNotEqual(base["presentation_hints"], noisy["presentation_hints"])
        a, b = dict(base), dict(noisy)
        a.pop("presentation_hints")
        b.pop("presentation_hints")
        self.assertEqual(a, b)
        self.assertNotIn("furious", json.dumps(noisy))
        hour = tr.packet("x", {"time": {"local_hour": 23}}, cs, {})["presentation_hints"]
        self.assertEqual(hour, [])  # clock alone establishes no preference or mood
        self.assertEqual(tr.packet("x", {"time": {"local_hour": 14}}, cs, {})["presentation_hints"], [])
        self.assertEqual(tr.packet("x", {"presentation_preference": "bossy"}, cs, {})["presentation_hints"], [])

    def test_zero_evidence_is_valid(self):
        for cases in ([], corpus()):
            p = tr.packet("qqqq wwww", {}, cases, {})
            self.assertEqual(p["evidence"], [])
            self.assertEqual(p["alternatives"], [])
            self.assertEqual(p["uncertainty"]["support_families"], 0)
            self.assertEqual(p["uncertainty"]["top_similarity"], 0.0)
            self.assertEqual(p["literal_seed"], "qqqq wwww")
            self.assertIn("current words", p["note"])
            self.assertEqual(p["constraints"]["authority"], "current conversation and harness only")


class CorrectionTests(unittest.TestCase):
    def test_quotes_only_explicit_language(self):
        nexts = ["no, only that file", "Actually keep the old name", "i meant the other repo", "Don’t touch tests",
                 "not that one", "instead use the cache", "nope fine", "ok, no problem", "Notably good", "dont stop"]
        cs = [make_case(i, "seed %d" % i, n) for i, n in enumerate(nexts)]
        out = tr.correction_candidates(cs)
        self.assertEqual([o["case_id"] for o in out], ["c%d" % i for i in range(6)])
        first = out[0]
        self.assertEqual(first["before"], "seed 0")
        self.assertEqual(first["after"], "no, only that file")
        self.assertEqual(first["kind"], "possible_correction")
        self.assertEqual(first["source_ref"], "ref-t0")
        self.assertEqual(first["source_refs"], ["ref-s0", "ref-t0"])
        blob = json.dumps(out).lower()
        for invented in ("rule", "dissatisf", "frustrat", "always", "never", "prefers"):
            self.assertNotIn(invented, blob)
        self.assertEqual(set(first), {"kind", "case_id", "source", "source_refs", "source_ref", "before", "after", "counterexamples"})

    def test_counterexamples_same_seed_different_next(self):
        cs = [make_case(0, "Rename   the module", "no, keep it"), make_case(1, "rename the module", "ok go"),
              make_case(2, "rename the module", "No, keep it"), make_case(3, "other", "fine")]
        out = tr.correction_candidates(cs)
        self.assertEqual([o["case_id"] for o in out], ["c0", "c2"])
        self.assertEqual([c["case_id"] for c in out[0]["counterexamples"]], ["c1"])
        self.assertEqual(out[0]["counterexamples"][0]["after"], "ok go")

    def test_bounded_and_no_mutation(self):
        cs = [make_case(i, "s%d" % i, "no, again %d" % i) for i in range(150)]
        before = copy.deepcopy(cs)
        self.assertEqual(len(tr.correction_candidates(cs)), 100)
        self.assertEqual(cs, before)
        self.assertEqual(tr.correction_candidates([]), [])


if __name__ == "__main__":
    unittest.main()
