"""Post-evaluation refit: fixed recipe, new directory, evaluated artifacts untouched. Fictional data only."""
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (puts scripts/ on sys.path)

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from mr import trace_forecast, trace_learning, trace_refit
from mr.trace_runtime import load_cases, VERSION

SENTINEL = "SENTINEL-TEST-METRICS-DO-NOT-SELECT"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def case(i, **over):
    row = {"id": "c%d" % i, "family": "f%d" % (i // 2), "source": "claude", "session": "s%d" % i, "project": "p",
           "origin": "human", "weight": 1, "source_refs": ["ref%d" % i],
           "input": {"seed": "please fix export number %d now" % i, "prior_user_turns": []},
           "target": {"text": "then verify file %d" % i, "source_ref": "t%d" % i},
           "seed_time": "2026-01-%02dT10:00:00Z" % (i + 1), "target_time": "2026-01-%02dT11:00:00Z" % (i + 1),
           "reaction_input": {"post_action": {"assistant_text": "ran export %d" % i}, "secret": "drop me"}}
    row.update(over)
    return row


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


@unittest.skipIf(np is None, "numpy not installed")
class RefitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "study"
        (self.home / "candidate").mkdir(parents=True)
        (self.home / "research" / "predictors").mkdir(parents=True)
        split = json.dumps({"train_ids": ["c0", "c1", "c2"], "validation_ids": ["c3"], "test_ids": ["c4"]}).encode()
        (self.home / "candidate" / "split.json").write_bytes(split)
        (self.home / "candidate" / "manifest.json").write_text(json.dumps(
            {"version": VERSION, "split_sha256": sha(split), "project_aliases": {"p": ["proj"]}}))
        (self.home / "research" / "test-results.json").write_text(json.dumps({"best": SENTINEL, "ridge": 0.99}))
        (self.home / "research" / "seal.json").write_text('{"sealed":true}')
        self.write_index({"name": "ridge-a", "kind": "ridge", "model": "ridge-a", "alpha": 3.5, "context_blend": 0.5,
                          "baseline": False, "primary": True})
        self.cases = [case(i) for i in range(6)]
        self.examples = self.tmp / "examples.jsonl"
        self.write_examples(self.cases)
        rng = np.random.default_rng(0)
        self.vec = {k: rng.normal(size=(6, 4)).astype("float32") for k in ("seed", "context", "target")}
        self.vectors = self.tmp / "vectors.npz"
        self.write_vectors()
        self.out = self.tmp / "refit"

    def write_index(self, spec, name="ridge-a", temperature=0.2, flat=False):
        entry = dict(spec, temperature=temperature) if flat else {"spec": spec, "temperature": temperature}
        (self.home / "research" / "predictors" / "index.json").write_text(json.dumps(
            {"selected": name, "encoder": {"path": "/enc", "model": "toy", "weights_sha256": {"w": "ab" * 32}},
             "methods": {name: entry, "prior": {"spec": {"kind": "prior"}, "temperature": 9.0}}}))

    def write_examples(self, rows):
        self.examples.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def write_vectors(self, ids=None, **over):
        arrays = dict(self.vec, ids=np.asarray(ids or [c["id"] for c in self.cases]), reaction=np.zeros((6, 4)))
        arrays.update(over)
        np.savez(self.vectors, **arrays)

    def run_refit(self, **kw):
        return trace_refit.refit_candidate(self.home, self.examples, self.vectors, kw.get("out", self.out))

    def test_ridge_refit_uses_frozen_alpha_and_only_new_rows(self):
        before = tree_bytes(self.home)
        real = trace_forecast.fit_ridge
        with patch.object(trace_forecast, "fit_ridge", side_effect=real) as fit:
            manifest = self.run_refit()
        args, kwargs = fit.call_args
        self.assertEqual(kwargs, {"alpha": 3.5})
        np.testing.assert_array_equal(args[0], self.vec["context"])
        np.testing.assert_array_equal(args[1], self.vec["target"])
        self.assertEqual(tree_bytes(self.home), before)
        loaded, rows = load_cases(self.out)
        self.assertEqual(loaded, manifest)
        self.assertEqual(len(rows), 6)
        self.assertNotIn("reaction_input", rows[0])
        self.assertEqual(rows[0]["historical_response"], {"assistant_text": "ran export 0"})
        self.assertNotIn("drop me", (self.out / "cases.jsonl").read_text())
        with np.load(self.out / "predictor.npz", allow_pickle=False) as p:
            self.assertEqual(set(p.files), {"weight", "x_mean", "y_mean", "alpha"})
        with np.load(self.out / "train-vectors.npz", allow_pickle=False) as v:
            self.assertEqual(v["ids"].tolist(), ["c%d" % i for i in range(6)])

    def test_manifest_lineage_and_no_forward_claims(self):
        m = self.run_refit()
        h = self.home
        self.assertIs(m["fresh_evaluation"], False)
        self.assertEqual(m["stage"], "post_evaluation_refit")
        self.assertEqual(m["selected_method"], "ridge-a")
        self.assertEqual(m["encoder"]["weights_sha256"], {"w": "ab" * 32})
        self.assertEqual(m["predictor"], {"kind": "ridge", "calibration_temperature": 0.2, "temperature": 0.2,
                                          "alpha": 3.5, "context_blend": 0.5, "input": "context",
                                          "file": "predictor.npz"})
        self.assertEqual(m["trained_through"], "2026-01-06T11:00:00Z")
        self.assertEqual(m["project_aliases"], {"p": ["proj"]})
        pe = m["parent_evaluation"]
        self.assertEqual(pe["receipt_sha256"], sha((h / "research" / "test-results.json").read_bytes()))
        self.assertEqual(pe["seal_sha256"], sha((h / "research" / "seal.json").read_bytes()))
        self.assertEqual(pe["evaluated_candidate_manifest_sha256"], sha((h / "candidate" / "manifest.json").read_bytes()))
        self.assertIn("no fresh held-out evaluation", pe["scope"])
        self.assertIs(m["calibration"]["validated_for_candidate"], False)
        self.assertEqual(m["calibration"]["temperature"], 0.2)
        split = json.loads((self.out / "split.json").read_text())
        self.assertEqual(split["protocol"], "post-evaluation-refit")
        self.assertEqual(split["parent_split_sha256"], sha((h / "candidate" / "split.json").read_bytes()))
        self.assertEqual(split["validation_ids"] + split["test_ids"] + split["excluded_ids"], [])
        self.assertEqual(split["former_holdout_or_new_ids"], ["c3", "c4", "c5"])
        self.assertTrue(split["includes_former_holdouts"])
        text = (self.out / "manifest.json").read_text().lower()
        for banned in ("top1", "mrr", "test_score", "held_out_score", "unseen"):
            self.assertNotIn(banned, text)
        self.assertNotIn(SENTINEL, text)

    def test_selection_ignores_test_receipt(self):
        (self.home / "research" / "test-results.json").write_bytes(b"\xff not even json \x00")
        m = self.run_refit()
        self.assertEqual(m["selected_method"], "ridge-a")
        self.assertEqual(m["fixed_recipe"], {"kind": "ridge", "alpha": 3.5, "context_blend": 0.5, "model": "ridge-a"})

    def test_semantic_recipe_maps_to_retrieval_config(self):
        path = self.home / "candidate" / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["retrieval_future_weight"] = 0.4
        path.write_text(json.dumps(manifest))
        self.write_index({"kind": "semantic", "k": 5, "temperature_retrieval": 0.07, "context_blend": 0.3},
                         name="semantic-k5", temperature=0.06)
        m = self.run_refit()
        self.assertEqual(m["predictor"], {"kind": "retrieval", "calibration_temperature": 0.06, "temperature": 0.07,
                                          "retriever": "semantic", "k": 5, "query": "context",
                                          "retrieval_future_weight": 0.4})
        self.assertEqual(m["retrieval_future_weight"], 0.4)
        self.assertFalse((self.out / "predictor.npz").exists())
        self.assertNotIn("predictor.npz", m["files"])
        self.assertEqual(m["calibration"]["temperature"], 0.06)

    def test_semantic_needs_k_and_retrieval_temperature(self):
        for spec in ({"kind": "semantic", "temperature_retrieval": 0.1}, {"kind": "semantic", "k": 3}):
            self.write_index(spec)
            with self.assertRaises(trace_refit.RefitError):
                self.run_refit()

    def test_seed_and_context_kinds_stay_plain_without_k_or_temperature(self):
        for kind in ("seed", "context"):
            out = self.tmp / kind
            self.write_index({"kind": kind}, name=kind, temperature=0.1)
            m = self.run_refit(out=out)
            self.assertEqual(m["predictor"], {"kind": kind, "calibration_temperature": 0.1, "temperature": 0.1})
            self.assertEqual(m["fixed_recipe"], {"kind": kind})
            self.assertFalse((out / "predictor.npz").exists())
            self.assertEqual(m["project_aliases"], {"p": ["proj"]})

    def test_flat_entry_and_missing_aliases(self):
        self.write_index({"kind": "ridge", "alpha": 2.0, "context_blend": 0.0}, flat=True)
        path = self.home / "candidate" / "manifest.json"
        manifest = json.loads(path.read_text())
        del manifest["project_aliases"]
        path.write_text(json.dumps(manifest))
        m = self.run_refit()
        self.assertEqual(m["predictor"]["alpha"], 2.0)
        self.assertEqual(m["project_aliases"], {})

    def test_adapter_refit_with_mocked_fit(self):
        self.write_index({"name": "adapter-r2", "kind": "adapter", "model": "adapter-r2", "rank": 2, "steps": 7,
                          "seed": 11, "context_blend": 0.5}, name="adapter-r2", temperature=0.06)
        fake = {"down_weight": np.ones((2, 4)), "up_weight": np.ones((4, 2)), "loss_last": 0.5, "rank": 2,
                "sampling": "text must be dropped"}
        with patch.object(trace_learning, "fit_adapter", return_value=fake) as fit:
            m = self.run_refit()
        args, kwargs = fit.call_args
        self.assertEqual(kwargs, {"rank": 2, "steps": 7, "seed": 11})
        self.assertEqual(list(args[2]), [c["family"] for c in self.cases])
        self.assertEqual(m["predictor"], {"kind": "adapter", "calibration_temperature": 0.06, "temperature": 0.06,
                                          "rank": 2, "steps": 7, "seed": 11, "context_blend": 0.5,
                                          "input": "context", "file": "predictor.npz"})
        with np.load(self.out / "predictor.npz", allow_pickle=False) as p:
            self.assertNotIn("sampling", p.files)
            self.assertIn("down_weight", p.files)

    def test_unusable_selected_kinds_refuse(self):
        for kind in ("prior", "lexical", "control", "reaction", "mystery"):
            self.write_index({"kind": kind})
            with self.assertRaises(trace_refit.RefitError):
                self.run_refit()
        self.assertFalse(self.out.exists())

    def test_requires_receipt_and_seal(self):
        for name in ("test-results.json", "seal.json"):
            path = self.home / "research" / name
            data = path.read_bytes()
            path.unlink()
            with self.assertRaises(trace_refit.RefitError):
                self.run_refit()
            path.write_bytes(data)
        self.assertFalse(self.out.exists())

    def test_output_collision_and_source_refuse(self):
        self.out.mkdir()
        (self.out / "keep").write_text("x")
        with self.assertRaises(trace_refit.RefitError):
            self.run_refit()
        self.assertEqual((self.out / "keep").read_text(), "x")
        for bad in (self.home / "candidate", self.home / "candidate" / "inner", self.home):
            with self.assertRaises(trace_refit.RefitError):
                self.run_refit(out=bad)
        self.assertEqual(self.run_refit(out=self.tmp / "ok")["case_count"], 6)

    def test_malformed_examples_reject_without_output(self):
        def with_row(i, **over):
            rows = [dict(c) for c in self.cases]
            rows[i] = case(i, **over)
            return rows
        bad = {
            "duplicate id": with_row(1, id="c0"),
            "non-human": with_row(1, origin="agent"),
            "weight": with_row(1, weight=0.5),
            "clock order": with_row(1, seed_time="2026-02-01T00:00:00Z", target_time="2026-01-01T00:00:00Z"),
            "naive time": with_row(1, seed_time="2026-01-01T00:00:00", target_time="2026-01-02T00:00:00"),
            "short seed": with_row(1, input={"seed": "hi"}),
            "long seed": with_row(1, input={"seed": "w " * 61}),
            "no family": with_row(1, family=""),
        }
        for label, rows in bad.items():
            with self.subTest(label):
                self.write_examples(rows)
                with self.assertRaises(trace_refit.RefitError):
                    self.run_refit()
                self.assertFalse(self.out.exists())
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".refit-")], [])

    def test_malformed_vectors_reject(self):
        ids = [c["id"] for c in self.cases]
        nan = self.vec["context"].copy()
        nan[0, 0] = np.nan
        cases = {
            "id order": dict(ids=ids[::-1]),
            "id set": dict(ids=ids[:-1] + ["zz"]),
            "nan": dict(context=nan),
            "rows": dict(target=self.vec["target"][:5]),
            "dim": dict(seed=np.zeros((6, 3), dtype="float32")),
        }
        for label, over in cases.items():
            with self.subTest(label):
                self.write_vectors(**over)
                with self.assertRaises(trace_refit.RefitError):
                    self.run_refit()
                self.assertFalse(self.out.exists())
        np.savez(self.vectors, ids=np.array(["c0", None], dtype=object), seed=self.vec["seed"])
        with self.assertRaises(trace_refit.RefitError):
            self.run_refit()


