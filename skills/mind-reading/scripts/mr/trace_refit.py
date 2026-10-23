"""Post-evaluation refit of the FIXED, validation-selected recipe into a different candidate.

After the sealed test has been read, the recipe chosen on validation alone is refit on every eligible human example,
including rows that were validation or test holdouts of the evaluated candidate. The result is a new directory; the
evaluated candidate, the predictor index, the protocol and the receipts are only read, never changed. The new weights
have no unseen-test score: the evaluation receipt is hashed for lineage and its contents are never inspected, and no
method is chosen or ranked from test numbers. Stdlib plus numpy; torch only when the selected recipe is an adapter.
"""
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path

from . import trace_forecast, trace_learning
from .trace_runtime import (CASES_NAME, MANIFEST_NAME, SPLIT_NAME, VERSION, TraceFormatError, _ts, _validate_case,
                            load_cases, render_input)

PREDICTOR_NAME = "predictor.npz"
VECTORS_NAME = "train-vectors.npz"
PLAIN_KINDS = ("seed", "context")  # similarity over stored vectors; no k or temperature of their own
LEARNED_KINDS = ("ridge", "adapter")
KINDS = PLAIN_KINDS + ("semantic",) + LEARNED_KINDS
FORBIDDEN_KINDS = ("prior", "lexical", "control", "reaction")
MIN_SEED_WORDS, MAX_SEED_WORDS = 2, 60
MAX_BYTES = 256 * 1024 * 1024
SCOPE = "earlier weight snapshot only; this refit has no fresh held-out evaluation"
CALIBRATION_NOTE = ("temperature carried over from the earlier 8-way study of the evaluated candidate; not validated for "
                    "this refit; runtime confidence is not a specific desire probability")
HOLDOUT_NOTE = ("every row is training data; rows that were validation, test or excluded in the evaluated candidate "
                "were added only after its test was read, so no held-out claim applies to any weight here")


class RefitError(ValueError):
    pass


# ---------------------------------------------------------------- helpers

def _np():
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("trace_refit needs numpy (use the local optional ML environment)") from exc
    return numpy


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_regular(path, what, limit=MAX_BYTES):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise RefitError("cannot open %s: %s" % (what, exc.strerror or exc)) from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise RefitError("%s is not a regular file" % what)
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise RefitError("%s is too large" % what)
    return data


def _no_constant(name):
    raise RefitError("non-finite JSON number %s" % name)


def _json(data, what):
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefitError("%s is not valid UTF-8 JSON" % what) from None


def _number(spec, key, kind, positive=True):
    value = spec.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RefitError("selected recipe needs a finite numeric %s" % key)
    if kind is int and (int(value) != value):
        raise RefitError("selected recipe %s must be an integer" % key)
    if positive and not value > 0:
        raise RefitError("selected recipe %s must be > 0" % key)
    return kind(value)


# ---------------------------------------------------------------- inputs

def _blend(spec):
    value = spec.get("context_blend")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise RefitError("selected recipe context_blend must be a number from 0 to 1")
    return value


def _predictor_config(recipe, temp):
    """The manifest predictor section, field for field what the study's candidate writer emits for this recipe."""
    kind = recipe["kind"]
    cfg = {"kind": "retrieval" if kind == "semantic" else kind, "calibration_temperature": temp, "temperature": temp}
    if kind == "semantic":
        cfg.update(retriever="semantic", k=recipe["k"], temperature=recipe["temperature_retrieval"], query="context",
                   retrieval_future_weight=recipe["retrieval_future_weight"])
    elif kind == "ridge":
        cfg.update(alpha=recipe["alpha"], context_blend=recipe["context_blend"], input="context", file=PREDICTOR_NAME)
    elif kind == "adapter":
        cfg.update(rank=recipe["rank"], steps=recipe["steps"], seed=recipe["seed"],
                   context_blend=recipe["context_blend"], input="context", file=PREDICTOR_NAME)
    return cfg


