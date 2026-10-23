#!/usr/bin/env python3
"""Executable candidate runtime: train-only trajectory evidence for the host agent.

  python3 -I scripts/intent.py --candidate DIR packet|status|corrections|explain|counterfactual

The candidate directory comes from --candidate or MIND_READING_CANDIDATE; there is no default and no live home.
`packet` and `counterfactual` read one JSON object {seed, context, project} (at most 128 KiB) on stdin. Output is JSON
on stdout; every error is one line on stderr with exit 2. The host interprets the contract; historical continuations
are evidence, never new authority. Lexical retrieval is stdlib only. --local uses embeddings and weights that are
already on disk (no network, no auto download) and falls back to lexical, visibly, only when an optional dependency or
weight directory is missing. A hash, membership, split or tamper failure is never a fallback.
"""
import argparse
import hashlib
import io
import json
import math
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mr import trace_runtime as rt  # noqa: E402
from mr.trace_rerank import TOP_K, rerank_pairs  # noqa: E402
from mr.trace_runtime import TraceFormatError  # noqa: E402

MAX_STDIN = 128 * 1024
MAX_NPZ_BYTES = 2 * 1024 ** 3
VECTORS_NAME, PREDICTOR_NAME = "train-vectors.npz", "predictor.npz"
KINDS = ("ridge", "adapter", "retrieval", "seed", "context", "prior")
DEFAULT_FUTURE_WEIGHT = 0.25
CLEARED = {"prior_user_turns": [], "prior_agent_text": "", "prior_tools": []}
ABLATION_NOTE = ("ablation of the prior_* context only; the listed case ids changed, which is not a causal claim "
                 "about the person or the task")


class CliError(Exception):
    pass


class _Fallback(Exception):
    """An optional dependency or weight directory is missing; lexical retrieval takes over, visibly."""


# ---------------------------------------------------------------- input

def _read_payload(stdin):
    data = stdin.read(MAX_STDIN + 1)
    if len(data) > MAX_STDIN:
        raise CliError("stdin exceeds %d bytes" % MAX_STDIN)
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=rt._no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, TraceFormatError):
        raise CliError("stdin is not one valid UTF-8 JSON object") from None
    if not isinstance(obj, dict):
        raise CliError("stdin JSON must be an object")
    extra = sorted(set(obj) - {"seed", "context", "project"})
    if extra:
        raise CliError("unknown stdin keys: %s" % ", ".join(extra))
    seed, context, project = obj.get("seed"), obj.get("context", {}), obj.get("project")
    if not isinstance(seed, str):
        raise CliError("seed must be a string")
    if not isinstance(context, dict):
        raise CliError("context must be an object")
    if project is not None and not isinstance(project, str):
        raise CliError("project must be a string or null")
    for name in ("prior_user_turns", "verified_facts"):
        value = context.get(name)
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            raise CliError("context.%s must be a list of strings" % name)
    if context.get("prior_tools") is not None and (not isinstance(context["prior_tools"], list) or not all(isinstance(t, dict) for t in context["prior_tools"])):
        raise CliError("context.prior_tools must be a list of objects")
    for name in ("goal", "phase", "prior_agent_text"):
        if context.get(name) is not None and not isinstance(context[name], str):
            raise CliError("context.%s must be a string" % name)
    return seed, context, project


def _query_input(seed, context):
    """The only fields a model or lexical index may see: the literal seed plus the prior_* prefix."""
    return {"seed": seed, "prior_user_turns": context.get("prior_user_turns"),
            "prior_agent_text": context.get("prior_agent_text"), "prior_tools": context.get("prior_tools"), "goal": context.get("goal")}


def _excluded(cases, context):
    session = context.get("session") if rt._nonempty(context.get("session")) else None
    family = context.get("family") if rt._nonempty(context.get("family")) else None
    return [i for i, c in enumerate(cases) if c["session"] == session or c["family"] == family]


# ---------------------------------------------------------------- backends

class LexicalBackend:
    mode = "lexical"

    def __init__(self, cases):
        self.cases = cases
        self.provenance = {"kind": "lexical", "predictor_sha256": None, "vector_sha256": None}
        self.warnings = []

    def pairs(self, seed, context):
        return [(i, s) for i, s in enumerate(rt.lexical_scores(seed, context, self.cases)) if s > 0.0]


