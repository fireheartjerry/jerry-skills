"""Benchmark history: `dev.py perf run` times the `benchmarks` entries of tools/dev.json with
the devkit.measure timing core and appends one line per benchmark to
`.devkit/perf/<name>.jsonl`; `dev.py perf history NAME` prints the series, and
`dev.py changepoint --perf NAME` looks for step changes in its medians.

A benchmark entry: {"name": "report-step", "command": "python3 -m app.report", "runs": 7,
"warmup": 1, "shell": false}. Only name and command are required. The command runs from the
repository root as an argv list (shlex), or through /bin/sh -c with "shell": true.
"""
import json
import os
import re
import shlex

from devkit.common import (Parser, UsageError, devkit_dir, fmt_time, git_info, load_config,
                           note, now_iso, print_json, repo_root, to_posix)
from devkit.measure import DEFAULT_RUNS, DEFAULT_WARMUP, summarize_arm, time_many

SCHEMA = "devkit-perf/1"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HELP = ("usage: dev.py perf {run,history} ...\n"
        "  run     [--name NAME] [--runs N] [--json]   time tools/dev.json benchmarks, append "
        "to .devkit/perf/<name>.jsonl\n"
        "  history NAME [--json]                       print the recorded series\n")


def perf_path(root, name):
    return os.path.join(root, ".devkit", "perf", name + ".jsonl")


def check_name(name, where):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise UsageError("%s: benchmark name %r must match [A-Za-z0-9][A-Za-z0-9._-]* (it is "
                         "used as a file name)" % (where, name))


def benchmarks(cfg):
    """Validated benchmark entries from the config."""
    out, seen = [], set()
    for i, b in enumerate(cfg.get("benchmarks") or []):
        where = "tools/dev.json benchmarks[%d]" % i
        if not isinstance(b, dict) or not isinstance(b.get("command"), str) \
                or not b["command"].strip():
            raise UsageError("%s needs {\"name\": ..., \"command\": \"...\"}" % where)
        check_name(b.get("name"), where)
        if b["name"] in seen:
            raise UsageError("%s: duplicate benchmark name %r" % (where, b["name"]))
        seen.add(b["name"])
        for key in ("runs", "warmup"):
            v = b.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0
                                  or (key == "runs" and v < 1)):
                raise UsageError("%s: %s must be a positive integer" % (where, key))
        out.append(b)
    return out


def load_series(root, name):
    """The recorded entries of one benchmark, oldest first; UsageError when there are none."""
    check_name(name, "perf")
    path = perf_path(root, name)
    if not os.path.isfile(path):
        raise UsageError("no perf history for %r (%s); run dev.py perf run --name %s" % (
            name, to_posix(os.path.relpath(path, root)), name))
    out = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                float(r["median"])
            except (ValueError, KeyError, TypeError):
                raise UsageError("%s line %d is not a perf record" % (
                    to_posix(os.path.relpath(path, root)), lineno))
            out.append(r)
    if not out:
        raise UsageError("perf history for %r is empty" % name)
    return out


def label_of(r):
    """Short commit, `-dirty` when the tree had changes, else the timestamp."""
    commit = r.get("commit")
    if commit:
        return commit[:7] + ("-dirty" if r.get("dirty") else "")
    return str(r.get("ts", "?"))


def changepoint_series(root, name):
    """[(label, median)] for dev.py changepoint --perf."""
    out = []
    for i, r in enumerate(load_series(root, name)):
        v = float(r["median"])
        if v != v or v in (float("inf"), float("-inf")):
            raise UsageError("perf series %s: run %d (%s) has median %r; a non-finite value "
                             "hides every change point" % (name, i, label_of(r), v))
        out.append((label_of(r), v))
    return out


