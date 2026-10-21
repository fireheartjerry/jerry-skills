"""Every user-facing command (cmd_*) and the views they print.

Human output is `key : value` lines plus the untrusted framing around child
text; `--json` prints exactly one JSON document.
"""

import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
import uuid

from agent_sessions.common import (
    DEFAULT_BUDGET, ENGINES, HOOK_DONE, MAX_CHILDREN, MAX_DEPTH, NAME_RE, STATE_ROOT, TIERS, TMUX_INSTALL, die,
    engine_cap, fmt_cost, kv, now_iso, options_of, out, parse_args, parse_int, passes_env, read_json, read_text, run,
    sdir_of, touch, warn, write_json, write_private_json, write_text,
)
from agent_sessions.procs import engine_alive, killpg, owner_short, owner_token, ps_lstart, wait_dead
from agent_sessions.backend import (
    BACKENDS, Liveness, backend_of, capture, choose_backend, end_session, env_socket, session_of, target, tmux,
    tmux_alive, tmux_argv, tmux_bin,
)
from agent_sessions.backend import alive as backend_alive, kill as backend_kill
from agent_sessions.state import (
    active_pauses, activity_seconds, all_metas, build_result, check_terminal, child_alive, descendants,
    empty_budget, events_path, fmt_usd, group_members, is_queued, kick, ledger_remaining, ledgers_dir,
    load_group, load_ledger, load_meta, load_progress, load_turn, load_turns, merge_judge, normalize_result,
    pauses_dir, read_pause, require_meta, session_state, stale_reservations, state_lock, turn_numbers, unfinished,
    working_children, write_group, write_lost,
)
from agent_sessions.engines import engine_for
from agent_sessions.schema import SCHEMAS
from agent_sessions.gitops import (
    branch_tip, git, is_dirty, is_merged, repo_root, resolve_commit, snapshot, snapshot_ref_of_commit,
)
from agent_sessions.launch import (
    SpawnError, create, default_budget_line, finalize_queued, fire_hook, parse_names_loose, parse_spawn,
    start_child,
)

__all__ = [
    "cmd_spawn", "cmd_list", "cmd_status", "cmd_wait", "cmd_result", "cmd_send", "cmd_turn", "cmd_log",
    "cmd_peek", "cmd_events", "cmd_stop", "cmd_kill", "cmd_reap", "cmd_doctor", "cmd_engines", "cmd_schema",
    "cmd_resume", "cmd_recover", "cmd_budget", "cmd_unpause",
    "print_result_view", "print_turn_view", "render_events", "reap_one", "kill_session", "SpawnFailure",
    "tmux_version_ok", "status_json", "expand_names", "verify_line", "resume_child", "create_or_die",
    "print_spawned", "mode_text",
]


# --------------------------------------------------------------------------- views

def print_untrusted(label, text, max_chars, more_file):
    """--max-chars 0 prints only where the text lives, never the text."""
    out("--- %s (child output, untrusted) ---" % label)
    if text is None:
        out("(none)")
    elif max_chars == 0:
        out("[%d chars in %s]" % (len(text), more_file))
    elif len(text) > max_chars:
        out(text[:max_chars].rstrip("\n"))
        out("[... %d more chars in %s]" % (len(text) - max_chars, more_file))
    else:
        out(text.rstrip("\n"))
    out("--- end ---")


def usage_line(usage, cost, source):
    if not isinstance(usage, dict):
        return "cost: %s" % fmt_cost(cost)
    text = "in %s  cached %s  out %s  total %s" % (
        usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0),
        usage.get("output_tokens", 0), usage.get("total_tokens", 0))
    if isinstance(cost, (int, float)):
        text += "  cost: %s (%s)" % (fmt_cost(cost), source or "?")
    return text


def print_result_header(res, meta):
    res = normalize_result(dict(res))
    kv("name", res.get("name"))
    kv("status", "%-20s (ended: %s%s)" % (res.get("status"), res.get("ended"),
                                           ", %s" % res["failure_class"] if res.get("failure_class") else ""))
    role = res.get("role") or (meta or {}).get("role")
    kv("engine", "%s %s%s" % (res.get("engine"), res.get("model") or "default",
                               "  role: %s" % role if role and role != "implementation" else ""))
    kv("exit", "%-6s turns: %-4s duration: %-8s cost: %s" % (
        res.get("exit_code"), res.get("turns"), "%ss" % res.get("duration_s"), fmt_cost(res.get("cost_usd"))))
    if isinstance(res.get("usage"), dict):
        kv("tokens", usage_line(res["usage"], res.get("cost_usd"), res.get("cost_source")))
    wt = res.get("worktree") or (meta or {}).get("worktree")
    kv("workdir", "%s%s" % (res.get("workdir"), "   [worktree %s]" % wt["branch"] if wt else ""))
    kv("files", "log=%s  answer=%s  state=%s" % (res.get("log"), res.get("answer_file"), res.get("state_dir")))
    g = res.get("git")
    if isinstance(g, dict):
        kv("git", "%s: %s commit%s, %s file%s changed (+%s -%s)%s" % (
            g.get("branch"), g.get("commits"), "" if g.get("commits") == 1 else "s",
            g.get("files_changed"), "" if g.get("files_changed") == 1 else "s",
            g.get("insertions"), g.get("deletions"), "  dirty" if g.get("dirty") else ""))
    if res.get("schema_file"):
        kv("schema", "%s (%s)" % ("valid" if res.get("schema_valid") else "invalid", res["schema_file"]))
    if isinstance(res.get("verify"), dict):
        kv("verify", verify_line(res["verify"]))
    if res.get("failed_turns"):
        kv("failed", ", ".join("turn %s %s" % (t["turn"], t["status"]) for t in res["failed_turns"]))
    if res.get("reason"):
        kv("reason", res["reason"])
    for w in res.get("warnings") or []:
        kv("warning", w)
    if isinstance(res.get("judge"), dict):
        j = res["judge"]
        kv("judge", "%s (%s, %d findings)" % (j.get("verdict"), j.get("engine"), len(j.get("findings") or [])))
    return res


def verify_line(v):
    status = v.get("status")
    if status == "pass":
        return "pass (%ss%s)" % (v.get("duration_s"), ", %d runs" % v["attempts"] if (v.get("attempts") or 1) > 1 else "")
    if status == "fail":
        return "fail exit %s (see %s)" % (v.get("exit_code"), v.get("output_file"))
    if status == "error":
        return "error: %s (see %s)" % (v.get("error") or "did not complete", v.get("output_file"))
    return "skipped (the child did not finish ok)"


def print_result_view(res, meta, max_chars, answer=True):
    if res.get("name") and "judge" not in res:
        res = merge_judge(res["name"], res)
    res = print_result_header(res, meta)
    if not answer:
        return
    print_untrusted("answer", res.get("answer"), max_chars, res.get("answer_file"))
    if res.get("status") != "ok" and res.get("stderr_tail"):
        print_untrusted("stderr", res["stderr_tail"], max_chars, (meta or {}).get("name") and
                        os.path.join(res.get("state_dir", ""), "turns"))


def print_turn_view(name, rec, max_chars):
    kv("name", name)
    kv("turn", "%-6s message : %s" % (rec.get("turn"), rec.get("message_id")))
    kv("status", "%s%s" % (rec.get("status"), " (%s)" % rec["failure_class"] if rec.get("failure_class") else ""))
    kv("exit", "%-6s duration: %-8s cost: %s  tool calls: %s" % (
        rec.get("exit_code"), "%ss" % rec.get("duration_s"), fmt_cost(rec.get("cost_usd")), rec.get("tool_calls")))
    if rec.get("schema_valid") is not None:
        kv("schema", "valid" if rec["schema_valid"] else "invalid")
    if rec.get("reason"):
        kv("reason", rec["reason"])
    print_untrusted("answer", rec.get("answer"), max_chars, rec.get("answer_file"))
    if rec.get("status") != "ok" and rec.get("stderr_tail"):
        print_untrusted("stderr", rec["stderr_tail"], max_chars, rec.get("stderr_file"))


def pending_line(name, meta, hint_turn=False):
    state = session_state(name, meta)
    act = activity_seconds(name, meta)
    out("%-9s : %s (%s, last activity %s ago)" % ("pending", name, state, "%ds" % act if act is not None else "?"))
    if hint_turn and meta.get("steerable") and state == "waiting":
        nums = turn_numbers(sdir_of(name))
        out("%-9s : %s is steerable and idle between turns; it finishes only after stop or the idle exit. "
            "Read its replies with: agent-session turn %s %s" % ("note", name, name, nums[-1] if nums else 1))


