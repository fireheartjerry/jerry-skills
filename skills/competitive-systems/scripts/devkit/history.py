"""Test history and the commands that read it: slowest, flakes, smart and shards.

Store: `.devkit/runs/*.jsonl`, written by `dev.py test` (devkit.checks.write_records), one
record per test: {run_id, nodeid, target, outcome, duration_seconds, timestamp}. A target is
the unit a runner accepts (a test file, a Go package directory, a Rust crate). Per run, a
target failed when any of its records failed or errored, passed when one passed and none
failed, skipped otherwise; its duration in that run is the sum of its records' durations.
Records are deduplicated by (run_id, nodeid), the last one winning.

`smart` uses devkit.graph.knapsack (additive value, integer costs: exact); `shards` uses
devkit.graph.lpt and lower_bound, the same tested implementations as the cards.
"""
import datetime
import glob
import json
import math
import os
import re

from devkit import graph
from devkit.common import (Parser, UsageError, devkit_dir, fmt_time, note, now_iso,
                           print_json, repo_root, to_posix, warn)
from devkit.stats import median

RECENT_DAYS = 14                 # "failed recently" window for smart's optional set
UNKNOWN_SECONDS = 5.0            # duration estimate when nothing at all is recorded
DEFAULT_BUDGET = 480
QUARANTINE_FILE = "quarantine.json"
FULL_SUITE = "<full suite>"
FAILED = ("failed", "error")
_RUN_LANG = re.compile(r"^\d{8}T\d{6}Z-([A-Za-z]+)")


# ---------------------------------------------------------------- loading


def parse_ts(text):
    """ISO 8601 timestamp to an aware datetime (naive means UTC); None when unreadable."""
    if not isinstance(text, str) or not text:
        return None
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def runs_dir(root):
    return os.path.join(root, ".devkit", "runs")


def run_language(run_id):
    m = _RUN_LANG.match(run_id or "")
    return m.group(1) if m else None


def read_records(root, days=None, now=None):
    """(records, info). Records deduplicated by (run_id, nodeid), last one wins; with days,
    only records whose timestamp lies within the last `days` days. info counts files, bad
    lines and records dropped by the window."""
    now = now or utcnow()
    since = now - datetime.timedelta(days=days) if days is not None else None
    files = sorted(glob.glob(os.path.join(runs_dir(root), "*.jsonl")))
    info = {"files": len(files), "bad_lines": 0, "outside_window": 0}
    keyed = {}
    for path in files:
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            info["bad_lines"] += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                info["bad_lines"] += 1
                continue
            if not isinstance(r, dict) or not isinstance(r.get("target"), str) or not r["target"]:
                info["bad_lines"] += 1
                continue
            run_id = str(r.get("run_id") or stem)
            ts = parse_ts(r.get("timestamp"))
            if since is not None and (ts is None or ts < since):
                info["outside_window"] += 1
                continue
            dur = r.get("duration_seconds")
            dur = float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) \
                and dur >= 0 else None
            keyed[(run_id, str(r.get("nodeid") or r["target"]))] = {
                "run_id": run_id, "target": r["target"], "outcome": str(r.get("outcome", "")),
                "duration": dur, "ts": ts, "language": run_language(run_id) or run_language(stem)}
    return list(keyed.values()), info