def _read_selected_recipe(index_path):
    """Selected method spec and encoder from the validation-time index. Test numbers are never consulted.

    The index entry is {spec: {...}, temperature: calibration}; a flat entry (spec fields beside temperature) is also
    accepted."""
    index = _json(_read_regular(index_path, "predictor index"), "predictor index")
    if not isinstance(index, dict):
        raise RefitError("predictor index must be an object")
    name = index.get("selected", index.get("selected_method"))
    methods = index.get("methods")
    if not isinstance(name, str) or not name or not isinstance(methods, dict) or not isinstance(methods.get(name), dict):
        raise RefitError("predictor index must name a selected method present in methods")
    entry = methods[name]
    spec = entry["spec"] if "spec" in entry else entry
    if not isinstance(spec, dict):
        raise RefitError("selected method spec must be an object")
    kind = spec.get("kind", name)
    if kind in FORBIDDEN_KINDS or kind not in KINDS:
        raise RefitError("selected method kind %r cannot be refit" % (kind,))
    recipe = {"kind": kind}
    if kind == "semantic":
        recipe["k"] = _number(spec, "k", int)
        recipe["temperature_retrieval"] = _number(spec, "temperature_retrieval", float)
    elif kind == "ridge":
        recipe["alpha"] = _number(spec, "alpha", float)
        recipe["context_blend"] = _blend(spec)
    elif kind == "adapter":
        recipe["rank"] = _number(spec, "rank", int)
        recipe["steps"] = _number(spec, "steps", int)
        recipe["seed"] = _number(spec, "seed", int, positive=False)
        recipe["context_blend"] = _blend(spec)
        if "learning_rate" in spec:
            recipe["learning_rate"] = _number(spec, "learning_rate", float)
    elif "context_blend" in spec:
        recipe["context_blend"] = _blend(spec)
    if isinstance(spec.get("model"), str):
        recipe["model"] = spec["model"]
    temperature = entry.get("temperature")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                    or not math.isfinite(temperature) or temperature <= 0):
        raise RefitError("index temperature must be a finite number > 0")
    encoder = index.get("encoder", spec.get("encoder"))
    return name, recipe, encoder, temperature


def _read_examples(path):
    raw = _read_regular(path, "examples")
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    cases, seen, latest = [], set(), None
    for number, line in enumerate(lines, 1):
        case = _json(line, "examples line %d" % number)
        try:
            _validate_case(case, float("inf"), number)
        except TraceFormatError as exc:
            raise RefitError(str(exc)) from None
        if case["id"] in seen:
            raise RefitError("duplicate case id %r" % case["id"])
        seen.add(case["id"])
        words = len(case["input"]["seed"].split())
        if not MIN_SEED_WORDS <= words <= MAX_SEED_WORDS:
            raise RefitError("case %r seed must have %d to %d words" % (case["id"], MIN_SEED_WORDS, MAX_SEED_WORDS))
        stamp = _ts(case["target_time"], "target_time")
        if latest is None or stamp > latest[0]:
            latest = (stamp, case["target_time"])
        cases.append(case)
    if len(cases) < 2:
        raise RefitError("need at least 2 eligible examples")
    return cases, latest[1]


def _read_vectors(path, ids):
    np = _np()
    try:
        with np.load(os.fspath(path), allow_pickle=False) as data:
            names = set(data.files)
            if not {"ids", "seed", "context", "target"} <= names:
                raise RefitError("vectors need ids, seed, context and target arrays")
            got = [str(i) for i in data["ids"].tolist()] if data["ids"].ndim == 1 else None
            arrays = {k: data[k] for k in ("seed", "context", "target")}
    except (OSError, ValueError) as exc:
        if isinstance(exc, RefitError):
            raise
        raise RefitError("cannot read vectors without pickle: %s" % exc) from None
    if got != list(ids):
        raise RefitError("vector ids must equal the example ids in the same order")
    out = {}
    dim = None
    for key, arr in arrays.items():
        if arr.dtype.kind not in "fiub" or arr.ndim != 2 or arr.shape[0] != len(ids):
            raise RefitError("%s vectors must be a numeric matrix with one row per example" % key)
        if not np.isfinite(arr).all():
            raise RefitError("%s vectors must be finite" % key)
        if dim is not None and arr.shape[1] != dim:
            raise RefitError("vector dimensions must agree")
        dim = arr.shape[1]
        out[key] = arr.astype("float32")
    return out


