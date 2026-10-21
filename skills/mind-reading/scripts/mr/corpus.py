"""Append-only corpus: corpus/messages.jsonl plus manifest, scrub report and unknown-tag counts.

Key per record: sha256("{source}\\n{session}\\n{record_id}")[:24]. Every run parses every source file, builds the
candidate set, and appends records whose key is unseen. Records are never removed or rewritten, except by
`extract --rescrub`, which re-applies the scrub in place (keys unchanged, scrub_version bumped)."""
import datetime as _dt
import hashlib
import json
import os
import re
import string
from pathlib import Path

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


def _load_keys(messages_path):
    keys = set()
    if not Path(messages_path).exists():
        return keys
    with open(messages_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                keys.add(json.loads(line)["key"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    return keys


def _file_stat(path):
    try:
        st = os.stat(path)
        return {"size": st.st_size, "mtime": _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    except OSError:
        return {"size": None, "mtime": None}


def build_candidates(raw_iter, people_re, scrub_counts, unknown_tags):
    """Apply clean/keep/origin/scrub to raw reader records. Yields corpus records without first_seen."""
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
        yield {
            "key": record_key(raw["source"], raw["session"], raw["record_id"]),
            "source": raw["source"], "session": raw["session"], "record_id": raw["record_id"],
            "project": project or None, "time": raw.get("time"), "ordinal": raw.get("ordinal", 0),
            "origin": origin, "text": text, "text_norm_hash": norm_hash(text), "meta": meta,
            "scrub_version": scrub.SCRUB_VERSION, "_path": raw.get("path"),
        }


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
    if claude_dir and Path(os.path.expanduser(str(claude_dir))).exists():
        readers.append(("claude-code", extract_claude.iter_records(claude_dir)))
    if codex_dir and Path(os.path.expanduser(str(codex_dir))).exists():
        readers.append(("codex", extract_codex.iter_records(codex_dir)))
    existing = _load_keys(paths.messages)
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
            for rec in build_candidates(it, people_re, scrub_counts, unknown):
                p = rec.pop("_path")
                seen[source] += 1
                seen_this_run[p] = seen_this_run.get(p, 0) + 1
                if rec["key"] in run_keys:
                    dup_in_run += 1
                    continue
                run_keys.add(rec["key"])
                if rec["key"] in existing:
                    continue
                rec["first_seen"] = stamp
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                existing.add(rec["key"])
                added[source] += 1
                first_seen_this_run[p] = first_seen_this_run.get(p, 0) + 1
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
               "scrub": scrub_counts, "unknown_tags": unknown, "sources": {"claude": str(claude_dir), "codex": str(codex_dir)}}
    log("extract: sources claude=%s codex=%s; seen %s, added %s, corpus %d records" % (claude_dir, codex_dir, seen, added, len(existing)))
    return summary


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
