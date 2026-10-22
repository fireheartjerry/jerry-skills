"""Append-only corpus: corpus/messages.jsonl plus manifest, scrub report and unknown-tag counts.

Key per record: sha256("{source}\\n{session}\\n{record_id}")[:24]. Every run parses every source file, builds the
candidate set, and appends records whose key is unseen. Records are never removed or rewritten, except by
`extract --rescrub`, which re-applies the scrub in place (keys unchanged, scrub_version bumped), and the one-time
agent-context backfill: when `extract.agent_context_chars` is above zero, a stored record that predates the field gets
its `agent_context` set in place the next time its source file is read (keys and text unchanged).

`agent_context` ({"origin": "agent_context", "head", "markers"} or None) is the head of the agent's last reply before
the message and how that turn ended (agent_context.py). It is scrubbed like the text, kept apart from `text`, and never
the user's voice."""
import datetime as _dt
import hashlib
import json
import os
import re
import string
from pathlib import Path

from . import agent_context as _ac
from . import extract_claude, extract_codex, filters, scrub
from .config import read_json, write_json

_WS = re.compile(r"\s+")
_PUNCT = string.punctuation + "‘’“”…"


def normalize(text):
    """Dedup key: lowercase, collapse whitespace, strip leading and trailing punctuation. No digit folding."""
    t = _WS.sub(" ", (text or "").lower()).strip()
    return t.strip(_PUNCT + " ")