def _training_case(case):
    out = {k: v for k, v in case.items() if k != "reaction_input"}
    reaction = case.get("reaction_input")
    post = reaction.get("post_action") if isinstance(reaction, dict) else None
    if isinstance(post, dict):
        out["historical_response"] = dict(post)
    elif isinstance(post, str):
        out["historical_response"] = {"assistant_text": post}
    return out


# ---------------------------------------------------------------- output

def _check_output(output, source):
    output, source = Path(output).absolute(), Path(source).resolve()
    resolved = output.parent.resolve() / output.name
    if os.path.lexists(output):
        raise RefitError("output already exists; refusing to overwrite")
    if resolved == source or source in resolved.parents or resolved in source.parents:
        raise RefitError("output must be a different directory from the evaluated candidate")
    return output


def _save_npz(path, **arrays):
    np = _np()
    with open(path, "wb") as handle:
        np.savez(handle, **arrays)


def _plain(model):
    np = _np()
    out = {}
    for key, value in model.items():
        arr = np.asarray(value)
        if arr.dtype.kind in "fiub":
            out[key] = arr
    return out


def refit_candidate(study_home, examples_path, vectors_path, output_dir, log=None):
    """Refit the fixed selected recipe on all eligible examples into a new candidate directory."""
    say = log or (lambda message: None)
    home = Path(study_home)
    source = home / "candidate"
    research = home / "research"
    receipt_bytes = _read_regular(research / "test-results.json", "test receipt")  # opaque: hashed, never parsed
    seal_bytes = _read_regular(research / "seal.json", "seal")
    source_manifest_bytes = _read_regular(source / MANIFEST_NAME, "evaluated candidate manifest")
    source_split_bytes = _read_regular(source / SPLIT_NAME, "evaluated candidate split")
    source_manifest = _json(source_manifest_bytes, "evaluated candidate manifest")
    source_split = _json(source_split_bytes, "evaluated candidate split")
    if not isinstance(source_manifest, dict) or not isinstance(source_split, dict):
        raise RefitError("evaluated candidate manifest and split must be objects")
    output = _check_output(output_dir, source)
    name, recipe, encoder, temperature = _read_selected_recipe(research / "predictors" / "index.json")
    weight = source_manifest.get("retrieval_future_weight")
    if recipe["kind"] == "semantic":
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 <= weight <= 1:
            raise RefitError("evaluated candidate manifest needs retrieval_future_weight for a semantic recipe")
        recipe["retrieval_future_weight"] = weight
    config = _predictor_config(recipe, temperature)
    cases, latest = _read_examples(examples_path)
    ids = [c["id"] for c in cases]
    vectors = _read_vectors(vectors_path, ids)
    say("selected %s (%s) refit on %d examples" % (name, recipe["kind"], len(cases)))

    np = _np()
    predictor = None
    if recipe["kind"] == "ridge":
        model = trace_forecast.fit_ridge(vectors["context"], vectors["target"], alpha=recipe["alpha"])
        predictor = _plain(model)
    elif recipe["kind"] == "adapter":
        extra = {"learning_rate": recipe["learning_rate"]} if "learning_rate" in recipe else {}
        model = trace_learning.fit_adapter(vectors["context"], vectors["target"], [c["family"] for c in cases],
                                           rank=recipe["rank"], steps=recipe["steps"], seed=recipe["seed"], **extra)
        predictor = _plain(model)

    old_train = set(source_split.get("train_ids", [])) if isinstance(source_split.get("train_ids"), list) else set()
    former = [i for i in ids if i not in old_train]
    parent_split_sha = _sha(source_split_bytes)
    receipt_sha = _sha(receipt_bytes)
    split = {"protocol": "post-evaluation-refit", "train_ids": ids, "validation_ids": [], "test_ids": [],
             "excluded_ids": [], "cutoffs": {"train": latest}, "parent_split_sha256": parent_split_sha,
             "evaluation_receipt_sha256": receipt_sha, "former_holdout_or_new_ids": former,
             "includes_former_holdouts": bool(former), "note": HOLDOUT_NOTE}
    split_bytes = (json.dumps(split, indent=2, sort_keys=True) + "\n").encode()
    cases_bytes = b"".join((json.dumps(_training_case(c), ensure_ascii=False, sort_keys=True) + "\n").encode()
                           for c in cases)

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".refit-", dir=output.parent))
    try:
        (tmp / CASES_NAME).write_bytes(cases_bytes)
        (tmp / SPLIT_NAME).write_bytes(split_bytes)
        _save_npz(tmp / VECTORS_NAME, ids=np.asarray(ids), seed=vectors["seed"], context=vectors["context"],
                  target=vectors["target"])
        files = {CASES_NAME: _sha(cases_bytes), VECTORS_NAME: trace_learning.sha256(tmp / VECTORS_NAME)}
        if predictor is not None:
            _save_npz(tmp / PREDICTOR_NAME, **predictor)
            files[PREDICTOR_NAME] = trace_learning.sha256(tmp / PREDICTOR_NAME)
        manifest = {
            "version": VERSION, "stage": "post_evaluation_refit", "fresh_evaluation": False,
            "selected_method": name, "fixed_recipe": recipe, "encoder": encoder,
            "predictor": config,
            "calibration": {"temperature": temperature, "source": CALIBRATION_NOTE, "validated_for_candidate": False},
            "parent_evaluation": {"receipt_sha256": receipt_sha, "seal_sha256": _sha(seal_bytes),
                                  "evaluated_candidate_manifest_sha256": _sha(source_manifest_bytes), "scope": SCOPE},
            "files": files,
            **({"retrieval_future_weight": weight} if weight is not None else {}),
            "train_ids_sha256": _sha(json.dumps(sorted(ids), separators=(",", ":")).encode()),
            "split_sha256": _sha(split_bytes), "trained_through": latest, "case_count": len(cases),
            "former_holdout_or_new_case_count": len(former),
            "project_aliases": source_manifest.get("project_aliases") or {},
        }
        (tmp / MANIFEST_NAME).write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        load_cases(tmp)
        if os.path.lexists(output):
            raise RefitError("output already exists; refusing to overwrite")
        os.rename(tmp, output)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    say("wrote %s" % output)
    return manifest