def run_one(root, b, runs_override=None):
    runs = runs_override or b.get("runs") or DEFAULT_RUNS
    warmup = b.get("warmup", DEFAULT_WARMUP)
    shell = bool(b.get("shell"))
    try:
        cmd = b["command"] if shell else shlex.split(b["command"])
    except ValueError as e:
        raise UsageError("benchmark %s: cannot split %r: %s (check the quotes in tools/dev.json)"
                         % (b.get("name"), b["command"], e))
    cwd = os.getcwd()
    os.chdir(root)            # benchmark commands are written relative to the repository root
    try:
        walls, cpus = time_many(cmd, runs, warmup, shell=shell)
    finally:
        os.chdir(cwd)
    s = summarize_arm(walls, cpus)
    g = git_info(root)
    return {"schema": SCHEMA, "name": b["name"], "command": b["command"], "ts": now_iso(),
            "commit": g["rev"], "dirty": g["dirty"], "median": s["median"], "iqr": s["iqr"],
            "cpu_median": s["cpu_median"], "runs": runs, "wall": walls}


def append(root, record):
    devkit_dir(root, create=True)
    d = os.path.join(root, ".devkit", "perf")
    if not os.path.isdir(d):
        os.makedirs(d)
    path = perf_path(root, record["name"])
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return path


def cmd_perf(args):
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    sub = {"run": perf_run, "history": perf_history}.get(args[0])
    if sub is None:
        raise UsageError("unknown perf subcommand %r; one of run, history" % args[0])
    return sub(args[1:])


def _root(a):
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    return root


def perf_run(args):
    p = Parser(prog="dev.py perf run", description="Time the tools/dev.json benchmarks and "
               "append one record per benchmark to .devkit/perf/<name>.jsonl.")
    p.add_argument("--name", default=None, help="only this benchmark")
    p.add_argument("--runs", type=int, default=None,
                   help="timed runs (default: the entry's runs, else %d)" % DEFAULT_RUNS)
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if a.runs is not None and a.runs < 1:
        raise UsageError("--runs must be >= 1")
    root = _root(a)
    cfg = load_config(root)
    entries = benchmarks(cfg)
    if not entries:
        raise UsageError("no benchmarks in tools/dev.json; add {\"benchmarks\": [{\"name\": "
                         "\"step\", \"command\": \"python3 bench.py\"}]}")
    if a.name is not None:
        entries = [b for b in entries if b["name"] == a.name]
        if not entries:
            raise UsageError("no benchmark named %r in tools/dev.json" % a.name)
    results = []
    for b in entries:
        r = run_one(root, b, a.runs)
        r["file"] = to_posix(os.path.relpath(append(root, r), root))
        results.append(r)
    if a.json:
        print_json({"schema": "devkit-perf-run/1", "results": results})
        return 0
    for r in results:
        cpu = fmt_time(r["cpu_median"]) if r["cpu_median"] is not None else "n/a"
        print("%s: median %s (IQR %s, CPU %s, runs=%d) at %s; appended to %s" % (
            r["name"], fmt_time(r["median"]), fmt_time(r["iqr"]), cpu, r["runs"],
            label_of(r), r["file"]))
    return 0


def perf_history(args):
    p = Parser(prog="dev.py perf history", description="Print the recorded series of one "
               "benchmark; dev.py changepoint --perf NAME tests it for step changes.")
    p.add_argument("name", help="benchmark name from tools/dev.json benchmarks")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    root = _root(a)
    series = load_series(root, a.name)
    if a.json:
        print_json({"schema": "devkit-perf-history/1", "name": a.name, "points": len(series),
                    "series": series})
        return 0
    print("%s: %d point(s)" % (a.name, len(series)))
    print("  %-25s %-14s %-10s %-10s %-10s %s" % ("ts", "commit", "median", "iqr", "cpu", "runs"))
    for r in series:
        cpu = fmt_time(r["cpu_median"]) if r.get("cpu_median") is not None else "n/a"
        print("  %-25s %-14s %-10s %-10s %-10s %s" % (
            r.get("ts", "?"), label_of(r) if r.get("commit") else "-", fmt_time(r["median"]),
            fmt_time(r.get("iqr") or 0.0), cpu, r.get("runs", "?")))
    if len(series) >= 10:
        print("check for step changes: dev.py changepoint --perf %s" % a.name)
    else:
        print("changepoint needs at least 10 points (2 x --min-size 5); %d recorded" % len(series))
    return 0