def done_line(name, res):
    out("%-9s : %s %s%s" % ("done", name, res.get("status"),
                            "   (agent-session result %s)" % name if res.get("answer") else ""))


def status_exit(status):
    return 0 if status == "ok" else 1


def pending_json(name, meta):
    return {"name": name, "status": "pending", "state": session_state(name, meta),
            "activity_s": activity_seconds(name, meta)}


# --------------------------------------------------------------------------- spawn

class SpawnFailure(Exception):
    pass


def engine_list():
    return ", ".join(ENGINES[:-1]) + " or " + ENGINES[-1]


def create_or_die(spec):
    try:
        return create(spec)
    except SpawnError as exc:
        die(exc.msg, exc.hint, exc.code)


def mode_text(meta):
    mode = ("steerable" if meta.get("steerable") else "one-shot") + (" + readonly" if meta.get("readonly") else "")
    if meta.get("role") and meta["role"] != "implementation":
        mode += " (role %s)" % meta["role"]
    return mode


def print_spawned(meta, info):
    engine = engine_for(meta["engine"])
    for w in info.get("warnings") or []:
        warn(w)
    if info.get("notice"):
        out(info["notice"])
    name = meta["name"]
    kv("spawned", name)
    model, tier = meta.get("model"), meta.get("tier")
    kv("engine", "%s (%s)%s" % (meta["engine"], ("%s, %s" % (model, tier)) if model else "default",
                                "   [experimental: documented format only]" if not engine.verified else ""))
    kv("mode", mode_text(meta))
    wt = meta.get("worktree")
    if wt:
        kv("workdir", "%s   [worktree %s from %s]" % (meta["workdir"], wt["branch"], wt["base_commit"][:7]))
    elif meta.get("worktree_requested"):
        kv("workdir", "%s   [worktree agent-session/%s created when it starts, from %s]" % (
            meta["origin"], name, meta.get("base_ref") or "HEAD"))
    else:
        kv("workdir", meta["workdir"])
    kv("state", sdir_of(name))
    if meta.get("schema_file"):
        kv("schema", "%s (validated locally; one fixup turn on mismatch)" % meta["schema_file"])
    if meta.get("verify"):
        kv("verify", "%s (after the last turn; %d fixup turn%s on failure)" % (
            meta["verify"], meta.get("verify_fixups") or 0, "" if meta.get("verify_fixups") == 1 else "s"))
    line = default_budget_line(meta)
    if line:
        kv("budget", line)
    if meta.get("after"):
        kv("after", ", ".join(meta["after"]))
    if info.get("state") == "queued":
        kv("queued", "%s: %s" % (info.get("queue_reason"), info.get("queue_detail")))
    elif info.get("state") == "finished":
        res = read_json(os.path.join(sdir_of(name), "result.json")) or {}
        kv("finished", "%s before starting: %s" % (res.get("status"), res.get("reason")))
    if meta.get("steerable"):
        kv("next", "agent-session turn %s 1 --timeout %d   (opening reply; repeat while exit code is 3), "
           "then agent-session send %s \"<text>\"" % (name, DEFAULT_BUDGET, name))
    else:
        kv("next", "agent-session wait %s --timeout %d   (repeat while exit code is 3)" % (name, DEFAULT_BUDGET))
    if meta.get("backend") == "process":
        kv("output", os.path.join(sdir_of(name), "pane.log"))
    elif info.get("state") != "queued":
        sock = meta.get("tmux_socket")
        kv("attach", "tmux %sattach -t '%s'" % ("-L %s " % sock if sock else "", target(name)))


def cmd_spawn(args):
    spec = parse_spawn(args)
    if spec["n"] is not None:
        return spawn_best_of(spec)
    meta, info = create_or_die(spec)
    if spec["json"]:
        doc = dict(meta)
        doc["state"] = info["state"]
        if info.get("warnings"):
            doc["warnings"] = info["warnings"]
        out(json.dumps(doc, indent=2))
        return 0
    print_spawned(meta, info)
    return 0


def spawn_best_of(spec):
    """spawn --n N: members <name>-1..N in their own worktrees, engines
    round-robin from --engines, recorded as a group for wait, select, collect
    and reap."""
    n, name = spec["n"], spec["name"]
    if n < 2 or n > 6:
        die("--n must be between 2 and 6")
    if len(name) > 38:
        die("best-of-N names its members <name>-1..<name>-%d; use a name of at most 38 characters" % n)
    if not repo_root(spec["dir"]):
        die("--n needs a git repository: every member works in its own worktree (%s)" % spec["dir"])
    if spec["steerable"]:
        die("--n runs one-shot members so select can rank finished work; drop --steerable")
    engines = parse_names_loose(spec["engines"]) if spec["engines"] else [spec["engine"]]
    for e in engines:
        if e not in ENGINES:
            die("--engines: unknown engine '%s'; use %s" % (e, engine_list()))
    if spec["model"] and len(set(engines)) > 1:
        die("--model names one engine's model, and --engines mixes %s; use --tier instead" % ", ".join(
            sorted(set(engines))))
    old = load_group(name)
    if old:
        if not spec["replace"]:
            die("group '%s' already exists" % name, "agent-session reap %s, or pass --replace" % name)
        if old.get("kind") != "best-of":
            die("'%s' is a %s group; pick another name" % (name, old.get("kind")))
        orphans = [m for m in old.get("members") or []
                   if load_meta(m) and m not in ["%s-%d" % (name, k) for k in range(1, n + 1)]]
        if orphans:
            die("--replace with --n %d would leave %s outside the group" % (n, ", ".join(orphans)),
                "agent-session reap %s" % " ".join(orphans))
    if load_meta(name):
        die("'%s' is already a child name; pick another group name" % name)
    members = []
    rows = []
    group = {"name": name, "kind": "best-of", "members": [], "engines": [], "created_iso": now_iso(),
             "owner": owner_token(), "winner": None, "selection": None, "dir": spec["dir"]}
    write_group(name, group)
    for k in range(1, n + 1):
        member = dict(spec)
        member.update({"name": "%s-%d" % (name, k), "engine": engines[(k - 1) % len(engines)], "worktree": True,
                       "queue": True, "group": name, "n": None})
        try:
            meta, info = create(member)
        except SpawnError as exc:
            group["members"] = members
            write_group(name, group)
            die("member %s: %s" % (member["name"], exc.msg), exc.hint, exc.code)
        members.append(meta["name"])
        group["members"] = members
        group["engines"].append(meta["engine"])
        write_group(name, group)
        rows.append((meta, info))
    if spec["json"]:
        out(json.dumps({"group": name, "members": [dict(m, state=i["state"]) for m, i in rows]}, indent=2))
        return 0
    kv("group", "%s (best of %d)" % (name, n))
    for meta, info in rows:
        kv("member", "%s  %s %s  %s" % (meta["name"], meta["engine"], meta.get("model") or "default",
                                        info["state"] if info["state"] != "queued" else
                                        "queued (%s)" % info.get("queue_reason")))
    if spec.get("verify"):
        kv("verify", spec["verify"])
    kv("next", "agent-session wait %s --timeout %d, then agent-session select %s --judge" % (
        name, DEFAULT_BUDGET, name))
    return 0


# --------------------------------------------------------------------------- list / status / log / peek / events

def my_metas(me=None):
    me = me or owner_token()
    return [m for m in all_metas() if m.get("owner") == me]