def norm_hash(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


def record_key(source, session, record_id):
    return hashlib.sha256(("%s\n%s\n%s" % (source, session, record_id)).encode("utf-8")).hexdigest()[:24]


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _project_label(project):
    if not project:
        return None
    p = str(project)
    m = re.match(r"^-(?:Users|home)-[^-]+-?(.*)$", p)
    if m:
        return "~/" + m.group(1).replace("-", "/")
    return p


def _load_keys(messages_path, missing=None):
    """Keys of the stored records. `missing`, when a set, collects the keys of records without an `agent_context` field."""
    keys = set()
    if not Path(messages_path).exists():
        return keys
    with open(messages_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                keys.add(r["key"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if missing is not None and "agent_context" not in r:
                missing.add(r["key"])
    return keys


def context_chars(cfg):
    """`extract.agent_context_chars` (0 turns the agent context off)."""
    try:
        return max(0, int(((cfg or {}).get("extract") or {}).get("agent_context_chars", _ac.DEFAULT_CHARS)))
    except (TypeError, ValueError):
        return _ac.DEFAULT_CHARS


def clean_agent_context(ctx, people_re, counts):
    """The stored form of a reader's agent context: harness markup stripped, scrubbed, labelled. None when empty."""
    if not isinstance(ctx, dict):
        return None
    head, _ = filters.clean(ctx.get("head") or "")
    local = {}
    head, _ = scrub.scrub(head, people_re, local)
    for k, n in local.items():
        counts["agent_context:" + k] = counts.get("agent_context:" + k, 0) + n
    markers = [m for m in _ac.MARKERS if m in (ctx.get("markers") or [])]
    head = head.strip()
    if not head and not markers:
        return None
    return {"origin": _ac.ORIGIN, "head": head, "markers": markers}


def _file_stat(path):
    try:
        st = os.stat(path)
        return {"size": st.st_size, "mtime": _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    except OSError:
        return {"size": None, "mtime": None}


def build_candidates(raw_iter, people_re, scrub_counts, unknown_tags, with_context=False):
    """Apply clean/keep/origin/scrub to raw reader records. Yields corpus records without first_seen. With
    `with_context` every record carries an `agent_context` field (None when the turn left nothing)."""
    for raw in raw_iter:
        text, info = filters.clean(raw["text"])
        if not filters.keep(text):
            continue
        tag = filters.unknown_tag(text)
        if tag:
            unknown_tags[tag] = unknown_tags.get(tag, 0) + 1
        origin = filters.origin(text, info, raw.get("origin_hint"))
        text, _ = scrub.scrub(text, people_re, scrub_counts)
        project, _ = scrub.scrub(_project_label(raw.get("project")) or "", people_re, {})
        meta = dict(raw.get("meta") or {})
        if "question" in meta:
            meta["question"], _ = scrub.scrub(meta["question"], people_re, scrub_counts)
        rec = {
            "key": record_key(raw["source"], raw["session"], raw["record_id"]),
            "source": raw["source"], "session": raw["session"], "record_id": raw["record_id"],
            "project": project or None, "time": raw.get("time"), "ordinal": raw.get("ordinal", 0),
            "origin": origin, "text": text, "text_norm_hash": norm_hash(text), "meta": meta,
            "scrub_version": scrub.SCRUB_VERSION, "_path": raw.get("path"),
        }
        if with_context:
            rec["agent_context"] = clean_agent_context(raw.get("agent_context"), people_re, scrub_counts)
        yield rec


def run_extract(paths, cfg, claude_dir=None, codex_dir=None, rescrub=False, log=print):
    """Append new records. Returns a summary dict."""
    paths.ensure_tree()
    people_re = scrub.compile_people(cfg.get("people") or {})
    if rescrub:
        return _rescrub(paths, people_re, log)
    explicit = {}
    if claude_dir:
        explicit["claude_projects_dir"] = str(claude_dir)
    if codex_dir:
        explicit["codex_sessions_dir"] = str(codex_dir)
    if explicit and any(cfg["sources"].get(k) != v for k, v in explicit.items()):
        # an explicit directory is remembered so refresh --extend reads the same transcripts next time
        cfg["sources"].update(explicit)
        if cfg.get("_exists"):
            from .config import save_config
            save_config(paths, cfg)
    claude_dir = claude_dir or cfg["sources"]["claude_projects_dir"]
    codex_dir = codex_dir or cfg["sources"]["codex_sessions_dir"]
    readers = []
    n_ctx = context_chars(cfg)
    if claude_dir and Path(os.path.expanduser(str(claude_dir))).exists():
        readers.append(("claude-code", extract_claude.iter_records(claude_dir, context_chars=n_ctx)))
    if codex_dir and Path(os.path.expanduser(str(codex_dir))).exists():
        readers.append(("codex", extract_codex.iter_records(codex_dir, context_chars=n_ctx)))
    missing_ctx = set()
    existing = _load_keys(paths.messages, missing_ctx if n_ctx else None)
    backfill = {}
    manifest = read_json(paths.manifest, {"version": 2, "files": {}}) or {"version": 2, "files": {}}
    files = manifest.setdefault("files", {})
    unknown = read_json(paths.unknown_tags, {}) or {}
    scrub_counts = {}
    stamp = now_iso()
    seen_this_run = {}
    first_seen_this_run = {}
    added = {"claude-code": 0, "codex": 0}
    seen = {"claude-code": 0, "codex": 0}
    dup_in_run = 0
    run_keys = set()
    paths.messages.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.messages, "a", encoding="utf-8") as out:
        for source, it in readers:
            for rec in build_candidates(it, people_re, scrub_counts, unknown, with_context=bool(n_ctx)):
                p = rec.pop("_path")
                seen[source] += 1
                seen_this_run[p] = seen_this_run.get(p, 0) + 1
                if rec["key"] in run_keys:
                    dup_in_run += 1
                    continue
                run_keys.add(rec["key"])
                if rec["key"] in existing:
                    if rec["key"] in missing_ctx:
                        backfill[rec["key"]] = rec.get("agent_context")
                    continue
                rec["first_seen"] = stamp
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                existing.add(rec["key"])
                added[source] += 1
                first_seen_this_run[p] = first_seen_this_run.get(p, 0) + 1
    if backfill:
        _backfill_context(paths, backfill)
        log("extract: added agent context to %d stored record(s) that predate it" % len(backfill))
    for p, n in seen_this_run.items():
        entry = files.get(p) or {"records_first_seen": 0}
        entry.update(_file_stat(p))
        entry["records_seen"] = n
        entry["records_first_seen"] = entry.get("records_first_seen", 0) + first_seen_this_run.get(p, 0)
        entry["last_scan"] = stamp
        files[p] = entry
    manifest["last_run"] = stamp
    write_json(paths.manifest, manifest)
    write_json(paths.unknown_tags, dict(sorted(unknown.items(), key=lambda kv: -kv[1])))
    report = read_json(paths.scrub_report, {"runs": []}) or {"runs": []}
    report["runs"].append({"time": stamp, "scrub_version": scrub.SCRUB_VERSION, "counts": scrub_counts})
    report["runs"] = report["runs"][-50:]
    write_json(paths.scrub_report, report)
    summary = {"seen": seen, "added": added, "duplicates_in_run": dup_in_run, "total": len(existing),
               "agent_context_backfilled": len(backfill), "agent_context_chars": n_ctx,
               "scrub": scrub_counts, "unknown_tags": unknown, "sources": {"claude": str(claude_dir), "codex": str(codex_dir)}}
    log("extract: sources claude=%s codex=%s; seen %s, added %s, corpus %d records" % (claude_dir, codex_dir, seen, added, len(existing)))
    return summary


def _backfill_context(paths, backfill):
    """Set `agent_context` in place on stored records that lack the field (keys, text and order unchanged)."""
    tmp = paths.messages.with_suffix(".jsonl.tmp")
    with open(paths.messages, encoding="utf-8", errors="replace") as fh, open(tmp, "w", encoding="utf-8") as out:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                out.write(line)
                continue
            if isinstance(r, dict) and r.get("key") in backfill and "agent_context" not in r:
                r["agent_context"] = backfill[r["key"]]
                line = json.dumps(r, ensure_ascii=False) + "\n"
            out.write(line)
    os.replace(tmp, paths.messages)


def _rescrub(paths, people_re, log):
    rows = []
    counts = {}
    if paths.messages.exists():
        with open(paths.messages, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                r["text"], _ = scrub.scrub(r.get("text", ""), people_re, counts)
                if r.get("project"):
                    r["project"], _ = scrub.scrub(r["project"], people_re, {})
                if (r.get("meta") or {}).get("question"):
                    r["meta"]["question"], _ = scrub.scrub(r["meta"]["question"], people_re, counts)
                ctx = r.get("agent_context")
                if isinstance(ctx, dict) and ctx.get("head"):
                    ctx["head"], _ = scrub.scrub(ctx["head"], people_re, counts)
                r["text_norm_hash"] = norm_hash(r["text"])
                r["scrub_version"] = scrub.SCRUB_VERSION
                rows.append(r)
    tmp = paths.messages.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as out:
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, paths.messages)
    report = read_json(paths.scrub_report, {"runs": []}) or {"runs": []}
    report["runs"].append({"time": now_iso(), "scrub_version": scrub.SCRUB_VERSION, "counts": counts, "rescrub": True})
    write_json(paths.scrub_report, report)
    log("rescrub: %d records, counts %s" % (len(rows), counts))
    return {"rescrubbed": len(rows), "scrub": counts}


def load_corpus(paths):
    rows = []
    if not paths.messages.exists():
        return rows
    with open(paths.messages, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def corpus_hash(rows):
    h = hashlib.sha256()
    for k in sorted(r["key"] for r in rows):
        h.update(k.encode("utf-8"))
    return h.hexdigest()[:16]


def sources_gone(paths):
    """[(path, records_first_seen)] for manifest entries whose file is missing on disk."""
    manifest = read_json(paths.manifest, {}) or {}
    gone = []
    for p, entry in (manifest.get("files") or {}).items():
        if not Path(p).exists():
            gone.append((p, entry.get("records_first_seen", 0)))
    return gone