def _make_encoder(path, device, max_length):
    from mr.trace_learning import LocalEncoder
    return LocalEncoder(path, device=device, max_length=max_length)


def _pick_device(requested):
    if requested != "auto":
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.backends.mps.is_available():
        return "mps"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _number(value, what, lo=None, hi=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TraceFormatError("manifest %s must be a finite number" % what)
    if (lo is not None and value < lo) or (hi is not None and value > hi) or (positive and value <= 0):
        raise TraceFormatError("manifest %s is out of range" % what)
    return float(value)


def _local_spec(manifest):
    """Validate the manifest's local sections. Absent sections mean no local retrieval; malformed ones are errors."""
    encoder, predictor = manifest.get("encoder"), manifest.get("predictor")
    if encoder is None or predictor is None:
        raise _Fallback("candidate declares no local encoder/predictor")
    if not isinstance(encoder, dict) or not rt._nonempty(encoder.get("path")):
        raise TraceFormatError("manifest encoder needs a path")
    if not isinstance(encoder.get("weights_sha256"), dict) or not encoder["weights_sha256"]:
        raise TraceFormatError("local encoder needs pinned model file digests")
    for key in ("model", "revision"):
        if key in encoder and not isinstance(encoder[key], str):
            raise TraceFormatError("manifest encoder %s must be a string" % key)
    length = encoder.get("max_length", 256)
    if type(length) is not int or not 1 <= length <= 8192:
        raise TraceFormatError("manifest encoder max_length must be an integer from 1 to 8192")
    if not isinstance(predictor, dict) or predictor.get("kind") not in KINDS:
        raise TraceFormatError("manifest predictor kind must be one of %s" % ", ".join(KINDS))
    spec = {"kind": predictor["kind"], "blend": _number(predictor.get("context_blend", 0.0), "context_blend", 0, 1)}
    if "alpha" in predictor:
        _number(predictor["alpha"], "alpha", positive=True)
    if spec["kind"] == "retrieval":
        k = predictor.get("k")
        if type(k) is not int or k < 1:
            raise TraceFormatError("manifest predictor k must be a positive integer")
        spec["k"] = k
        spec["temperature"] = _number(predictor.get("temperature"), "temperature", positive=True)
    weight = _number(manifest.get("retrieval_future_weight", DEFAULT_FUTURE_WEIGHT), "retrieval_future_weight", 0, 1)
    return {"encoder": encoder, "max_length": length, "predictor": spec, "future_weight": weight}


def _verified(directory, manifest, name, required):
    digest = manifest["files"].get(name)
    if digest is None:
        if required:
            raise TraceFormatError("manifest does not list %s" % name)
        return None
    data = rt._read_exact(directory, name, MAX_NPZ_BYTES)
    if hashlib.sha256(data).hexdigest() != digest:
        raise TraceFormatError("%s sha256 does not match manifest" % name)
    return data


def _npz(np, data, keys, what):
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            return {k: np.array(z[k]) for k in keys}
    except KeyError as exc:
        raise TraceFormatError("%s is missing array %s" % (what, exc)) from None
    except (ValueError, OSError, EOFError, zipfile.BadZipFile):
        raise TraceFormatError("%s is not a plain numeric npz" % what) from None


def _float_array(np, arr, shape, what):
    if arr.dtype.kind != "f" or arr.shape != shape or not np.isfinite(arr).all():
        raise TraceFormatError("%s must be finite floats of shape %s" % (what, shape))
    return arr.astype(np.float32, copy=False)


class LocalBackend:
    mode = "local"

    def __init__(self, directory, manifest, cases, manifest_sha, device):
        spec = _local_spec(manifest)
        kind = spec["predictor"]["kind"]
        needs_predictor = kind in ("ridge", "adapter")
        vector_bytes = _verified(directory, manifest, VECTORS_NAME, required=False)
        if vector_bytes is None:
            raise _Fallback("candidate has no %s" % VECTORS_NAME)
        predictor_bytes = _verified(directory, manifest, PREDICTOR_NAME, required=needs_predictor)
        try:
            import numpy as np
        except ImportError as exc:
            raise _Fallback("numpy unavailable: %s" % exc) from None
        from mr.trace_forecast import normalize, predict_ridge, retrieve_forecast
        from mr.trace_learning import predict_adapter
        self.np, self.normalize, self.cases = np, normalize, cases
        self.spec, self.kind = spec, kind
        self._retrieve, self._predict_ridge, self._predict_adapter = retrieve_forecast, predict_ridge, predict_adapter
        raw = _npz(np, vector_bytes, ("seed", "context", "target", "ids"), VECTORS_NAME)
        ids = raw.pop("ids")
        if ids.ndim != 1 or ids.dtype.kind != "U" or list(ids) != [c["id"] for c in cases]:
            raise TraceFormatError("training vector ids are not aligned with the case bank")
        n = len(cases)
        dims = {a.shape[1] for a in raw.values() if a.ndim == 2}
        if len(dims) != 1 or any(a.ndim != 2 for a in raw.values()):
            raise TraceFormatError("%s arrays must be 2D with one dimension" % VECTORS_NAME)
        self.dim = dims.pop()
        self.train = {k: _float_array(np, v, (n, self.dim), "train " + k) for k, v in raw.items()}
        self.unit = {k: normalize(self.train[k]) for k in ("context", "target")}
        self.model = None
        if needs_predictor:
            keys = (("weight", "x_mean", "y_mean") if kind == "ridge"
                    else ("down_weight", "down_bias", "up_weight", "up_bias"))
            arrays = _npz(np, predictor_bytes, keys, PREDICTOR_NAME)
            for k, v in arrays.items():
                if v.dtype.kind != "f" or not np.isfinite(v).all():
                    raise TraceFormatError("predictor array %s must be finite floats" % k)
            self.model = arrays
        self.encoder = None
        if n:
            path = Path(spec["encoder"]["path"])
            path = path if path.is_absolute() else Path(directory) / path
            hashes = spec["encoder"].get("weights_sha256", {})
            if not isinstance(hashes, dict):
                raise TraceFormatError("encoder weight digests must be an object")
            from mr.trace_learning import sha256
            for name, digest in hashes.items():
                relative = Path(name) if isinstance(name, str) else None
                if relative is None or relative.is_absolute() or ".." in relative.parts or "\\" in name or not rt._HEX64.fullmatch(str(digest)):
                    raise TraceFormatError("invalid encoder weight digest")
                if not (path/name).is_file() or sha256(path/name) != digest:
                    raise TraceFormatError("local encoder weight digest mismatch")
            try:
                self.encoder = _make_encoder(str(path), _pick_device(device), spec["max_length"])
            except ImportError as exc:
                raise _Fallback("local encoder dependencies unavailable: %s" % exc) from None
            except ValueError as exc:
                raise _Fallback(str(exc)) from None
        self.warnings = []
        self.provenance = {"kind": kind, "manifest_sha256": manifest_sha,
                           "predictor_sha256": manifest["files"].get(PREDICTOR_NAME) if needs_predictor else None,
                           "vector_sha256": manifest["files"][VECTORS_NAME]}

    def _embed(self, seed, context):
        np = self.np
        texts = [rt.render_input(_query_input(seed, context))]
        if self.kind == "seed":
            texts.append(rt.render_input(_query_input(seed, context), True))
        emb = np.asarray(self.encoder.encode(texts), dtype=np.float32)
        if emb.shape != (len(texts), self.dim) or not np.isfinite(emb).all():
            raise ValueError("encoder output does not match the candidate vectors")
        return self.normalize(emb)

    def _predict(self, q, seed_vec, exclude):
        np, kind, spec = self.np, self.kind, self.spec["predictor"]
        if kind == "context":
            return q
        if kind == "seed":
            return seed_vec
        if kind == "prior":
            return self.normalize(self.train["target"].mean(axis=0))
        if kind == "retrieval":
            return self._retrieve(q[None], self.train["context"], self.train["target"], spec["k"],
                                  spec["temperature"], exclude=[exclude])[0]
        if kind == "ridge":
            pred = self._predict_ridge(self.model, q[None])[0]
        else:
            pred = self._predict_adapter(self.model, q[None])[0]
        if spec["blend"] > 0:
            pred = self.normalize(((1 - spec["blend"]) * pred + spec["blend"] * q).astype(np.float32))
        return pred

    def pairs(self, seed, context):
        n = len(self.cases)
        exclude = _excluded(self.cases, context)
        if not n or len(set(exclude)) >= n:
            return []
        emb = self._embed(seed, context)
        q = emb[0]
        pred = self._predict(q, emb[-1], exclude)
        w = self.spec["future_weight"]
        scores = (1 - w) * (self.unit["context"] @ q) + w * (self.unit["target"] @ pred)
        if not self.np.isfinite(scores).all():
            raise ValueError("non-finite retrieval scores")
        return [(i, float(s)) for i, s in enumerate(scores)]


# ---------------------------------------------------------------- session

class Session:
    def __init__(self, args):
        directory = getattr(args, "candidate", None) or os.environ.get("MIND_READING_CANDIDATE") or ""
        if not directory.strip():
            raise CliError("no candidate: pass --candidate PATH or set MIND_READING_CANDIDATE")
        self.directory = os.fspath(Path(directory))
        before = self._manifest_bytes()
        self.manifest, self.cases = rt.load_cases(self.directory)
        if self._manifest_bytes() != before:
            raise TraceFormatError("manifest changed while it was being read")
        self.manifest_sha = hashlib.sha256(before).hexdigest()

    def _manifest_bytes(self):
        return rt._read_exact(self.directory, rt.MANIFEST_NAME, rt.MAX_MANIFEST_BYTES)

    def backend(self, local, device):
        warnings = []
        backend = None
        if local:
            try:
                backend = LocalBackend(self.directory, self.manifest, self.cases, self.manifest_sha, device)
            except _Fallback as exc:
                warnings.append("local retrieval unavailable, used lexical: %s" % exc)
        if backend is None:
            backend = LexicalBackend(self.cases)
            if local:
                backend.mode = "lexical_fallback"
        backend.provenance["manifest_sha256"] = self.manifest_sha
        backend.warnings = warnings
        return backend

    def reranker(self, path, device, warnings):
        if not path:
            return None
        try:
            from mr.trace_rerank import LocalReranker
            return LocalReranker(path, device=_pick_device(device))
        except ImportError as exc:
            warnings.append("rerank skipped, dependencies unavailable: %s" % exc)
        except ValueError as exc:
            warnings.append("rerank skipped: %s" % exc)
        return None


def _ranked(session, backend, reranker, seed, context):
    pairs = backend.pairs(seed, context)
    if reranker is None:
        return pairs
    skipped = set(_excluded(session.cases, context))
    cases = session.cases
    return rerank_pairs(pairs, lambda i: rt.render_input(cases[i]["input"]),
                        rt.render_input(_query_input(seed, context)), reranker, TOP_K, skipped.__contains__)


def _packet(session, backend, reranker, seed, context, project, limit):
    literal_project = project
    if project and project not in {c.get("project") for c in session.cases}:
        from mr.trace_build import project_identity
        project = project_identity(project, session.manifest.get("project_aliases", {}))
    out = rt.packet(seed, context, session.cases, session.manifest, project=project,
                    ranked=_ranked(session, backend, reranker, seed, context), limit=limit)
    out["retrieval_mode"] = backend.mode
    out["predictor_provenance"] = dict(backend.provenance)
    out["rerank"] = {"applied": True, "top_k": TOP_K} if reranker is not None else None
    out["warnings"] = list(backend.warnings)
    out["project_identity"] = project
    out["project"] = literal_project
    return out


# ---------------------------------------------------------------- commands

def cmd_packet(session, args, stdin):
    seed, context, project = _read_payload(stdin)
    backend = session.backend(args.local, args.device)
    return _packet(session, backend, session.reranker(args.rerank_path, args.device, backend.warnings),
                   seed, context, project, args.limit)


def cmd_counterfactual(session, args, stdin):
    seed, context, project = _read_payload(stdin)
    backend = session.backend(args.local, args.device)
    reranker = session.reranker(args.rerank_path, args.device, backend.warnings)
    full = _packet(session, backend, reranker, seed, context, project, args.limit)
    bare = _packet(session, backend, reranker, seed, dict(context, **CLEARED), project, args.limit)
    with_ids = [e["case_id"] for e in full["evidence"]]
    without_ids = [e["case_id"] for e in bare["evidence"]]
    brief = lambda p: [{"case_id": e["case_id"], "score": e["score"]} for e in p["evidence"]]  # noqa: E731
    return {"kind": "context_ablation", "note": ABLATION_NOTE, "literal_seed": seed, "project": project,
            "cleared_fields": sorted(CLEARED), "retrieval_mode": full["retrieval_mode"],
            "predictor_provenance": full["predictor_provenance"], "warnings": full["warnings"],
            "with_context": brief(full), "without_context": brief(bare),
            "changed_case_ids": {"gained_with_context": [i for i in with_ids if i not in without_ids],
                                 "lost_with_context": [i for i in without_ids if i not in with_ids]},
            "same_case_ids": [i for i in with_ids if i in without_ids]}


def cmd_status(session, args, stdin):
    split = rt._load_json(rt._read_exact(session.directory, rt.SPLIT_NAME, rt.MAX_MANIFEST_BYTES), rt.SPLIT_NAME)
    manifest = session.manifest
    return {"version": manifest["version"], "trained_through": manifest["trained_through"],
            "case_count": len(session.cases), "manifest_sha256": session.manifest_sha,
            "train_ids_sha256": manifest["train_ids_sha256"], "split_sha256": manifest["split_sha256"],
            "files": sorted(manifest["files"]),
            "partition_sizes": {"train": len(split["train_ids"]),
                                **{k: len(split.get(k, [])) for k in ("validation_ids", "test_ids", "excluded_ids")}},
            "families": len({c["family"] for c in session.cases}),
            "sources": {s: sum(1 for c in session.cases if c["source"] == s)
                        for s in sorted({c["source"] for c in session.cases})},
            "local_declared": isinstance(manifest.get("encoder"), dict) and isinstance(manifest.get("predictor"), dict),
            "verified": True}


def cmd_corrections(session, args, stdin):
    found = rt.correction_candidates(session.cases)
    return {"corrections": found, "count": len(found)}


def cmd_explain(session, args, stdin):
    matches = [c for c in session.cases if c["id"] == args.case_id]
    if len(matches) != 1:
        raise CliError("case id %r is not a train case in this candidate" % args.case_id)
    return {"case": matches[0]}


# ---------------------------------------------------------------- main

def _parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--candidate", default=argparse.SUPPRESS, metavar="PATH",
                        help="candidate directory (or MIND_READING_CANDIDATE)")
    parser = argparse.ArgumentParser(prog="intent.py", description=__doc__.split("\n")[0], parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (("packet", "evidence packet for a seed"), ("counterfactual", "context ablation of a packet")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--limit", type=int, default=6)
        p.add_argument("--local", action="store_true", help="use already-downloaded local weights; lexical on missing")
        p.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
        p.add_argument("--rerank-path", default=None, metavar="PATH", help="local reranker weights (top 12)")
    for name, helptext in (("status", "verified candidate summary"), ("corrections", "quoted correction pairs")):
        sub.add_parser(name, parents=[common], help=helptext)
    p = sub.add_parser("explain", parents=[common], help="one exact train case")
    p.add_argument("--case-id", required=True)
    return parser


COMMANDS = {"packet": cmd_packet, "counterfactual": cmd_counterfactual, "status": cmd_status,
            "corrections": cmd_corrections, "explain": cmd_explain}


def _dump(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates survive only as escapes
        return json.dumps(obj, ensure_ascii=True, allow_nan=False).encode("ascii")


def main(argv=None, stdin=None, stdout=None, stderr=None):
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    stderr = stderr if stderr is not None else sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        session = Session(args)
        result = COMMANDS[args.command](session, args, stdin)
        data = _dump(result) + b"\n"
    except (CliError, TraceFormatError, ValueError, OSError) as exc:
        stderr.write("intent: %s\n" % (str(exc).replace("\n", " ") or exc.__class__.__name__))
        return 2
    stdout.write(data)
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
