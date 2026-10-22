"""State root layout, meta/turn/result records, liveness summaries, the state
lock and the result v3 builder.

Layout under the state root (default ~/.local/state/agent-sessions):

  .lock                    exclusive flock held by spawn, kick, reap and lost-detection
  engine-versions.json     cache of `<engine> --version`, keyed by binary path
  pauses/<engine>.json     engine paused after a rate or usage limit
  groups/<group>.json      best-of-N and plan groups
  ledgers/<id>.json        tree budgets shared by a child and its descendants
  snapshots.jsonl          record of snapshot refs written by reap
  <name>/meta.json         everything spawn decided; version 3
  <name>/prompt.txt        the first turn's prompt (never in argv)
  <name>/schema.json       copy of --schema FILE
  <name>/env.json          0600 passthrough environment; the runner deletes it
  <name>/progress.json     live phase, usage so far, stall deadline
  <name>/events.jsonl      one object per event, every turn
  <name>/log               rendered transcript
  <name>/turns/N.json      one record per turn (+ N.events.jsonl, N.stderr, N.answer.txt)
  <name>/result.json       written once, when the child ends
  <name>/inbox, messages   steerable children: queued messages and message -> turn map
  <name>/worktree          the git worktree when --worktree was used
  <name>/attempts/         result.json of earlier runs, moved there by resume
  <name>/verify.<k>.out    output of the k-th verification run
  <name>/judge/, judge.json  inputs and verdict of `judge`
  <name>/hooks.log         output of --on-finish
  <name>/pane.log          runner output (process backend)
"""

import fcntl
import os
import re
import time

from agent_sessions.common import (
    FAILURE_CLASSES, NAME_RE, RESERVATION_S, STATE_ROOT, UNTRUSTED, die, env_int, iso_of, now_iso, read_json,
    sdir_of, write_json,
)
from agent_sessions.procs import engine_alive, runner_alive
from agent_sessions import backend
from agent_sessions.backend import Liveness, backend_of

__all__ = [
    "empty_usage", "add_usage", "empty_budget", "RESULT_DEFAULTS", "normalize_result",
    "load_meta", "require_meta", "load_progress", "turn_numbers", "load_turn", "load_turns",
    "build_result", "write_lost", "session_state", "activity_seconds", "state_lock",
    "working_children", "stale_reservations", "all_metas", "child_alive", "retire_runner", "check_terminal",
    "effective_depth", "descendants", "unfinished", "events_path", "read_events",
    "pauses_dir", "read_pause", "active_pauses", "write_pause", "groups_dir", "load_group", "group_members",
    "write_group", "kick", "merge_judge", "result_path", "is_queued", "ledgers_dir", "load_ledger",
    "write_ledger", "ledger_remaining", "ledger_add", "ledger_exhausted", "fmt_usd",
]


def empty_usage():
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0, "total_tokens": 0}


def add_usage(total, part):
    if not isinstance(part, dict):
        return total
    for key in total:
        value = part.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] += int(value)
    return total


def empty_budget():
    return {"usd_limit": None, "tokens_limit": None, "turns_limit": None, "deadline_iso": None,
            "ledger": None, "ledger_remaining_usd": None}


# Fields a v3 reader expects; a v2 result.json on disk gets these when read.
RESULT_DEFAULTS = (
    ("failure_class", None), ("attempts", 1), ("retries", []), ("resumes", []), ("tier", None),
    ("role", None), ("engine_version", None), ("events_file", None), ("structured_output", None),
    ("schema_valid", None), ("schema_file", None), ("usage", None), ("cost_source", None),
    ("budget", None), ("verify", None), ("git", None), ("pipeline", None), ("warnings", []),
)


def normalize_result(res):
    """Fill the v3 fields a v2 result lacks (nulls and empty lists). Never
    rewrites the file; the stored version number stays."""
    if not isinstance(res, dict):
        return res
    for key, default in RESULT_DEFAULTS:
        if key not in res:
            res[key] = list(default) if isinstance(default, list) else default
    return res


def result_path(name):
    return os.path.join(sdir_of(name), "result.json")


def events_path(name):
    return os.path.join(sdir_of(name), "events.jsonl")


