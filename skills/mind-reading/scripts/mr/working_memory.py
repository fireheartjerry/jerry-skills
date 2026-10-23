"""Private, project-scoped working memory for the intent skill.

The notebook is explicit state that a caller records and queries; nothing here
is inferred from conversation text, and a record never grants authority. State
lives in ``<home>/context/notebook.json`` (mode 0600, directory 0700), is
replaced atomically under an exclusive lock, and is validated whole before any
write, so a failed call leaves the previous bytes untouched.

Times are UTC ``YYYY-MM-DDTHH:MM:SSZ``. A record is expired once
``now >= expires_at`` (the boundary second is already expired).
"""
import copy
import datetime
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path

try:  # POSIX advisory locking; the notebook refuses to mutate without it.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

from .preferences import PreferenceError, _private_bytes, _safe_path
from . import intent_context


class MemoryError(ValueError):  # noqa: A001 - public contract name
    pass


VERSION = 1
MAX_RECORDS = 1000
MAX_HISTORY = 2000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 4000
MAX_FACTS = 8

KINDS = ("goal", "loop", "decision", "assumption", "binding", "answer",
         "constraint", "fact", "preference", "alias", "counterexample")
CONFIDENCE = ("S", "L", "G")
INPUT_STATUSES = ("active", "closed")
STORED_STATUSES = ("active", "closed", "forgotten", "superseded", "resolved")
CHANGE_STATUSES = ("active", "closed", "forgotten")
VERIFIED_KINDS = ("fact", "constraint", "answer", "decision", "binding")
VERIFIED_CONFIDENCE = ("S",)

_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_STRING_LIMITS = {"text": 2000, "source_ref": 500, "project": 1000, "key": 200,
                  "phase": 80, "condition": 500, "counter_to": 200}
_LIST_FIELDS = ("terms", "unless", "requires", "alternatives", "counter_to")
_INPUT_FIELDS = {"kind", "text", "source_ref", "project", "id", "key", "value", "status",
                 "phase", "terms", "unless", "requires", "expires_at", "confidence",
                 "condition", "counter_to", "alternatives"}
_STORED_EXTRA = {"created_at", "updated_at", "seq", "superseded_by", "resolution"}


def _fail(message):
    raise MemoryError(message)


def _is_int(value):
    return type(value) is int


def _string(record, name, maximum, required=False, strip_empty=False):
    value = record.get(name, "")
    if type(value) is not str or len(value) > maximum or "\x00" in value:
        _fail("%s must be a string of at most %d characters" % (name, maximum))
    if required and not value.strip():
        _fail("%s is required" % name)
    return value


def _json_value(value, depth=0):
    if depth > 4:
        _fail("value is nested too deeply")
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail("value must be finite")
        return
    if type(value) is str:
        if len(value) > 1000 or "\x00" in value:
            _fail("value strings are limited to 1000 characters")
        return
    if type(value) is list:
        if len(value) > 50:
            _fail("value lists are limited to 50 items")
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict:
        if len(value) > 50:
            _fail("value objects are limited to 50 keys")
        for key, item in value.items():
            if type(key) is not str or len(key) > 100:
                _fail("value keys must be strings of at most 100 characters")
            _json_value(item, depth + 1)
        return
    _fail("value must be bounded JSON")


def _timestamp(value, name):
    if type(value) is not str or not _TIME.match(value):
        _fail("%s must be UTC YYYY-MM-DDTHH:MM:SSZ" % name)
    try:
        datetime.datetime.strptime(value, _TIME_FORMAT)
    except ValueError:
        _fail("%s is not a valid time" % name)
    return value