class FakeEncoder:
    instances = []

    def __init__(self, path, device="cpu", max_length=256):
        self.args = (str(path), device, max_length)
        self.batches = []
        FakeEncoder.instances.append(self)

    def encode(self, texts, batch_size=16):
        self.batches.append((len(texts), batch_size))
        out = np.zeros((len(texts), 4), dtype="float32")
        for i, t in enumerate(texts):
            h = hashlib.sha256(t.encode()).digest()
            out[i] = [1.0 + h[j] / 255.0 for j in range(4)]
        return out


@unittest.skipIf(np is None, "numpy not installed")
class PrepareTests(unittest.TestCase):
    write_index = RefitTests.write_index
    write_examples = RefitTests.write_examples
    write_vectors = RefitTests.write_vectors

    def setUp(self):
        RefitTests.setUp(self)
        FakeEncoder.instances = []
        self.enc_dir = self.tmp / "enc"
        self.enc_dir.mkdir()
        (self.enc_dir / "w.bin").write_bytes(b"weights")
        self.pins = {"w.bin": sha(b"weights")}
        self.write_encoder()
        (self.home / "research" / "project-map.json").write_text(json.dumps({"/work/proj": "proj"}))
        rows = [case(i, project="/Work/Proj/sub") for i in range(5)]
        rows.append(case(5, project="/other/place", input={"seed": "hi"}))  # one word: not eligible
        self.tape = self.home / "tape.sqlite"
        db = sqlite3.connect(str(self.tape))
        db.execute("CREATE TABLE ex (data TEXT)")
        db.executemany("INSERT INTO ex VALUES (?)", [(json.dumps(r),) for r in rows])
        db.commit()
        db.close()
        self.outcome_fence = []
        store = types.ModuleType("mr.trace_store")
        store.connect = lambda path: sqlite3.connect(str(path))
        store.make_examples = lambda db: [json.loads(r[0]) for r in db.execute("SELECT data FROM ex ORDER BY rowid")]
        build = types.ModuleType("mr.trace_build")
        build.project_identity = lambda path, aliases: next(
            (v for k, v in sorted(aliases.items(), key=lambda kv: -len(kv[0])) if str(path).casefold().startswith(k)),
            None if not path else "unmapped")
        build.git_catalog = lambda home: ({}, [{"project": "proj"}])
        build.nearby_outcomes = lambda ex, repos, through: self.outcome_fence.append(through) or [{"through": through}]
        patcher = patch.dict(sys.modules, {"mr.trace_store": store, "mr.trace_build": build})
        patcher.start()
        self.addCleanup(patcher.stop)
        enc = patch.object(trace_learning, "LocalEncoder", FakeEncoder)
        enc.start()
        self.addCleanup(enc.stop)

    def write_encoder(self, **over):
        enc = {"model": "toy", "revision": "r1", "path": str(self.enc_dir), "max_length": 64,
               "weights_sha256": self.pins, "device": "cpu", "dtype": "float32", "cache_schema": "trace-prefix-2"}
        enc.update(over)
        (self.home / "research" / "predictors" / "index.json").write_text(json.dumps(
            {"selected": "x", "encoder": enc, "methods": {"x": {"spec": {"kind": "seed"}, "temperature": 0.1}}}))

    def prep(self, **kw):
        return trace_refit.prepare_refit_data(self.home, **kw)

    def snapshot(self):
        skip = ("embedding-cache.sqlite", "refit-data-", "tape.sqlite-")
        return {k: v for k, v in tree_bytes(self.home).items() if not k.startswith(skip)}

    def test_receipt_and_seal_required_before_any_work(self):
        for name in ("test-results.json", "seal.json"):
            path = self.home / "research" / name
            data = path.read_bytes()
            path.unlink()
            with self.assertRaises(trace_refit.RefitError):
                self.prep()
            path.write_bytes(data)
        self.assertEqual(FakeEncoder.instances, [])
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(("refit-data-", "embedding"))], [])

    def test_whitespace_successors_are_counted_and_never_learned_as_preferences(self):
        db=sqlite3.connect(str(self.tape))
        for i, text in enumerate((" ","\n\t","\u2003"),100):
            row=case(i);row["target"]["text"]=text
            db.execute("INSERT INTO ex VALUES (?)",(json.dumps(row),))
        db.commit();db.close()
        examples,_,directory=self.prep()
        rows=[json.loads(line) for line in examples.read_text().splitlines()]
        self.assertTrue(all(row["target"]["text"].strip() for row in rows))
        self.assertEqual(json.loads((directory/"receipt.json").read_text())["counts"]["excluded_empty_targets"],3)

    def test_prepared_rows_are_aligned_train_only_and_canonical(self):
        before = self.snapshot()
        examples, vectors, data_dir = self.prep(log=lambda m: None)
        self.assertEqual(self.snapshot(), before)
        rows = [json.loads(line) for line in examples.read_text().splitlines()]
        self.assertEqual([r["id"] for r in rows], ["c0", "c1", "c2", "c3", "c4"])
        self.assertTrue(all(r["project"] == "proj" and r["input"]["project"] == "proj" for r in rows))
        self.assertTrue(all(r["partition"] == "train" and "reaction_input" not in r for r in rows))
        self.assertEqual(rows[0]["historical_response"], {"assistant_text": "ran export 0"})
        self.assertNotIn("drop me", examples.read_text())
        self.assertEqual(set(self.outcome_fence), {"2026-01-05T11:00:00Z"})
        self.assertEqual(rows[0]["historical_outcomes"], [{"through": "2026-01-05T11:00:00Z"}])
        with np.load(vectors, allow_pickle=False) as v:
            self.assertEqual(sorted(v.files), ["context", "ids", "seed", "target"])
            self.assertEqual(v["ids"].tolist(), [r["id"] for r in rows])
            self.assertEqual(v["seed"].shape, (5, 4))
            expected = FakeEncoder(self.enc_dir, "cpu", 64).encode([rows[2]["input"]["seed"]])[0]
            np.testing.assert_array_equal(v["seed"][2], expected)
        receipt = json.loads((data_dir / "receipt.json").read_text())
        self.assertEqual(receipt["counts"]["eligible"], 5)
        self.assertEqual(receipt["counts"]["tape_examples"], 6)
        self.assertIn("post-test", receipt["phase"])
        self.assertTrue(data_dir.name.startswith("refit-data-") and data_dir.parent == self.home)
        self.assertEqual(FakeEncoder.instances[0].args, (str(self.enc_dir), "cpu", 64))
        self.assertTrue(all(size <= 256 and batch == 16 for inst in FakeEncoder.instances for size, batch in inst.batches))

    def test_cache_identity_and_reuse(self):
        self.prep()
        db = sqlite3.connect(str(self.home / "embedding-cache.sqlite"))
        ident = json.dumps({"model": "toy", "revision": "r1", "max_length": 64, "weights_sha256": self.pins,
                            "device": "cpu", "dtype": "float32", "cache_schema": "trace-prefix-2"}, sort_keys=True)
        key = sha((ident + "|" + "please fix export number 0 now").encode())
        row = db.execute("SELECT dim,length(vector) FROM vectors WHERE id=?", (key,)).fetchone()
        db.close()
        self.assertEqual(row, (4, 16))
        FakeEncoder.instances = []
        _, _, second = self.prep()
        self.assertEqual(FakeEncoder.instances, [])
        self.assertEqual(json.loads((second / "receipt.json").read_text())["counts"]["newly_encoded_texts"], 0)
        self.write_encoder(max_length=65)
        self.prep()
        self.assertEqual(len(FakeEncoder.instances), 1)

    def test_unique_dirs_and_refit_consumes_output(self):
        a, b = self.prep(), self.prep()
        self.assertNotEqual(a[2], b[2])
        manifest = trace_refit.refit_candidate(self.home, a[0], a[1], self.out)
        self.assertEqual(manifest["case_count"], 5)
        self.assertEqual(load_cases(self.out)[0], manifest)
        split = json.loads((self.out / "split.json").read_text())
        self.assertEqual(split["former_holdout_or_new_ids"], ["c3", "c4"])

    def test_encoder_pins_must_match_every_file(self):
        (self.enc_dir / "extra.bin").write_bytes(b"x")
        with self.assertRaises(trace_refit.RefitError):
            self.prep()
        (self.enc_dir / "extra.bin").unlink()
        (self.enc_dir / "w.bin").write_bytes(b"tampered")
        with self.assertRaises(trace_refit.RefitError):
            self.prep()
        self.assertEqual(FakeEncoder.instances, [])

    def test_bad_clock_and_missing_tape_refuse(self):
        with self.assertRaises(trace_refit.RefitError):
            self.prep(tape=self.tmp / "absent.sqlite")
        db = sqlite3.connect(str(self.tape))
        bad = case(9, seed_time="2026-03-01T00:00:00Z", target_time="2026-01-01T00:00:00Z")
        db.execute("INSERT INTO ex VALUES (?)", (json.dumps(bad),))
        db.commit()
        db.close()
        with self.assertRaises(trace_refit.RefitError):
            self.prep()
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith("refit-data-")], [])


if __name__ == "__main__":
    unittest.main()
