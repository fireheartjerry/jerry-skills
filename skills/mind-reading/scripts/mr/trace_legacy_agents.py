"""Legacy Claude worker traces are agent evidence, never independent user voice.

Older clients put agent-*.jsonl beside main sessions, not in subagents/. Workflow
journals are machine metadata. Keep this large secondary tape separate from the
personal training cohort so copied instructions never inflate preference support.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
from . import trace_ingest as ti


def discover(root):
    return sorted(p for p in ti._rglob(Path(root), lambda n: (n.startswith("agent-") and n.endswith(".jsonl")) or n == "journal.jsonl")
                  if (p.name.startswith("agent-") and p.parent.name != "subagents")
                  or (p.name == "journal.jsonl" and "workflows" in p.parts and "subagents" in p.parts))


def iter_events(path, source="claude", machine="local", limit_bytes=None):
    path = Path(path)
    if path.name == "journal.jsonl":
        run = ti._Run(path, "claude", machine, limit_bytes)
        parts = path.parts
        parent = parts[parts.index("subagents")-1] if "subagents" in parts else None
        session = (parent or "workflow")+"/"+path.parent.name
        for _, row in ti._rows(run):
            typ = str(row.get("type", "workflow_event"))[:80]
            yield from run.ev(native=None, role="meta", actor="agent", text="", session=session,
                              project=row.get("cwd"), time=ti._iso(row.get("timestamp") or row.get("ts")),
                              signal="claude.workflow_journal", kinds=["workflow_event"], parent=parent,
                              action=ti._action("workflow_event", typ, None, ti._success_from(row),
                                                json.dumps({"keys": sorted(row)[:30]})))
        yield run.summary_event()
        return
    counts = Counter()
    for ev in ti.iter_events(path, "claude", machine, limit_bytes):
        if (ev.get("action") or {}).get("kind") == "source_summary":
            diag = json.loads(ev["action"]["summary"])
            diag["events"] = dict(counts)
            diag["provenance"] = "legacy worker file; copied user turns are agent context, not independent human observations"
            ev["action"]["summary"] = json.dumps(diag)
        else:
            parent = ev.get("parent_session") or ev["session"]
            if not ev.get("parent_session"):
                ev["session"] = str(parent)+"/"+path.stem
                ev["parent_session"] = parent
            if ev["role"] == "user":
                ev["actor"] = "agent"
                ev["human_signal"] = "claude.legacy_worker_context"
            counts[ev["role"]+"/"+ev["actor"]] += 1
        ev["id"] = hashlib.sha256(("legacy-agent|"+str(ev["session"])+"|"+ev["id"]).encode()).hexdigest()[:24]
        yield ev


def ingest(home, roots, machine="local", log=None):
    from .trace_store import connect, ingest_file, coverage
    db = connect(Path(home)/"agent-tape.sqlite")
    fingerprint = hashlib.sha256(Path(__file__).read_bytes()+Path(ti.__file__).read_bytes()).hexdigest()
    failures = []
    try:
        paths = sorted({p for root in roots for p in discover(root)})
        for n, path in enumerate(paths):
            try:
                ingest_file(db, path, "claude", machine, parser=fingerprint, iterator=iter_events)
            except (ValueError, OSError) as exc:
                failures.append({"path": str(path), "error": type(exc).__name__})
            if log and n % 100 == 0:
                log({"agent_files": n+1, "total": len(paths)})
        result = coverage(db)
        result["failures"] = failures
        if db.execute("SELECT count(*) FROM events WHERE role='user' AND actor='human'").fetchone()[0]:
            raise ValueError("legacy workers cannot become independent personal evidence")
        (Path(home)/"agent-coverage.json").write_text(json.dumps(result, indent=2)+"\n")
        return result
    finally:
        db.close()