def cmd_list(args):
    _pos, o = parse_args(args, **options_of("list"))
    if not tmux_bin(required=False):
        warn("tmux not found; liveness unknown")
    with_lock_kick()
    live = Liveness()
    me = owner_token()
    members = None
    if "group" in o:
        members = group_members(o["group"])
        if members is None:
            die("no group '%s'" % o["group"], "agent-session list")
    rows = []
    for meta in all_metas():
        name = meta["name"]
        if o.get("mine") and meta.get("owner") != me:
            continue
        if members is not None and name not in members:
            continue
        finished = os.path.exists(os.path.join(sdir_of(name), "result.json"))
        if o.get("running") and finished:
            continue
        state = session_state(name, meta, live)
        act = None if finished else activity_seconds(name, meta)
        rows.append((meta, state, act))
    pauses = active_pauses()
    if o.get("json"):
        doc = [dict(meta, state=state, activity_s=act) for meta, state, act in rows]
        out(json.dumps(doc, indent=2))
        return 0
    for engine, pause in sorted(pauses.items()):
        out("paused: %s until %s (%s)" % (engine, time.strftime("%H:%M", time.localtime(pause["until_epoch"])),
                                           pause.get("reason") or "usage limit"))
    if not rows:
        out("(no sessions)")
        return 0
    out("%-20s %-7s %-11s %-9s %-9s %-14s %s" % ("NAME", "ENGINE", "STATE", "MODEL", "ACTIVITY", "OWNER", "DIR"))
    for meta, state, act in rows:
        owner = "mine" if meta.get("owner") == me else owner_short(meta.get("owner"))
        out("%-20s %-7s %-11s %-9s %-9s %-14s %s" % (
            meta["name"][:20], meta["engine"], state[:11], (meta.get("model") or "default")[:9],
            ("%ds ago" % act) if act is not None else "-", owner[:14], meta.get("workdir", "")))
    return 0


def with_lock_kick():
    """kick() under the lock; a failure to take the lock is not an error for a read-only command."""
    try:
        with state_lock():
            kick()
    except OSError:
        pass


def budget_view(meta, prog):
    limits = meta.get("budget") if isinstance(meta.get("budget"), dict) else empty_budget()
    view = dict(limits)
    usage = prog.get("usage_so_far") if isinstance(prog.get("usage_so_far"), dict) else None
    cost = prog.get("cost_so_far")
    view["remaining_usd"] = (round(limits["usd_limit"] - cost, 6)
                             if isinstance(limits.get("usd_limit"), (int, float)) and isinstance(cost, (int, float))
                             else None)
    view["remaining_tokens"] = (limits["tokens_limit"] - usage.get("total_tokens", 0)
                                if isinstance(limits.get("tokens_limit"), int) and usage else None)
    return view


def queue_view(name, meta):
    """Position and blocking reason of a queued child, else None."""
    meta = load_meta(name) or meta
    q = meta.get("queue")
    if not isinstance(q, dict):
        return None
    queued = sorted((m for m in all_metas() if isinstance(m.get("queue"), dict)),
                    key=lambda m: (m["queue"].get("since") or 0, m["name"]))
    names = [m["name"] for m in queued]
    view = dict(q)
    view["position"] = names.index(name) + 1 if name in names else None
    return view


def status_json(name, meta):
    """The `status --json` document; a finished child's result without its answer."""
    sdir = sdir_of(name)
    res = check_terminal(name, meta)
    prog = load_progress(name)
    if res:
        doc = {"name": name, "state": res.get("status"), "phase": "finished"}
        body = merge_judge(name, res)
        for key, value in body.items():
            if key not in ("answer", "name"):
                doc[key] = value
        return doc, res
    now = time.time()
    stall_in = None
    if prog.get("phase") == "turn" and isinstance(prog.get("stall_deadline_epoch"), (int, float)):
        stall_in = max(0, int(prog["stall_deadline_epoch"] - now))
    info = read_json(os.path.join(sdir, "engine.pid")) or {}
    doc = {
        "name": name, "state": session_state(name, meta), "phase": prog.get("phase"),
        "turn": prog.get("turn"), "attempt": prog.get("attempt", 1),
        "activity_s": activity_seconds(name, meta), "events": prog.get("events", 0),
        "tool_calls": prog.get("tool_calls", 0), "last_event": prog.get("last_event"),
        "last_event_iso": prog.get("last_event_iso"),
        "usage_so_far": prog.get("usage_so_far"), "cost_so_far": prog.get("cost_so_far"),
        "budget": budget_view(meta, prog), "stall_s": meta.get("stall_s"), "stall_in_s": stall_in,
        "queue": queue_view(name, meta), "verify": prog.get("verify"),
        "pid": info.get("pid") if engine_alive(info) else None, "runner_pid": prog.get("runner_pid"),
        "engine": meta.get("engine"), "model": meta.get("model"), "role": meta.get("role"),
        "workdir": meta.get("workdir"), "state_dir": sdir, "started_iso": meta.get("started_iso"),
    }
    return doc, None


def cmd_status(args):
    pos, o = parse_args(args, **options_of("status"))
    if len(pos) != 1:
        die("usage: status <name> [--json]")
    name = pos[0]
    meta = require_meta(name)
    with_lock_kick()
    doc, res = status_json(name, meta)
    if o.get("json"):
        out(json.dumps(doc, indent=2))
        return status_exit(res.get("status")) if res else 3
    if res:
        print_result_view(res, meta, 0, answer=False)
        kv("answer", "agent-session result %s" % name)
        return status_exit(res.get("status"))
    kv("name", name)
    kv("state", "%s (phase %s, turn %s, attempt %s)" % (doc["state"], doc["phase"], doc["turn"], doc["attempt"]))
    kv("activity", "%s ago; %s events, %s tool calls" % (
        "%ds" % doc["activity_s"] if doc["activity_s"] is not None else "?", doc["events"], doc["tool_calls"]))
    if doc.get("last_event"):
        kv("last", doc["last_event"])
    kv("tokens", usage_line(doc.get("usage_so_far"), doc.get("cost_so_far"), "so far"))
    if doc.get("stall_in_s") is not None:
        kv("stall", "watchdog fires in %ss without engine output (--stall %s)" % (doc["stall_in_s"], doc["stall_s"]))
    if doc.get("queue"):
        q = doc["queue"]
        kv("queued", "position %s, blocked by %s: %s (since %s)" % (
            q.get("position"), q.get("reason"), q.get("detail"), q.get("since_iso")))
    kv("pid", "engine %s, runner %s" % (doc.get("pid"), doc.get("runner_pid")))
    kv("next", "agent-session wait %s --timeout %d" % (name, DEFAULT_BUDGET))
    return 3


def render_events(path, engine):
    """Rendered lines of a raw per-turn events file (`log --turn K`)."""
    lines = []
    parser = engine_for(engine).parser(lambda kind, text, data=None: lines.append(text))
    text = read_text(path, "")
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            lines.append(raw)
            continue
        if isinstance(obj, dict):
            parser.feed(obj)
    return lines


def cmd_log(args):
    pos, o = parse_args(args, **options_of("log"))
    if not pos:
        die("usage: log <name> [N] [--turn K]")
    name = pos[0]
    meta = require_meta(name)
    count = parse_int(pos[1], "line count") if len(pos) > 1 else 80
    if "turn" in o:
        k = parse_int(o["turn"], "--turn", 1)
        path = os.path.join(sdir_of(name), "turns", "%d.events.jsonl" % k)
        if not os.path.exists(path):
            die("no turn %d for '%s'" % (k, name))
        lines = render_events(path, meta["engine"])
    else:
        lines = (read_text(os.path.join(sdir_of(name), "log"), "") or "").splitlines()
    for line in lines[-count:] if count else lines:
        out(line)
    return 0


def cmd_peek(args):
    pos, _o = parse_args(args, **options_of("peek"))
    if not pos:
        die("usage: peek <name> [N]")
    name = pos[0]
    meta = require_meta(name)
    count = parse_int(pos[1], "line count") if len(pos) > 1 else 60
    text = capture(name, meta, count)
    if text is None:
        die("no live session '%s'" % name, "agent-session log %s" % name)
    sys.stdout.write(text)
    return 0


