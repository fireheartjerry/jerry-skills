"""CLI tests for scripts/intent.py on fictional candidates. No network, no model, no live home."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SCRIPTS

import intent
from mr import trace_runtime as rt

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

INTENT = SCRIPTS / "intent.py"
CUTOFF = "2026-01-01T00:00:00Z"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_case(cid, seed, target, family=None, session=None, project="proj-a", turns=(), agent="", tools=()):
    return {"id": cid, "family": family or "fam-" + cid, "source": "claude", "session": session or "sess-" + cid,
            "project": project, "origin": "human", "weight": 1, "source_refs": ["ref-" + cid],
            "input": {"seed": seed, "prior_user_turns": list(turns), "prior_agent_text": agent,
                      "prior_tools": list(tools)},
            "target": {"text": target, "source_ref": "tref-" + cid},
            "seed_time": "2025-06-01T00:00:00Z", "target_time": "2025-06-01T00:05:00Z"}


CASES = [
    make_case("c1", "alpha export the report", "now add a header row"),
    make_case("c2", "beta fix the parser", "no, keep the old parser", turns=["gamma context"]),
    make_case("c3", "alpha export the report", "actually include totals", agent="gamma notes"),
    make_case("c4", "gamma plan the trip", "book the earlier train"),
]


def build_candidate(root, cases=CASES, manifest_extra=None, files_extra=None, partitions=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    raw = b"".join(json.dumps(c, ensure_ascii=False).encode() + b"\n" for c in cases)
    split = {"train_ids": [c["id"] for c in cases], "validation_ids": ["v1"], "test_ids": ["t1", "t2"],
             "excluded_ids": []}
    split.update(partitions or {})
    split_raw = json.dumps(split).encode()
    (root / "cases.jsonl").write_bytes(raw)
    (root / "split.json").write_bytes(split_raw)
    manifest = {"version": "trace-candidate-1", "trained_through": CUTOFF, "case_count": len(cases),
                "train_ids_sha256": sha(json.dumps(sorted(c["id"] for c in cases), separators=(",", ":")).encode()),
                "split_sha256": sha(split_raw), "files": {"cases.jsonl": sha(raw)}}
    manifest["files"].update(files_extra or {})
    manifest.update(manifest_extra or {})
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def run_cli(args, payload=None, env=None, cwd=None):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": cwd or tempfile.gettempdir()}
    base.update(env or {})
    data = payload if isinstance(payload, bytes) else (json.dumps(payload).encode() if payload is not None else b"")
    return subprocess.run([sys.executable, "-I", str(INTENT)] + args, input=data, capture_output=True, env=base,
                          cwd=cwd, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intent-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cand = build_candidate(self.tmp / "cand")

    def ok(self, args, payload=None):
        res = run_cli(["--candidate", str(self.cand)] + args, payload, cwd=str(self.tmp))
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertEqual(res.stderr, b"")
        return json.loads(res.stdout.decode("utf-8"))

    def fail(self, args, payload=None, candidate=None, env=None):
        cand = ["--candidate", str(candidate or self.cand)] if candidate != "none" else []
        res = run_cli(cand + args, payload, env=env, cwd=str(self.tmp))
        self.assertEqual(res.returncode, 2, res.stdout.decode())
        self.assertEqual(res.stdout, b"")
        self.assertTrue(res.stderr.startswith(b"intent: "), res.stderr)
        return res.stderr.decode()


class LexicalPacketTests(Base):
    def test_malformed_context_is_a_clean_cli_error(self):
        for value in (9, {}, [1]):
            message = self.fail(["packet"], {"seed": "alpha", "context": {"prior_user_turns": value}})
            self.assertIn("context.prior_user_turns", message)
    def test_packet_ranks_lexically_and_keeps_literal_seed(self):
        seed = "  alpha  export\tthe report é☃\U0001F600 \n"
        out = self.ok(["packet"], {"seed": seed, "context": {"goal": "ship the export"}, "project": "proj-a"})
        self.assertEqual(out["literal_seed"], seed)
        self.assertEqual(out["retrieval_mode"], "lexical")
        self.assertEqual(out["warnings"], [])
        self.assertEqual(out["current_context"]["goal"], "ship the export")
        self.assertEqual(out["predictor_provenance"]["kind"], "lexical")
        self.assertEqual(out["predictor_provenance"]["manifest_sha256"], sha((self.cand / "manifest.json").read_bytes()))
        self.assertEqual({e["case_id"] for e in out["evidence"][:2]}, {"c1", "c3"})
        self.assertEqual(out["constraints"]["authority"], "current conversation and harness only")

    def test_unicode_seed_round_trips_through_escaped_json_too(self):
        seed = "é‮  x y"
        res = run_cli(["--candidate", str(self.cand), "packet"], json.dumps({"seed": seed}).encode())
        self.assertEqual(json.loads(res.stdout.decode())["literal_seed"], seed)

    def test_lone_surrogate_survives_as_an_escape(self):
        res = run_cli(["--candidate", str(self.cand), "packet"], b'{"seed": "a\\ud800b"}')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["literal_seed"], "a\ud800b")

    def test_historical_targets_are_not_indexed(self):
        out = self.ok(["packet"], {"seed": "header row totals earlier train"})
        self.assertEqual(out["evidence"], [])
        self.assertIn("no train-only evidence", out["note"])

    def test_leaveout_by_session_and_family(self):
        out = self.ok(["packet"], {"seed": "alpha export the report", "context": {"session": "sess-c1"}})
        self.assertNotIn("c1", [e["case_id"] for e in out["evidence"]])
        out = self.ok(["packet"], {"seed": "alpha export the report", "context": {"family": "fam-c3"}})
        self.assertNotIn("c3", [e["case_id"] for e in out["evidence"]])

    def test_limit(self):
        out = self.ok(["packet", "--limit", "1"], {"seed": "alpha export the report"})
        self.assertEqual(len(out["evidence"]), 1)
        self.fail(["packet", "--limit", "0"], {"seed": "x"})

    def test_packet_is_deterministic(self):
        body = {"seed": "alpha beta gamma", "context": {"prior_user_turns": ["gamma context"]}}
        self.assertEqual(self.ok(["packet"], body), self.ok(["packet"], body))

    def test_empty_candidate_is_valid_everywhere(self):
        cand = build_candidate(self.tmp / "empty", cases=[])
        for args, payload in ((["packet"], {"seed": "anything"}), (["packet", "--local"], {"seed": "anything"}),
                              (["status"], None), (["corrections"], None),
                              (["counterfactual"], {"seed": "anything"})):
            res = run_cli(["--candidate", str(cand)] + args, payload, cwd=str(self.tmp))
            self.assertEqual(res.returncode, 0, res.stderr.decode())
        out = json.loads(run_cli(["--candidate", str(cand), "status"], cwd=str(self.tmp)).stdout)
        self.assertEqual(out["case_count"], 0)
        self.assertEqual(json.loads(run_cli(["--candidate", str(cand), "corrections"], cwd=str(self.tmp)).stdout),
                         {"corrections": [], "count": 0})
        self.fail(["explain", "--case-id", "c1"], candidate=cand)


class InputTests(Base):
    def test_malformed_stdin(self):
        for body in (b"", b"not json", b"[1]", b'"seed"', b'{"seed": 3}', b'{"seed": "a", "context": []}',
                     b'{"seed": "a", "project": 4}', b'{"seed": "a", "extra": 1}', b'{"seed": NaN}',
                     b"\xff\xfe", b'{"context": {}}'):
            with self.subTest(body=body):
                self.fail(["packet"], body)
                self.fail(["counterfactual"], body)

    def test_stdin_size_limit(self):
        edge = {"seed": "a" * 10}
        pad = 128 * 1024 - len(json.dumps(edge)) + 1
        self.assertIn("exceeds", self.fail(["packet"], json.dumps({"seed": "a" * (pad + 10)}).encode()))
        under = json.dumps({"seed": "x" * 1000}).encode()
        self.ok(["packet"], under)

    def test_no_candidate_is_an_error_even_with_a_home_full_of_state(self):
        home = self.tmp / "home"
        (home / ".mind-reading").mkdir(parents=True)
        res = run_cli(["packet"], {"seed": "x"}, env={"HOME": str(home)}, cwd=str(self.tmp))
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"no candidate", res.stderr)
        res = run_cli(["status"], env={"HOME": str(home), "MIND_READING_CANDIDATE": ""}, cwd=str(self.tmp))
        self.assertEqual(res.returncode, 2)

    def test_env_candidate_and_no_home_writes(self):
        home = self.tmp / "home"
        home.mkdir()
        res = run_cli(["packet"], {"seed": "alpha"}, env={"HOME": str(home), "MIND_READING_CANDIDATE": str(self.cand)},
                      cwd=str(self.tmp))
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertEqual(list(home.iterdir()), [])

    def test_candidate_flag_wins_over_env_and_can_follow_the_subcommand(self):
        res = run_cli(["status", "--candidate", str(self.cand)], env={"MIND_READING_CANDIDATE": "/nonexistent"},
                      cwd=str(self.tmp))
        self.assertEqual(res.returncode, 0, res.stderr.decode())

    def test_missing_candidate_directory(self):
        self.fail(["status"], candidate=self.tmp / "nope")

    def test_usage_error_exits_two(self):
        res = run_cli(["--candidate", str(self.cand), "bogus"])
        self.assertEqual(res.returncode, 2)


class TamperTests(Base):
    def tamper(self, name, fn):
        path = self.cand / name
        data = path.read_bytes()
        path.write_bytes(fn(data))

    def test_cases_edit_refused_by_every_command(self):
        self.tamper("cases.jsonl", lambda d: d.replace(b"header", b"HEADER"))
        for args, payload in ((["packet"], {"seed": "alpha"}), (["status"], None), (["corrections"], None),
                              (["explain", "--case-id", "c1"], None), (["counterfactual"], {"seed": "alpha"}),
                              (["packet", "--local"], {"seed": "alpha"})):
            self.assertIn("sha256", self.fail(args, payload))

    def test_split_edit_refused(self):
        self.tamper("split.json", lambda d: d.replace(b"t1", b"t9"))
        self.assertIn("split", self.fail(["status"]))

    def test_manifest_membership_edit_refused(self):
        manifest = json.loads((self.cand / "manifest.json").read_text())
        manifest["train_ids_sha256"] = "0" * 64
        (self.cand / "manifest.json").write_text(json.dumps(manifest))
        self.assertIn("membership", self.fail(["packet"], {"seed": "alpha"}))

    def test_bad_manifest_and_symlinked_file_refused(self):
        (self.cand / "manifest.json").write_text("{}")
        self.fail(["status"])
        cand = build_candidate(self.tmp / "linked")
        real = self.tmp / "real-cases.jsonl"
        shutil.move(str(cand / "cases.jsonl"), str(real))
        os.symlink(str(real), str(cand / "cases.jsonl"))
        self.assertIn("symlink", self.fail(["status"], candidate=cand))

    def test_status_reports_without_held_out_text(self):
        out = self.ok(["status"])
        self.assertEqual(out["case_count"], 4)
        self.assertEqual(out["partition_sizes"], {"train": 4, "validation_ids": 1, "test_ids": 2, "excluded_ids": 0})
        self.assertTrue(out["verified"])
        self.assertEqual(out["families"], 4)
        self.assertFalse(out["local_declared"])
        self.assertNotIn("header", json.dumps(out))


class ExplainCorrectionTests(Base):
    def test_explain_returns_exactly_one_case(self):
        out = self.ok(["explain", "--case-id", "c2"])
        self.assertEqual(out["case"], CASES[1])
        self.fail(["explain", "--case-id", "missing"])
        self.assertEqual(run_cli(["--candidate", str(self.cand), "explain"]).returncode, 2)

    def test_explain_refuses_duplicate_ids(self):
        dup = [CASES[0], dict(CASES[0])]
        cand = build_candidate(self.tmp / "dup", cases=dup, partitions={"train_ids": ["c1"]})
        self.assertIn("duplicate", self.fail(["explain", "--case-id", "c1"], candidate=cand))

    def test_corrections_are_quoted_pairs_with_counterexamples(self):
        out = self.ok(["corrections"])
        ids = {c["case_id"]: c for c in out["corrections"]}
        self.assertEqual(set(ids), {"c2", "c3"})
        self.assertEqual(ids["c3"]["after"], "actually include totals")
        self.assertEqual([x["case_id"] for x in ids["c3"]["counterexamples"]], ["c1"])
        self.assertEqual(out["count"], 2)


class CounterfactualTests(Base):
    def test_ablation_lists_changed_ids_and_never_mutates_seed_or_goal(self):
        body = {"seed": "alpha", "context": {"goal": "keep this goal", "phase": "build", "verified_facts": ["f"],
                                              "prior_agent_text": "gamma notes", "prior_user_turns": ["gamma context"]},
                "project": "proj-a"}
        out = self.ok(["counterfactual", "--limit", "10"], body)
        self.assertEqual(out["literal_seed"], "alpha")
        self.assertIn("not a causal", out["note"])
        self.assertEqual(out["cleared_fields"], ["prior_agent_text", "prior_tools", "prior_user_turns"])
        with_ids = [e["case_id"] for e in out["with_context"]]
        without_ids = [e["case_id"] for e in out["without_context"]]
        self.assertTrue(set(with_ids) - set(without_ids))
        self.assertEqual(out["changed_case_ids"]["gained_with_context"], [i for i in with_ids if i not in without_ids])
        self.assertEqual(out["changed_case_ids"]["lost_with_context"], [i for i in without_ids if i not in with_ids])
        self.assertEqual(out["same_case_ids"], [i for i in with_ids if i in without_ids])
        same = self.ok(["packet", "--limit", "10"], body)
        self.assertEqual(with_ids, [e["case_id"] for e in same["evidence"]])

    def test_no_context_means_no_change(self):
        out = self.ok(["counterfactual"], {"seed": "alpha export the report"})
        self.assertEqual(out["changed_case_ids"], {"gained_with_context": [], "lost_with_context": []})


class FallbackTests(Base):
    def test_local_without_declared_vectors_falls_back_visibly(self):
        out = self.ok(["packet", "--local"], {"seed": "alpha export the report"})
        self.assertEqual(out["retrieval_mode"], "lexical_fallback")
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("lexical", out["warnings"][0])
        self.assertTrue(out["evidence"])

    def test_missing_rerank_weights_warn_and_keep_going(self):
        out = self.ok(["packet", "--rerank-path", str(self.tmp / "no-weights")], {"seed": "alpha export the report"})
        self.assertIsNone(out["rerank"])
        self.assertTrue(any("rerank skipped" in w for w in out["warnings"]))
        self.assertTrue(out["evidence"])


@unittest.skipIf(np is None, "numpy unavailable")
class LocalTests(unittest.TestCase):
    DIM = 4
    WORDS = ("alpha", "beta", "gamma")

    class FakeEncoder:
        def __init__(self, path, device="cpu", max_length=256):
            self.path, self.device, self.max_length = path, device, max_length
            self.texts = []

        def encode(self, texts):
            self.texts.extend(texts)
            out = np.full((len(texts), 4), 0.01, dtype=np.float32)
            for r, text in enumerate(texts):
                for c, w in enumerate(("alpha", "beta", "gamma")):
                    if w in text:
                        out[r, c] += 1.0
            return out / np.linalg.norm(out, axis=1, keepdims=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intent-local-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.encoders = []
        enc = self.FakeEncoder

        def make(path, device, max_length):
            e = enc(path, device, max_length)
            self.encoders.append(e)
            return e
        patcher = unittest.mock.patch.object(intent, "_make_encoder", make)
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidate(self, kind="retrieval", blend=0.0, mutate=None, **predictor):
        cases = CASES
        fake = self.FakeEncoder("p")
        ctx = fake.encode([rt.render_input(c["input"]) for c in cases])
        seed = fake.encode([rt.render_input(c["input"], True) for c in cases])
        tgt = fake.encode([c["target"]["text"] for c in cases])
        self.count = getattr(self, "count", 0) + 1
        root = self.tmp / ("cand-%s-%d" % (kind, self.count))
        vec = io.BytesIO()
        np.savez(vec, ids=np.array([c["id"] for c in CASES]), seed=seed, context=ctx, target=tgt)
        files = {"train-vectors.npz": sha(vec.getvalue())}
        spec = {"kind": kind, "alpha": 1.0, "k": 2, "temperature": 0.1, "context_blend": blend}
        spec.update(predictor)
        extra = {"encoder": {"path": "encoder", "model": "fictional", "revision": "r0", "max_length": 256,
                             "weights_sha256": {"weights.bin": sha(b"fictional model")}},
                 "predictor": spec, "retrieval_future_weight": 0.25}
        pred = None
        if kind == "ridge":
            pred = io.BytesIO()
            np.savez(pred, weight=np.eye(self.DIM, dtype=np.float32), x_mean=np.zeros(self.DIM, np.float32),
                     y_mean=np.zeros(self.DIM, np.float32))
        elif kind == "adapter":
            pred = io.BytesIO()
            np.savez(pred, down_weight=np.zeros((2, self.DIM), np.float32), down_bias=np.zeros(2, np.float32),
                     up_weight=np.zeros((self.DIM, 2), np.float32), up_bias=np.zeros(self.DIM, np.float32))
        if pred is not None:
            files["predictor.npz"] = sha(pred.getvalue())
        extra.update(mutate or {})
        build_candidate(root, cases, manifest_extra=extra, files_extra=files)
        (root / "train-vectors.npz").write_bytes(vec.getvalue())
        if pred is not None:
            (root / "predictor.npz").write_bytes(pred.getvalue())
        (root / "encoder").mkdir()
        (root / "encoder" / "weights.bin").write_bytes(b"fictional model")
        return root

    def run_main(self, cand, args, payload):
        out, err = io.BytesIO(), io.StringIO()
        code = intent.main(["--candidate", str(cand)] + args, io.BytesIO(json.dumps(payload).encode()), out, err)
        return code, out.getvalue(), err.getvalue()

    def packet(self, cand, payload=None, args=()):
        code, out, err = self.run_main(cand, ["packet", "--local", "--device", "cpu"] + list(args),
                                       payload or {"seed": "alpha export the report"})
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_local_encoder_file_tamper_is_not_a_lexical_fallback(self):
        cand=self.candidate("context")
        (cand/"encoder"/"weights.bin").write_bytes(b"changed model")
        code,out,err=self.run_main(cand,["packet","--local"],{"seed":"alpha"})
        self.assertEqual(code,2)
        self.assertEqual(out,b"")
        self.assertIn("weight digest mismatch",err)

    def test_local_encoder_requires_nonempty_digest_pins(self):
        cand=self.candidate("context")
        path=cand/"manifest.json";manifest=json.loads(path.read_text());manifest["encoder"]["weights_sha256"]={};path.write_text(json.dumps(manifest))
        code,out,err=self.run_main(cand,["packet","--local"],{"seed":"alpha"})
        self.assertEqual(code,2)
        self.assertIn("pinned model file",err)

    def test_every_predictor_kind_ranks_with_provenance(self):
        for kind in ("retrieval", "context", "seed", "prior", "ridge", "adapter"):
            with self.subTest(kind=kind):
                cand = self.candidate(kind, blend=0.5 if kind in ("ridge", "adapter") else 0.0)
                out = self.packet(cand)
                self.assertEqual(out["retrieval_mode"], "local")
                self.assertEqual(out["warnings"], [])
                prov = out["predictor_provenance"]
                files = json.loads((cand / "manifest.json").read_text())["files"]
                self.assertEqual(prov["kind"], kind)
                self.assertEqual(prov["manifest_sha256"], sha((cand / "manifest.json").read_bytes()))
                self.assertEqual(prov["vector_sha256"], files["train-vectors.npz"])
                self.assertEqual(prov["predictor_sha256"], files.get("predictor.npz"))
                self.assertEqual({e["case_id"] for e in out["evidence"][:2]}, {"c1", "c3"})

    def test_query_is_the_rendered_prefix_only_and_goal_stays_in_the_packet(self):
        cand = self.candidate("seed")
        payload = {"seed": "alpha", "context": {"goal": "zzz-secret-goal", "prior_user_turns": ["beta turn"],
                                                 "session": "s", "verified_facts": ["zzz-fact"]}}
        out = self.packet(cand, payload)
        texts = self.encoders[-1].texts
        self.assertEqual(texts, [rt.render_input({"seed": "alpha", "prior_user_turns": ["beta turn"]}), "alpha"])
        self.assertFalse(any("zzz" in t for t in texts))
        self.assertEqual(out["current_context"]["goal"], "zzz-secret-goal")
        self.assertEqual(self.encoders[-1].max_length, 256)
        self.assertEqual(self.encoders[-1].path, str(cand / "encoder"))

    def test_hybrid_score_formula(self):
        cand = self.candidate("context")
        out = self.packet(cand)
        top = out["evidence"][0]
        enc = self.FakeEncoder("p")
        q = enc.encode([rt.render_input({"seed": "alpha export the report"})])[0]
        ctx = enc.encode([rt.render_input(CASES[0]["input"])])[0]
        tgt = enc.encode([CASES[0]["target"]["text"]])[0]
        expected = 0.75 * float(ctx @ q) + 0.25 * float(tgt @ q)
        c1 = [e for e in out["evidence"] if e["case_id"] == "c1"][0]
        self.assertAlmostEqual(c1["score"], expected, places=5)
        self.assertGreaterEqual(top["score"], c1["score"])

    def test_hash_tamper_never_falls_back(self):
        for name in ("train-vectors.npz", "predictor.npz"):
            cand = self.candidate("ridge")
            with open(cand / name, "ab") as f:
                f.write(b"x")
            code, out, err = self.run_main(cand, ["packet", "--local"], {"seed": "alpha"})
            self.assertEqual((code, out), (2, b""), name)
            self.assertIn("sha256", err)

    def test_listed_but_missing_vectors_is_an_error(self):
        cand = self.candidate("context")
        (cand / "train-vectors.npz").unlink()
        code, out, err = self.run_main(cand, ["packet", "--local"], {"seed": "alpha"})
        self.assertEqual(code, 2)

    def test_missing_predictor_digest_for_ridge_is_an_error(self):
        cand = self.candidate("ridge")
        manifest = json.loads((cand / "manifest.json").read_text())
        del manifest["files"]["predictor.npz"]
        (cand / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.run_main(cand, ["packet", "--local"], {"seed": "a"})[0], 2)

    def test_shape_and_pickle_arrays_refused(self):
        cand = self.candidate("context")
        bad = io.BytesIO()
        np.savez(bad, seed=np.zeros((3, 4), np.float32), context=np.zeros((4, 4), np.float32),
                 target=np.zeros((4, 4), np.float32))
        (cand / "train-vectors.npz").write_bytes(bad.getvalue())
        manifest = json.loads((cand / "manifest.json").read_text())
        manifest["files"]["train-vectors.npz"] = sha(bad.getvalue())
        (cand / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.run_main(cand, ["packet", "--local"], {"seed": "a"})[0], 2)
        obj = io.BytesIO()
        np.savez(obj, seed=np.array([{"a": 1}] * 4, dtype=object), context=np.zeros((4, 4)), target=np.zeros((4, 4)))
        (cand / "train-vectors.npz").write_bytes(obj.getvalue())
        manifest["files"]["train-vectors.npz"] = sha(obj.getvalue())
        (cand / "manifest.json").write_text(json.dumps(manifest))
        code, _, err = self.run_main(cand, ["packet", "--local"], {"seed": "a"})
        self.assertEqual(code, 2)
        self.assertIn("plain numeric", err)

    def test_nonfinite_vectors_refused(self):
        cand = self.candidate("context")
        bad = io.BytesIO()
        arr = np.ones((4, 4), np.float32)
        arr[0, 0] = np.nan
        np.savez(bad, seed=arr, context=arr, target=arr)
        (cand / "train-vectors.npz").write_bytes(bad.getvalue())
        manifest = json.loads((cand / "manifest.json").read_text())
        manifest["files"]["train-vectors.npz"] = sha(bad.getvalue())
        (cand / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.run_main(cand, ["packet", "--local"], {"seed": "a"})[0], 2)

    def test_missing_encoder_weights_fall_back_visibly(self):
        cand = self.candidate("context")

        def missing(path, device, max_length):
            raise ValueError("local encoder weights missing; downloads must be explicit")
        with unittest.mock.patch.object(intent, "_make_encoder", missing):
            out = self.packet(cand)
        self.assertEqual(out["retrieval_mode"], "lexical_fallback")
        self.assertIn("weights missing", out["warnings"][0])

    def test_missing_dependency_falls_back_but_tamper_still_refused(self):
        cand = self.candidate("context")

        def no_torch(path, device, max_length):
            raise ImportError("No module named torch")
        with unittest.mock.patch.object(intent, "_make_encoder", no_torch):
            self.assertEqual(self.packet(cand)["retrieval_mode"], "lexical_fallback")
            with open(cand / "train-vectors.npz", "ab") as f:
                f.write(b"x")
            self.assertEqual(self.run_main(cand, ["packet", "--local"], {"seed": "a"})[0], 2)

    def test_bad_manifest_local_sections_are_errors_not_fallbacks(self):
        for mutate in ({"predictor": {"kind": "nope"}}, {"encoder": {"path": ""}},
                       {"retrieval_future_weight": 2}, {"predictor": {"kind": "retrieval", "k": 0, "temperature": 0.1}},
                       {"encoder": {"path": "e", "max_length": "256"}}):
            with self.subTest(mutate=mutate):
                cand = self.candidate("context", mutate=mutate)
                self.assertEqual(self.run_main(cand, ["packet", "--local"], {"seed": "a"})[0], 2)

    def test_counterfactual_local_changes_ids(self):
        cand = self.candidate("context")
        payload = {"seed": "export", "context": {"prior_agent_text": "gamma notes"}}
        code, out, err = self.run_main(cand, ["counterfactual", "--local", "--device", "cpu", "--limit", "10"], payload)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["retrieval_mode"], "local")
        self.assertTrue(result["changed_case_ids"]["gained_with_context"] or
                        result["changed_case_ids"]["lost_with_context"] or result["same_case_ids"])

    def test_local_leaveout_excludes_session_from_retrieval_prediction(self):
        cand = self.candidate("retrieval", k=4)
        out = self.packet(cand, {"seed": "alpha export the report", "context": {"session": "sess-c1"}})
        self.assertNotIn("c1", [e["case_id"] for e in out["evidence"]])


class RerankIntegrationTests(Base):
    def test_rerank_reorders_head_using_prefixes_only(self):
        from mr import trace_rerank
        seen = []

        class Fake:
            def __init__(self, path, device="cpu", max_length=512):
                seen.append(path)

            def scores(self, query, documents):
                seen.append((query, list(documents)))
                return [0.9 if "gamma notes" in d else 0.1 for d in documents]

        body = {"seed": "alpha export the report", "context": {"session": "sess-c4"}}
        with unittest.mock.patch.object(trace_rerank, "LocalReranker", Fake):
            out, err = io.BytesIO(), io.StringIO()
            code = intent.main(["--candidate", str(self.cand), "packet", "--rerank-path", "/fictional", "--device",
                                "cpu"], io.BytesIO(json.dumps(body).encode()), out, err)
        self.assertEqual(code, 0, err.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result["rerank"], {"applied": True, "top_k": 12})
        self.assertEqual(result["evidence"][0]["case_id"], "c3")
        query, docs = seen[1]
        self.assertEqual(query, "seed: alpha export the report")
        self.assertNotIn("target", " ".join(docs))
        self.assertFalse(any(t in " ".join(docs) for t in ("header row", "totals", "keep the old parser")))
        self.assertNotIn("c4", [e["case_id"] for e in result["evidence"]])


class StatusImportTests(unittest.TestCase):
    def test_module_has_no_import_time_side_effects_on_home(self):
        with tempfile.TemporaryDirectory() as home:
            res = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import intent" % str(SCRIPTS)],
                                 env={"HOME": home, "PATH": os.environ.get("PATH", "")}, capture_output=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(os.listdir(home), [])


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
