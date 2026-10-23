"""Build a private, source-first research cohort and frozen local representations.

Explicit homes only. This module never installs a skill, switches a live model,
edits instruction files or calls a hosted inference service.
"""
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3


def _write(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def _key(path):
    return str(path).replace("\\", "/").rstrip("/").casefold()


def project_identity(path, aliases):
    if not path:
        return None
    key = _key(path)
    for prefix in sorted(aliases, key=len, reverse=True):
        if key == prefix or key.startswith(prefix+"/"):
            return aliases[prefix]
    return "unmapped_" + hashlib.sha256(key.encode()).hexdigest()[:20]


def git_catalog(home):
    """Verified checkout aliases, immutable commit observations, no reachability inference."""
    import subprocess
    aliases, repositories = {}, []
    for path in sorted((Path(home)/"git").glob("*.json")):
        repo = json.loads(path.read_text())
        repositories.append(repo)
        aliases[_key(repo["checkout"])] = repo["project"]
        root = Path(repo["checkout"])
        if root.exists():
            run = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"],
                                 capture_output=True, text=True, timeout=30)
            if run.returncode == 0:
                for line in run.stdout.splitlines():
                    if line.startswith("worktree "):
                        aliases[_key(line[9:])] = repo["project"]
    return aliases, repositories


def _time(value):
    if not isinstance(value, str):
        raise ValueError("aware timestamp required")
    value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return value


def nearby_outcomes(example, repositories, observed_through):
    """Time/project association only. A later commit is never a prefix feature."""
    begin, end, fence = _time(example["seed_time"]), _time(example["target_time"]), _time(observed_through)
    rows = []
    for repo in repositories:
        if repo["project"] != example.get("project"):
            continue
        commits = [c for c in repo["commits"] if _time(c["committed_at"]) <= fence]
        reverts = {t for c in commits for t in c.get("explicit_revert_targets", [])}
        for c in commits:
            if begin <= _time(c["committed_at"]) <= end:
                rows.append({"source_ref": c["source_ref"], "committed_at": c["committed_at"],
                             "subject": c["subject"], "changes": c.get("changes"),
                             "explicit_revert_observed_by_training_cutoff": any(c["id"].startswith(t) for t in reverts),
                             "interpretation": "same project and time window; association, not endorsement or proof of authorship"})
    rows = list({r["source_ref"]: r for r in rows}.values())
    return sorted(rows, key=lambda c: c["committed_at"])[-4:]


def ingest_sources(home, sources, log=None):
    from .trace_ingest import discover
    from .trace_store import connect, ingest_file, coverage
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    db = connect(home/"tape.sqlite")
    failures = []
    try:
        for spec in sources:
            source, root, machine = spec["source"], spec["path"], spec.get("machine", "local")
            iterator, fingerprint = None, None
            if source == "grok":
                from . import trace_grok, trace_ingest
                files = trace_grok.discover(root)
                iterator = trace_grok.iter_events
                fingerprint = hashlib.sha256(Path(trace_grok.__file__).read_bytes()+Path(trace_ingest.__file__).read_bytes()).hexdigest()
            else:
                files = discover(root, source)
            for n, path in enumerate(files):
                try:
                    result = ingest_file(db, path, source, machine, parser=fingerprint, iterator=iterator)
                    diag = result["diagnostics"]
                    if diag.get("errors") or diag.get("malformed") or diag.get("native_id_conflicts"):
                        failures.append({"path": str(path), "diagnostics": diag})
                except (ValueError, OSError) as exc:
                    failures.append({"path": str(path), "error": str(exc)})
                if log and n % 50 == 0:
                    log({"source": source, "processed": n+1, "files": len(files)})
        result = coverage(db)
        result["failures"] = failures
        _write(home/"coverage.json", result)
        return result
    finally:
        db.close()


