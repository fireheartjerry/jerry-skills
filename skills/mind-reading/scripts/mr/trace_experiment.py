"""Validation-selection-then-sealed-test experiment: forecast the embedding of the actual next HUMAN turn.

This measures prediction of what a person wrote next after a 2..60 word human seed. It is not endorsement: a close
forecast or a near semantic match says what the person tended to write, never that it was right or wanted.

Protocol (every choice fixed before the test partition is read):
  train_experiment   reads only research/{train,validation}.{jsonl,npz}, split.json, protocol.json, encoder.json.
                     Fits fixed methods on train, calibrates a temperature per method on validation, selects the
                     deployable primary on validation family-macro top1 (then family-macro MRR, then name), freezes the
                     best baseline, and materializes home/candidate/ from TRAIN cases only.
  seal_experiment    hashes every frozen artifact into research/seal.json without touching any test file.
  evaluate_experiment  the only reader of test.jsonl/test.npz: verifies the seal, runs the frozen predictors without any
                     fit call, and writes research/test-results.json exactly once.
"""
import hashlib
import io
import json
import os
import shutil
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import trace_forecast as tf
from . import trace_learning as tl
from . import trace_runtime as rt

VERSION = "trace-experiment-1"
CANDIDATE_VERSION = rt.VERSION
CODE_MODULES = ("trace_experiment", "trace_learning", "trace_forecast", "trace_runtime", "trace_cases", "trace_preferences", "trace_rerank", "trace_build", "trace_ingest", "trace_store", "trace_git", "trace_grok")
TRAIN_READS = frozenset(("train.jsonl", "validation.jsonl", "train.npz", "validation.npz", "split.json",
                         "protocol.json", "encoder.json"))
SEAL, RESULTS, VALIDATION, INDEX = "seal.json", "test-results.json", "validation-results.json", "index.json"
TEST_SCORES = "test-scores.npz"
RIDGE_ALPHAS = (0.1, 1.0, 10.0, 100.0)
ADAPTER_RANKS = (16, 64)
BLENDS = (0.0, 0.25, 0.5)
SEMANTIC_KS = (1, 4, 8, 16)
SEMANTIC_TEMPERATURE = 0.1
LEXICAL_K = 8
RECENT = 64
ADAPTER_STEPS, ADAPTER_SEED = 300, 42
CONTROL_SEED = 73
BOOTSTRAP_SEED, BOOTSTRAP_DRAWS = 42, 2000
RETRIEVAL_FUTURE_WEIGHT = 0.25
SEED_WORDS = (2, 60)
INTERPRETATION = ("Prediction of the actual next human turn, not endorsement: a close forecast or near semantic match "
                  "is evidence of what the person tended to write, never that it was right or wanted.")
ELIGIBILITY = {
    "primary_eligible": "seed, context, semantic retrieval, ridge and adapter: these have deployable runtime kinds",
    "baselines": "seed, context, prior, recent-prior, lexical retrieval, semantic retrieval",
    "baseline_only": "prior, recent-prior and lexical retrieval have no runtime predictor kind, so they never become "
                     "the candidate even if they win",
    "never_selected": "the shuffled-target negative control and the reaction-input diagnostic",
}


class ExperimentError(ValueError):
    pass


# ---------------------------------------------------------------- io