def format_event(ev):
    ts = ev.get("ts")
    clock = time.strftime("%H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "--:--:--"
    turn = ev.get("turn")
    return "[%s] turn %s %s: %s" % (clock, turn if turn is not None else "-", ev.get("type"), ev.get("text", ""))


def cmd_events(args):
    pos, o = parse_args(args, **options_of("events"))
    if len(pos) != 1:
        die("usage: events <name> [--follow] [--timeout S] [--since N] [--json]")
    name = pos[0]
    meta = require_meta(name)
    since = parse_int(o.get("since", 0), "--since")
    budget = parse_int(o.get("timeout", DEFAULT_BUDGET), "--timeout")
    path = events_path(name)
    printed = 0
    deadline = time.time() + budget

    def flush():
        nonlocal printed
        text = read_text(path, "") or ""
        lines = text.splitlines()
        for raw in lines[max(printed, since):]:
            raw = raw.strip()
            if not raw:
                continue
            if o.get("json"):
                out(raw)
            else:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    out(raw)
                    continue
                out(format_event(ev) if isinstance(ev, dict) else raw)
        printed = max(printed, len(lines), since)
        sys.stdout.flush()

    flush()
    if not o.get("follow"):
        return 0
    while True:
        res = check_terminal(name, meta)
        flush()
        if res:
            return status_exit(res.get("status"))
        if time.time() >= deadline:
            return 3
        time.sleep(0.5)


# --------------------------------------------------------------------------- wait / result / turn

def expand_names(names):
    """Child names, with group names (best-of-N, plans) replaced by their members."""
    found = []
    for name in names:
        if load_meta(name) is None:
            members = group_members(name)
            if members is not None:
                for m in members:
                    if m not in found:
                        found.append(m)
                continue
        if name not in found:
            found.append(name)
    return found


def cmd_wait(args):
    pos, o = parse_args(args, **options_of("wait"))
    if len(pos) >= 2 and re.match(r"^\d+$", pos[-1]) and "timeout" not in o:
        o["timeout"] = pos.pop()
    if o.get("mine"):
        if pos:
            die("--mine takes no names: it waits for every unfinished child you own")
        pos = [m["name"] for m in my_metas() if not os.path.exists(os.path.join(sdir_of(m["name"]), "result.json"))]
    if not pos and not o.get("mine"):
        die("usage: wait [--timeout S] [--any] [--json] [--max-chars N] <name>... | --mine")
    pos = expand_names(pos)
    budget = parse_int(o.get("timeout", DEFAULT_BUDGET), "--timeout")
    max_chars = parse_int(o.get("max-chars", 20000), "--max-chars")
    metas = {}
    for name in pos:
        metas[name] = require_meta(name)
    if not pos:
        if o.get("json"):
            out(json.dumps({"finished": [], "pending": []}, indent=2))
        else:
            out("(no unfinished children of yours)")
        return 0
    deadline = time.time() + budget
    done = {}
    while True:
        with_lock_kick()
        for name in pos:
            if name not in done:
                res = check_terminal(name, metas[name])
                if res:
                    done[name] = res
        if len(done) == len(pos) or time.time() >= deadline:
            break
        if o.get("any") and done:
            break
        time.sleep(0.5)
    all_done = len(done) == len(pos)
    finished_ok = all(r.get("status") == "ok" for r in done.values())
    if o.get("any"):
        if o.get("json"):
            out(json.dumps({"finished": [merge_judge(n, done[n]) for n in pos if n in done],
                            "pending": [pending_json(n, metas[n]) for n in pos if n not in done]}, indent=2))
        else:
            first = True
            for name in pos:
                if name in done:
                    if not first:
                        out()
                    first = False
                    print_result_view(done[name], metas[name], max_chars)
            for name in pos:
                if name not in done:
                    pending_line(name, metas[name], hint_turn=True)
        if not done:
            return 3
        return 0 if finished_ok else 1
    if o.get("json"):
        items = []
        for name in pos:
            if name in done:
                items.append(merge_judge(name, done[name]))
            else:
                items.append(pending_json(name, metas[name]))
        out(json.dumps(items[0] if len(pos) == 1 else items, indent=2))
    elif all_done:
        first = True
        for name in pos:
            if not first:
                out()
            first = False
            print_result_view(done[name], metas[name], max_chars)
    else:
        # Still polling: one line per child. The full answers are printed once,
        # when the last child finishes, not on every poll of the loop.
        for name in pos:
            if name in done:
                done_line(name, done[name])
            else:
                pending_line(name, metas[name], hint_turn=True)
    if not all_done:
        return 3
    return 0 if finished_ok else 1


def cmd_result(args):
    pos, o = parse_args(args, **options_of("result"))
    if len(pos) != 1:
        die("usage: result <name> [--json] [--max-chars N]")
    name = pos[0]
    meta = require_meta(name)
    max_chars = parse_int(o.get("max-chars", 20000), "--max-chars")
    res = check_terminal(name, meta)
    if not res:
        if o.get("json"):
            out(json.dumps(pending_json(name, meta), indent=2))
        else:
            pending_line(name, meta, hint_turn=True)
            sys.stderr.write("  try: agent-session wait %s --timeout %d\n" % (name, DEFAULT_BUDGET))
        return 3
    if o.get("json"):
        out(json.dumps(merge_judge(name, res), indent=2))
    else:
        print_result_view(res, meta, max_chars)
    return status_exit(res.get("status"))


def resolve_turn(sdir, spec, message_id):
    if message_id:
        text = read_text(os.path.join(sdir, "messages", message_id))
        if text is None or not text.strip().isdigit():
            return None
        return int(text.strip())
    if spec is None or spec == "last":
        nums = turn_numbers(sdir)
        return nums[-1] if nums else None
    if not re.match(r"^\d+$", spec):
        die("turn must be a number, 'last' or --message ID")
    return int(spec)


def wait_for_turn(name, meta, spec, message_id, budget):
    """Poll for a turn record. Returns (rec, failure_message)."""
    sdir = sdir_of(name)
    deadline = time.time() + budget
    while True:
        n = resolve_turn(sdir, spec, message_id)
        rec = load_turn(sdir, n) if n else None
        if rec:
            return rec, None
        res = read_json(os.path.join(sdir, "result.json"))
        if res:
            return None, "child ended before answering (status %s)" % res.get("status")
        if not child_alive(name, meta):
            n = resolve_turn(sdir, spec, message_id)
            rec = load_turn(sdir, n) if n else None
            if rec:
                return rec, None
            res = check_terminal(name, meta)
            if res:
                return None, "child ended before answering (status %s)" % res.get("status")
        if time.time() >= deadline:
            return None, None
        time.sleep(0.5)


def cmd_turn(args):
    pos, o = parse_args(args, **options_of("turn"))
    if not pos or len(pos) > 2:
        die("usage: turn <name> [N | last] [--message ID] [--timeout S] [--json]")
    name = pos[0]
    meta = require_meta(name)
    spec = pos[1] if len(pos) > 1 else None
    max_chars = parse_int(o.get("max-chars", 20000), "--max-chars")
    budget = parse_int(o["timeout"], "--timeout") if "timeout" in o else 0
    rec, failure = wait_for_turn(name, meta, spec, o.get("message"), budget)
    if rec is None:
        if failure:
            die(failure, code=1)
        if spec is None and not o.get("message"):
            die("no turns recorded yet for '%s'" % name, code=3)
        what = "message %s" % o["message"] if o.get("message") else "turn %s" % spec
        out("%-9s : %s of %s" % ("pending", what, name))
        sys.stderr.write("  try: agent-session turn %s %s --timeout %d\n" % (
            name, "--message %s" % o["message"] if o.get("message") else spec, DEFAULT_BUDGET))
        return 3
    if o.get("json"):
        out(json.dumps(rec, indent=2))
    else:
        print_turn_view(name, rec, max_chars)
    return status_exit(rec.get("status"))


# --------------------------------------------------------------------------- send / stop

def cmd_send(args):
    pos, o = parse_args(args, lenient_prompt=True, **options_of("send"))
    if not pos:
        die("usage: send <name> [<text>] [--file F | -] [--no-wait] [--interrupt] [--timeout S] [--json]")
    name = pos[0]
    words = pos[1:]
    meta = require_meta(name)
    sdir = sdir_of(name)
    if not meta.get("steerable"):
        die("'%s' is a one-shot child; spawn with --steerable to hold a conversation" % name)
    res = read_json(os.path.join(sdir, "result.json"))
    if res:
        die("'%s' has finished (status %s)" % (name, res.get("status")), "agent-session result %s" % name)
    if not child_alive(name, meta):
        die("'%s' is no longer running; agent-session result %s for what happened" % (name, name))
    if os.path.exists(os.path.join(sdir, "stop")):
        die("stop was requested for '%s'; it will exit after its current turn and any queued messages" % name)
    if "file" in o and words:
        die("give the message once: text, --file F, or - (stdin)")
    if "file" in o:
        text = read_text(o["file"])
        if text is None:
            die("cannot read --file %s" % o["file"])
    elif words == ["-"]:
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
    else:
        text = " ".join(words)
    if not text.strip():
        die("nothing to send")
    budget = parse_int(o.get("timeout", DEFAULT_BUDGET), "--timeout")
    max_chars = parse_int(o.get("max-chars", 20000), "--max-chars")

    interrupted = None
    if o.get("interrupt"):
        prog = load_progress(name)
        # progress.json can lag a turn that just ended; a turn whose record
        # exists is finished and has nothing left to stop.
        if (prog.get("phase") == "turn" and isinstance(prog.get("turn"), int)
                and not os.path.exists(os.path.join(sdir, "turns", "%d.json" % prog["turn"]))):
            interrupted = prog["turn"]
            write_text(os.path.join(sdir, "interrupt"), "%d\n" % interrupted)
    message_id = uuid.uuid4().hex[:12]
    inbox = os.path.join(sdir, "inbox")
    tmp = os.path.join(inbox, ".tmp-%s" % message_id)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(inbox, "%020d-%s" % (time.time_ns(), message_id)))
    if o.get("no-wait"):
        if o.get("json"):
            out(json.dumps({"message_id": message_id, "interrupted_turn": interrupted}))
        else:
            kv("queued", message_id)
            if interrupted:
                kv("interrupt", "turn %d is being stopped" % interrupted)
        return 0
    if not o.get("json"):
        kv("queued", message_id)
        if interrupted:
            kv("interrupt", "turn %d is being stopped" % interrupted)
    rec, failure = wait_for_turn(name, meta, None, message_id, budget)
    if rec is None:
        if failure:
            die(failure, "agent-session result %s" % name, code=1)
        if o.get("json"):
            out(json.dumps({"message_id": message_id, "status": "pending"}))
        else:
            kv("pending", "reply to %s" % message_id)
        sys.stderr.write("  try: agent-session turn %s --message %s --timeout %d\n" % (name, message_id, DEFAULT_BUDGET))
        return 3
    if o.get("json"):
        out(json.dumps(rec, indent=2))
    else:
        print_turn_view(name, rec, max_chars)
    return status_exit(rec.get("status"))