def _now(now):
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc).strftime(_TIME_FORMAT)
    if isinstance(now, datetime.datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            _fail("now must be timezone-aware")
        return now.astimezone(datetime.timezone.utc).strftime(_TIME_FORMAT)
    return _timestamp(now, "now")


def _record_id(value, name="id"):
    if type(value) is not str or not _ID.match(value):
        _fail("%s must match [A-Za-z0-9_.-]{1,64}" % name)
    return value


def _clean(record, stored):
    """Validate one record and return a normalized copy; reject unknown fields."""
    if type(record) is not dict:
        _fail("record must be an object")
    if any(type(key) is not str for key in record):
        _fail("record field names must be strings")
    allowed = _INPUT_FIELDS | (_STORED_EXTRA if stored else set())
    unknown = set(record) - allowed
    if unknown:
        _fail("unknown record fields: %s" % ", ".join(sorted(unknown)))
    out = {}
    if record.get("kind") not in KINDS:
        _fail("kind must be one of: %s" % ", ".join(KINDS))
    out["kind"] = record["kind"]
    out["text"] = _string(record, "text", _STRING_LIMITS["text"], required=True)
    out["source_ref"] = _string(record, "source_ref", _STRING_LIMITS["source_ref"], required=True)
    if "project" not in record:
        _fail("project is required (use an empty string for global)")
    out["project"] = _string(record, "project", _STRING_LIMITS["project"])
    if "id" in record or stored:
        out["id"] = _record_id(record.get("id"))
    for name in ("key", "phase"):
        if name in record:
            out[name] = _string(record, name, _STRING_LIMITS[name])
    if "condition" in record:
        condition=record['condition']
        if (not isinstance(condition,dict) or set(condition)-{'project','phase','terms','unless'}):
            _fail('condition must be an object with project, phase, terms or unless')
        for name,value in condition.items():
            if name in ('project','phase'):
                if not isinstance(value,str) or not value.strip() or len(value)>_STRING_LIMITS[name]:
                    _fail('condition %s must be bounded nonempty text' % name)
            elif (not isinstance(value,list) or len(value)>20 or
                  any(not isinstance(v,str) or not v.strip() or len(v)>200 for v in value)):
                _fail('condition terms/unless must be bounded phrase lists')
        out['condition']=copy.deepcopy(condition)
    if "value" in record:
        _json_value(record["value"])
        if len(json.dumps(record["value"], ensure_ascii=False)) > 2000:
            _fail("value is limited to 2000 serialized characters")
        out["value"] = record["value"]
    for name in _LIST_FIELDS:
        if name not in record:
            continue
        items = record[name]
        if type(items) is not list or len(items) > 20:
            _fail("%s must be a list of at most 20 strings" % name)
        for item in items:
            if type(item) is not str or not item.strip() or len(item) > 200 or "\x00" in item:
                _fail("%s entries must be non-empty strings of at most 200 characters" % name)
            if name == "requires":
                _record_id(item, "requires entry")
        if name == "requires" and len(set(items)) != len(items):
            _fail("requires entries must be unique")
        out[name] = list(items)
    if "expires_at" in record:
        out["expires_at"] = _timestamp(record["expires_at"], "expires_at")
    confidence = record.get("confidence", "G")
    if confidence not in CONFIDENCE:
        _fail("confidence must be one of S, L, G")
    out["confidence"] = confidence
    status = record.get("status", "active")
    if status not in (STORED_STATUSES if stored else INPUT_STATUSES):
        _fail("status must be one of: %s" % ", ".join(STORED_STATUSES if stored else INPUT_STATUSES))
    out["status"] = status
    if stored:
        out["created_at"] = _timestamp(record.get("created_at"), "created_at")
        out["updated_at"] = _timestamp(record.get("updated_at"), "updated_at")
        if not _is_int(record.get("seq")) or record["seq"] < 1:
            _fail("seq must be a positive integer")
        out["seq"] = record["seq"]
        if "superseded_by" in record:
            out["superseded_by"] = _record_id(record["superseded_by"], "superseded_by")
        if status == "superseded" and "superseded_by" not in out:
            _fail("superseded records name their successor")
        if "resolution" in record:
            res = record["resolution"]
            if (type(res) is not dict or set(res) != {"text", "source_ref", "at"}
                    or type(res["text"]) is not str or not res["text"].strip() or len(res["text"]) > 2000
                    or type(res["source_ref"]) is not str or not res["source_ref"].strip() or len(res["source_ref"]) > 500):
                _fail("resolution is malformed")
            _timestamp(res["at"], "resolution.at")
            out["resolution"] = dict(res)
        if status == "resolved" and ("resolution" not in out or out["kind"] not in ("assumption", "loop")):
            _fail("resolved records need an explicit resolution")
    if len(json.dumps(out, ensure_ascii=False)) > MAX_RECORD_BYTES:
        _fail("record exceeds %d serialized characters" % MAX_RECORD_BYTES)
    return out


def _validate_state(state):
    if type(state) is not dict or set(state) != {"version", "revision", "records", "history"}:
        _fail("notebook state is malformed")
    if not _is_int(state["version"]) or state["version"] != VERSION:
        _fail("notebook version must be %d" % VERSION)
    if not _is_int(state["revision"]) or state["revision"] < 0:
        _fail("notebook revision is invalid")
    if type(state["records"]) is not list or len(state["records"]) > MAX_RECORDS:
        _fail("notebook holds at most %d records" % MAX_RECORDS)
    if type(state["history"]) is not list or len(state["history"]) > MAX_HISTORY:
        _fail("notebook history holds at most %d events" % MAX_HISTORY)
    ids = set()
    records = []
    for raw in state["records"]:
        rec = _clean(raw, True)
        if rec["id"] in ids:
            _fail("duplicate record id: %s" % rec["id"])
        ids.add(rec["id"])
        if rec["seq"] > state["revision"]:
            _fail("record sequence is ahead of the revision")
        records.append(rec)
    last = 0
    for event in state["history"]:
        if (type(event) is not dict or set(event) != {"rev", "op", "id", "before", "after", "time"}
                or not _is_int(event["rev"]) or event["rev"] < last or event["rev"] > state["revision"]
                or type(event["op"]) is not str or len(event["op"]) > 40
                or type(event["id"]) is not str or not _ID.match(event["id"])
                or not (event["before"] is None or type(event["before"]) is dict)
                or not (event["after"] is None or type(event["after"]) is dict)):
            _fail("notebook history is malformed")
        _timestamp(event["time"], "history time")
        last = event["rev"]
    return {"version": VERSION, "revision": state["revision"], "records": records,
            "history": copy.deepcopy(state["history"])}


def _paths(home):
    if not isinstance(home, (str, os.PathLike)):
        _fail("home must be a path")
    try:
        root = _safe_path(home)
        state = _safe_path(root / "context" / "notebook.json")
    except (PreferenceError, OSError, RuntimeError) as exc:
        raise MemoryError(str(exc)) from exc
    return root, state


def _read(state_path):
    if not state_path.exists():
        return {"version": VERSION, "revision": 0, "records": [], "history": []}
    try:
        if not state_path.is_file() or state_path.stat().st_size > MAX_FILE_BYTES:
            _fail("notebook is not a regular file within the size limit")
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if isinstance(exc, MemoryError):
            raise
        raise MemoryError("notebook is unreadable or corrupt; it was left untouched") from exc
    return _validate_state(data)


def load(home):
    """Return the validated notebook; a missing notebook is empty at revision 0."""
    _root, state_path = _paths(home)
    return _read(state_path)


class _Lock:
    def __init__(self, root):
        self.root = root
        self.fd = None

    def __enter__(self):
        if fcntl is None and msvcrt is None:
            _fail("file locking is unavailable on this platform")
        context = self.root / "context"
        try:
            if not self.root.exists():
                os.makedirs(str(self.root), mode=0o700, exist_ok=True)
            if not context.exists():
                try:
                    os.mkdir(str(context), 0o700)
                except FileExistsError:
                    pass  # another first writer created the directory
            context = _safe_path(context)
            if not context.is_dir():
                _fail("context is not a directory")
            os.chmod(str(context), 0o700)
            lock = _safe_path(context / ".notebook.lock")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            self.fd = os.open(str(lock), flags, 0o600)
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            else:
                # Windows byte-range locks require one byte and a fixed offset.
                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd,b'\0')
                os.lseek(self.fd,0,os.SEEK_SET)
                msvcrt.locking(self.fd,msvcrt.LK_LOCK,1)
        except (PreferenceError, OSError) as exc:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise MemoryError("cannot lock notebook: %s" % exc) from exc
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                else:
                    os.lseek(self.fd,0,os.SEEK_SET)
                    msvcrt.locking(self.fd,msvcrt.LK_UNLCK,1)
            finally:
                os.close(self.fd)
        return False