# ---------------------------------------------------------------- data preparation

PREP_PHASE = "post-test refit data; every row is training data"
ENCODER_TEXT_KEYS = ("model", "revision", "path", "device", "dtype", "cache_schema")


def _pinned_encoder(index_path):
    """index.encoder with every pin checked against the files on disk. Nothing else in the index is read."""
    index = _json(_read_regular(index_path, "predictor index"), "predictor index")
    enc = index.get("encoder") if isinstance(index, dict) else None
    if not isinstance(enc, dict) or not all(isinstance(enc.get(k), str) and enc[k].strip() for k in ENCODER_TEXT_KEYS):
        raise RefitError("index encoder needs model, revision, path, device, dtype and cache_schema")
    if type(enc.get("max_length")) is not int or enc["max_length"] < 1:
        raise RefitError("index encoder max_length must be a positive integer")
    pins = enc.get("weights_sha256")
    root = Path(enc["path"])
    if not isinstance(pins, dict) or not pins or not root.is_dir():
        raise RefitError("index encoder needs pinned digests for every weight file in an existing directory")
    digests = {p.relative_to(root).as_posix(): trace_learning.sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}
    if digests != pins:
        raise RefitError("encoder files differ from the pinned weights_sha256")
    return {k: enc[k] for k in ENCODER_TEXT_KEYS + ("max_length", "weights_sha256")}