def load_meta(name):
    if not NAME_RE.match(name or ""):
        return None
    return read_json(os.path.join(sdir_of(name), "meta.json"))


def require_meta(name):
    meta = load_meta(name)
    if not meta:
        die("no session '%s'" % name, "agent-session list")
    return meta


def load_progress(name):
    return read_json(os.path.join(sdir_of(name), "progress.json")) or {}


def turn_numbers(sdir):
    nums = []
    try:
        for f in os.listdir(os.path.join(sdir, "turns")):
            m = re.match(r"^(\d+)\.json$", f)
            if m:
                nums.append(int(m.group(1)))
    except OSError:
        pass
    return sorted(nums)


def load_turn(sdir, n):
    return read_json(os.path.join(sdir, "turns", "%d.json" % n))


def load_turns(sdir):
    recs = []
    for n in turn_numbers(sdir):
        rec = load_turn(sdir, n)
        if rec:
            recs.append(rec)
    return recs


def read_events(name):
    from agent_sessions.common import read_jsonl
    return read_jsonl(events_path(name))


def _default_class(status, turns):
    if status == "ok":
        return None
    if status in ("killed", "lost", "timeout"):
        return status
    for t in reversed(turns):
        if t.get("status") != "ok" and t.get("failure_class") in FAILURE_CLASSES:
            return t["failure_class"]
    return "engine_error"


def build_result(meta, turns, status, ended, reason=None, failure_class=None, warnings=None, git=None,
                 verify=None):
    """The result.json document (v3). `turns` are the final attempts, in order."""
    sdir = sdir_of(meta["name"])
    last = turns[-1] if turns else None
    costs = [t.get("cost_usd") for t in turns if isinstance(t.get("cost_usd"), (int, float))]
    started_epoch = meta.get("started_epoch") or time.time()
    usage = empty_usage()
    reported = False
    retries = []
    for t in turns:
        if isinstance(t.get("usage"), dict):
            add_usage(usage, t["usage"])
            reported = True
        retries.extend(r for r in (t.get("retries") or []) if isinstance(r, dict))
    sources = set(t.get("cost_source") for t in turns if t.get("cost_source"))
    cost_source = "engine" if "engine" in sources else ("estimate" if "estimate" in sources else None)
    warnings = list(warnings or [])
    if turns and not reported and "usage not reported by engine" not in warnings:
        warnings.append("usage not reported by engine")
    if status == "ok":
        failure_class = None
    elif failure_class not in FAILURE_CLASSES:
        failure_class = _default_class(status, turns)
    budget = dict(meta.get("budget")) if isinstance(meta.get("budget"), dict) else empty_budget()
    for key in empty_budget():
        budget.setdefault(key, None)
    for key in list(budget):
        if key not in empty_budget():
            del budget[key]
    if budget.get("ledger"):
        ledger = load_ledger(budget["ledger"])
        rem_usd, _rem_tok = ledger_remaining(ledger) if ledger else (None, None)
        budget["ledger_remaining_usd"] = rem_usd
    res = {
        "version": 3,
        "name": meta["name"], "engine": meta["engine"], "model": meta.get("model"),
        "tier": meta.get("tier"), "role": meta.get("role") or "implementation",
        "workdir": meta.get("workdir"), "state_dir": sdir,
        "log": os.path.join(sdir, "log"), "events_file": os.path.join(sdir, "events.jsonl"),
        "status": status, "ended": ended, "failure_class": failure_class,
    }
    if reason:
        res["reason"] = reason
    res.update({
        "exit_code": last.get("exit_code") if last else None,
        "turns": len(turns),
        "failed_turns": [{"turn": t["turn"], "status": t["status"]} for t in turns
                         if t.get("status") != "ok" and not t.get("interrupted")],
        "interrupted_turns": [t["turn"] for t in turns if t.get("interrupted")],
        "attempts": 1 + len(retries), "retries": retries,
        "resumes": [r for r in (meta.get("resumes") or []) if isinstance(r, dict)],
        "started_iso": meta.get("started_iso"), "finished_iso": now_iso(),
        "duration_s": round(max(0.0, time.time() - started_epoch), 1),
        "answer": last.get("answer") if last else None,
        "answer_chars": last.get("answer_chars", 0) if last else 0,
        "answer_file": last.get("answer_file") if last else None,
        "structured_output": last.get("structured_output") if last else None,
        "schema_valid": last.get("schema_valid") if last else None,
        "schema_file": meta.get("schema_file"),
        "session_id": (last.get("session_id") if last else None) or meta.get("conversation_id"),
        "engine_version": meta.get("engine_version"),
        "usage": usage,
        "cost_usd": round(sum(costs), 6) if costs else None,
        "cost_source": cost_source if costs else None,
        "budget": budget,
        "verify": verify,
        "git": git,
        "pipeline": meta.get("pipeline") if isinstance(meta.get("pipeline"), dict) else None,
        "warnings": warnings,
        "worktree": meta.get("worktree"),
    })
    if status != "ok" and last and last.get("stderr_tail"):
        res["stderr_tail"] = last["stderr_tail"]
    res["untrusted"] = UNTRUSTED
    return res


