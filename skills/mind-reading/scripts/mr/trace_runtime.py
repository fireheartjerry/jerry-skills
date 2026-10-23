"""Train-only trajectory evidence for the host agent.

Given a literal current seed and the current situation, retrieve past (seed -> next user turn) examples from a
verified, train-only candidate and return a constrained packet. A historical next turn is one possible continuation:
never permission, never proof of endorsement. Current words and the harness always win. Time and other transient state
may shape presentation hints only. Stdlib only, no network, no model calls; learned retrieval can be supplied as
`ranked` (index, score) pairs.
"""
import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

VERSION = "trace-candidate-1"
MANIFEST_NAME = "manifest.json"
CASES_NAME = "cases.jsonl"
SPLIT_NAME = "split.json"
MAX_CASES_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_USER_TURNS, MAX_USER_CHARS = 3, 2000
MAX_AGENT_CHARS = 2000
MAX_TOOLS = 8
MAX_TOOL_FIELD = 80
MAX_CORRECTIONS = 100
MAX_COUNTEREXAMPLES = 5
PER_FAMILY = 2
MAX_LIMIT = 50

INTERPRETATION = ("ranking score from similarity of past situations; not a calibrated probability that the "
                  "historical continuation applies now")
CONSTRAINTS = {
    "authority": "current conversation and harness only",
    "historical_continuations": "evidence, not new assignments",
    "transient_state": "presentation only",
}
PRESENTATION_PREFERENCES = {
    "brief": "keep replies brief",
    "detailed": "give detailed replies",
    "step_by_step": "present steps in order",
    "plain_text": "use plain text without heavy formatting",
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_LABEL = re.compile(r"^(?:seed|current goal|previous user turn|previous assistant|tool): ", re.MULTILINE)
_CORRECTION = re.compile(r"^(?:no,|actually\b|i meant\b|don['’]t\b|not that\b|instead\b)")


class TraceFormatError(ValueError):
    pass


# ---------------------------------------------------------------- rendering

def _tail(text, n):
    return text[-n:] if len(text) > n else text


def _bounded_turns(inp):
    turns = inp.get("prior_user_turns")
    if not isinstance(turns, (list, tuple)):
        return []
    return [_tail(t, MAX_USER_CHARS) for t in list(turns)[-MAX_USER_TURNS:] if isinstance(t, str)]


def _bounded_agent(inp):
    text = inp.get("prior_agent_text")
    return _tail(text, MAX_AGENT_CHARS) if isinstance(text, str) else ""


def _bounded_tools(inp):
    tools = inp.get("prior_tools")
    if not isinstance(tools, (list, tuple)):
        return []
    out = []
    for tool in list(tools)[-MAX_TOOLS:]:
        if not isinstance(tool, dict):
            continue
        out.append({
            "name": str(tool.get("name", ""))[:MAX_TOOL_FIELD],
            "kind": str(tool.get("kind", ""))[:MAX_TOOL_FIELD],
            "success": tool.get("success") if isinstance(tool.get("success"), bool) else None,
        })
    return out


def render_input(inp, seed_only=False):
    """Embedding/lexical text for an input prefix. Never includes target, reaction_input, project or time."""
    if not isinstance(inp, dict):
        raise TypeError("input must be a dict")
    seed = inp.get("seed")
    seed = seed if isinstance(seed, str) else ""
    if seed_only:
        return seed
    parts = ["seed: " + seed]
    for turn in _bounded_turns(inp):
        parts.append("previous user turn: " + turn)
    agent = _bounded_agent(inp)
    if agent:
        parts.append("previous assistant: " + agent)
    for tool in _bounded_tools(inp):
        parts.append("tool: %s %s %s" % (tool["name"], tool["kind"], {True: "ok", False: "failed", None: "unknown"}[tool["success"]]))
    return "\n".join(parts)


# ---------------------------------------------------------------- loading

def _ts(value, what):
    if isinstance(value, bool):
        raise TraceFormatError("%s is not a timestamp" % what)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise TraceFormatError("%s is not finite" % what)
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise TraceFormatError("%s is not an ISO timestamp" % what) from None
        if dt.tzinfo is None:
            raise TraceFormatError("%s needs an explicit timezone" % what)
        return dt.timestamp()
    raise TraceFormatError("%s is not a timestamp" % what)


def _read_exact(directory, name, limit):
    """Open one allowlisted regular file inside `directory`; refuse symlinks; bounded read."""
    path = os.path.join(directory, name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        if os.path.islink(path):
            raise TraceFormatError("%s must not be a symlink" % name)
        fd = os.open(path, flags)
    except OSError as exc:
        raise TraceFormatError("cannot open %s: %s" % (name, exc.strerror or exc)) from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise TraceFormatError("%s is not a regular file" % name)
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise TraceFormatError("%s exceeds %d bytes" % (name, limit))
    return data


def _no_constant(name):
    raise TraceFormatError("non-finite JSON number %s" % name)


def _load_json(data, what):
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TraceFormatError("%s is not valid UTF-8 JSON" % what) from None


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise TraceFormatError("manifest must be an object")
    if manifest.get("version") != VERSION:
        raise TraceFormatError("manifest version must be %s" % VERSION)
    for key in ("train_ids_sha256", "split_sha256"):
        if not isinstance(manifest.get(key), str) or not _HEX64.match(manifest[key]):
            raise TraceFormatError("manifest %s must be a lowercase sha256 hex digest" % key)
    if "trained_through" not in manifest or manifest["trained_through"] in (None, ""):
        raise TraceFormatError("manifest is missing trained_through")
    cutoff = _ts(manifest["trained_through"], "trained_through")
    count = manifest.get("case_count")
    if type(count) is not int or count < 0:
        raise TraceFormatError("manifest case_count must be a non-negative integer")
    files = manifest.get("files")
    if not isinstance(files, dict) or CASES_NAME not in files:
        raise TraceFormatError("manifest files must list %s" % CASES_NAME)
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str) or not _HEX64.match(digest):
            raise TraceFormatError("manifest file digest for %r is not a sha256 hex digest" % (name,))
    return cutoff, count


def _validate_case(case, cutoff, line):
    def bad(msg):
        raise TraceFormatError("cases.jsonl line %d: %s" % (line, msg))

    if not isinstance(case, dict):
        bad("case must be an object")
    for key in ("id", "family", "source", "session"):
        if not _nonempty(case.get(key)):
            bad("%s must be a non-empty string" % key)
    if case.get("project") is not None and not isinstance(case["project"], str):
        bad("project must be a string or null")
    if case.get("origin") != "human":
        bad("origin must be 'human'")
    weight = case.get("weight")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight != 1:
        bad("weight must be 1")
    refs = case.get("source_refs")
    if isinstance(refs, dict):
        if not _nonempty(refs.get("seed")) or not _nonempty(refs.get("target")):
            bad("source_refs needs seed and target references")
        prior = refs.get("prior_user_turns", [])
        if not isinstance(prior, list) or not all(_nonempty(r) for r in prior):
            bad("invalid prior turn references")
    elif not isinstance(refs, list) or not refs or not all(_nonempty(r) for r in refs):
        bad("source_refs must contain exact observation references")
    inp = case.get("input")
    if not isinstance(inp, dict) or not isinstance(inp.get("seed"), str):
        bad("input.seed must be a string")
    if not isinstance(inp.get("prior_user_turns", []), list) or not all(isinstance(t, str) for t in inp.get("prior_user_turns", [])):
        bad("input.prior_user_turns must be a list of strings")
    if not isinstance(inp.get("prior_agent_text", ""), str):
        bad("input.prior_agent_text must be a string")
    if not isinstance(inp.get("prior_tools", []), list) or not all(isinstance(t, dict) for t in inp.get("prior_tools", [])):
        bad("input.prior_tools must be a list of objects")
    target = case.get("target")
    if not isinstance(target, dict) or not _nonempty(target.get("text")):
        bad("target.text must be a non-empty string")
    if not _nonempty(target.get("source_ref")):
        bad("target.source_ref must be a non-empty string")
    seed_time = _ts(case.get("seed_time"), "seed_time")
    target_time = _ts(case.get("target_time"), "target_time")
    if target_time > cutoff:
        bad("target_time is after trained_through")
    if seed_time > target_time:
        bad("seed_time is after target_time")
    if target.get("time") is not None and _ts(target["time"], "target.time") > cutoff:
        bad("target.time is after trained_through")


def load_cases(candidate_dir):
    """Load cases plus frozen partition metadata, never held-out conversation text.

    Hashes detect inconsistency, not a malicious builder rewriting all artifacts.
    An external evaluation receipt must anchor the exact candidate being claimed.
    """
    directory = os.fspath(Path(candidate_dir))
    if not os.path.isdir(directory):
        raise TraceFormatError("candidate directory not found")
    manifest = _load_json(_read_exact(directory, MANIFEST_NAME, MAX_MANIFEST_BYTES), "manifest")
    cutoff, count = _validate_manifest(manifest)
    split_raw = _read_exact(directory, SPLIT_NAME, MAX_MANIFEST_BYTES)
    if hashlib.sha256(split_raw).hexdigest() != manifest["split_sha256"]:
        raise TraceFormatError("frozen split sha256 does not match manifest")
    split = _load_json(split_raw, SPLIT_NAME)
    if not isinstance(split, dict) or not isinstance(split.get("train_ids"), list):
        raise TraceFormatError("split needs train_ids")
    train_ids = split["train_ids"]
    if not all(_nonempty(i) for i in train_ids) or len(set(train_ids)) != len(train_ids):
        raise TraceFormatError("invalid or duplicate split train ids")
    other_ids = set()
    for partition in ("validation_ids", "test_ids", "excluded_ids"):
        values = split.get(partition, [])
        if not isinstance(values, list) or not all(_nonempty(i) for i in values):
            raise TraceFormatError("invalid split partition")
        other_ids.update(values)
    if set(train_ids) & other_ids:
        raise TraceFormatError("training ids overlap another partition")
    raw = _read_exact(directory, CASES_NAME, MAX_CASES_BYTES)
    if hashlib.sha256(raw).hexdigest() != manifest["files"][CASES_NAME]:
        raise TraceFormatError("cases.jsonl sha256 does not match manifest")
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    cases, seen = [], set()
    for number, line in enumerate(lines, 1):
        case = _load_json(line, "cases.jsonl line %d" % number)
        _validate_case(case, cutoff, number)
        if case["id"] in seen:
            raise TraceFormatError("duplicate case id %r" % case["id"])
        seen.add(case["id"])
        cases.append(case)
    if len(cases) != count:
        raise TraceFormatError("case_count %d does not match %d cases" % (count, len(cases)))
    digest = hashlib.sha256(json.dumps(sorted(seen), separators=(",", ":")).encode()).hexdigest()
    if digest != manifest["train_ids_sha256"]:
        raise TraceFormatError("train ids sha256 does not match the case membership")
    if seen != set(train_ids):
        raise TraceFormatError("case ids do not equal frozen split train ids")
    return manifest, cases


# ---------------------------------------------------------------- lexical retrieval

def _tokens(text):
    """Content tokens of rendered text; the structural labels render_input adds are not indexed."""
    return _TOKEN.findall(_LABEL.sub("", text).casefold())


def _bm25(query_tokens, doc_tokens, k1=1.5, b=0.75):
    """Normalised BM25 of one query against every doc; result in [0, 1)."""
    n = len(doc_tokens)
    terms = set(query_tokens)
    if not n or not terms:
        return [0.0] * n
    freqs, df, total = [], {}, 0
    for toks in doc_tokens:
        tf = {}
        for tok in toks:
            if tok in terms:
                tf[tok] = tf.get(tok, 0) + 1
        freqs.append(tf)
        total += len(toks)
        for tok in tf:
            df[tok] = df.get(tok, 0) + 1
    avg = total / n or 1.0
    idf = {t: math.log(1.0 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5)) for t in terms}
    ceiling = sum(idf[t] * (k1 + 1.0) for t in terms)
    out = []
    for toks, tf in zip(doc_tokens, freqs):
        norm = k1 * (1.0 - b + b * len(toks) / avg)
        out.append(sum(idf[t] * f * (k1 + 1.0) / (f + norm) for t, f in tf.items()) / ceiling)
    return out


