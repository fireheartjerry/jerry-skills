"""Private, resumable event tape. Raw files remain read-only; models get separate views.

The manifest freezes each plain transcript at its initial byte length. Compressed
files must remain immutable. Native event collisions are reported, never silently
treated as additional human observations. SQLite and source paths stay private.
"""
import hashlib
import json
from pathlib import Path
import sqlite3


def _digest(path, size=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        remaining = size
        while remaining is None or remaining > 0:
            chunk = f.read(min(1024 * 1024, remaining) if remaining is not None else 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    if remaining not in (None, 0):
        raise ValueError("source shrank during snapshot")
    return h.hexdigest()


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY, path TEXT, source TEXT, machine TEXT, size INTEGER,
        sha256 TEXT, parser TEXT, diagnostics TEXT, event_count INTEGER);
      CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, session_key TEXT, source TEXT, role TEXT, actor TEXT,
        time TEXT, ordinal INTEGER, data TEXT);
      CREATE INDEX IF NOT EXISTS event_session ON events(session_key,ordinal);
      CREATE TABLE IF NOT EXISTS locations (
        event_id TEXT, file_id TEXT, source_ref TEXT,
        PRIMARY KEY(event_id,file_id,source_ref));
      CREATE TABLE IF NOT EXISTS conflicts (
        event_id TEXT, file_id TEXT, reason TEXT,
        PRIMARY KEY(event_id,file_id));
    """)
    return db


def parser_fingerprint():
    from . import trace_ingest
    return _digest(trace_ingest.__file__)


def ingest_file(db, path, source, machine="local", parser=None, iterator=None):
    from .trace_ingest import iter_events
    iterator = iterator or iter_events
    path = Path(path).resolve()
    fid = hashlib.sha256(str(path).encode()).hexdigest()[:24]
    parser = parser or parser_fingerprint()
    prior = db.execute("SELECT parser,diagnostics,event_count FROM files WHERE id=?", (fid,)).fetchone()
    if prior:
        if prior[0] != parser:
            raise ValueError("parser changed: use a new tape, never mix parser revisions")
        return {"cached": True, "events": prior[2], "diagnostics": json.loads(prior[1])}
    size = path.stat().st_size
    sha = _digest(path, size)
    compressed = path.name.endswith(".zst")
    # SQLite sources are queried read-only by the parser, not byte-streamed.
    limit = None if compressed or path.suffix in (".sqlite", ".db") else size
    diagnostics, count, collisions = {}, 0, 0
    db.execute("SAVEPOINT source_file")
    try:
        for ev in iterator(path, source, machine=machine, limit_bytes=limit):
            if (ev.get("action") or {}).get("kind") == "source_summary":
                diagnostics = json.loads(ev["action"]["summary"])
                continue
            if not ev.get("session"):
                continue
            stored = db.execute("SELECT data FROM events WHERE id=?", (ev["id"],)).fetchone()
            if stored:
                old = json.loads(stored[0])
                fields = ("source", "session", "role", "actor", "text", "time", "action")
                if any(old.get(k) != ev.get(k) for k in fields):
                    collisions += 1
                    db.execute("INSERT OR IGNORE INTO conflicts VALUES (?,?,?)", (ev["id"], fid, "native_id_content_conflict"))
            else:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
                    ev["id"], ev["source"] + ":" + ev["session"], source, ev["role"], ev["actor"],
                    ev.get("time"), ev.get("ordinal"), json.dumps(ev, ensure_ascii=False)))
            db.execute("INSERT OR IGNORE INTO locations VALUES (?,?,?)", (ev["id"], fid, ev.get("source_ref") or ""))
            count += 1
        if _digest(path, size) != sha or (compressed and path.stat().st_size != size):
            raise ValueError("source changed inside frozen snapshot")
        diagnostics["native_id_conflicts"] = collisions
        db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)", (
            fid, str(path), source, machine, size, sha, parser,
            json.dumps(diagnostics, ensure_ascii=False), count))
        db.execute("RELEASE source_file")
        db.commit()
    except BaseException:
        db.execute("ROLLBACK TO source_file")
        db.execute("RELEASE source_file")
        raise
    return {"cached": False, "events": count, "diagnostics": diagnostics}


def iter_sessions(db, keys=None):
    """One session at a time, so a multi-gigabyte tape needn't fit in RAM."""
    if keys is None:
        keys = [r[0] for r in db.execute("SELECT DISTINCT session_key FROM events ORDER BY session_key")]
    for key in keys:
        yield key, [json.loads(r[0]) for r in db.execute(
            "SELECT data FROM events WHERE session_key=? ORDER BY ordinal,id", (key,))]