def write_lost(name, meta):
    """The backend is gone and no result exists: record what can be known."""
    sdir = sdir_of(name)
    if backend_of(meta) == "process":
        reason = "runner process gone without a result (killed, crash or reboot)"
    else:
        reason = "tmux session gone without a result (server killed, crash or reboot)"
    info = read_json(os.path.join(sdir, "engine.pid"))
    if engine_alive(info):
        reason += "; engine pid %d still alive, run: agent-session kill %s" % (info["pid"], name)
    res = build_result(meta, load_turns(sdir), "lost", "lost", reason, failure_class="lost")
    write_json(os.path.join(sdir, "result.json"), res)
    return res


def session_state(name, meta, live=None):
    """live: a Liveness cache for one pass over many children, else None."""
    sdir = sdir_of(name)
    res = read_json(os.path.join(sdir, "result.json"))
    if live is None:
        live = Liveness()
    alive = live.tmux(name, meta)
    if res:
        return res.get("status", "?") + ("/linger" if alive else "")
    if is_queued(name, meta) and not alive:
        return "queued"
    if alive or runner_alive(name):
        if not meta.get("steerable"):
            return "running"
        prog = read_json(os.path.join(sdir, "progress.json")) or {}
        if prog.get("phase") == "turn":
            return "turn %s" % prog.get("turn", "?")
        return "waiting"
    return "lost"


def activity_seconds(name, meta):
    prog = read_json(os.path.join(sdir_of(name), "progress.json")) or {}
    epoch = prog.get("last_event_epoch") or prog.get("updated_epoch") or meta.get("started_epoch")
    if not epoch:
        return None
    return max(0, int(time.time() - epoch))