def _write(state_path, state):
    state = _validate_state(state)
    content = (json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(content) > MAX_FILE_BYTES:
        _fail("notebook would exceed the size limit")
    try:
        _private_bytes(state_path, content, replace=True)
    except (PreferenceError, OSError) as exc:
        raise MemoryError("cannot write notebook: %s" % exc) from exc


def _event(state, op, rid, before, after, now):
    state["history"].append({"rev": state["revision"], "op": op, "id": rid,
                             "before": before, "after": after, "time": now})
    del state["history"][:-MAX_HISTORY]


def _delta(before, after):
    keys = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return ({k: before[k] for k in keys if k in before}, {k: after[k] for k in keys if k in after})


def _mutate(home, action):
    root, state_path = _paths(home)
    with _Lock(root):
        state = _read(state_path)
        result = action(state)
        _write(state_path, state)
        return copy.deepcopy(result)


def _find(state, record_id):
    _record_id(record_id, "record id")
    for rec in state["records"]:
        if rec["id"] == record_id:
            return rec
    _fail("no such record: %s" % record_id)


def _supersede_goals(state, keep, now):
    for rec in state["records"]:
        if (rec["kind"] == "goal" and rec["status"] == "active" and rec["id"] != keep["id"]
                and rec["project"] == keep["project"]):
            before = copy.deepcopy(rec)
            rec["status"] = "superseded"
            rec["superseded_by"] = keep["id"]
            rec["updated_at"] = now
            _event(state, "supersede", rec["id"], *_delta(before, rec), now)


def remember(home, record, now=None):
    """Save one explicit record; a new active goal supersedes older goals in its scope."""
    now = _now(now)
    clean = _clean(record, False)

    def action(state):
        if len(state["records"]) >= MAX_RECORDS:
            _fail("notebook is full (%d records); forget or close nothing is deleted, so raise the limit deliberately" % MAX_RECORDS)
        existing = {r["id"] for r in state["records"]}
        rid = clean.get("id")
        if rid is None:
            rid = "mem_" + uuid.uuid4().hex[:16]
            while rid in existing:
                rid = "mem_" + uuid.uuid4().hex[:16]
        elif rid in existing:
            _fail("record id already exists: %s" % rid)
        state["revision"] += 1
        rec = dict(clean)
        rec.update(id=rid, created_at=now, updated_at=now, seq=state["revision"])
        state["records"].append(rec)
        _event(state, "remember", rid, None, copy.deepcopy(rec), now)
        if rec["kind"] == "goal" and rec["status"] == "active":
            _supersede_goals(state, rec, now)
        return rec

    return _mutate(home, action)


def _expired(rec, now):
    return "expires_at" in rec and now >= rec["expires_at"]


def _scoped(rec, project):
    return rec["project"] == "" or (project and rec["project"] == project)


def _project_arg(project):
    if project is None:
        return ""
    if type(project) is not str or len(project) > _STRING_LIMITS["project"] or "\x00" in project:
        _fail("project must be a string of at most 1000 characters")
    return project


def _live(rec, now):
    return rec["status"] == "active" and not _expired(rec, now)


def query(home, project=None, now=None, include_inactive=False):
    """Global records plus those of exactly this project, ordered by creation."""
    now = _now(now)
    project = _project_arg(project)
    state = load(home)
    out = [r for r in state["records"] if _scoped(r, project) and (include_inactive or _live(r, now))]
    return copy.deepcopy(sorted(out, key=lambda r: r["seq"]))


def change(home, record_id, status, now=None):
    """Close, forget or reactivate a record. Superseded records stay superseded."""
    now = _now(now)
    if status not in CHANGE_STATUSES:
        _fail("status must be one of: %s" % ", ".join(CHANGE_STATUSES))

    def action(state):
        rec = _find(state, record_id)
        if rec["status"] == "superseded":
            _fail("superseded records cannot be changed; record a new goal instead")
        if rec["status"] == "resolved" and status == "active":
            _fail("resolved records carry an explicit resolution and cannot be reactivated")
        if rec["status"] == status:
            return rec
        before = copy.deepcopy(rec)
        state["revision"] += 1
        rec["status"] = status
        rec["updated_at"] = now
        _event(state, "change", rec["id"], *_delta(before, rec), now)
        if status == "active" and rec["kind"] == "goal":
            _supersede_goals(state, rec, now)
        return rec

    return _mutate(home, action)


def resolve(home, record_id, text, source_ref, now=None):
    """Mark an assumption or loop resolved with an explicit source; never creates a fact."""
    now = _now(now)
    holder = {"text": text, "source_ref": source_ref, "project": ""}
    _string(holder, "text", _STRING_LIMITS["text"], required=True)
    _string(holder, "source_ref", _STRING_LIMITS["source_ref"], required=True)

    def action(state):
        rec = _find(state, record_id)
        if rec["kind"] not in ("assumption", "loop"):
            _fail("only assumptions and loops can be resolved")
        if rec["status"] != "active":
            _fail("only active records can be resolved (status is %s)" % rec["status"])
        before = copy.deepcopy(rec)
        state["revision"] += 1
        rec["status"] = "resolved"
        rec["resolution"] = {"text": text, "source_ref": source_ref, "at": now}
        rec["updated_at"] = now
        _event(state, "resolve", rec["id"], *_delta(before, rec), now)
        return rec

    return _mutate(home, action)


def _cycle_members(graph):
    """Nodes on a cycle (including self-loops) via iterative Tarjan SCC; returns {node: component}."""
    index, low, on_stack, stack, comps = {}, {}, set(), [], []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in graph:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                if len(comp) > 1 or node in graph[node]:
                    comps.append(sorted(comp))
    return {member: comp for comp in comps for member in comp}


def ready(home, project=None, now=None):
    """Split active loops into ready (all requirements done) and blocked (with reasons)."""
    now = _now(now)
    project = _project_arg(project)
    state = load(home)
    visible = {r["id"]: r for r in state["records"] if _scoped(r, project)}
    graph = {rid: set(r.get("requires", [])) for rid, r in visible.items()}
    cycles = _cycle_members(graph)
    ready_list, blocked = [], []
    for rec in sorted(visible.values(), key=lambda r: r["seq"]):
        if rec["kind"] != "loop" or not _live(rec, now):
            continue
        reasons = []
        for dep_id in rec.get("requires", []):
            dep = visible.get(dep_id)  # other projects' records are indistinguishable from missing
            if dep is None:
                reasons.append({"id": dep_id, "reason": "missing"})
            elif dep["status"] == "forgotten":
                reasons.append({"id": dep_id, "reason": "forgotten"})
            elif dep["status"] == "superseded":
                reasons.append({"id": dep_id, "reason": "superseded"})
            elif _expired(dep, now):
                reasons.append({"id": dep_id, "reason": "expired"})
            elif dep["status"] not in ("closed", "resolved"):
                reasons.append({"id": dep_id, "reason": "open"})
        entry = {"record": copy.deepcopy(rec), "reasons": reasons}
        if rec["id"] in cycles:
            entry["reasons"].append({"id": rec["id"], "reason": "cycle"})
            entry["cycle"] = list(cycles[rec["id"]])
        if entry["reasons"]:
            blocked.append(entry)
        else:
            ready_list.append(copy.deepcopy(rec))
    return {"ready": ready_list, "blocked": blocked}


def _summary(rec):
    parts = [rec["kind"], ": ", rec["text"].strip()]
    if rec.get("key") and "value" in rec:
        parts.append(" [%s=%s]" % (rec["key"], json.dumps(rec["value"], ensure_ascii=False)))
    if rec.get("condition"):
        parts.append(" (when %s)" % rec["condition"])
    return "".join(parts)[:500]


def frame(home, project=None, now=None):
    """Return an intent_context frame from active, scoped, confirmed records only."""
    now = _now(now)
    project = _project_arg(project)
    state = load(home)
    live = [r for r in state["records"] if _scoped(r, project) and _live(r, now)]

    def rank(rec):  # exact project beats global, then newest
        return (1 if rec["project"] == project and project else 0, rec["seq"])

    out = {}
    goals = sorted((r for r in live if r["kind"] == "goal"), key=rank, reverse=True)
    if goals:
        out["goal"] = goals[0]["text"].strip()
        if goals[0].get("phase", "").strip():
            out["phase"] = goals[0]["phase"].strip()
    if project:
        out["project"] = project
    facts = sorted((r for r in live if r["kind"] in VERIFIED_KINDS and r["confidence"] in VERIFIED_CONFIDENCE
                    and not any(r.get(k) for k in ('condition','phase','terms','unless'))),
                   key=rank, reverse=True)[:MAX_FACTS]
    if facts:
        out["verified_facts"] = [_summary(r) for r in facts]
    return intent_context.normalize(out)


def history(home, since=0):
    """Revision events newer than ``since``; bounded to the most recent 2000."""
    if not _is_int(since) or since < 0:
        _fail("since must be a non-negative integer")
    state = load(home)
    return copy.deepcopy([e for e in state["history"] if e["rev"] > since])


def import_records(home, records, capsule_hash, now=None):
    """Validate a portable batch before one locked commit. Never overwrite a live mission."""
    if not isinstance(capsule_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',capsule_hash):
        _fail('invalid capsule digest')
    if not isinstance(records,list) or len(records)>MAX_RECORDS:
        _fail('import needs a bounded record list')
    now=_now(now)
    source=[_clean(r,True) for r in records]
    if len({r['id'] for r in source}) != len(source):
        _fail('duplicate imported record ids')
    mapping={r['id']:'imp_'+hashlib.sha256((r['project']+'\0'+r['id']+'\0'+r['source_ref']).encode()).hexdigest()[:24] for r in source}
    incoming=[]
    for old in source:
        row={k:copy.deepcopy(v) for k,v in old.items() if k in _INPUT_FIELDS}
        row['id']=mapping[old['id']]
        row['confidence']='G'
        for field in ('requires','counter_to'):
            if field in row:
                row[field]=[mapping.get(v,'missing_'+hashlib.sha256(v.encode()).hexdigest()[:20]) for v in row[field]]
        # A resolved prerequisite is completed work, not an active new hypothesis.
        row['status']='closed' if old['status'] in ('closed','resolved') else 'active'
        if old['status'] not in ('active','closed','resolved'):
            _fail('inactive or forgotten records cannot be imported into recall')
        incoming.append(_clean(row,False))

    def action(state):
        by_id={r['id']:r for r in state['records']}
        additions=[]
        skipped=[]
        compare=_INPUT_FIELDS-{'status'}
        for row in incoming:
            existing=by_id.get(row['id'])
            if existing:
                if any(existing.get(k)!=row.get(k) for k in compare):
                    _fail('imported record conflicts with existing content')
                skipped.append(row['id'])
                continue
            if row['kind']=='goal' and row['status']=='active':
                if any(r['kind']=='goal' and r['project']==row['project'] and _live(r,now) for r in state['records']+additions):
                    _fail('import would replace an active mission; close or explicitly redirect it first')
            additions.append(row)
        if len(state['records'])+len(additions)>MAX_RECORDS:
            _fail('import exceeds notebook capacity')
        for row in additions:
            state['revision']+=1
            row=dict(row,created_at=now,updated_at=now,seq=state['revision'])
            state['records'].append(row)
            _event(state,'import',row['id'],None,copy.deepcopy(row),now)
        return {'imported':[r['id'] for r in additions],'skipped':skipped,'revision':state['revision'],
                'capsule_sha256':capsule_hash,'notice':'Imported context is G until verified; no personal model or permissions changed.'}
    return _mutate(home,action)