def make_examples(db, allow_unknown=False):
    from .trace_cases import build_examples, families
    # Parent links and human trajectories establish global families. Assistant and
    # tool payloads are only loaded for the one session whose prefix needs them.
    skeleton = []
    links = set()
    for key, session in iter_sessions(db):
        for ev in session:
            link = (key, ev.get("parent_session"))
            if ev["role"] == "user" or (ev.get("parent_session") and link not in links):
                skeleton.append(ev)
            if ev.get("parent_session"):
                links.add(link)
    mapping = families(skeleton)
    examples = []
    actors = ("human", "unknown") if allow_unknown else ("human",)
    eligible = [r[0] for r in db.execute("SELECT DISTINCT session_key FROM events WHERE role='user' AND actor IN (%s) ORDER BY session_key" % ",".join("?" for _ in actors), actors)]
    for key, session in iter_sessions(db, eligible):
        batch = build_examples(session, allow_unknown=allow_unknown)
        for ex in batch:
            ex["family"] = mapping.get(key, ex["family"])
        examples.extend(batch)
    return examples


def coverage(db):
    return {
        "files": db.execute("SELECT count(*) FROM files").fetchone()[0],
        "bytes": db.execute("SELECT coalesce(sum(size),0) FROM files").fetchone()[0],
        "events": db.execute("SELECT count(*) FROM events").fetchone()[0],
        "native_id_conflicts": db.execute("SELECT count(*) FROM conflicts").fetchone()[0],
        "roles": {"/".join(r[:3]): r[3] for r in db.execute(
            "SELECT source,role,actor,count(*) FROM events GROUP BY source,role,actor")},
        "sessions": {r[0]: r[1] for r in db.execute(
            "SELECT source,count(DISTINCT session_key) FROM events GROUP BY source")},
    }


def import_bundle(db, path, expected_parser):
    """Import a gzip JSONL tape parsed next to remote raw bytes, with file receipts.

    This exchanges local observations between the user's machines, not a model
    service. Each file is transactional; an incomplete stream leaves its file out.
    The remote file hashes are observations in the receipt, not locally verified
    raw bytes. A caller must anchor the parser revision before accepting a bundle.
    """
    import gzip
    current, diagnostics, count = None, {}, 0
    active = False
    imported = cached = 0
    try:
        with gzip.open(str(path), "rt", encoding="utf-8") as f:
            for line in f:
                item = json.loads(line)
                kind = item.get("kind")
                if kind == "start":
                    if current is not None:
                        raise ValueError("nested bundle file")
                    current = item["file"]
                    if current.get("parser") != expected_parser:
                        raise ValueError("remote parser revision mismatch")
                    if current.get("source") not in ("claude", "codex", "kimi", "grok", "memory"):
                        raise ValueError("invalid remote source")
                    skip = db.execute("SELECT 1 FROM files WHERE id=?", (current["id"],)).fetchone() is not None
                    db.execute("SAVEPOINT remote_file")
                    active = True
                    count = 0
                elif kind == "event":
                    if current is None:
                        raise ValueError("event outside bundle file")
                    if skip:
                        continue
                    ev = item["event"]
                    if ev["source"] != current["source"] or ev["role"] not in ("user", "assistant", "tool", "meta") or ev["actor"] not in ("human", "agent", "unknown", "injected"):
                        raise ValueError("invalid remote event provenance")
                    if not ev.get("session"):
                        continue
                    old = db.execute("SELECT data FROM events WHERE id=?", (ev["id"],)).fetchone()
                    if old:
                        old = json.loads(old[0])
                        if any(old.get(k) != ev.get(k) for k in ("source", "session", "role", "actor", "text", "time", "action")):
                            db.execute("INSERT OR IGNORE INTO conflicts VALUES (?,?,?)", (ev["id"], current["id"], "remote_native_id_content_conflict"))
                    else:
                        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
                            ev["id"], ev["source"]+":"+ev["session"], ev["source"], ev["role"], ev["actor"], ev.get("time"),
                            ev.get("ordinal"), json.dumps(ev, ensure_ascii=False)))
                    db.execute("INSERT OR IGNORE INTO locations VALUES (?,?,?)", (ev["id"], current["id"], ev.get("source_ref") or ""))
                    count += 1
                elif kind == "end":
                    if current is None or current["id"] != item.get("file_id"):
                        raise ValueError("bundle file end mismatch")
                    if not skip:
                        if count != item["event_count"]:
                            raise ValueError("bundle event count mismatch")
                        db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)", (
                            current["id"], current["path"], current["source"], current["machine"], current["size"],
                            current["sha256"], current["parser"], json.dumps(item["diagnostics"]), count))
                        imported += 1
                    else:
                        cached += 1
                    db.execute("RELEASE remote_file")
                    active = False
                    db.commit()
                    current = None
                else:
                    raise ValueError("unknown bundle record")
        if current is not None:
            raise ValueError("incomplete bundle file")
    except BaseException:
        if active:
            db.execute("ROLLBACK TO remote_file")
            db.execute("RELEASE remote_file")
        raise
    return {"imported": imported, "cached": cached}