class state_lock(object):
    """Exclusive flock on <state>/.lock. Re-entrant within one process, so
    spawn --replace can call reap_one while holding it. Held by spawn from the
    cap check until the tmux session is up, and by anything that is about to
    declare a child lost or delete its state, so neither can misjudge a child
    that is still being started."""
    depth = 0
    fd = None

    def __enter__(self):
        if state_lock.depth == 0:
            os.makedirs(STATE_ROOT, 0o700, exist_ok=True)
            fd = os.open(os.path.join(STATE_ROOT, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            state_lock.fd = fd
        state_lock.depth += 1
        return self

    def __exit__(self, *exc):
        state_lock.depth -= 1
        if state_lock.depth == 0:
            fd, state_lock.fd = state_lock.fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return False


def is_queued(name, meta=None):
    """Created by spawn --queue/--after (or resume) and not started yet."""
    meta = load_meta(name) or meta or {}
    return isinstance(meta.get("queue"), dict)


def working_children(engine=None):
    """Names holding a concurrency slot: unfinished, not queued, and live or
    just reserved. With `engine`, only children of that engine."""
    names = []
    live = Liveness()
    try:
        entries = sorted(os.listdir(STATE_ROOT))
    except OSError:
        return names
    for name in entries:
        if not NAME_RE.match(name):
            continue
        sdir = sdir_of(name)
        meta_p = os.path.join(sdir, "meta.json")
        reserved_p = os.path.join(sdir, "reserved")
        marker = meta_p if os.path.exists(meta_p) else (reserved_p if os.path.exists(reserved_p) else None)
        if marker is None or os.path.exists(os.path.join(sdir, "result.json")):
            continue
        meta = read_json(meta_p) if marker == meta_p else None
        if meta is not None and isinstance(meta.get("queue"), dict):
            continue
        if engine is not None and (meta is None or meta.get("engine") != engine):
            continue
        if live.alive(name, meta):
            names.append(name)
            continue
        # A reserved marker without meta.json is a spawn that died or is
        # mid-flight; it holds its slot for the reservation window. A child
        # with meta.json is started under the lock, so liveness is exact.
        if marker == reserved_p:
            try:
                if time.time() - os.stat(marker).st_mtime < RESERVATION_S:
                    names.append(name)
            except OSError:
                pass
    return names


def stale_reservations():
    """State dirs left by a spawn that died between reserving and writing
    meta.json (reserved marker, no meta, older than the reservation window)."""
    names = []
    try:
        entries = sorted(os.listdir(STATE_ROOT))
    except OSError:
        return names
    for name in entries:
        if not NAME_RE.match(name):
            continue
        sdir = sdir_of(name)
        reserved_p = os.path.join(sdir, "reserved")
        if os.path.exists(os.path.join(sdir, "meta.json")) or not os.path.exists(reserved_p):
            continue
        try:
            if time.time() - os.stat(reserved_p).st_mtime >= RESERVATION_S:
                names.append(name)
        except OSError:
            pass
    return names


def all_metas():
    metas = []
    try:
        names = os.listdir(STATE_ROOT)
    except OSError:
        return metas
    for name in names:
        if not NAME_RE.match(name):
            continue
        meta = load_meta(name)
        if meta:
            metas.append(meta)
    metas.sort(key=lambda m: (m.get("started_epoch") or 0, m.get("started_iso") or "", m["name"]))
    return metas


def child_alive(name, meta):
    if meta is None:
        meta = load_meta(name)
    return backend.alive(name, meta)


def retire_runner(name, meta, seconds=10):
    """End a finished child's lingering session and wait for its runner to
    exit: on its way out it still writes progress.json, which must not land
    in a state directory that is being removed or reused. Call it outside the
    state lock (that runner's last kick() takes it). True when it is gone."""
    backend.end_session(name, meta)
    deadline = time.time() + seconds
    while child_alive(name, meta) and time.time() < deadline:
        time.sleep(0.2)
    return not child_alive(name, meta)


def check_terminal(name, meta):
    """Return the (normalized) result dict when the child is finished or lost, else None.
    A queued child is pending, never lost."""
    sdir = sdir_of(name)
    res = read_json(os.path.join(sdir, "result.json"))
    if res:
        return normalize_result(res)
    meta = load_meta(name) or meta
    if is_queued(name, meta) or child_alive(name, meta):
        return None
    # No result, no tmux, no runner. Decide under the lock: a spawn in flight
    # holds it until tmux is up, and the runner may have just written the result.
    with state_lock():
        res = read_json(os.path.join(sdir, "result.json"))
        if res:
            return normalize_result(res)
        meta = load_meta(name) or meta
        if is_queued(name, meta) or child_alive(name, meta):
            return None
        return write_lost(name, meta)


def effective_depth():
    """Nesting depth of the caller. AGENT_SESSION_DEPTH is the fast path; when
    the caller is a child (AGENT_SESSION_NAME) the parent's recorded depth wins,
    so a child that unset the variable still cannot spawn past the ceiling."""
    depth = env_int("AGENT_SESSION_DEPTH", 0)
    parent = os.environ.get("AGENT_SESSION_NAME")
    if parent:
        pm = load_meta(parent)
        if pm and isinstance(pm.get("depth"), int):
            depth = max(depth, pm["depth"])
        elif depth < 1:
            depth = 1
    return depth


def descendants(meta):
    """Metas of children this child spawned (their owner is its child token)."""
    from agent_sessions.procs import child_token
    token = child_token(meta)
    return [m for m in all_metas() if m.get("owner") == token]


def unfinished(meta):
    """A child that still needs killing: no result, or lost with a live engine."""
    sdir = sdir_of(meta["name"])
    res = read_json(os.path.join(sdir, "result.json"))
    if not res:
        return True
    return res.get("status") == "lost" and engine_alive(read_json(os.path.join(sdir, "engine.pid")))


# --------------------------------------------------------------------------- pauses, groups, kick

def pauses_dir():
    return os.path.join(STATE_ROOT, "pauses")


def read_pause(engine):
    """The active pause for an engine, or None (expired pauses count as none)."""
    pause = read_json(os.path.join(pauses_dir(), "%s.json" % engine))
    if not isinstance(pause, dict):
        return None
    until = pause.get("until_epoch")
    if not isinstance(until, (int, float)) or until <= time.time():
        return None
    pause.setdefault("until_iso", iso_of(until))
    return pause


def active_pauses():
    found = {}
    try:
        names = os.listdir(pauses_dir())
    except OSError:
        return found
    for f in sorted(names):
        if f.endswith(".json"):
            pause = read_pause(f[:-5])
            if pause:
                found[f[:-5]] = pause
    return found


def write_pause(engine, until_epoch, reason, by):
    os.makedirs(pauses_dir(), 0o700, exist_ok=True)
    write_json(os.path.join(pauses_dir(), "%s.json" % engine),
               {"until_epoch": int(until_epoch), "until_iso": iso_of(until_epoch), "reason": reason, "by": by,
                "written_iso": now_iso()})


def groups_dir():
    return os.path.join(STATE_ROOT, "groups")


def load_group(group):
    if not NAME_RE.match(group or ""):
        return None
    return read_json(os.path.join(groups_dir(), "%s.json" % group))


def write_group(group, doc):
    os.makedirs(groups_dir(), 0o700, exist_ok=True)
    write_json(os.path.join(groups_dir(), "%s.json" % group), doc)


def group_members(group):
    g = load_group(group)
    if not g:
        return None
    return [m for m in (g.get("members") or []) if isinstance(m, str)]


def kick():
    """Advance cooperative scheduling (see launch.kick). Called by every runner
    at exit and by spawn, wait, status, list, unpause, resume and recover."""
    from agent_sessions.launch import kick as _kick
    return _kick()


# --------------------------------------------------------------------------- ledgers

def ledgers_dir():
    return os.path.join(STATE_ROOT, "ledgers")


def load_ledger(ledger_id):
    if not re.match(r"^[0-9a-f]{6,32}$", ledger_id or ""):
        return None
    return read_json(os.path.join(ledgers_dir(), "%s.json" % ledger_id))


def write_ledger(ledger):
    os.makedirs(ledgers_dir(), 0o700, exist_ok=True)
    write_json(os.path.join(ledgers_dir(), "%s.json" % ledger["id"]), ledger)


def ledger_remaining(ledger):
    """(usd remaining or None, tokens remaining or None)."""
    usd = tok = None
    if isinstance(ledger.get("limit_usd"), (int, float)):
        usd = round(ledger["limit_usd"] - (ledger.get("spent_usd") or 0.0), 6)
    if isinstance(ledger.get("limit_tokens"), int):
        tok = ledger["limit_tokens"] - int(ledger.get("spent_tokens") or 0)
    return usd, tok


def ledger_exhausted(ledger):
    usd, tok = ledger_remaining(ledger)
    return (usd is not None and usd <= 0) or (tok is not None and tok <= 0)


def fmt_usd(value):
    text = ("%.6f" % value).rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def ledger_add(ledger_id, name, usd, tokens):
    """Add one turn's spend under the state lock. Returns the updated ledger or None."""
    with state_lock():
        ledger = load_ledger(ledger_id)
        if not ledger:
            return None
        child = ledger.setdefault("children", {}).setdefault(name, {"usd": 0.0, "tokens": 0})
        if isinstance(usd, (int, float)):
            ledger["spent_usd"] = round((ledger.get("spent_usd") or 0.0) + usd, 6)
            child["usd"] = round(child.get("usd", 0.0) + usd, 6)
        if isinstance(tokens, int):
            ledger["spent_tokens"] = int(ledger.get("spent_tokens") or 0) + tokens
            child["tokens"] = int(child.get("tokens", 0)) + tokens
        write_ledger(ledger)
        return ledger


def merge_judge(name, res):
    """`result --json` and `status --json` carry judge.json under `judge` when it exists."""
    judge = read_json(os.path.join(sdir_of(name), "judge.json"))
    if isinstance(judge, dict) and isinstance(res, dict):
        res = dict(res)
        res["judge"] = judge
    return res