def _context_input(seed, context):
    ctx = context if isinstance(context, dict) else {}
    return {"seed": seed if isinstance(seed, str) else "", "prior_user_turns": ctx.get("prior_user_turns"),
            "prior_agent_text": ctx.get("prior_agent_text"), "prior_tools": ctx.get("prior_tools"), "goal": ctx.get("goal")}


def lexical_scores(seed, context, cases):
    """Deterministic BM25-style scores of the current prefix against past prefixes. Targets are never indexed."""
    query = _context_input(seed, context)
    seed_scores = _bm25(_tokens(render_input(query, True)), [_tokens(render_input(c["input"], True)) for c in cases])
    full_scores = _bm25(_tokens(render_input(query)), [_tokens(render_input(c["input"])) for c in cases])
    return [0.65 * s + 0.35 * f for s, f in zip(seed_scores, full_scores)]


# ---------------------------------------------------------------- packet

def _validate_ranked(ranked, n):
    if isinstance(ranked, (str, bytes)) or not isinstance(ranked, (list, tuple)):
        raise ValueError("ranked must be a list of (index, score) pairs")
    out, seen = [], set()
    for item in ranked:
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("ranked entries must be (index, score) pairs")
        index, score = item
        if type(index) is not int or not 0 <= index < n:
            raise ValueError("ranked index %r is out of range" % (index,))
        if index in seen:
            raise ValueError("ranked index %d is duplicated" % index)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("ranked score for index %d must be a finite number" % index)
        seen.add(index)
        out.append((index, float(score)))
    return out