def cmd_stop(args):
    pos, _o = parse_args(args, **options_of("stop"))
    if len(pos) != 1:
        die("usage: stop <name>")
    name = pos[0]
    meta = require_meta(name)
    sdir = sdir_of(name)
    if not meta.get("steerable"):
        die("'%s' is a one-shot child; it stops when its turn ends (agent-session kill %s to end it now)" % (name, name))
    res = read_json(os.path.join(sdir, "result.json"))
    if res:
        die("'%s' has already finished (status %s)" % (name, res.get("status")))
    touch(os.path.join(sdir, "stop"))
    out("asked %s to finish after its current turn and any queued messages" % name)
    return 0


# --------------------------------------------------------------------------- kill / reap

def kill_session(name, meta, grace, depth=0):
    """Shared by kill and reap --force. Returns (engine_line, runner_line, result,
    children): children is a list of (name, engine_line, runner_line) for the
    unfinished children this child had spawned, which are killed too. They run
    in their own tmux sessions, so killing the engine alone would leave them
    running unsandboxed and holding cap slots."""
    sdir = sdir_of(name)
    result_p = os.path.join(sdir, "result.json")
    if is_queued(name, meta) and not os.path.exists(result_p):
        with state_lock():
            if is_queued(name, meta) and not os.path.exists(result_p):
                res = finalize_queued(meta, "killed", "killed", "killed while queued", "killed")
                fire_hook(name)
                return "never started (was queued)", "no runner", res, []
    touch(os.path.join(sdir, "kill"))
    info = read_json(os.path.join(sdir, "engine.pid"))
    engine_line = "not running"
    if engine_alive(info):
        pid, pgid = info["pid"], info.get("pgid") or info["pid"]
        killpg(pgid, signal.SIGTERM)
        if wait_dead(pid, grace):
            engine_line = "pid %d terminated" % pid
        else:
            killpg(pgid, signal.SIGKILL)
            wait_dead(pid, 5)
            engine_line = "pid %d force-killed after %ds" % (pid, grace)
    prior = read_json(result_p)
    orphan = bool(prior) and prior.get("status") == "lost"
    res = None
    if not orphan:
        deadline = time.time() + 5
        while time.time() < deadline:
            res = read_json(result_p)
            if res:
                break
            time.sleep(0.2)
    if res and not orphan:
        runner_line = "wrote result"
    else:
        reason = ("orphaned engine killed by agent-session kill" if orphan
                  else "killed by agent-session kill; runner did not respond")
        # A runner that is still there but did not write its result in time
        # is hung or stopped: end it (TERM, then KILL) before writing for it.
        hung = backend_alive(name, meta)
        if hung:
            backend_kill(name, meta)
            reason = "killed by agent-session kill; the runner did not respond and was terminated"
        res = build_result(meta, load_turns(sdir), "killed", "killed", reason, failure_class="killed")
        write_json(result_p, res)
        runner_line = ("did not respond; terminated, result written by kill" if hung
                       else "was gone; result written by kill")
        fire_hook(name)
    end_session(name, meta)
    children = []
    if depth < MAX_DEPTH:
        for cm in descendants(meta):
            if not unfinished(cm):
                continue
            c_engine, c_runner, _r, more = kill_session(cm["name"], cm, grace, depth + 1)
            children.append((cm["name"], c_engine, c_runner))
            children.extend(more)
    return engine_line, runner_line, res, children


def cmd_kill(args):
    pos, o = parse_args(args, **options_of("kill"))
    if len(pos) != 1:
        die("usage: kill <name> [--grace S]")
    name = pos[0]
    meta = require_meta(name)
    grace = parse_int(o.get("grace", 10), "--grace")
    sdir = sdir_of(name)
    res = read_json(os.path.join(sdir, "result.json"))
    info = read_json(os.path.join(sdir, "engine.pid"))
    orphan = bool(res) and res.get("status") == "lost" and engine_alive(info)
    if res and not orphan:
        if tmux_alive(name, meta):
            end_session(name, meta)
            out("ended linger for %s" % name)
        else:
            out("%s already finished (%s)" % (name, res.get("status")))
        return 0
    engine_line, runner_line, _res, children = kill_session(name, meta, grace)
    kv("killed", name)
    kv("engine", engine_line)
    kv("runner", runner_line)
    for cname, c_engine, c_runner in children:
        kv("child", "%s killed too (engine %s; runner %s)" % (cname, c_engine, c_runner))
    return 0


def reap_one(name, force, dry_run, quiet=False, discard=None):
    with state_lock():
        return _reap_one_locked(name, force, dry_run, discard)


