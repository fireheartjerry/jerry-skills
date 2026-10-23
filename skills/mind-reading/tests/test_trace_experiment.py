"""Validation-selection-then-sealed-test experiment on a small fictional cohort (12 families per partition, dim 4)."""
import builtins
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts scripts on sys.path)

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

if np is not None:
    from mr import trace_experiment as ex
    from mr import trace_forecast as tf
    from mr import trace_learning as tl
    from mr import trace_runtime as rt

N, DIM = 12, 4
SECRET = "SECRET-FUTURE-TEXT"
TRAIN_END = "2025-02-01T00:00:00Z"
DAY = 86400


def _iso(base, i):
    from datetime import datetime, timedelta, timezone
    return (datetime(2025, base, 3, tzinfo=timezone.utc) + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(home, mutate=None):
    """Write a fictional research dir. `mutate(rows, arrays, split)` may break any piece before it is written."""
    research = Path(home) / "research"
    research.mkdir(parents=True)
    rng = np.random.default_rng(7)
    mix = rng.normal(size=(DIM, DIM))
    rows, arrays = {}, {}
    for part, month in (("train", 1), ("validation", 3), ("test", 5)):
        part_rows = []
        for i in range(N):
            tag = "%s%d" % (part[:2], i)
            part_rows.append({
                "id": "%s-%d" % (part, i), "family": "fam-%s-%d" % (part, i), "session": "sess-%s-%d" % (part, i),
                "source": "claude-code" if i % 2 else "codex", "project": "p%d" % (i % 3), "origin": "human",
                "weight": 1, "partition": part,
                "source_refs": {"seed": "ref-seed-%s" % tag, "target": "ref-target-%s" % tag, "prior_user_turns": []},
                "evidence_keys": {"seed": "original-seed-%s" % tag, "target": "original-target-%s" % tag, "prior_user_turns": []},
                "input": {"seed": "please check widget%s now" % tag, "prior_user_turns": ["earlier note%s" % tag],
                          "prior_agent_text": "", "prior_tools": []},
                "target": {"text": ("%s " % SECRET if part == "test" else "") + "reply%s unique" % tag,
                           "source_ref": "ref-target-%s" % tag, "time": _iso(month, i)},
                "seed_time": _iso(month, i), "target_time": _iso(month, i),
                "reaction_input": {"text": "reaction%s" % tag}})
        seed = rng.normal(size=(N, DIM))
        arrays[part] = {"ids": np.array([r["id"] for r in part_rows]), "seed": seed,
                        "context": seed + 0.1 * rng.normal(size=(N, DIM)),
                        "target": seed @ mix + 0.1 * rng.normal(size=(N, DIM)),
                        "reaction": rng.normal(size=(N, DIM))}
        rows[part] = part_rows
    split = {"train_ids": [r["id"] for r in rows["train"]], "validation_ids": [r["id"] for r in rows["validation"]],
             "test_ids": [r["id"] for r in rows["test"]], "cutoffs": {"train_end": TRAIN_END}}
    if mutate:
        mutate(rows, arrays, split)
    for part in rows:
        (research / (part + ".jsonl")).write_text("\n".join(json.dumps(r) for r in rows[part]) + "\n")
        np.savez(research / (part + ".npz"), **arrays[part])
    (research / "split.json").write_text(json.dumps(split))
    (research / "encoder.json").write_text(json.dumps(
        {"model": "fictional-encoder", "revision": "r1", "path": "/nowhere/encoder", "max_length": 128}))
    (research / "protocol.json").write_text(json.dumps({"task": "next human turn", "fictional": True}))
    return research


def deny_test_open():
    real = builtins.open

    def guarded(file, *a, **k):
        if os.path.basename(os.fspath(file)).startswith("test."):
            raise AssertionError("opened %s" % file)
        return real(file, *a, **k)
    return mock.patch("builtins.open", guarded)


@unittest.skipIf(np is None, "numpy not installed")
class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.home = helpers.tmpdir("mr-exp-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def trained(self, **kw):
        research = build(self.home, kw.pop("mutate", None))
        return research, ex.train_experiment(self.home, adapters=False, **kw)

    def test_interrupted_test_look_blocks_refit_and_reencoding(self):
        from mr.trace_build import encode_partitions
        research=build(self.home)
        (research/"evaluation-started.json").write_text("{}")
        with self.assertRaises(ex.ExperimentError): ex.train_experiment(self.home,adapters=False)
        with self.assertRaises(ex.ExperimentError): ex.seal_experiment(self.home)
        with self.assertRaises(ValueError): encode_partitions(self.home,{"path":"missing"})

    def test_training_lock_is_exclusive_and_not_removed_by_a_second_run(self):
        research=build(self.home)
        (research/"training.lock").write_text("another run")
        with self.assertRaises(ex.ExperimentError): ex.train_experiment(self.home,adapters=False)
        self.assertEqual((research/"training.lock").read_text(),"another run")
        from mr.trace_build import encode_partitions
        with self.assertRaises(ValueError): encode_partitions(self.home,{"path":"missing"})

    def test_seal_refuses_reencoded_features_or_metadata_after_training(self):
        research,_=self.trained()
        path=research/"encoder.json";encoder=json.loads(path.read_text());encoder["dtype"]="different";path.write_text(json.dumps(encoder))
        with self.assertRaisesRegex(ex.ExperimentError,"changed after fitting"): ex.seal_experiment(self.home)

    def test_seal_rejects_mix_of_training_runs(self):
        research,_=self.trained()
        path=self.home/"candidate"/"manifest.json"
        manifest=json.loads(path.read_text());manifest["run_id"]="other-training-run";path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ex.ExperimentError,"one training run"): ex.seal_experiment(self.home)

    def test_seal_checks_deployed_arrays_even_after_their_digest_is_updated(self):
        build(self.home)
        with mock.patch.object(ex,"select_methods",return_value=("ridge-a1-b0","context")):
            ex.train_experiment(self.home,adapters=False)
        path=self.home/"candidate"/"predictor.npz"
        with np.load(path,allow_pickle=False) as data: arrays={k:data[k].copy() for k in data.files}
        arrays["weight"][0,0]+=1;np.savez(path,**arrays)
        manifest_path=self.home/"candidate"/"manifest.json"
        manifest=json.loads(manifest_path.read_text());manifest["files"]["predictor.npz"]=ex._sha(path.read_bytes());manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ex.ExperimentError,"deployed arrays"): ex.seal_experiment(self.home)

    def test_end_to_end_with_sealed_single_look(self):
        research, summary = self.trained()
        self.assertEqual(summary["task"]["candidate_count"], 8)
        self.assertEqual(sum(summary["task"]["strata"].values()), N)
        self.assertNotIn(summary["selected"], ("prior", "recent-prior", "lexical-k8"))
        self.assertFalse(summary["selected"].startswith(("control", "diagnostic")))
        self.assertTrue((research / "predictors" / "index.json").is_file())
        seal = ex.seal_experiment(self.home)
        self.assertEqual(ex.seal_experiment(self.home), seal)
        out = ex.evaluate_experiment(self.home)
        self.assertEqual(out["selected"], summary["selected"])
        self.assertEqual(set(out["table"]), set(summary["table"]))
        receipt = json.loads((research / "test-results.json").read_text())
        self.assertEqual(receipt["seal_sha256"], ex._sha((research / "seal.json").read_bytes()))
        self.assertTrue((research / "test-scores.npz").is_file())
        with self.assertRaises(ex.ExperimentError):
            ex.evaluate_experiment(self.home)
        with self.assertRaises(ex.ExperimentError):
            ex.train_experiment(self.home, adapters=False)

    def test_train_and_seal_never_open_test_files(self):
        research = build(self.home)
        with deny_test_open():
            ex.train_experiment(self.home, adapters=False)
            ex.seal_experiment(self.home)
        for path in list((research / "predictors").iterdir()) + list((self.home / "candidate").iterdir()) + [
                research / "validation-results.json", research / "seal.json"]:
            self.assertNotIn(SECRET.encode(), path.read_bytes(), path.name)

    def test_candidate_is_train_only_and_loader_accepts_it(self):
        research, summary = self.trained()
        cand = self.home / "candidate"
        manifest, cases = rt.load_cases(cand)
        self.assertEqual({c["id"] for c in cases}, {"train-%d" % i for i in range(N)})
        self.assertTrue(all("reaction_input" not in c for c in cases))
        self.assertEqual((cand / "split.json").read_bytes(), (research / "split.json").read_bytes())
        self.assertEqual(manifest["version"], "trace-candidate-1")
        self.assertEqual(manifest["trained_through"], TRAIN_END)
        z = np.load(cand / "train-vectors.npz", allow_pickle=False)
        self.assertEqual(sorted(z.files), ["context", "ids", "seed", "target"])
        self.assertEqual(manifest["predictor"]["calibration_temperature"],
                         json.loads((research / "validation-results.json").read_text())["methods"][
                             summary["selected"]]["temperature"])
        joined = b"".join(p.read_bytes() for p in cand.iterdir())
        for part in ("validation", "test"):
            self.assertNotIn(("reply%s0 unique" % part[:2]).encode(), joined)

    def test_all_methods_share_one_candidate_pool(self):
        build(self.home)
        seen = []
        real = tf.rank_candidates

        def spy(pred, cand):
            seen.append(ex._sha(np.ascontiguousarray(cand).tobytes()))
            return real(pred, cand)
        with mock.patch.object(tf, "rank_candidates", spy):
            ex.train_experiment(self.home, adapters=False)
        self.assertEqual(len(seen), len(ex.method_specs(False)))
        self.assertEqual(len(set(seen)), 1)
        res = json.loads((self.home / "research" / "validation-results.json").read_text())
        self.assertEqual({m["n"] for m in res["methods"].values()}, {N})
        for m in res["methods"].values():
            self.assertEqual(sum(v["n"] for v in m["by_stratum"].values()), N)
            self.assertIn("by_source", m)
            self.assertIn("by_project", m)

    def test_adapters_are_fit_on_train_only(self):
        build(self.home)
        calls = []

        def fake_fit(x, y, families, rank=32, steps=300, seed=42, learning_rate=0.002):
            calls.append((np.asarray(x).copy(), list(families), rank, steps, seed))
            rng = np.random.default_rng(rank)
            return {"down_weight": rng.normal(size=(rank, DIM)).astype("float32"),
                    "down_bias": np.zeros(rank, "float32"), "up_weight": rng.normal(size=(DIM, rank)).astype("float32"),
                    "up_bias": np.zeros(DIM, "float32"), "rank": rank, "steps": steps, "seed": seed}
        with mock.patch.object(tl, "fit_adapter", fake_fit):
            summary = ex.train_experiment(self.home, adapters=True)
        self.assertEqual([c[2:] for c in calls], [(16, 300, 42), (64, 300, 42)])
        self.assertTrue(all(len(c[1]) == N and all(f.startswith("fam-train") for f in c[1]) for c in calls))
        with np.load(self.home / "research" / "train.npz", allow_pickle=False) as data:
            self.assertTrue(all(np.allclose(c[0], data["context"].astype("float32")) for c in calls))
        self.assertIn("adapter-r64-b0.25", summary["table"])

    def test_seal_detects_tamper_and_evaluation_refuses(self):
        research, _ = self.trained()
        ex.seal_experiment(self.home)
        target = next((research / "predictors").glob("ridge-a10.npz"))
        original = target.read_bytes()
        target.write_bytes(original + b"x")
        with self.assertRaises(ex.ExperimentError):
            ex.evaluate_experiment(self.home)
        with self.assertRaises(ex.ExperimentError):
            ex.seal_experiment(self.home)
        self.assertFalse((research / "test-results.json").exists())
        target.write_bytes(original)
        cases = self.home / "candidate" / "cases.jsonl"
        cases.write_bytes(cases.read_bytes() + b"\n")
        with self.assertRaises(ex.ExperimentError):
            ex.evaluate_experiment(self.home)
        cases.write_bytes(cases.read_bytes().rstrip(b"\n") + b"\n")
        self.assertFalse((research / "test-results.json").exists())
        cases.write_bytes(cases.read_bytes().rstrip(b"\n") + b"\n")

    def test_code_change_after_seal_refuses_test(self):
        research, _ = self.trained()
        ex.seal_experiment(self.home)
        changed = dict(ex._code_hashes(), trace_experiment="0" * 64)
        with mock.patch.object(ex, "_code_hashes", lambda: changed):
            with self.assertRaises(ex.ExperimentError):
                ex.evaluate_experiment(self.home)
        self.assertFalse((research / "test-results.json").exists())

    def test_evaluation_makes_no_fit_calls(self):
        self.trained()
        ex.seal_experiment(self.home)
        boom = mock.Mock(side_effect=AssertionError("fit during evaluation"))
        with mock.patch.object(tf, "fit_ridge", boom), mock.patch.object(tl, "fit_adapter", boom):
            ex.evaluate_experiment(self.home)
        boom.assert_not_called()

    def test_train_refused_after_seal(self):
        self.trained()
        ex.seal_experiment(self.home)
        with self.assertRaises(ex.ExperimentError):
            ex.train_experiment(self.home, adapters=False)

    def test_insufficient_candidates_error_without_shrinking(self):
        build(self.home)
        with self.assertRaisesRegex(ex.ExperimentError, "cannot build 20"):
            ex.train_experiment(self.home, adapters=False, candidate_count=20)
        self.assertFalse((self.home / "candidate").exists())
        self.assertFalse((self.home / "research" / ".predictors-tmp").exists())

    def test_rejections(self):
        def set_row(part, i, **kw):
            def f(rows, arrays, split):
                for k, v in kw.items():
                    rows[part][i][k] = v
            return f

        def inp(part, i, **kw):
            def f(rows, arrays, split):
                rows[part][i]["input"].update(kw)
            return f

        def dup_id(rows, arrays, split):
            rows["train"][1]["id"] = "train-0"

        def shared_family(rows, arrays, split):
            rows["validation"][0]["family"] = rows["train"][0]["family"]

        def shared_ref(rows, arrays, split):
            rows["validation"][0]["source_refs"]["seed"] = "ref-seed-tr0"

        def shared_evidence(rows, arrays, split):
            rows["train"][0]["evidence_key"] = "ev-1"
            rows["validation"][0]["evidence_key"] = "ev-1"

        def misaligned(rows, arrays, split):
            arrays["validation"]["ids"] = arrays["validation"]["ids"][::-1].copy()

        def nonfinite(rows, arrays, split):
            arrays["train"]["context"][2, 1] = np.nan

        def future_end(rows, arrays, split):
            split["cutoffs"]["train_end"] = "2999-01-01T00:00:00Z"

        def late_train(rows, arrays, split):
            rows["train"][0]["target_time"] = "2025-02-10T00:00:00Z"

        def early_validation(rows, arrays, split):
            rows["validation"][0].update(seed_time="2025-01-05T00:00:00Z", target_time="2025-01-06T00:00:00Z")

        def split_mismatch(rows, arrays, split):
            split["train_ids"] = split["train_ids"][:-1] + ["train-other"]

        def object_ids(rows, arrays, split):
            arrays["train"]["ids"] = np.array(list(arrays["train"]["ids"]), dtype=object)

        cases = {
            "nonhuman": set_row("train", 0, origin="assistant"), "weight": set_row("train", 0, weight=2),
            "duplicate id": dup_id, "family overlap": shared_family, "ref overlap": shared_ref,
            "evidence overlap": shared_evidence, "partition mismatch": set_row("validation", 0, partition="train"),
            "bad clock": set_row("train", 0, seed_time="2025-01-20T00:00:00Z", target_time="2025-01-10T00:00:00Z"),
            "misaligned vectors": misaligned, "nonfinite": nonfinite, "future train end": future_end,
            "train after cutoff": late_train, "validation inside train period": early_validation,
            "split mismatch": split_mismatch, "post_action in input": inp("train", 0, post_action={"x": 1}),
            "short seed": inp("train", 0, seed="hello"), "long seed": inp("train", 0, seed="w " * 61),
            "object ids": object_ids}
        for label, mutate in cases.items():
            with self.subTest(label):
                home = helpers.tmpdir("mr-exp-bad-")
                self.addCleanup(shutil.rmtree, home, ignore_errors=True)
                build(home, mutate)
                with self.assertRaises(ex.ExperimentError):
                    ex.train_experiment(home, adapters=False)
                self.assertFalse((home / "candidate").exists())

    def test_test_partition_overlap_is_rejected_at_evaluation_without_a_receipt(self):
        def overlap(rows, arrays, split):
            rows["test"][0]["family"] = rows["train"][0]["family"]
        research, _ = self.trained(mutate=overlap)
        ex.seal_experiment(self.home)
        with self.assertRaisesRegex(ex.ExperimentError, "share a family"):
            ex.evaluate_experiment(self.home)
        self.assertFalse((research / "test-results.json").exists())

    def test_runtime_loader_needs_frozen_split_and_exact_hashes(self):
        self.trained()
        cand = self.home / "candidate"
        rt.load_cases(cand)
        split = cand / "split.json"
        raw = split.read_bytes()
        split.write_bytes(raw.replace(b"train-0", b"train-x", 1))
        with self.assertRaises(rt.TraceFormatError):
            rt.load_cases(cand)
        split.write_bytes(raw)
        split.unlink()
        with self.assertRaises(rt.TraceFormatError):
            rt.load_cases(cand)
        split.write_bytes(raw)
        (cand / "cases.jsonl").write_bytes((cand / "cases.jsonl").read_bytes() + b"\n")
        with self.assertRaises(rt.TraceFormatError):
            rt.load_cases(cand)

    def test_selection_order_and_ineligible_methods(self):
        specs = ex.method_specs(True)
        res = {s["name"]: {"family_macro_top1": 0.1, "family_macro_mrr": 0.2} for s in specs}
        res["prior"] = {"family_macro_top1": 0.9, "family_macro_mrr": 0.9}
        res["control-shuffled-ridge-a10"] = {"family_macro_top1": 0.95, "family_macro_mrr": 0.9}
        res["diagnostic-reaction-ridge-a10"] = {"family_macro_top1": 0.99, "family_macro_mrr": 0.9}
        res["semantic-k4"] = {"family_macro_top1": 0.5, "family_macro_mrr": 0.5}
        res["ridge-a1-b0"] = {"family_macro_top1": 0.5, "family_macro_mrr": 0.6}
        res["adapter-r16-b0"] = {"family_macro_top1": 0.5, "family_macro_mrr": 0.6}
        self.assertEqual(ex.select_methods(res, specs), ("adapter-r16-b0", "prior"))
        res["adapter-r16-b0"]["family_macro_mrr"] = 0.55
        self.assertEqual(ex.select_methods(res, specs)[0], "ridge-a1-b0")

    def test_negative_control_and_reaction_use_expected_fits(self):
        build(self.home)
        real, calls = tf.fit_ridge, []

        def spy(X, Y, alpha=10.0):
            calls.append((np.asarray(X).copy(), np.asarray(Y).copy(), alpha))
            return real(X, Y, alpha)
        with mock.patch.object(tf, "fit_ridge", spy):
            ex.train_experiment(self.home, adapters=False)
        self.assertEqual([c[2] for c in calls], [0.1, 1.0, 10.0, 100.0, 10.0, 10.0, 10.0])
        base, control, seed_ablation, reaction = calls[2], calls[4], calls[5], calls[6]
        self.assertTrue(np.array_equal(base[0], control[0]))
        self.assertFalse(np.array_equal(base[1], control[1]))
        self.assertTrue(np.allclose(np.sort(base[1], axis=0), np.sort(control[1], axis=0)))
        self.assertFalse(np.array_equal(base[0], reaction[0]))
        self.assertFalse(np.array_equal(base[0], seed_ablation[0]))
        with np.load(self.home / "research" / "train.npz", allow_pickle=False) as data:
            self.assertTrue(np.allclose(base[0], data["context"].astype("float32")))
            self.assertTrue(np.allclose(seed_ablation[0], data["seed"].astype("float32")))


if __name__ == "__main__":
    unittest.main()