def _cache_identity(enc):
    keys = ("model", "revision", "max_length", "weights_sha256", "device", "dtype", "cache_schema")
    return json.dumps({k: enc[k] for k in keys}, sort_keys=True)


def _embed(db, enc, texts, log):
    """Cached unit-normalized float32 vectors for `texts`; only uncached unique texts reach the encoder."""
    np = _np()
    identity = _cache_identity(enc)

    def key(text):
        return _sha((identity + "|" + text).encode())

    db.execute("CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY,vector BLOB,dim INTEGER)")
    unique = list(dict.fromkeys(texts))
    missing = [t for t in unique if not db.execute("SELECT 1 FROM vectors WHERE id=?", (key(t),)).fetchone()]
    if missing:
        encoder = trace_learning.LocalEncoder(enc["path"], device=enc["device"], max_length=enc["max_length"])
        for start in range(0, len(missing), 256):
            chunk = missing[start:start + 256]
            arrays = np.asarray(encoder.encode(chunk, batch_size=16), dtype="float32")
            if arrays.ndim != 2 or arrays.shape[0] != len(chunk):
                raise RefitError("encoder returned vectors that do not align with the texts")
            db.executemany("INSERT OR IGNORE INTO vectors VALUES (?,?,?)",
                           [(key(t), a.tobytes(), int(a.shape[0])) for t, a in zip(chunk, arrays)])
            db.commit()
            say = log or (lambda message: None)
            say("encoded %d of %d uncached texts" % (min(start + 256, len(missing)), len(missing)))
    rows = []
    for t in texts:
        found = db.execute("SELECT vector,dim FROM vectors WHERE id=?", (key(t),)).fetchone()
        vec = np.frombuffer(found[0], dtype="float32").copy()
        if vec.shape[0] != found[1]:
            raise RefitError("cached vector has the wrong length")
        rows.append(vec)
    if len({r.shape[0] for r in rows}) != 1:
        raise RefitError("cached vectors disagree on dimension")
    return np.stack(rows), len(unique), len(missing)