def _reap_one_locked(name, force, dry_run, discard=None):
    """Remove one child's state and worktree. Never loses work silently: a
    dirty worktree is skipped unless --force, and then snapshotted first; a
    branch with unmerged commits is kept unless --discard names its tip, and
    then snapshotted before `git branch -D`."""
    meta = load_meta(name)
    sdir = sdir_of(name)
    r = {"name": name, "action": None, "reason": None, "branch": None, "worktree": None, "snapshots": []}
    if not meta:
        r["action"] = "skipped"
        r["reason"] = "no meta.json"
        return r
    res = read_json(os.path.join(sdir, "result.json"))
    queued = is_queued(name, meta) and not res
    alive = child_alive(name, meta)
    info = read_json(os.path.join(sdir, "engine.pid"))
    live = (alive and not res) or (bool(res) and res.get("status") == "lost" and engine_alive(info))
    if queued:
        if not force:
            r["action"] = "skipped"
            r["reason"] = "queued, not started (agent-session kill %s, or reap --force)" % name
            return r
        if not dry_run:
            finalize_queued(meta, "killed", "killed", "killed while queued", "killed")
        r["reason"] = "was queued"
    elif live:
        if not force:
            r["action"] = "skipped"
            r["reason"] = "running (use --force to kill and reap)"
            return r
        if not dry_run:
            kill_session(name, meta, 10)
            r["reason"] = "killed first"
    elif alive:
        if not dry_run:
            end_session(name, meta)
    elif not res:
        if not dry_run:
            write_lost(name, meta)
        r["reason"] = "lost (no live session and no result)"
    wt = meta.get("worktree")
    if wt and isinstance(wt, dict):
        root, path, branch = wt.get("root"), wt.get("path"), wt.get("branch")
        if path and os.path.isdir(path):
            dirty = is_dirty(path)
            if dirty and not force:
                r["action"] = "skipped"
                r["reason"] = "worktree has uncommitted changes at %s (commit them, review with agent-session diff %s, " \
                              "or reap --force to snapshot and discard)" % (path, name)
                return r
            if dirty and not dry_run:
                ref, err = snapshot(root, path, name)
                if not ref:
                    r["action"] = "skipped"
                    r["reason"] = "could not snapshot the dirty worktree, so nothing was discarded: %s" % err
                    return r
                r["snapshots"].append(ref)
            if not dry_run:
                cp = git(root, "worktree", "remove", "--force", path) if force else git(root, "worktree", "remove", path)
                if cp.returncode != 0:
                    git(root, "worktree", "prune")
                    cp = git(root, "worktree", "remove", "--force", path) if force else git(root, "worktree", "remove", path)
                if cp.returncode != 0:
                    r["action"] = "skipped"
                    r["reason"] = "git worktree remove failed: %s" % cp.stderr.strip()
                    return r
            r["worktree"] = "removed"
        if dry_run and root and branch:
            # What the real reap would do with the branch (git branch -d semantics).
            tip = branch_tip(root, branch)
            if tip:
                if is_merged(root, tip):
                    r["branch"] = "deleted"
                elif discard and tip == discard:
                    r["branch"] = "discarded"
                else:
                    r["branch"] = "kept"
        if not dry_run and root and branch:
            git(root, "worktree", "prune")
            tip = branch_tip(root, branch)
            if tip:
                cp = git(root, "branch", "-d", branch)
                if cp.returncode == 0:
                    r["branch"] = "deleted"
                elif discard and tip == discard:
                    ref, err = snapshot_ref_of_commit(root, tip, name)
                    if not ref:
                        r["branch"] = "kept"
                        r["reason"] = "branch kept: could not snapshot its tip: %s" % err
                    else:
                        r["snapshots"].append(ref)
                        cp = git(root, "branch", "-D", branch)
                        r["branch"] = "discarded" if cp.returncode == 0 else "kept"
                else:
                    r["branch"] = "kept"
    judge_name = "%s-judge" % name
    jm = load_meta(judge_name) if len(judge_name) <= 40 else None
    if jm and jm.get("judge_of") == name and not dry_run:
        _reap_one_locked(judge_name, True, False)
    if not dry_run:
        shutil.rmtree(sdir, ignore_errors=True)
    r["action"] = "reaped"
    return r


def cmd_reap(args):
    pos, o = parse_args(args, **options_of("reap"))
    force, dry = bool(o.get("force")), bool(o.get("dry-run"))
    discard = o.get("discard")
    groups = []
    if pos:
        names = []
        for name in pos:
            if load_meta(name) is None and group_members(name) is not None:
                groups.append(name)
                names.extend(m for m in group_members(name) if load_meta(m) and m not in names)
                continue
            require_meta(name)
            if name not in names:
                names.append(name)
    else:
        me = owner_token()
        names = [m["name"] for m in all_metas() if (o.get("all") or m.get("owner") == me)
                 and not (m.get("judge_of") and load_meta(m["judge_of"]))]
    discard_sha = None
    if discard is not None:
        # Checked before anything is touched: the SHA must be the branch tip.
        if len(names) != 1:
            die("--discard names one branch tip, so it takes exactly one child")
        meta = load_meta(names[0]) or {}
        wt = meta.get("worktree") or {}
        if not wt.get("root") or not wt.get("branch"):
            die("'%s' has no worktree branch to discard" % names[0])
        tip = branch_tip(wt["root"], wt["branch"])
        if not tip:
            die("branch %s no longer exists" % wt["branch"])
        if len(discard) < 7 or not tip.startswith(discard.lower()):
            die("--discard %s is not the tip of %s; its tip is %s (pass that to give up its commits)"
                % (discard, wt["branch"], tip))
        discard_sha = tip
        force = True
    # A child's own children are owned by its child token, not by you; reap
    # them with their parent so they do not outlive it as orphans.
    for name in list(names):
        meta = load_meta(name)
        if meta:
            for cm in descendants(meta):
                if cm["name"] not in names:
                    names.append(cm["name"])
    results = []
    for name in names:
        meta = load_meta(name) or {}
        r = reap_one(name, force, dry, discard=discard_sha)
        results.append(r)
        if o.get("json"):
            continue
        verb = "would reap" if dry else "reaped"
        wt = meta.get("worktree") or {}
        if r["action"] == "reaped":
            extra = " (%s)" % r["reason"] if r["reason"] else ""
            out("%s %s%s" % (verb, name, extra))
            for ref in r["snapshots"]:
                kv("snapshot", "%s (restore: git -C %s worktree add <dir> %s)" % (
                    ref, shlex.quote(wt.get("root", "")), ref))
            if r["branch"] == "kept":
                # The child's state is gone after a reap, so `collect` no longer
                # knows it: the hints are plain git.
                root_q = shlex.quote(wt.get("root", ""))
                out("%s branch %s (has commits not merged into the current branch); inspect: git -C %s log %s; "
                    "merge it: git -C %s merge %s; or delete it: git -C %s branch -D %s"
                    % ("would keep" if dry else "kept", wt.get("branch"), root_q, wt.get("branch"), root_q,
                       wt.get("branch"), root_q, wt.get("branch")))
            elif r["branch"] == "discarded":
                out("discarded branch %s after the snapshot above" % wt.get("branch"))
        else:
            out("skipped %s: %s" % (name, r["reason"]))
    if not pos:
        for name in stale_reservations():
            if not dry:
                with state_lock():
                    if name in stale_reservations():
                        shutil.rmtree(sdir_of(name), ignore_errors=True)
            results.append({"name": name, "action": "reaped", "reason": "abandoned reservation",
                            "branch": None, "worktree": None, "snapshots": []})
            if not o.get("json"):
                out("%s %s (abandoned reservation, no meta.json)" % ("would reap" if dry else "reaped", name))
    if not dry:
        for g in groups:
            if all(load_meta(m) is None for m in group_members(g) or []):
                try:
                    os.unlink(os.path.join(STATE_ROOT, "groups", "%s.json" % g))
                except OSError:
                    pass
    reaped = sum(1 for r in results if r["action"] == "reaped")
    skipped = len(results) - reaped
    kept = sum(1 for r in results if r["branch"] == "kept")
    if o.get("json"):
        out(json.dumps({"dry_run": dry, "results": results,
                        "reaped": reaped, "skipped": skipped, "kept_branches": kept}, indent=2))
    else:
        out("%sreaped %d, skipped %d, kept-branches %d" % ("dry run: would have " if dry else "", reaped, skipped, kept))
    return 0


# --------------------------------------------------------------------------- engines / schema / doctor

def engine_report(name):
    eng = engine_for(name)
    binary = eng.binary()
    return {
        "installed": binary,
        "version": eng.version() if binary else None,
        "verified": eng.verified,
        "tiers": dict((t, eng.tier_model(t)) for t in TIERS),
        "capabilities": eng.capabilities(),
        "auth": eng.auth_status() if binary else None,
        "install": eng.install_hint,
        "missing_flags": eng.missing_flags() if binary and not eng.verified else None,
    }


def cmd_engines(args):
    _pos, o = parse_args(args, **options_of("engines"))
    report = dict((name, engine_report(name)) for name in ENGINES)
    installed = [n for n in ENGINES if report[n]["installed"]]
    if o.get("json"):
        out(json.dumps(report, indent=2))
        return 0 if installed else 1
    for name in ENGINES:
        r = report[name]
        if r["installed"]:
            head = "%s (%s)" % (r["installed"], r["version"] or "version unknown")
        else:
            head = "not installed (%s)" % (r["install"] or "see the engine's docs")
        flags = []
        if not r["verified"]:
            flags.append("experimental: documented format only, not tested against the real CLI")
        if r.get("missing_flags"):
            flags.append("--help lacks %s: spawns will likely fail" % ", ".join(r["missing_flags"]))
        if r["auth"]:
            flags.append("auth %s" % r["auth"])
        kv(name, head + ("   [%s]" % "; ".join(flags) if flags else ""))
        kv("  tiers", "  ".join("%s=%s" % (t, r["tiers"][t] or "engine default") for t in TIERS))
        caps = r["capabilities"]
        yes = [k for k in sorted(caps) if caps[k]]
        no = [k for k in sorted(caps) if not caps[k]]
        kv("  can", ", ".join(yes) or "-")
        kv("  cannot", ", ".join(no) or "-")
    if not installed:
        out("no engine installed; install one of %s" % ", ".join(ENGINES))
        return 1
    return 0