def _norm_text(text):
    return " ".join(text.casefold().split())


def _presentation_hints(context):
    explicit = context.get("presentation_preference")
    if isinstance(explicit, str) and explicit in PRESENTATION_PREFERENCES:
        return [PRESENTATION_PREFERENCES[explicit] + " (stated preference)"]
    # A late timestamp alone does not establish energy, mood, or desired brevity.
    # Inferred presentation defaults require separately validated evidence.
    return []


def packet(seed, context, cases, manifest, project=None, ranked=None, limit=6, evidence_budget_chars=24000):
    """Constrained evidence frame for the host agent. Never raises for lack of evidence."""
    context = context if isinstance(context, dict) else {}
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit must be an integer from 1 to %d" % MAX_LIMIT)
    if type(evidence_budget_chars) is not int or evidence_budget_chars < 2000:
        raise ValueError("evidence budget must be at least 2000 characters")
    if ranked is None:
        pairs = [(i, s) for i, s in enumerate(lexical_scores(seed, context, cases)) if s > 0.0]
    else:
        pairs = _validate_ranked(ranked, len(cases))
    project = project if isinstance(project, str) and project.strip() else None
    skip_session = context.get("session") if _nonempty(context.get("session")) else None
    skip_family = context.get("family") if _nonempty(context.get("family")) else None

    def same_project(case):
        return project is not None and case.get("project") == project

    # Score ties (to 9 places) fall to same-project, then original index; cross-project cases stay eligible.
    ordered = sorted(pairs, key=lambda p: (-round(p[1], 9), not same_project(cases[p[0]]), p[0]))
    chosen, per_family = [], {}
    for index, score in ordered:
        case = cases[index]
        if case["session"] == skip_session or case["family"] == skip_family:
            continue
        if per_family.get(case["family"], 0) >= PER_FAMILY:
            continue
        per_family[case["family"]] = per_family.get(case["family"], 0) + 1
        chosen.append((case, score))
        if len(chosen) >= limit:
            break

    evidence = [{
        "case_id": c["id"], "family": c["family"], "source": c["source"], "source_refs": c["source_refs"],
        "past_seed": c["input"]["seed"][:1200],
        "past_prefix": {"prior_user_turns": [t[:600] for t in _bounded_turns(c["input"])], "prior_agent_text": _bounded_agent(c["input"])[:700]},
        "historical_next_user_turn": c["target"]["text"][:1600], "score": s,
        "continuation_key": hashlib.sha256(_norm_text(c["target"]["text"]).encode()).hexdigest()[:20],
        "excerpted": len(c["target"]["text"]) > 1600 or len(c["input"]["seed"]) > 1200,
        "historical_response": {"assistant_text": str(c.get("historical_response", {}).get("assistant_text", ""))[:1200],
                                "interpretation": "The next historical user turn may react to this observed action; it does not by itself reveal a preference."},
        "historical_outcomes": c.get("historical_outcomes", []),
        "source_locations": c.get("source_locations", []),
        "relation": "same_project" if same_project(c) else "cross_project_analogy",
    } for c, s in chosen]
    kept, used = [], 0
    for item in evidence:
        cost = len(json.dumps(item, ensure_ascii=False))
        if used+cost > evidence_budget_chars:
            continue
        kept.append(item); used += cost
    evidence = kept

    groups = {}
    for item in evidence:
        key = item["continuation_key"]
        group = groups.setdefault(key, {"historical_next_user_turn": item["historical_next_user_turn"], "count": 0,
                                        "case_ids": [], "families": [], "best_score": item["score"]})
        group["count"] += 1
        group["case_ids"].append(item["case_id"])
        if item["family"] not in group["families"]:
            group["families"].append(item["family"])
        group["best_score"] = max(group["best_score"], item["score"])
    alternatives = sorted(groups.values(), key=lambda g: -g["best_score"])

    ctx = _context_input(seed, context)
    manifest = manifest if isinstance(manifest, dict) else {}
    from .trace_preferences import select_quotes
    return {
        "literal_seed": seed,
        "current_context": {"prior_user_turns": _bounded_turns(ctx), "prior_agent_text": _bounded_agent(ctx),
                            "prior_tools": _bounded_tools(ctx),
                            "goal": context.get("goal") if isinstance(context.get("goal"), str) else None,
                            "phase": context.get("phase") if isinstance(context.get("phase"), str) else None,
                            "verified_facts": [f[:2000] for f in context.get("verified_facts", []) if isinstance(f, str)][:12]
                            if isinstance(context.get("verified_facts", []), list) else []},
        "project": project,
        "evidence": evidence,
        "alternatives": alternatives,
        "preference_evidence": select_quotes(seed, context, cases, pairs, project=project),
        "evidence_budget": {"maximum_chars": evidence_budget_chars, "used_chars": used,
                            "full_training_case": "intent.py explain --case-id <id>"},
        "uncertainty": {"support_families": len({e["family"] for e in evidence}),
                        "top_similarity": max((e["score"] for e in evidence), default=0.0),
                        "interpretation": INTERPRETATION},
        "constraints": dict(CONSTRAINTS),
        "presentation_hints": _presentation_hints(context),
        "provenance": {"version": manifest.get("version"), "trained_through": manifest.get("trained_through"),
                       "case_count": manifest.get("case_count")},
        "note": ("" if evidence else "no train-only evidence matched; proceed from the current words alone"),
    }


# ---------------------------------------------------------------- corrections

def correction_candidates(cases):
    """Quoted seed -> next-turn pairs where the next turn opens with explicit correction wording.

    Pure quotation: no inferred dissatisfaction, no rule. Counterexamples are other cases with the same normalized
    seed and a different next turn.
    """
    by_seed = {}
    for case in cases:
        by_seed.setdefault(_norm_text(case["input"]["seed"]), []).append(case)
    out = []
    for case in cases:
        after = case["target"]["text"]
        if not _CORRECTION.match(after.strip().casefold()):
            continue
        mine = _norm_text(after)
        counter = [{"case_id": o["id"], "after": o["target"]["text"], "source": o["source"],
                    "source_ref": o["target"]["source_ref"]}
                   for o in by_seed[_norm_text(case["input"]["seed"])]
                   if o is not case and _norm_text(o["target"]["text"]) != mine][:MAX_COUNTEREXAMPLES]
        out.append({"kind": "possible_correction", "case_id": case["id"], "source": case["source"],
                    "source_refs": case["source_refs"], "source_ref": case["target"]["source_ref"],
                    "before": case["input"]["seed"], "after": after, "counterexamples": counter})
        if len(out) >= MAX_CORRECTIONS:
            break
    return out