def freeze_research(home, tape=None):
    from .trace_store import connect, make_examples, coverage
    from .trace_cases import freeze_split, audit_split
    home = Path(home)
    research = home/"research"
    research.mkdir(parents=True, exist_ok=True)
    if (research/"split.json").exists():
        raise ValueError("research split already frozen; use a new study directory")
    db = connect(tape or home/"tape.sqlite")
    try:
        ambiguous = db.execute("SELECT count(*) FROM conflicts c JOIN events e ON e.id=c.event_id WHERE e.role IN ('user','assistant','tool')").fetchone()[0]
        if ambiguous:
            raise ValueError("conflicting original observations need quarantine before freezing: %d" % ambiguous)
        examples = make_examples(db)
        aliases, repos = git_catalog(home)
        for ex in examples:
            ex["project"] = project_identity(ex.get("project"), aliases)
            ex["input"]["project"] = ex["project"]
            ex["reaction_input"]["project"] = ex["project"]
        split = freeze_split(examples, boundary_unit="families")
        audit = audit_split(examples, split)
        if not audit["ok"]:
            raise ValueError("split audit failed: "+str(audit["issues"]))
        # References let a person locate the original observation without giving
        # the runtime access to the raw tape or to any held-out conversation.
        locations = {}
        for ref, path, machine, digest in db.execute("""
            SELECT l.source_ref,f.path,f.machine,f.sha256 FROM locations l
            JOIN files f ON f.id=l.file_id JOIN events e ON e.id=l.event_id
            WHERE e.role='user' AND e.actor='human'
        """):
            locations.setdefault(ref, {"source_ref": ref, "path": path, "machine": machine, "raw_prefix_sha256": digest})
        by_id = {e["id"]: e for e in examples}
        for name in ("train", "validation", "test"):
            rows = []
            for ident in split[name+"_ids"]:
                ex = by_id[ident]
                ex["partition"] = name
                if name == "train":
                    ex["historical_outcomes"] = nearby_outcomes(ex, repos, split["cutoffs"]["train_end"])
                    ex["source_locations"] = [locations[r] for r in (ex["source_refs"]["seed"], ex["source_refs"]["target"]) if r in locations]
                rows.append(ex)
            with (research/(name+".jsonl")).open("w", encoding="utf-8") as out:
                for row in rows:
                    out.write(json.dumps(row, ensure_ascii=False)+"\n")
        _write(research/"split.json", split)
        _write(research/"split-audit.json", audit)
        _write(research/"project-map.json", aliases)
        _write(research/"coverage.json", coverage(db))
        excluded = [e for e in examples if e["id"] in set(split["excluded_ids"])]
        _write(research/"exclusions.json", {"source": dict(Counter(e["source"] for e in excluded)),
                                            "project": dict(Counter(e["project"] for e in excluded))})
        return {"status": split["status"], "counts": split["counts"], "cutoffs": split["cutoffs"], "audit": audit}
    finally:
        db.close()


def encode_partitions(home, encoder_info, device="cpu", log=None):
    """Frozen encoder preprocessing only. Each predictor later opens train/val separately."""
    import numpy as np
    from .trace_runtime import render_input
    from .trace_learning import LocalEncoder
    home, research = Path(home), Path(home)/"research"
    if any((research/name).exists() for name in ("seal.json", "evaluation-started.json", "test-results.json", "test-scores.npz", "training.lock")):
        raise ValueError("sealed study representations are immutable")
    info = dict(encoder_info)
    info.setdefault("max_length", 256)
    from .trace_learning import sha256
    model_root = Path(info["path"])
    digests = {p.relative_to(model_root).as_posix(): sha256(p) for p in sorted(model_root.rglob("*")) if p.is_file()}
    if not digests:
        raise ValueError("local encoder directory contains no model files")
    if any(digests.get(k) != v for k, v in info.get("weights_sha256", {}).items()):
        raise ValueError("encoder files changed from the supplied digest pins")
    info["weights_sha256"] = digests
    info["device"] = device
    info["dtype"] = "float32" if device == "cpu" else "float16"
    info["cache_schema"] = "trace-prefix-2"
    cache_identity = json.dumps({k: info[k] for k in ("model", "revision", "max_length", "weights_sha256", "device", "dtype", "cache_schema")}, sort_keys=True)
    encoder = LocalEncoder(info["path"], device=device, max_length=info["max_length"])
    db = sqlite3.connect(str(home/"embedding-cache.sqlite"))
    db.execute("CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY,vector BLOB,dim INTEGER)")
    def key(text):
        return hashlib.sha256((cache_identity+"|"+text).encode()).hexdigest()
    try:
        for name in ("train", "validation", "test"):
            rows = [json.loads(line) for line in (research/(name+".jsonl")).read_text().splitlines() if line]
            texts = []
            for ex in rows:
                response = ex.get("reaction_input", {}).get("post_action", {}).get("assistant_text", "")
                texts.extend([ex["input"]["seed"], render_input(ex["input"]), ex["target"]["text"],
                              ex["input"]["seed"]+"\nobserved agent response: "+response[:6000]])
            missing = [t for t in dict.fromkeys(texts) if not db.execute("SELECT 1 FROM vectors WHERE id=?", (key(t),)).fetchone()]
            for start in range(0, len(missing), 256):
                chunk = missing[start:start+256]
                arrays = encoder.encode(chunk, batch_size=16)
                db.executemany("INSERT OR IGNORE INTO vectors VALUES (?,?,?)", [(key(t), a.astype("float32").tobytes(), len(a)) for t, a in zip(chunk, arrays)])
                db.commit()
                if log:
                    log({"partition": name, "encoded": min(start+256, len(missing)), "uncached": len(missing)})
            if not rows:
                raise ValueError("empty partition: "+name)
            arrays = [np.frombuffer(db.execute("SELECT vector FROM vectors WHERE id=?", (key(t),)).fetchone()[0], dtype="float32").copy() for t in texts]
            a = np.stack(arrays).reshape(len(rows), 4, -1)
            np.savez_compressed(research/(name+".npz"), ids=np.array([e["id"] for e in rows]), seed=a[:, 0], context=a[:, 1], target=a[:, 2], reaction=a[:, 3])
        _write(research/"encoder.json", info)
        return {"encoder": info["model"], "revision": info["revision"], "device": device, "dimension": a.shape[2]}
    finally:
        db.close()