def cmd_schema(args):
    pos, _o = parse_args(args, **options_of("schema"))
    which = pos[0] if pos else "result"
    if len(pos) > 1 or which not in SCHEMAS:
        die("usage: schema [result|judge|event]")
    out(json.dumps(SCHEMAS[which], indent=2))
    return 0


def tmux_version_ok(text):
    m = re.search(r"(\d+)\.(\d+)", text or "")
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (3, 2)


def cmd_doctor(args):
    _pos, o = parse_args(args, **options_of("doctor"))
    only = o.get("engine")
    if only and only not in ENGINES:
        die("--engine must be %s" % engine_list())
    checks = []

    def add(name, status, detail, remedy=None):
        checks.append({"check": name, "status": status, "detail": detail, "remedy": remedy})

    v = sys.version_info
    add("python", "ok" if v >= (3, 8) else "FAIL", "%d.%d.%d at %s" % (v[0], v[1], v[2], sys.executable),
        None if v >= (3, 8) else "install python 3.8 or newer")

    forced = o.get("backend") or os.environ.get("AGENT_SESSIONS_BACKEND") or None
    if forced and forced not in BACKENDS:
        die("--backend must be tmux or process")
    tm = shutil.which("tmux")
    tmux_ok = False
    backend = forced or ("tmux" if tm else "process")
    if not tm:
        add("tmux", "FAIL" if backend == "tmux" else "warn",
            "not found on PATH; install: %s%s" % (TMUX_INSTALL, "" if backend == "tmux" else
                                                   " (the process backend works without it)"), TMUX_INSTALL)
    else:
        cp = run([tm, "-V"])
        ver = cp.stdout.strip() or cp.stderr.strip()
        tmux_ok = tmux_version_ok(ver)
        add("tmux", "ok" if tmux_ok else ("FAIL" if backend == "tmux" else "warn"), "%s at %s" % (ver, tm),
            None if tmux_ok else "tmux 3.2 or newer is needed for new-session -e; upgrade tmux")
    add("backend", "ok" if backend == "process" or tmux_ok else "FAIL",
        "%s%s" % (backend, " (tmux not found)" if backend == "process" and not tm else
                  " (forced)" if forced else ""),
        None if backend == "process" or tmux_ok else "install tmux 3.2+, or set AGENT_SESSIONS_BACKEND=process")

    # One working verified engine is enough to use the skill; a missing second
    # engine is a warning unless --engine asked for it. gemini is experimental
    # and never a FAIL unless asked for by name.
    found = {}
    for engine in ENGINES:
        if only and engine != only:
            add(engine, "warn", "skipped (--engine %s)" % only)
            continue
        found[engine] = engine_for(engine).binary()
    verified_found = [e for e, b in found.items() if b and engine_for(e).verified]
    for engine, binary in found.items():
        eng = engine_for(engine)
        if not binary:
            if only or (eng.verified and not verified_found):
                status = "FAIL"
            else:
                status = "warn"
            detail = "not found on PATH or in a login shell"
            if status == "warn":
                others = [e for e in ENGINES if e != engine and found.get(e)]
                if others:
                    detail += "; fine if you only use %s" % " or ".join(others)
                if not eng.verified:
                    detail += " (experimental engine)"
            add(engine, status, detail,
                "install the %s CLI%s (or pass --engine <other> to check only that one)" % (
                    engine, ": %s" % eng.install_hint if eng.install_hint else ""))
            continue
        version = eng.version()
        add(engine, "ok" if version else "FAIL", "%s (%s)%s" % (version or "no version output", binary,
                                                               "" if eng.verified else "  [experimental]"),
            None if version else "%s --version failed; check the installation" % engine)
        if version and not eng.verified:
            missing = eng.missing_flags()
            if missing:
                add("%s-flags" % engine, "FAIL" if only == engine else "warn",
                    "%s --help does not list %s, which the adapter passes" % (engine, ", ".join(missing)),
                    "upgrade the %s CLI, or use another engine" % engine)
            elif missing is None:
                add("%s-flags" % engine, "warn", "%s --help printed nothing; adapter flags unchecked" % engine)
            else:
                add("%s-flags" % engine, "ok", "--help lists %s" % ", ".join(eng.required_flags))

    g = shutil.which("git")
    add("git", "ok" if g else "warn", g or "not found (only needed for --worktree and reap of worktrees)",
        None if g else "install git to use --worktree")

    try:
        os.makedirs(STATE_ROOT, 0o700, exist_ok=True)
        with state_lock():
            pass
        add("state", "ok", "%s writable, lock ok" % STATE_ROOT)
    except OSError as exc:
        add("state", "FAIL", "%s: %s" % (STATE_ROOT, exc), "set AGENT_SESSIONS_STATE to a writable directory")

    lstart = ps_lstart(os.getpid())
    add("ps", "ok" if lstart else "FAIL", "lstart of own pid: %s" % lstart,
        None if lstart else "ps -o lstart= -p PID must work (BSD or procps ps)")

    if tm and tmux_ok and backend == "tmux":
        name = "doctor-%d" % os.getpid()
        cp = run(tmux_argv("new-session", "-d", "-s", session_of(name), "sleep 30"))
        if cp.returncode == 0:
            tmux("kill-session", "-t", target(name))
            sock = env_socket()
            add("tmux-session", "ok", "created and destroyed %s%s" % (session_of(name), " on socket %s" % sock if sock else ""))
        else:
            add("tmux-session", "FAIL", "cannot create a session: %s" % (cp.stderr.strip() or cp.stdout.strip()),
                "check the tmux socket directory permissions (TMUX_TMPDIR) or AGENT_SESSIONS_TMUX_SOCKET")
    elif backend == "tmux":
        add("tmux-session", "warn", "skipped (tmux missing or too old)")

    failed = any(c["status"] == "FAIL" for c in checks)
    if o.get("json"):
        out(json.dumps(dict((c["check"], {"status": c["status"], "detail": c["detail"], "remedy": c["remedy"]})
                            for c in checks), indent=2))
    else:
        for c in checks:
            line = "%-4s  %s: %s" % (c["status"], c["check"], c["detail"])
            if c["status"] == "FAIL" and c["remedy"] and c["remedy"] not in c["detail"]:
                line += "  (%s)" % c["remedy"]
            out(line)
        out("doctor: %s" % ("all required checks passed" if not failed else "fix the FAIL lines above"))
    return 1 if failed else 0



# --------------------------------------------------------------------------- resume / recover / budget / unpause

RESUMABLE = ("lost", "killed", "timeout", "stall", "crash", "rate_limit", "engine_error")
NOT_RESUMABLE = {
    "auth": "the engine is not logged in; fix the login, then respawn",
    "budget": "it stopped at its budget; raise the budget deliberately and respawn",
    "schema": "its output did not match the schema; fix the schema or the prompt and respawn",
    "verify": "its verification failed; read verify.N.out, then respawn with a clearer prompt",
    "dependency": "it never ran because an upstream failed; fix the upstream and spawn it again",
}


def session_known(name, meta):
    """The engine conversation id a resume can continue, or None."""
    sdir = sdir_of(name)
    for t in reversed(load_turns(sdir)):
        if t.get("session_id"):
            if meta["engine"] == "claude":
                return meta.get("conversation_id") or t["session_id"]
            return t["session_id"]
    for ev in reversed(read_events_safe(name)):
        if ev.get("type") == "init" and isinstance(ev.get("data"), dict):
            sid = ev["data"].get("session_id") or ev["data"].get("thread_id")
            if sid:
                return meta.get("conversation_id") if meta["engine"] == "claude" and meta.get("conversation_id") else sid
    if meta["engine"] != "claude" and meta.get("conversation_id"):
        return meta["conversation_id"]
    return None


def read_events_safe(name):
    from agent_sessions.common import read_jsonl
    return read_jsonl(events_path(name))