def aggregate(records):
    """{target: stats}; see load_stats."""
    per_run = {}
    for r in records:
        key = (r["target"], r["run_id"])
        cur = per_run.setdefault(key, {"failed": False, "passed": False, "durations": [],
                                       "ts": None, "language": r["language"]})
        if r["outcome"] in FAILED:
            cur["failed"] = True
        elif r["outcome"] == "passed":
            cur["passed"] = True
        if r["duration"] is not None:
            cur["durations"].append(r["duration"])
        if r["ts"] is not None and (cur["ts"] is None or r["ts"] > cur["ts"]):
            cur["ts"] = r["ts"]
    stats = {}
    for (target, _), run in sorted(per_run.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        s = stats.setdefault(target, {"target": target, "language": None, "runs": 0,
                                      "passes": 0, "failures": 0, "skips": 0,
                                      "_ran": [], "_all": [], "last_seen": None,
                                      "last_failed": None, "_lang_ts": None})
        s["runs"] += 1
        outcome = "failed" if run["failed"] else ("passed" if run["passed"] else "skipped")
        s[{"failed": "failures", "passed": "passes", "skipped": "skips"}[outcome]] += 1
        if run["durations"]:
            d = sum(run["durations"])
            s["_all"].append(d)
            if outcome != "skipped":
                s["_ran"].append(d)
        ts = run["ts"]
        if ts is not None and (s["last_seen"] is None or ts > s["last_seen"]):
            s["last_seen"] = ts
        if outcome == "failed" and ts is not None and (
                s["last_failed"] is None or ts > s["last_failed"]):
            s["last_failed"] = ts
        if run["language"] and (s["language"] is None or (ts is not None and (
                s["_lang_ts"] is None or ts >= s["_lang_ts"]))):
            s["language"], s["_lang_ts"] = run["language"], ts
    known = []
    for s in stats.values():
        durs = s["_ran"] or s["_all"]
        s["total_seconds"] = round(sum(s["_all"]), 6)
        s["max_seconds"] = round(max(s["_all"]), 6) if s["_all"] else None
        s["p50_seconds"] = round(median(durs), 6) if durs else None
        executed = s["passes"] + s["failures"]
        s["failure_rate"] = round(s["failures"] / float(executed), 6) if executed else 0.0
        if s["p50_seconds"] is not None:
            known.append(s["p50_seconds"])
    default = default_seconds(known)
    for s in stats.values():
        s["estimated"] = s["p50_seconds"] is None
        if s["estimated"]:
            s["p50_seconds"] = default
        for k in ("_ran", "_all", "_lang_ts"):
            del s[k]
    return stats


def default_seconds(known_p50s):
    """The duration assumed for a target without a recorded duration: the p50 of the known
    targets' p50s, or UNKNOWN_SECONDS when nothing is known."""
    return round(median(known_p50s), 6) if known_p50s else UNKNOWN_SECONDS


def load_stats(root, days=None, now=None):
    """Per-target statistics: runs, passes, failures, skips, failure_rate (failures over
    passes + failures), total_seconds, max_seconds, p50_seconds (median of per-run durations,
    skip-only runs excluded when any run executed), last_seen, last_failed, language (from
    the run file name) and estimated (True when no duration was recorded and p50_seconds is
    the default). Returns (stats, info)."""
    records, info = read_records(root, days, now)
    info["records"] = len(records)
    info["runs"] = len(set(r["run_id"] for r in records))
    stats = aggregate(records)
    info["default_seconds"] = default_seconds(
        [s["p50_seconds"] for s in stats.values() if not s["estimated"]])
    if info["bad_lines"]:
        warn("%d unreadable line(s) in .devkit/runs skipped" % info["bad_lines"])
    return stats, info


def iso(ts):
    return ts.astimezone().isoformat(timespec="seconds") if ts is not None else None


def public(s):
    out = dict(s)
    out["last_seen"], out["last_failed"] = iso(s["last_seen"]), iso(s["last_failed"])
    return out


def no_history_hint():
    return "no test history in .devkit/runs; run dev.py test (it records by default when " \
           "the runner has a report format)"


def _setup(p, a):
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    return root


def _common(p, days=True):
    if days:
        p.add_argument("--days", type=float, default=None,
                       help="only records from the last N days (default: all)")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def _check_days(days):
    if days is not None and days <= 0:
        raise UsageError("--days must be positive")


# ---------------------------------------------------------------- slowest


def slowest(stats, limit):
    rows = sorted(stats.values(), key=lambda s: (-s["p50_seconds"], s["target"]))
    return rows[:limit] if limit else rows


def cmd_slowest(args):
    p = Parser(prog="dev.py slowest", description="Test targets by median (p50) recorded "
               "duration, with the maximum and the run count.")
    p.add_argument("--limit", type=int, default=20, help="rows to print (0: all; default 20)")
    _common(p)
    a = p.parse_args(args)
    _check_days(a.days)
    if a.limit < 0:
        raise UsageError("--limit must be >= 0")
    root = _setup(p, a)
    stats, info = load_stats(root, a.days)
    rows = slowest(stats, a.limit)
    if a.json:
        print_json({"schema": "devkit-slowest/1", "targets": len(stats), "runs": info["runs"],
                    "rows": [public(s) for s in rows]})
        return 0
    if not stats:
        print(no_history_hint())
        return 0
    print("slowest %d of %d target(s) by p50 over %d recorded run(s):" % (
        len(rows), len(stats), info["runs"]))
    print("  %-10s %-10s %-5s %s" % ("p50", "max", "runs", "target"))
    for s in rows:
        mx = fmt_time(s["max_seconds"]) if s["max_seconds"] is not None else "n/a"
        tag = "  (no duration recorded; estimated)" if s["estimated"] else ""
        print("  %-10s %-10s %-5d %s%s" % (fmt_time(s["p50_seconds"]), mx, s["runs"],
                                          s["target"], tag))
    return 0


# ---------------------------------------------------------------- flakes and quarantine


def flakes(stats, min_runs=3, max_rate=0.5):
    """Targets with both passes and failures over at least min_runs executed runs (passes +
    failures) and a failure rate at most max_rate; highest rate first, then by name."""
    rows = [s for s in stats.values()
            if s["passes"] > 0 and s["failures"] > 0
            and s["passes"] + s["failures"] >= min_runs and s["failure_rate"] <= max_rate]
    return sorted(rows, key=lambda s: (-s["failure_rate"], s["target"]))


def quarantine_path(root):
    return os.path.join(root, ".devkit", QUARANTINE_FILE)


def write_quarantine(root, rows, min_runs, max_rate):
    devkit_dir(root, create=True)
    data = {"schema": "devkit-quarantine/1", "generated_at": now_iso(),
            "policy": {"min_runs": min_runs, "max_rate": max_rate},
            "targets": [{"target": s["target"], "runs": s["runs"], "failures": s["failures"],
                         "failure_rate": s["failure_rate"]} for s in rows]}
    path = quarantine_path(root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    return path


def load_quarantine(root):
    """The quarantined target names (a set); empty when the file is absent or unreadable."""
    path = quarantine_path(root)
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return set(t["target"] if isinstance(t, dict) else str(t)
                   for t in data.get("targets", []))
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        warn(".devkit/%s is unreadable; no target treated as quarantined" % QUARANTINE_FILE)
        return set()


def cmd_flakes(args):
    p = Parser(prog="dev.py flakes", description="Targets that both passed and failed in the "
               "recorded runs, at a failure rate low enough to suggest flakiness.")
    p.add_argument("--min-runs", type=int, default=3, help="executed runs needed (default 3)")
    p.add_argument("--max-rate", type=float, default=0.5,
                   help="highest failure rate still called flaky (default 0.5)")
    p.add_argument("--write-quarantine", action="store_true",
                   help="write .devkit/quarantine.json; smart never picks those as optional")
    _common(p)
    a = p.parse_args(args)
    _check_days(a.days)
    if a.min_runs < 2:
        raise UsageError("--min-runs must be at least 2 (one pass and one failure)")
    if not 0 < a.max_rate <= 1:
        raise UsageError("--max-rate must be in (0, 1]")
    root = _setup(p, a)
    stats, info = load_stats(root, a.days)
    rows = flakes(stats, a.min_runs, a.max_rate)
    path = write_quarantine(root, rows, a.min_runs, a.max_rate) if a.write_quarantine else None
    caveat = ("a target that failed and was then fixed also has mixed outcomes; rerun it "
              "several times at one commit before calling it flaky")
    if a.json:
        print_json({"schema": "devkit-flakes/1", "policy": {"min_runs": a.min_runs,
                    "max_rate": a.max_rate}, "targets": len(stats), "runs": info["runs"],
                    "flakes": [public(s) for s in rows],
                    "quarantine": to_posix(os.path.relpath(path, root)) if path else None,
                    "note": caveat})
        return 0
    if not stats:
        print(no_history_hint())
    else:
        print("flakes: %d of %d target(s) passed and failed over >= %d executed run(s) with a "
              "failure rate <= %g%%" % (len(rows), len(stats), a.min_runs, a.max_rate * 100))
        for s in rows:
            print("  %-40s runs %-4d failed %d (%.0f%%)  last failed %s" % (
                s["target"], s["runs"], s["failures"], s["failure_rate"] * 100,
                iso(s["last_failed"]) or "n/a"))
        if rows:
            print("note: " + caveat)
    if path:
        print("wrote %s (%d target(s)); smart never selects them as optional, and still runs "
              "them when they are required" % (to_posix(os.path.relpath(path, root)), len(rows)))
    return 0


# ---------------------------------------------------------------- smart


def cost_of(seconds):
    """Integer knapsack cost: seconds rounded up, at least 1."""
    return max(1, int(math.ceil(seconds - 1e-9)))


def _exists(root, target):
    """False for a recorded target that cannot be passed to a runner any more: a whole-suite
    record (a command line), `unknown`, or a path that no longer exists."""
    if target == "unknown" or any(c.isspace() for c in target):
        return False
    if "/" in target or os.path.splitext(target)[1]:
        return os.path.exists(os.path.join(root, target))
    return True        # a crate or package name


def plan_smart(required, stats, quarantined, budget, now=None, default=UNKNOWN_SECONDS,
               exists=lambda t: True, full=None):
    """The budgeted selection.

    required: [(target, language, reason)] (reason affected | always_run); stats from
    load_stats; quarantined: a set of names; budget in seconds. full: the affected reason
    when it selected the full suite (required is then that marker alone).
    Returns a dict: mode (selected | over_budget | full), required, optional, not_selected,
    quarantined, required_seconds, optional_seconds, estimated_seconds, budget_left, message.
    """
    now = now or utcnow()
    recent = now - datetime.timedelta(days=RECENT_DAYS)
    budget = int(math.floor(budget))

    def secs(target):
        s = stats.get(target)
        return (s["p50_seconds"], s["estimated"]) if s else (default, True)

    out = {"budget_seconds": budget, "required": [], "optional": [], "not_selected": [],
           "quarantined": sorted(quarantined), "message": None}
    if full is not None:
        total = sum(cost_of(s["p50_seconds"]) for s in stats.values()) if stats else None
        out["required"] = [{"target": FULL_SUITE, "language": None,
                            "reason": "affected: full (%s)" % full, "seconds": total,
                            "estimated": not stats}]
        out.update(mode="full", required_seconds=total, optional_seconds=0,
                   estimated_seconds=total, budget_left=None if total is None else budget - total)
        msg = "affected selected the full suite (%s); run dev.py test" % full
        if total is not None and total > budget:
            msg += "; the recorded full-suite estimate exceeds the budget by %d s" % (total - budget)
        out["message"] = msg
        return out
    seen = set()
    for target, lang, reason in required:
        if target in seen:
            continue
        seen.add(target)
        p50, est = secs(target)
        out["required"].append({"target": target, "language": lang, "reason": reason,
                                "seconds": cost_of(p50), "estimated": est})
    req = sum(r["seconds"] for r in out["required"])
    left = budget - req
    out.update(required_seconds=req, budget_left=left)
    if left < 0:
        out.update(mode="over_budget", optional_seconds=0, estimated_seconds=req,
                   message="required set exceeds budget by %d s" % (-left))
        return out
    candidates = []
    for name in sorted(stats):
        s = stats[name]
        if name in seen or name in quarantined or not exists(name):
            continue
        if s["last_failed"] is None or s["last_failed"] < recent:
            continue
        value = 1.0 + s["failure_rate"] + (1.0 if s["last_failed"] >= recent else 0.0)
        candidates.append({"target": name, "language": s["language"], "seconds":
                           cost_of(s["p50_seconds"]), "estimated": s["estimated"],
                           "value": round(value, 6), "failure_rate": s["failure_rate"],
                           "last_failed": iso(s["last_failed"])})
    total = sum(c["seconds"] for c in candidates)
    if total <= left:
        chosen = set(c["target"] for c in candidates)
    else:
        names, _ = graph.knapsack([(c["target"], c["value"], c["seconds"]) for c in candidates],
                                  left)
        chosen = set(names)
    for c in candidates:
        if c["target"] in chosen:
            c["reason"] = "knapsack"
            out["optional"].append(c)
        else:
            c["reason"] = "not selected: budget"
            out["not_selected"].append(c)
    opt = sum(c["seconds"] for c in out["optional"])
    out.update(mode="selected", optional_seconds=opt, estimated_seconds=req + opt,
               candidates=len(candidates))
    return out


def required_targets(root, files, base, cfg, info):
    """(required [(target, language, reason)], affected result, graphs)."""
    from devkit import affected as aff
    changed = aff.gather_changed(root, files, base)
    langs = list(info["languages"])
    graphs = dict((lang, aff.build_graph(root, lang, info, cfg)) for lang in langs)
    graphs = dict((k, v) for k, v in graphs.items() if v is not None)
    res = aff.affected(root, changed, cfg, info, graphs)
    req = []
    if res["mode"] == "selected":
        for lang, targets in res["targets"].items():
            req.extend((t, lang, "affected") for t in targets)
    for t in cfg.get("always_run") or []:
        if not isinstance(t, str) or not t.strip():
            raise UsageError("tools/dev.json: always_run must be a list of target names")
        lang, name = aff._target_language(t, langs, graphs)
        req.append((name, lang, "always_run"))
    return req, res


def run_lines(selection, stats):
    """{language: [targets]} for the targets to run, by language, sorted."""
    out = {}
    for item in selection["required"] + selection["optional"]:
        if item["target"] == FULL_SUITE:
            continue
        lang = item.get("language") or (stats.get(item["target"]) or {}).get("language") \
            or "unknown"
        out.setdefault(lang, []).append(item["target"])
    return dict((k, sorted(set(v))) for k, v in sorted(out.items()))


def runner_args(lang, targets):
    if lang == "rust":
        return [tok for t in targets for tok in ("-p", t)]
    return list(targets)


def cmd_smart(args):
    from devkit.common import load_config
    from devkit.detect import detect
    p = Parser(prog="dev.py smart", description="Budgeted test selection: the affected targets "
               "and always_run are required; recently failed targets fill the remaining budget "
               "by 0/1 knapsack on recorded p50 durations.")
    p.add_argument("files", nargs="*", help="changed files (default: git diff against HEAD plus "
                                            "untracked files)")
    p.add_argument("--base", default=None, help="diff REF...HEAD plus the working tree")
    p.add_argument("--budget", type=float, default=None,
                   help="seconds (default: budget_seconds in tools/dev.json, else 480)")
    _common(p, days=False)
    a = p.parse_args(args)
    if a.files and a.base:
        raise UsageError("pass FILES or --base, not both")
    if a.budget is not None and a.budget < 0:
        raise UsageError("--budget must be >= 0 seconds")
    root = _setup(p, a)
    cfg = load_config(root)
    info = detect(root, cfg)
    budget = a.budget if a.budget is not None else cfg.get("budget_seconds", DEFAULT_BUDGET)
    source = "--budget" if a.budget is not None else (
        "tools/dev.json" if cfg.get("path") and "budget_seconds" in _config_keys(root)
        else "default")
    req, res = required_targets(root, a.files, a.base, cfg, info)
    stats, hinfo = load_stats(root)
    quarantined = load_quarantine(root)
    sel = plan_smart(req, stats, quarantined, budget, default=hinfo["default_seconds"],
                     exists=lambda t: _exists(root, t),
                     full=res["reason"] if res["mode"] == "full" else None)
    run = run_lines(sel, stats)
    estimated = sum(1 for r in sel["required"] + sel["optional"] if r.get("estimated"))
    est_note = "seconds are recorded p50 durations rounded up per target"
    if estimated:
        est_note += "; %d target(s) without history estimated at %s each" % (
            estimated, fmt_time(hinfo["default_seconds"]))
    if not stats:
        est_note += "; " + no_history_hint()
    sel = dict([("schema", "devkit-smart/1")] + list(sel.items()))
    sel.update(budget_source=source, affected={"mode": res["mode"], "reason": res["reason"]},
               run=run, args=dict((k, runner_args(k, v)) for k, v in run.items()),
               history={"runs": hinfo["runs"], "targets": len(stats),
                        "default_seconds": hinfo["default_seconds"]},
               estimate_note=est_note)
    if a.json:
        print_json(sel)
        return 0
    print_smart(sel)
    return 0


def _config_keys(root):
    try:
        with open(os.path.join(root, "tools", "dev.json"), encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, dict) else set()
    except (OSError, ValueError):
        return set()


def _secs(v):
    return "%d s" % v if v is not None else "unknown"


def print_smart(sel):
    est = sel["estimated_seconds"]
    print("smart: %s; estimated %s against a budget of %d s (%s)" % (
        sel["mode"], _secs(est), sel["budget_seconds"], sel["budget_source"]))
    if sel["message"]:
        print(sel["message"])
    print("required (%s s; affected: %s, %s):" % (
        sel["required_seconds"] if sel["required_seconds"] is not None else "unknown",
        sel["affected"]["mode"], sel["affected"]["reason"] or "-"))
    if not sel["required"]:
        print("  (none)")
    for r in sel["required"]:
        print("  %-8s %-40s %s%s" % (_secs(r["seconds"]), r["target"], r["reason"],
                                     " (no history; estimated)" if r["estimated"] else ""))
    if sel["mode"] == "selected":
        print("optional (knapsack over %d recently failed candidate(s), %d s left):" % (
            sel.get("candidates", 0), sel["budget_left"]))
        if not sel["optional"]:
            print("  (none)")
        for c in sel["optional"]:
            print("  %-8s %-40s knapsack: value %.2f (failure rate %.0f%%, last failed %s)" % (
                _secs(c["seconds"]), c["target"], c["value"], c["failure_rate"] * 100,
                c["last_failed"]))
        for c in sel["not_selected"]:
            print("  %-8s %-40s not selected: over the remaining budget" % (
                _secs(c["seconds"]), c["target"]))
    if sel["quarantined"]:
        print("quarantined (never optional; still run when required): %s" % " ".join(
            sel["quarantined"]))
    if sel["run"]:
        print("run:")
        for lang, targets in sel["run"].items():
            print("  %s: %s" % (lang, " ".join(runner_args(lang, targets))))
    print("note: " + sel["estimate_note"])


# ---------------------------------------------------------------- shards


def plan_shards(durations, n):
    """durations: {target: seconds}. LPT with ties by name; returns a dict with lb, the
    binding term, makespan, ratio and the shard list."""
    jobs = [(float(d), t) for t, d in sorted(durations.items())]
    plan = graph.lpt(jobs, n)
    total = sum(d for d, _ in jobs)
    longest = min(jobs, key=lambda j: (-j[0], j[1])) if jobs else (0.0, None)
    lb = graph.lower_bound(jobs, n)
    makespan = max(load for load, _ in plan) if plan else 0.0
    return {"shards": n, "targets": len(jobs), "total_seconds": round(total, 6),
            "longest": {"target": longest[1], "seconds": round(longest[0], 6)},
            "lb": round(lb, 6), "lb_binding": "longest target" if longest[0] >= total / n
            else "total/shards", "makespan": round(makespan, 6),
            "makespan_over_lb": round(makespan / lb, 6) if lb > 0 else 1.0,
            "lpt_guarantee": round(4.0 / 3.0 - 1.0 / (3.0 * n), 6),
            "plan": [{"shard": i + 1, "estimated_seconds": round(load, 6), "targets": names}
                     for i, (load, names) in enumerate(plan)]}


def cmd_shards(args):
    p = Parser(prog="dev.py shards", description="Split test targets into N shards by LPT over "
               "recorded p50 durations; prints the lower bound and makespan/LB.")
    p.add_argument("n", type=int, help="number of shards (runners)")
    p.add_argument("targets", nargs="*", help="targets (default: every recorded target)")
    p.add_argument("--language", default=None, help="only recorded targets of this language")
    p.add_argument("--matrix", action="store_true",
                   help="print a GitHub Actions matrix: {\"include\": [...]} on one line")
    _common(p)
    a = p.parse_args(args)
    _check_days(a.days)
    if a.n < 1:
        raise UsageError("N must be at least 1")
    root = _setup(p, a)
    stats, info = load_stats(root, a.days)
    default = info["default_seconds"]
    if a.targets:
        names = sorted(set(a.targets))
    else:
        names = sorted(t for t, s in stats.items() if _exists(root, t) and (
            a.language is None or s["language"] == a.language))
    if not names:
        raise UsageError("no targets: pass TARGETS or record history with dev.py test")
    unknown = [t for t in names if t not in stats or stats[t]["estimated"]]
    durations = dict((t, stats[t]["p50_seconds"] if t in stats else default) for t in names)
    res = dict([("schema", "devkit-shards/1")] + list(plan_shards(durations, a.n).items()))
    res.update(estimated=unknown, default_seconds=default)
    langs = sorted(set((stats.get(t) or {}).get("language") or "unknown" for t in names))
    if len(langs) > 1:
        warn("targets span several languages (%s); pass --language to shard one runner's "
             "targets" % ", ".join(langs))
    if unknown:
        note("%d target(s) without a recorded duration estimated at %s" % (
            len(unknown), fmt_time(default)))
    if a.matrix:
        print_json({"include": [{"shard": s["shard"], "estimated_seconds": s["estimated_seconds"],
                                 "targets": " ".join(s["targets"]),
                                 "has_targets": bool(s["targets"])} for s in res["plan"]]})
        return 0
    if a.json:
        print_json(res)
        return 0
    print("LB = max(sum/N, longest) = max(%s / %d, %s) = %s (%s binds)" % (
        fmt_time(res["total_seconds"]), a.n, fmt_time(res["longest"]["seconds"]),
        fmt_time(res["lb"]), res["lb_binding"]))
    for s in res["plan"]:
        print("shard %d: %-10s %s" % (s["shard"], fmt_time(s["estimated_seconds"]),
                                      " ".join(s["targets"]) or "(empty)"))
    print("makespan %s; makespan/LB %.3f (LPT guarantees at most %.3f x optimum)" % (
        fmt_time(res["makespan"]), res["makespan_over_lb"], res["lpt_guarantee"]))
    if res["lb_binding"] == "longest target":
        print("the longest target binds: more shards cannot help until %s is split" %
              res["longest"]["target"])
    return 0