def prepare_refit_data(study_home, tape=None, log=None):
    """Collect every strong-human example from the tape and embed it with the pinned encoder.

    Runs only after the test receipt and seal exist. No test metric is read and no method is chosen. Returns
    (examples_path, vectors_path, data_dir) for refit_candidate; data_dir is new and unique inside study_home."""
    np = _np()
    home = Path(study_home)
    research = home / "research"
    receipt_bytes = _read_regular(research / "test-results.json", "test receipt")  # opaque
    seal_bytes = _read_regular(research / "seal.json", "seal")
    enc = _pinned_encoder(research / "predictors" / "index.json")
    aliases = _json(_read_regular(research / "project-map.json", "frozen project map"), "project map")
    if not isinstance(aliases, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()):
        raise RefitError("project map must map checkout paths to project names")
    tape_path = Path(tape) if tape else home / "tape.sqlite"
    if not tape_path.is_file():
        raise RefitError("tape not found: %s" % tape_path)
    store = importlib.import_module(__package__ + ".trace_store")
    build = importlib.import_module(__package__ + ".trace_build")

    db = store.connect(tape_path)
    locations = {}
    try:
        examples = store.make_examples(db)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"events", "files", "locations"} <= tables:
            for ref, path, machine, digest in db.execute("SELECT l.source_ref,f.path,f.machine,f.sha256 FROM locations l JOIN files f ON f.id=l.file_id JOIN events e ON e.id=l.event_id WHERE e.role='user' AND e.actor='human'"):
                locations.setdefault(ref, {"source_ref": ref, "path": path, "machine": machine, "raw_prefix_sha256": digest})
    finally:
        db.close()
    kept, seen, latest, empty_targets = [], set(), None, 0
    for number, ex in enumerate(examples, 1):
        words = len(ex["input"]["seed"].split()) if isinstance(ex.get("input"), dict) and isinstance(ex["input"].get("seed"), str) else -1
        if words < 0:
            raise RefitError("example %d has no seed text" % number)
        if not MIN_SEED_WORDS <= words <= MAX_SEED_WORDS:
            continue
        target_text = ex.get("target", {}).get("text") if isinstance(ex.get("target"), dict) else None
        if isinstance(target_text, str) and not target_text.strip():
            empty_targets += 1
            continue
        project = build.project_identity(ex.get("project"), aliases)
        ex["project"] = project
        if isinstance(ex.get("source_refs"), dict):
            ex["source_locations"] = [locations[r] for r in (ex["source_refs"].get("seed"), ex["source_refs"].get("target")) if r in locations]
        ex["input"]["project"] = project
        if isinstance(ex.get("reaction_input"), dict):
            ex["reaction_input"]["project"] = project
        try:
            _validate_case(ex, float("inf"), number)
        except TraceFormatError as exc:
            raise RefitError(str(exc)) from None
        if ex["id"] in seen:
            raise RefitError("duplicate case id %r" % ex["id"])
        seen.add(ex["id"])
        stamp = _ts(ex["target_time"], "target_time")
        if not isinstance(ex["target_time"], str) or not isinstance(ex["seed_time"], str):
            raise RefitError("tape timestamps must be ISO strings")
        if latest is None or stamp > latest[0]:
            latest = (stamp, ex["target_time"])
        kept.append(ex)
    if len(kept) < 2:
        raise RefitError("need at least 2 eligible examples")
    _, repos = build.git_catalog(home)
    for ex in kept:
        reaction = ex.pop("reaction_input", None)
        post = reaction.get("post_action") if isinstance(reaction, dict) else None
        if isinstance(post, dict):
            ex["historical_response"] = dict(post)
        elif isinstance(post, str):
            ex["historical_response"] = {"assistant_text": post}
        ex["partition"] = "train"
        ex["historical_outcomes"] = build.nearby_outcomes(ex, repos, latest[1])

    ids = [e["id"] for e in kept]
    texts = []
    for ex in kept:
        texts.extend([ex["input"]["seed"], render_input(ex["input"]), ex["target"]["text"]])
    cache = sqlite3.connect(str(home / "embedding-cache.sqlite"))
    try:
        matrix, unique, encoded = _embed(cache, enc, texts, log)
    finally:
        cache.close()
    matrix = matrix.reshape(len(kept), 3, -1)

    data_dir = Path(tempfile.mkdtemp(prefix="refit-data-", dir=home))
    try:
        examples_path, vectors_path = data_dir / "examples.jsonl", data_dir / "vectors.npz"
        examples_path.write_bytes(b"".join((json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n").encode() for e in kept))
        np.savez(vectors_path, ids=np.asarray(ids), seed=matrix[:, 0], context=matrix[:, 1], target=matrix[:, 2])
        receipt = {"phase": PREP_PHASE, "counts": {"tape_examples": len(examples), "eligible": len(kept),
                                                  "unique_texts": unique, "newly_encoded_texts": encoded,
                                                  "excluded_empty_targets": empty_targets},
                   "partition": "all train", "observed_through": latest[1], "tape": str(tape_path),
                   "encoder": {k: enc[k] for k in ("model", "revision", "device", "dtype", "cache_schema")},
                   "evaluation_receipt_sha256": _sha(receipt_bytes), "seal_sha256": _sha(seal_bytes)}
        (data_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except BaseException:
        shutil.rmtree(data_dir, ignore_errors=True)
        raise
    return examples_path, vectors_path, data_dir