def _read_bytes(path):
    with open(os.fspath(path), "rb") as handle:
        return handle.read()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(obj):
    return (json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _no_constant(name):
    raise ExperimentError("non-finite JSON number %s" % name)


def _loads(data, what):
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExperimentError("%s is not valid UTF-8 JSON" % what) from None


def _write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(os.fspath(path)))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _write_exclusive(path, data):
    """Atomic create-if-absent: the final name either holds the complete file or does not exist."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(os.fspath(path)))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise ExperimentError("%s already exists and is immutable" % os.path.basename(path)) from None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _npz_bytes(arrays):
    import numpy as np
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def _load_npz(data):
    import numpy as np
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            return {k: z[k] for k in z.files}
    except (ValueError, OSError) as exc:
        raise ExperimentError("unreadable npz: %s" % exc) from None


def _code_hashes():
    here = Path(__file__).resolve().parent
    return {m: (_sha(_read_bytes(here / (m + ".py"))) if (here / (m + ".py")).is_file() else "absent")
            for m in CODE_MODULES}


def _native(obj):
    import numpy as np
    if isinstance(obj, dict):
        return {str(k): _native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_native(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return _native(obj.tolist())
    return obj


# ---------------------------------------------------------------- validation of inputs

def _ts(value, what):
    try:
        return rt._ts(value, what)
    except rt.TraceFormatError as exc:
        raise ExperimentError(str(exc)) from None


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _load_split(data):
    split = _loads(data, "split.json")
    if not isinstance(split, dict):
        raise ExperimentError("split.json must be an object")
    seen = {}
    for key in ("train_ids", "validation_ids", "test_ids", "excluded_ids"):
        values = split.get(key, [] if key == "excluded_ids" else None)
        if not isinstance(values, list) or not all(_nonempty(i) for i in values) or len(set(values)) != len(values):
            raise ExperimentError("split.%s must be a list of unique non-empty ids" % key)
        for i in values:
            if i in seen:
                raise ExperimentError("split id %r is in both %s and %s" % (i, seen[i], key))
            seen[i] = key
    cutoffs = split.get("cutoffs")
    if not isinstance(cutoffs, dict) or "train_end" not in cutoffs:
        raise ExperimentError("split.cutoffs.train_end is required")
    cutoff = _ts(cutoffs["train_end"], "split.cutoffs.train_end")
    if cutoff > datetime.now(timezone.utc).timestamp():
        raise ExperimentError("split.cutoffs.train_end is in the future")
    return split, cutoffs["train_end"], cutoff


def _load_encoder(data):
    enc = _loads(data, "encoder.json")
    if not isinstance(enc, dict) or not all(_nonempty(enc.get(k)) for k in ("model", "revision", "path")):
        raise ExperimentError("encoder.json needs model, revision and path")
    if type(enc.get("max_length")) is not int or enc["max_length"] < 1:
        raise ExperimentError("encoder.json max_length must be a positive integer")
    return {k: enc[k] for k in ("model", "revision", "path", "max_length", "weights_sha256", "license", "dtype", "device", "cache_schema") if k in enc}


def _scan_keys(obj, key):
    if isinstance(obj, dict):
        return key in obj or any(_scan_keys(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_scan_keys(v, key) for v in obj)
    return False


def _ref_keys(case):
    refs = case.get("source_refs")
    keys = set()
    if isinstance(refs, dict):
        for value in refs.values():
            keys.update(value if isinstance(value, list) else [value])
    elif isinstance(refs, list):
        keys.update(refs)
    target = case.get("target")
    if isinstance(target, dict):
        keys.add(target.get("source_ref"))
    def add_evidence(value):
        if isinstance(value, dict):
            for item in value.values(): add_evidence(item)
        elif isinstance(value, list):
            for item in value: add_evidence(item)
        elif isinstance(value, str):
            keys.add(value)
    for name in ("evidence_key", "evidence_keys"):
        add_evidence(case.get(name))
    return {k for k in keys if isinstance(k, str) and k}


def _parse_rows(data, name):
    rows = []
    for number, line in enumerate(data.split(b"\n"), 1):
        if line.strip():
            rows.append(_loads(line, "%s line %d" % (name, number)))
    if not rows:
        raise ExperimentError("%s has no rows" % name)
    return rows


def _check_rows(rows, partition, name, split_ids, train_end):
    seen = set()
    for number, case in enumerate(rows, 1):
        def bad(msg, number=number):
            raise ExperimentError("%s row %d: %s" % (name, number, msg))
        if not isinstance(case, dict):
            bad("row must be an object")
        for key in ("id", "family", "source", "session"):
            if not _nonempty(case.get(key)):
                bad("%s must be a non-empty string" % key)
        if case["id"] in seen:
            bad("duplicate id %r" % case["id"])
        seen.add(case["id"])
        if case.get("partition") != partition:
            bad("partition is %r, expected %r" % (case.get("partition"), partition))
        if case.get("origin") != "human":
            bad("origin must be 'human'")
        weight = case.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight != 1:
            bad("weight must be 1")
        inp, target = case.get("input"), case.get("target")
        if not isinstance(inp, dict) or not isinstance(inp.get("seed"), str):
            bad("input.seed must be a string")
        if _scan_keys(inp, "post_action") or _scan_keys(inp, "reaction_input"):
            bad("input must be a causal prefix: no post_action or reaction_input")
        if not isinstance(target, dict) or not _nonempty(target.get("text")):
            bad("target.text must be a non-empty string")
        if not SEED_WORDS[0] <= len(inp["seed"].split()) <= SEED_WORDS[1]:
            bad("seed must have %d..%d words" % SEED_WORDS)
        if not _ref_keys(case):
            bad("no source reference or evidence key")
        seed_time, target_time = _ts(case.get("seed_time"), "seed_time"), _ts(case.get("target_time"), "target_time")
        if seed_time > target_time:
            bad("seed_time is after target_time")
        if partition == "train" and target_time > train_end:
            bad("target_time is after split.cutoffs.train_end")
        if partition != "train" and target_time <= train_end:
            bad("target_time is not after split.cutoffs.train_end")
    if seen != set(split_ids):
        raise ExperimentError("%s ids do not equal the split's %s ids" % (name, partition))


def _check_separate(a, b, label_a, label_b):
    if {r["family"] for r in a} & {r["family"] for r in b}:
        raise ExperimentError("%s and %s share a family" % (label_a, label_b))
    if {r["id"] for r in a} & {r["id"] for r in b}:
        raise ExperimentError("%s and %s share an id" % (label_a, label_b))
    refs_a = set().union(*[_ref_keys(r) for r in a])
    if any(_ref_keys(r) & refs_a for r in b):
        raise ExperimentError("%s and %s share a source reference or evidence key" % (label_a, label_b))


def _load_vectors(data, rows, name):
    import numpy as np
    z = _load_npz(data)
    for key in ("ids", "seed", "context", "target", "reaction"):
        if key not in z:
            raise ExperimentError("%s is missing %s" % (name, key))
    ids = z["ids"]
    if ids.ndim != 1 or ids.dtype.kind != "U":
        raise ExperimentError("%s ids must be a 1D string array" % name)
    if [str(i) for i in ids] != [r["id"] for r in rows]:
        raise ExperimentError("%s vector ids are not aligned with the examples" % name)
    out = {"ids": [str(i) for i in ids]}
    for key in ("seed", "context", "target", "reaction"):
        a = z[key]
        if a.dtype.kind != "f" or a.ndim != 2 or a.shape[0] != len(rows):
            raise ExperimentError("%s %s must be a float matrix with one row per example" % (name, key))
        if not np.isfinite(a).all():
            raise ExperimentError("%s %s has non-finite values" % (name, key))
        out[key] = a.astype(np.float32)
    dims = {out[k].shape[1] for k in ("seed", "context", "target", "reaction")}
    if len(dims) != 1:
        raise ExperimentError("%s vector dimensions disagree" % name)
    return out


def _load_partition(research, partition, split, train_end, with_vectors=True):
    stem = {"train": "train", "validation": "validation", "test": "test"}[partition]
    rows = _parse_rows(_read_bytes(research / (stem + ".jsonl")), stem + ".jsonl")
    _check_rows(rows, partition, stem + ".jsonl", split[partition + "_ids"], train_end)
    vec = _load_vectors(_read_bytes(research / (stem + ".npz")), rows, stem + ".npz") if with_vectors else None
    return rows, vec


# ---------------------------------------------------------------- methods

def method_specs(adapters):
    specs = [dict(name="seed", kind="seed", baseline=True, primary=True),
             dict(name="context", kind="context", baseline=True, primary=True),
             dict(name="prior", kind="prior", baseline=True, primary=False),
             dict(name="recent-prior", kind="recent_prior", baseline=True, primary=False),
             dict(name="lexical-k%d" % LEXICAL_K, kind="lexical", k=LEXICAL_K, baseline=True, primary=False)]
    for k in SEMANTIC_KS:
        specs.append(dict(name="semantic-k%d" % k, kind="semantic", k=k, temperature_retrieval=SEMANTIC_TEMPERATURE,
                          baseline=True, primary=True))
    for alpha in RIDGE_ALPHAS:
        for blend in BLENDS:
            specs.append(dict(name="ridge-a%g-b%g" % (alpha, blend), kind="ridge", model="ridge-a%g" % alpha,
                              alpha=alpha, context_blend=blend, baseline=False, primary=True))
    if adapters:
        for rank in ADAPTER_RANKS:
            for blend in BLENDS:
                specs.append(dict(name="adapter-r%d-b%g" % (rank, blend), kind="adapter", model="adapter-r%d" % rank,
                                  rank=rank, steps=ADAPTER_STEPS, seed=ADAPTER_SEED, context_blend=blend,
                                  baseline=False, primary=True))
    specs.append(dict(name="control-shuffled-ridge-a10", kind="control", model="control-ridge-a10", alpha=10.0,
                      context_blend=0.0, baseline=False, primary=False))
    specs.append(dict(name="diagnostic-reaction-ridge-a10", kind="reaction", model="reaction-ridge-a10", alpha=10.0,
                      context_blend=0.0, baseline=False, primary=False))
    specs.append(dict(name="diagnostic-seed-ridge-a10", kind="seed_ridge", model="seed-ridge-a10", alpha=10.0,
                      context_blend=0.0, baseline=False, primary=False))
    return specs


def _blend(pred, context, weight):
    if not weight:
        return pred
    return tf.normalize((1.0 - weight) * pred + weight * tf.normalize(context))


def _lexical_index(inputs):
    return [(rt._tokens(rt.render_input(i, True)), rt._tokens(rt.render_input(i))) for i in inputs]


def _lexical_predict(inputs, index, train_y, prior_mean, k):
    import numpy as np
    seed_docs, full_docs = [s for s, _ in index], [f for _, f in index]
    out = np.zeros((len(inputs), train_y.shape[1]), dtype=np.float64)
    for r, inp in enumerate(inputs):
        query = rt._context_input(inp.get("seed"), inp)
        s = rt._bm25(rt._tokens(rt.render_input(query, True)), seed_docs)
        f = rt._bm25(rt._tokens(rt.render_input(query)), full_docs)
        score = np.asarray([0.65 * a + 0.35 * b for a, b in zip(s, f)])
        order = np.argsort(-score, kind="stable")[:k]
        w = score[order]
        out[r] = (w @ train_y[order]) if w.sum() > 0 else prior_mean  # no lexical overlap: fall back to the prior
    return tf.normalize(out)


def _predict(spec, ctx, data):
    import numpy as np
    kind = spec["kind"]
    if kind == "seed":
        return tf.normalize(data["seed"])
    if kind == "context":
        return tf.normalize(data["context"])
    n, d = data["seed"].shape
    if kind == "prior":
        return np.tile(tf.normalize(ctx["prior_mean"]), (n, 1))
    if kind == "recent_prior":
        return np.tile(tf.normalize(ctx["recent_mean"]), (n, 1))
    if kind == "lexical":
        if "lexical" not in ctx:
            ctx["lexical"] = _lexical_index(ctx["inputs"])
        return _lexical_predict(data["inputs"], ctx["lexical"], ctx["target"].astype(np.float64),
                                tf.normalize(ctx["prior_mean"]).astype(np.float64), spec["k"])
    if kind == "semantic":
        return tf.retrieve_forecast(data["context"], ctx["context"], ctx["target"], spec["k"],
                                    spec["temperature_retrieval"])
    model = ctx["models"][spec["model"]]
    if kind == "reaction":
        return tf.predict_ridge(model, data["reaction"])
    if kind == "seed_ridge":
        return tf.predict_ridge(model, data["seed"])
    pred = tl.predict_adapter(model, data["context"]) if kind == "adapter" else tf.predict_ridge(model, data["context"])
    return _blend(pred, data["context"], spec["context_blend"])


# ---------------------------------------------------------------- candidate task and metrics

def _build_task(examples, vec, partition, count):
    import numpy as np
    if any(e.get("partition") != partition for e in examples):
        raise ExperimentError("candidate task for %s needs a homogeneous %s partition" % (partition, partition))
    try:
        rows, gold, strata = tl.hard_candidates(examples, vec["seed"], count)
    except ValueError as exc:
        raise ExperimentError("%s: cannot build %d hard candidates per example: %s" % (partition, count, exc)) from None
    if rows.shape != (len(examples), count):
        raise ExperimentError("%s: candidate rows have unexpected shape %s" % (partition, rows.shape))
    families = [e["family"] for e in examples]
    if len(set(families)) < 2:
        raise ExperimentError("%s: need at least 2 families" % partition)
    return {"partition": partition, "rows": rows, "gold": gold, "strata": list(strata), "families": families,
            "sources": [e["source"] for e in examples], "projects": [e.get("project") or "(none)" for e in examples],
            "cand": vec["target"][rows], "ids": [e["id"] for e in examples],
            "rows_sha256": _sha(rows.tobytes() + gold.tobytes())}


def _group(per, families, idx):
    fam = {}
    for i in idx:
        fam.setdefault(families[i], []).append(i)
    def macro(key):
        return float(sum(sum(per[key][i] for i in v) / len(v) for v in fam.values()) / len(fam))
    return {"n": len(idx), "n_families": len(fam), "top1": float(sum(per["top1"][i] for i in idx) / len(idx)),
            "top3": float(sum(per["top3"][i] for i in idx) / len(idx)),
            "mrr": float(sum(per["mrr"][i] for i in idx) / len(idx)),
            "family_macro_top1": macro("top1"), "family_macro_mrr": macro("mrr")}


def _calibration(scores, gold, temp):
    import numpy as np
    z = np.asarray(scores, dtype=np.float64) / temp
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    p = e / e.sum(axis=1, keepdims=True)
    n = len(z)
    nll = float(np.mean(np.log(e.sum(axis=1)) - z[np.arange(n), gold]))
    onehot = np.zeros_like(p)
    onehot[np.arange(n), gold] = 1.0
    conf = p.max(axis=1)
    correct = (p[np.arange(n), gold] == conf).astype(np.float64) / (p == conf[:, None]).sum(axis=1)
    edges, ece = np.linspace(0.0, 1.0, 11), 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi) if lo > 0 else (conf >= lo) & (conf <= hi)
        if m.any():
            ece += m.mean() * abs(conf[m].mean() - correct[m].mean())
    return {"calibrated_nll": nll, "brier": float(((p - onehot) ** 2).sum(axis=1).mean()), "ece": float(ece)}


def _method_result(scores, task, temp):
    m = tf.metrics(scores, task["gold"], task["families"])
    per = {k: [float(x) for x in v] for k, v in m["per_example"].items()}
    out = {k: m[k] for k in ("n", "n_families", "top1", "top3", "mrr", "logloss", "family_macro_top1",
                             "family_macro_mrr")}
    out["temperature"] = temp
    out.update(_calibration(scores, task["gold"], temp))
    n = len(task["ids"])
    for key, labels in (("by_source", task["sources"]), ("by_project", task["projects"]),
                        ("by_stratum", task["strata"])):
        out[key] = {lab: _group(per, task["families"], [i for i in range(n) if labels[i] == lab])
                    for lab in sorted(set(labels))}
    return out, m["per_example"]


def _rank_key(entry):
    return (-entry[1]["family_macro_top1"], -entry[1]["family_macro_mrr"], entry[0])


def select_methods(results, specs):
    """Primary: best deployable method; baseline: best baseline. Validation family-macro top1, MRR, then name."""
    by_name = {s["name"]: s for s in specs}
    primary = [(n, r) for n, r in results.items() if by_name[n]["primary"]]
    baseline = [(n, r) for n, r in results.items() if by_name[n]["baseline"]]
    return min(primary, key=_rank_key)[0], min(baseline, key=_rank_key)[0]


def _evaluate_methods(specs, ctx, data, task, temps, log):
    scores_by, results, per_by = {}, {}, {}
    for spec in specs:
        pred = _predict(spec, ctx, data)
        scores = tf.rank_candidates(pred, task["cand"])
        scores_by[spec["name"]] = scores
        log("scored %s on %s" % (spec["name"], task["partition"]))
    for name, scores in scores_by.items():
        temp = temps[name] if temps else float(tl.temperature(scores, task["gold"]))
        results[name], per_by[name] = _method_result(scores, task, temp)
    return scores_by, results, per_by


def _bootstrap(per_by, primary, baseline, task):
    out = {}
    for key in ("top1", "mrr"):
        out[key] = tf.paired_bootstrap(per_by[primary][key], per_by[baseline][key], task["families"],
                                       seed=BOOTSTRAP_SEED, draws=BOOTSTRAP_DRAWS)
    out["primary_is_baseline"] = primary == baseline
    return out


def _summary_table(results):
    return {n: {k: r[k] for k in ("family_macro_top1", "family_macro_mrr", "top1", "top3", "mrr", "calibrated_nll",
                                  "temperature")} for n, r in results.items()}


def _task_meta(task):
    return {"candidate_count": int(task["rows"].shape[1]), "n": len(task["ids"]),
            "n_families": len(set(task["families"])), "strata": dict(Counter(task["strata"])),
            "candidate_rows_sha256": task["rows_sha256"]}


# ---------------------------------------------------------------- training

def _paths(home):
    home = Path(home)
    return home, home / "research"


def _fit_arrays(model):
    import numpy as np
    arrays = {k: v for k, v in model.items() if isinstance(v, np.ndarray)}
    scalars = {k: _native(v) for k, v in model.items() if not isinstance(v, np.ndarray)}
    return arrays, scalars


def _swap_dir(tmp, final):
    if os.path.exists(final):
        shutil.rmtree(final)
    os.replace(tmp, final)


def _fresh_dir(path):
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return Path(path)


def _file_hashes(directory):
    directory = Path(directory)
    return {p.name: _sha(_read_bytes(p)) for p in sorted(directory.iterdir()) if p.is_file()}


def _load_models(pred_dir, index):
    return {name: _load_npz(_read_bytes(Path(pred_dir) / info["file"])) for name, info in index["models"].items()}


def _train_context(cand_dir, models):
    cases = [json.loads(line) for line in _read_bytes(Path(cand_dir) / "cases.jsonl").decode("utf-8").splitlines()]
    z = _load_npz(_read_bytes(Path(cand_dir) / "train-vectors.npz"))
    ctx = {"models": {k: v for k, v in models.items() if k != "prior"}, "inputs": [c["input"] for c in cases],
           "context": z["context"], "target": z["target"], "seed": z["seed"], "ids": [str(i) for i in z["ids"]],
           "prior_mean": models["prior"]["prior_mean"], "recent_mean": models["prior"]["recent_mean"]}
    return ctx


def _predictor_config(spec, temp):
    kind = spec["kind"]
    cfg = {"kind": {"semantic": "retrieval"}.get(kind, kind), "calibration_temperature": temp, "temperature": temp}
    if kind == "semantic":
        cfg.update(retriever="semantic", k=spec["k"], temperature=spec["temperature_retrieval"], query="context",
                   retrieval_future_weight=RETRIEVAL_FUTURE_WEIGHT)
    elif kind == "ridge":
        cfg.update(alpha=spec["alpha"], context_blend=spec["context_blend"], input="context", file="predictor.npz")
    elif kind == "adapter":
        cfg.update(rank=spec["rank"], steps=spec["steps"], seed=spec["seed"], context_blend=spec["context_blend"],
                   input="context", file="predictor.npz")
    return cfg


def train_experiment(home, *, adapters=True, candidate_count=8, log=None):
    research = Path(home)/"research"
    for name in (SEAL, RESULTS, "evaluation-started.json", TEST_SCORES):
        if (research/name).exists():
            raise ExperimentError("%s exists: training after a test look is refused" % name)
    lock = research/"training.lock"
    try:
        fd = os.open(str(lock), os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    except FileExistsError:
        raise ExperimentError("training.lock exists: another run or an interrupted run needs inspection") from None
    inode = os.fstat(fd).st_ino
    with os.fdopen(fd, "w") as handle:
        handle.write(str(os.getpid()))
    try:
        return _train_experiment(home, adapters=adapters, candidate_count=candidate_count, log=log)
    finally:
        if lock.exists() and lock.stat().st_ino == inode:
            lock.unlink()


def _train_experiment(home, *, adapters=True, candidate_count=8, log=None):
    import numpy as np
    log = log or (lambda msg: None)
    home, research = _paths(home)
    input_hashes = {name: _sha(_read_bytes(research/name)) for name in sorted(TRAIN_READS)}
    for name in (SEAL, RESULTS):
        if (research / name).exists():
            raise ExperimentError("%s exists: training after a seal or a test look is refused" % name)
    split_raw = _read_bytes(research / "split.json")
    split, train_end_raw, train_end = _load_split(split_raw)
    encoder = _load_encoder(_read_bytes(research / "encoder.json"))
    protocol = _loads(_read_bytes(research / "protocol.json"), "protocol.json") if (research / "protocol.json").exists() else None
    if not isinstance(protocol, dict):
        raise ExperimentError("preregistered protocol.json is required before fitting")
    if "candidate_count" in protocol and protocol["candidate_count"] != candidate_count:
        raise ExperimentError("candidate count differs from the preregistered protocol")
    if "adapters" in protocol and protocol["adapters"] != bool(adapters):
        raise ExperimentError("adapter choice differs from the preregistered protocol")
    train, tv = _load_partition(research, "train", split, train_end)
    val, vv = _load_partition(research, "validation", split, train_end)
    _check_separate(train, val, "train", "validation")
    if tv["seed"].shape[1] != vv["seed"].shape[1]:
        raise ExperimentError("train and validation vector dimensions differ")
    log("validated %d train and %d validation examples" % (len(train), len(val)))
    val_task = _build_task(val, vv, "validation", candidate_count)
    specs = method_specs(adapters)
    run_id = uuid.uuid4().hex
    fams = [r["family"] for r in train]

    pred_tmp = Path(tempfile.mkdtemp(prefix=".predictors-", dir=research))
    cand_tmp = Path(tempfile.mkdtemp(prefix=".candidate-", dir=home))
    try:
        # fits: train arrays only
        fitted = {"prior": ({"prior_mean": tv["target"].mean(axis=0, dtype=np.float64).astype(np.float32),
                             "recent_mean": tv["target"][np.asarray(sorted(
                                 range(len(train)), key=lambda i: (_ts(train[i]["target_time"], "t"), train[i]["id"])
                             )[-RECENT:])].mean(axis=0, dtype=np.float64).astype(np.float32)}, {})}
        for alpha in RIDGE_ALPHAS:
            fitted["ridge-a%g" % alpha] = _fit_arrays(tf.fit_ridge(tv["context"], tv["target"], alpha))
        if adapters:
            for rank in ADAPTER_RANKS:
                log("fitting adapter rank %d" % rank)
                fitted["adapter-r%d" % rank] = _fit_arrays(tl.fit_adapter(
                    tv["context"], tv["target"], fams, rank=rank, steps=ADAPTER_STEPS, seed=ADAPTER_SEED))
        perm = np.random.default_rng(CONTROL_SEED).permutation(len(train))
        fitted["control-ridge-a10"] = _fit_arrays(tf.fit_ridge(tv["context"], tv["target"][perm], 10.0))
        fitted["seed-ridge-a10"] = _fit_arrays(tf.fit_ridge(tv["seed"], tv["target"], 10.0))
        fitted["reaction-ridge-a10"] = _fit_arrays(tf.fit_ridge(tv["reaction"], tv["target"], 10.0))
        index = {"version": VERSION, "run_id": run_id, "training_inputs": input_hashes, "models": {}, "methods": {}}
        for name, (arrays, scalars) in fitted.items():
            data = _npz_bytes(arrays)
            _write_atomic(pred_tmp / (name + ".npz"), data)
            index["models"][name] = {"file": name + ".npz", "sha256": _sha(data), "config": scalars}

        # candidate files that do not depend on selection
        cases = []
        for r in train:
            c = {k: v for k, v in r.items() if k != "reaction_input"}
            c["historical_response"] = r.get("reaction_input", {}).get("post_action", {})
            cases.append(json.dumps(c, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        _write_atomic(cand_tmp / "cases.jsonl", ("\n".join(cases) + "\n").encode("utf-8"))
        _write_atomic(cand_tmp / "split.json", split_raw)
        _write_atomic(cand_tmp / "train-vectors.npz", _npz_bytes(
            {"ids": np.array(tv["ids"]), "seed": tv["seed"], "context": tv["context"], "target": tv["target"]}))

        ctx = _train_context(cand_tmp, _load_models(pred_tmp, index))
        val_data = {"seed": vv["seed"], "context": vv["context"], "reaction": vv["reaction"],
                    "inputs": [r["input"] for r in val]}
        _, results, per_by = _evaluate_methods(specs, ctx, val_data, val_task, None, log)
        primary, baseline = select_methods(results, specs)
        spec_by = {s["name"]: s for s in specs}
        boot = _bootstrap(per_by, primary, baseline, val_task)
        for s in specs:
            index["methods"][s["name"]] = {"spec": _native(s), "temperature": results[s["name"]]["temperature"]}
        index.update(selected=primary, best_baseline=baseline, adapters=bool(adapters),
                     candidate_count=candidate_count, dim=int(tv["seed"].shape[1]), trained_through=train_end_raw,
                     encoder=encoder, eligibility=ELIGIBILITY, code=_code_hashes(),
                     protocol_sha256=_sha(_read_bytes(research / "protocol.json")) if protocol is not None else None,
                     split_sha256=_sha(split_raw), control="control-shuffled-ridge-a10",
                     reaction_diagnostic="diagnostic-reaction-ridge-a10")
        validation = {"version": VERSION, "run_id": run_id, "interpretation": INTERPRETATION, "task": _task_meta(val_task),
                      "methods": results, "selected": primary, "best_baseline": baseline, "bootstrap": boot,
                      "eligibility": ELIGIBILITY, "trained_through": train_end_raw, "n_train": len(train)}
        _write_atomic(pred_tmp / INDEX, _json_bytes(index))

        # materialize the candidate for the selected deployable method
        spec = spec_by[primary]
        if spec["kind"] in ("ridge", "adapter"):
            arrays, _ = fitted[spec["model"]]
            _write_atomic(cand_tmp / "predictor.npz", _npz_bytes(arrays))
        files = _file_hashes(cand_tmp)
        predictor = _predictor_config(spec, results[primary]["temperature"])
        manifest = {"version": CANDIDATE_VERSION, "run_id": run_id, "case_count": len(train), "trained_through": train_end_raw,
                    "train_ids_sha256": _sha(json.dumps(sorted(r["id"] for r in train), separators=(",", ":")).encode()),
                    "split_sha256": _sha(split_raw), "files": files, "encoder": encoder, "predictor": predictor,
                    "retrieval_future_weight": RETRIEVAL_FUTURE_WEIGHT, "selected_method": primary}
        if (research / "project-map.json").is_file():
            manifest["project_aliases"] = _loads(_read_bytes(research / "project-map.json"), "project-map.json")
        _write_atomic(cand_tmp / rt.MANIFEST_NAME, _json_bytes(manifest))
        rt.load_cases(cand_tmp)  # the runtime loader must accept exactly what was written
        if any(_sha(_read_bytes(research/name)) != digest for name, digest in input_hashes.items()):
            raise ExperimentError("training inputs changed while fitting")
        _swap_dir(pred_tmp, research / "predictors")
        _swap_dir(cand_tmp, home / "candidate")
        _write_atomic(research / VALIDATION, _json_bytes(_native(validation)))
    except BaseException:
        shutil.rmtree(pred_tmp, ignore_errors=True)
        shutil.rmtree(cand_tmp, ignore_errors=True)
        raise
    log("selected %s; best baseline %s" % (primary, baseline))
    return _native({"selected": primary, "best_baseline": baseline, "table": _summary_table(results),
                    "bootstrap": boot, "task": _task_meta(val_task), "candidate": str(home / "candidate"),
                    "predictors": str(research / "predictors")})


# ---------------------------------------------------------------- seal

def _frozen_hashes(home, research):
    pred = research / "predictors"
    if not (pred / INDEX).is_file() or not (research / VALIDATION).is_file() or not (home / "candidate").is_dir():
        raise ExperimentError("train_experiment outputs are missing")
    out = {"split.json": _sha(_read_bytes(research / "split.json")),
           "encoder.json": _sha(_read_bytes(research / "encoder.json")),
           "protocol.json": _sha(_read_bytes(research / "protocol.json")) if (research / "protocol.json").exists() else None,
           VALIDATION: _sha(_read_bytes(research / VALIDATION))}
    out["predictors"] = _file_hashes(pred)
    for name in ("coverage.json", "lineage.json", "project-map.json", "environment.txt"):
        if (research/name).is_file():
            out[name] = _sha(_read_bytes(research/name))
    out["candidate"] = _file_hashes(home / "candidate")
    return out


def seal_experiment(home):
    home, research = _paths(home)
    if (research/"training.lock").exists():
        raise ExperimentError("training is active or interrupted; cannot seal")
    if any((research/name).exists() for name in (RESULTS, "evaluation-started.json", TEST_SCORES)) and not (research / SEAL).exists():
        raise ExperimentError("test results exist without a seal")
    hashes = _frozen_hashes(home, research)
    index = _loads(_read_bytes(research / "predictors" / INDEX), INDEX)
    if any(_sha(_read_bytes(research/name)) != digest for name, digest in index["training_inputs"].items()):
        raise ExperimentError("training inputs or encoder changed after fitting")
    if _load_encoder(_read_bytes(research/"encoder.json")) != index["encoder"]:
        raise ExperimentError("study encoder differs from fitted encoder")
    code = _code_hashes()
    if code != index["code"]:
        raise ExperimentError("experiment code changed since training: %s" % sorted(
            k for k in code if code[k] != index["code"].get(k)))
    if hashes["split.json"] != index["split_sha256"]:
        raise ExperimentError("split.json changed since training")
    manifest, _ = rt.load_cases(home / "candidate")
    validation = _loads(_read_bytes(research/VALIDATION), VALIDATION)
    if not index.get("run_id") or index["run_id"] != manifest.get("run_id") or index["run_id"] != validation.get("run_id"):
        raise ExperimentError("candidate, predictors and validation do not belong to one training run")
    if index["selected"] != manifest.get("selected_method") or index["selected"] != validation.get("selected"):
        raise ExperimentError("candidate and validation selected method differs from predictor index")
    if manifest.get("encoder") != index.get("encoder"):
        raise ExperimentError("deployed encoder differs from the evaluated encoder")
    spec = index["methods"][index["selected"]]["spec"]
    if manifest.get("predictor") != _predictor_config(spec, index["methods"][index["selected"]]["temperature"]):
        raise ExperimentError("deployed predictor configuration differs from the evaluated predictor")
    if spec["kind"] in ("ridge", "adapter"):
        import numpy as np
        model = _load_npz(_read_bytes(research/"predictors"/index["models"][spec["model"]]["file"]))
        deployed = _load_npz(_read_bytes(home/"candidate"/"predictor.npz"))
        if set(model) != set(deployed) or any(not np.array_equal(model[k], deployed[k]) for k in model):
            raise ExperimentError("deployed arrays differ from the evaluated predictor")
    if manifest["split_sha256"] != hashes["split.json"]:
        raise ExperimentError("candidate split does not match research split")
    if (research / SEAL).exists():
        existing = _loads(_read_bytes(research / SEAL), SEAL)
        if existing.get("hashes") != hashes or existing.get("code") != code:
            raise ExperimentError("frozen artifacts differ from the existing seal")
        return existing
    seal = {"version": VERSION, "sealed_at": datetime.now(timezone.utc).isoformat(), "hashes": hashes, "code": code,
            "selected": index["selected"], "best_baseline": index["best_baseline"]}
    _write_exclusive(research / SEAL, _json_bytes(seal))
    return seal


# ---------------------------------------------------------------- sealed evaluation

def evaluate_experiment(home, log=None):
    import numpy as np
    log = log or (lambda msg: None)
    home, research = _paths(home)
    if (research / RESULTS).exists():
        raise ExperimentError("%s exists: the test partition has already been evaluated" % RESULTS)
    if not (research / SEAL).exists():
        raise ExperimentError("no seal: run seal_experiment first")
    seal_raw = _read_bytes(research / SEAL)
    seal = _loads(seal_raw, SEAL)
    if _frozen_hashes(home, research) != seal.get("hashes"):
        raise ExperimentError("frozen artifacts do not match the seal")
    if _code_hashes() != seal.get("code"):
        raise ExperimentError("experiment code changed since the seal")
    # Claim the one look before opening test bytes, including across concurrent
    # processes or a crash after scoring but before the final receipt is written.
    _write_exclusive(research / "evaluation-started.json", _json_bytes({
        "seal_sha256": _sha(seal_raw), "started_at": datetime.now(timezone.utc).isoformat()}))
    pred = research / "predictors"
    index = _loads(_read_bytes(pred / INDEX), INDEX)
    split_raw = _read_bytes(research / "split.json")
    split, _, train_end = _load_split(split_raw)
    rt.load_cases(home / "candidate")
    # only now: the test partition. Train and validation are re-read for separation checks only.
    train, _ = _load_partition(research, "train", split, train_end, with_vectors=False)
    val, _ = _load_partition(research, "validation", split, train_end, with_vectors=False)
    test, ev = _load_partition(research, "test", split, train_end)
    for rows, label in ((train, "train"), (val, "validation")):
        _check_separate(rows, test, label, "test")
    if ev["seed"].shape[1] != index["dim"]:
        raise ExperimentError("test vector dimension differs from the frozen predictors")
    task = _build_task(test, ev, "test", index["candidate_count"])
    specs = [dict(m["spec"]) for m in index["methods"].values()]
    temps = {n: m["temperature"] for n, m in index["methods"].items()}
    ctx = _train_context(home / "candidate", _load_models(pred, index))
    data = {"seed": ev["seed"], "context": ev["context"], "reaction": ev["reaction"],
            "inputs": [r["input"] for r in test]}
    scores_by, results, per_by = _evaluate_methods(specs, ctx, data, task, temps, log)
    primary, baseline = index["selected"], index["best_baseline"]
    boot = _bootstrap(per_by, primary, baseline, task)
    receipt = {"version": VERSION, "interpretation": INTERPRETATION,
               "evaluated_at": datetime.now(timezone.utc).isoformat(), "seal_sha256": _sha(seal_raw),
               "model_index_sha256": seal["hashes"]["predictors"][INDEX], "protocol_sha256": seal["hashes"]["protocol.json"],
               "split_sha256": seal["hashes"]["split.json"], "selected": primary, "best_baseline": baseline,
               "task": _task_meta(task), "methods": results, "bootstrap": boot,
               "primary": results[primary], "baseline": results[baseline]}
    _write_atomic(research / TEST_SCORES, _npz_bytes(dict(
        {"ids": np.array(task["ids"]), "rows": task["rows"], "gold": task["gold"]},
        **{"scores:" + n: np.asarray(s) for n, s in scores_by.items()})))
    _write_exclusive(research / RESULTS, _json_bytes(_native(receipt)))
    return _native({"selected": primary, "best_baseline": baseline, "table": _summary_table(results),
                    "bootstrap": boot, "task": _task_meta(task), "primary": results[primary],
                    "baseline": results[baseline]})