def resume_child(name, message=None):
    """Move result.json aside and start a new runner that continues the same
    engine conversation. Returns (state, detail): state is resumed | queued |
    refused | skipped."""
    meta = load_meta(name)
    sdir = sdir_of(name)
    res = check_terminal(name, meta)
    if res is None:
        return "refused", "still running or queued"
    res = normalize_result(res)
    cls = res.get("failure_class") or (res.get("status") if res.get("status") in ("lost", "killed", "timeout") else None)
    if res.get("status") == "ok":
        return "refused", "it finished ok; nothing to resume"
    if cls in NOT_RESUMABLE:
        return "refused", "%s (%s)" % (NOT_RESUMABLE[cls], cls)
    if cls not in RESUMABLE and res.get("status") not in ("lost", "killed", "timeout"):
        return "refused", "failure class %s cannot be resumed" % cls
    sid = session_known(name, meta)
    if not sid:
        return "skipped", "no session id; respawn"
    if not os.path.isdir(meta.get("workdir") or ""):
        return "skipped", "workdir %s no longer exists; respawn" % meta.get("workdir")
    from agent_sessions.launch import blockers
    # The previous runner may still be lingering (tmux) or finishing its
    # --on-finish hook and kick; it must be gone before a new runner takes the
    # state directory. Wait outside the lock: that runner's kick needs it.
    end_session(name, meta)
    deadline = time.time() + 10
    while child_alive(name, meta) and time.time() < deadline:
        time.sleep(0.2)
    with state_lock():
        meta = load_meta(name)
        if not os.path.exists(os.path.join(sdir, "result.json")):
            return "refused", "it was resumed meanwhile"
        if child_alive(name, meta):
            return "refused", "its previous runner is still finishing (an --on-finish hook can take 30 s); try again"
        # Decide before touching anything: an exhausted tree budget refuses the
        # resume and leaves the result as it was.
        block = blockers({"engine": meta["engine"], "after": [], "budget": meta.get("budget") or {}})
        if block and block[2]:
            return "refused", block[1]
        meta["conversation_id"] = sid
        os.makedirs(os.path.join(sdir, "attempts"), 0o700, exist_ok=True)
        k = 1
        while os.path.exists(os.path.join(sdir, "attempts", "%d.result.json" % k)):
            k += 1
        os.replace(os.path.join(sdir, "result.json"), os.path.join(sdir, "attempts", "%d.result.json" % k))
        at = now_iso()
        entry = {"at_iso": at, "from_status": res.get("status"), "message": message}
        meta.setdefault("resumes", []).append(entry)
        meta["resume_pending"] = {"message": message, "reason": res.get("reason") or res.get("status"),
                                  "at_iso": res.get("finished_iso") or at}
        for marker in ("kill", "stop", "interrupt", HOOK_DONE):
            try:
                os.unlink(os.path.join(sdir, marker))
            except OSError:
                pass
        passthrough = dict((k2, v) for k2, v in os.environ.items()
                           if passes_env(k2) and not k2.startswith(("AGENT_SESSION_", "AGENT_SESSIONS_")))
        write_private_json(os.path.join(sdir, "env.json"), passthrough)
        if block:
            meta["queue"] = {"reason": block[0], "detail": block[1], "since": time.time(), "since_iso": now_iso()}
            write_json(os.path.join(sdir, "meta.json"), meta)
            write_json(os.path.join(sdir, "progress.json"), {"phase": "queued", "attempt": 1, "events": 0,
                                                             "tool_calls": 0, "updated_iso": now_iso(),
                                                             "updated_epoch": time.time()})
            return "queued", "%s: %s" % (block[0], block[1])
        write_json(os.path.join(sdir, "meta.json"), meta)
        failure = start_child(name)
        if failure:
            meta = load_meta(name) or meta
            meta["resumes"] = (meta.get("resumes") or [])[:-1]
            meta["resume_pending"] = None
            write_json(os.path.join(sdir, "meta.json"), meta)
            os.replace(os.path.join(sdir, "attempts", "%d.result.json" % k), os.path.join(sdir, "result.json"))
            return "refused", "could not start: %s" % failure
    return "resumed", "session %s" % sid


def cmd_resume(args):
    pos, o = parse_args(args, **options_of("resume"))
    if len(pos) != 1:
        die("usage: resume <name> [--message TEXT] [--json]")
    name = pos[0]
    require_meta(name)
    with_lock_kick()
    state, detail = resume_child(name, o.get("message"))
    if o.get("json"):
        out(json.dumps({"name": name, "state": state, "detail": detail}, indent=2))
    elif state in ("resumed", "queued"):
        kv(state, "%s (%s)" % (name, detail))
        kv("next", "agent-session wait %s --timeout %d" % (name, DEFAULT_BUDGET))
    if state in ("refused", "skipped"):
        if not o.get("json"):
            die("cannot resume '%s': %s" % (name, detail))
        return 2
    return 0


def cmd_recover(args):
    _pos, o = parse_args(args, **options_of("recover"))
    me = owner_token()
    with_lock_kick()
    rows = []
    for meta in all_metas():
        name = meta["name"]
        if not o.get("all") and meta.get("owner") != me:
            continue
        sdir = sdir_of(name)
        res = read_json(os.path.join(sdir, "result.json"))
        if res is None:
            if is_queued(name, meta) or child_alive(name, meta):
                continue
            if o.get("dry-run"):
                rows.append({"name": name, "state": "would resume" if session_known(name, meta) else "skipped",
                             "detail": "unfinished with no live runner" if session_known(name, meta)
                             else "no session id; respawn"})
                continue
            res = check_terminal(name, meta)
            if res is None:
                continue
        if res.get("status") != "lost":
            continue
        if o.get("dry-run"):
            sid = session_known(name, meta)
            rows.append({"name": name, "state": "would resume" if sid else "skipped",
                         "detail": ("session %s" % sid) if sid else "no session id; respawn"})
            continue
        state, detail = resume_child(name)
        rows.append({"name": name, "state": state, "detail": detail})
    if o.get("json"):
        out(json.dumps({"dry_run": bool(o.get("dry-run")), "children": rows}, indent=2))
        return 0
    if not rows:
        out("(nothing to recover: no lost children%s)" % ("" if o.get("all") else " of yours"))
        return 0
    for r in rows:
        kv(r["state"], "%s (%s)" % (r["name"], r["detail"]))
    if not o.get("dry-run") and any(r["state"] in ("resumed", "queued") for r in rows):
        out("note : a resumed engine may not reload all of its earlier context; the continuation message tells it "
            "to re-read what it changed")
    return 0


def cmd_budget(args):
    _pos, o = parse_args(args, **options_of("budget"))
    me = owner_token()
    env_id = os.environ.get("AGENT_SESSIONS_LEDGER")
    ledgers = []
    try:
        files = sorted(os.listdir(ledgers_dir()))
    except OSError:
        files = []
    for f in files:
        if not f.endswith(".json"):
            continue
        ledger = load_ledger(f[:-5])
        if not ledger:
            continue
        if ledger.get("owner") == me or ledger.get("id") == env_id:
            rem_usd, rem_tok = ledger_remaining(ledger)
            ledger = dict(ledger, remaining_usd=rem_usd, remaining_tokens=rem_tok)
            ledgers.append(ledger)
    if o.get("json"):
        out(json.dumps({"ledgers": ledgers}, indent=2))
        return 0
    if not ledgers:
        out("(no tree budgets; create one with spawn --tree-budget-usd X or --tree-max-tokens N)")
        return 0
    for ledger in ledgers:
        limits = []
        if ledger.get("limit_usd") is not None:
            limits.append("$%s of $%s spent" % (fmt_usd(ledger.get("spent_usd") or 0), fmt_usd(ledger["limit_usd"])))
        if ledger.get("limit_tokens") is not None:
            limits.append("%d of %d tokens" % (ledger.get("spent_tokens") or 0, ledger["limit_tokens"]))
        kv("ledger", "%s (root %s): %s" % (ledger["id"], ledger.get("root"), "; ".join(limits)))
        for child, spent in sorted((ledger.get("children") or {}).items()):
            kv("  child", "%-20s $%s  %d tokens" % (child, fmt_usd(spent.get("usd") or 0), spent.get("tokens") or 0))
    out("note : the parent's own spend is not in any ledger")
    return 0


def cmd_unpause(args):
    pos, _o = parse_args(args, **options_of("unpause"))
    if len(pos) != 1 or pos[0] not in ENGINES:
        die("usage: unpause <engine>   (engine: %s)" % engine_list())
    path = os.path.join(pauses_dir(), "%s.json" % pos[0])
    existed = os.path.exists(path)
    with state_lock():
        try:
            os.unlink(path)
        except OSError:
            pass
        started = kick()
    out("%s %s" % ("unpaused" if existed else "was not paused:", pos[0]))
    for name in started or []:
        kv("started", name)
    return 0
