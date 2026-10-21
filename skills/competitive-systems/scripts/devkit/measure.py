"""Timing engine and the evidence commands bench, aa, scale and changepoint. The before/after
analysis is devkit.compare; the phase-1 bench.py subcommands are devkit.legacy.

Timing: wall via time.perf_counter around the child's life; the child's user+sys CPU and
max RSS from the rusage that os.wait4 returns for that child (POSIX). Without wait4 the
RUSAGE_CHILDREN deltas give CPU and RSS is null; without `resource` both are null.
"""
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import sys
import tempfile
import time

try:
    import resource
except ImportError:  # not POSIX
    resource = None

from devkit import stats
from devkit.common import (CommandFailed, GroupTimer, Parser, StartError, UsageError,
                           devkit_dir, kill_group, start_group, wait_unreaped,
                           environment, fmt_bytes, fmt_time, git_info, note, print_json,
                           repo_root, sha256_file, warn)
from devkit.stats import (BASELINE_RATIO, SCALE_FLOOR, classify_exponent,  # noqa: F401
                          fit_exponent, median, quartiles, scale_exponent)

DEFAULT_RUNS = 7
DEFAULT_WARMUP = 1
STARTUP_FLOOR = 0.020   # seconds; medians below this are mostly process startup
WAIT_RATIO = 1.5        # wall > WAIT_RATIO x CPU means the process is waiting
WAIT_FLOOR = 0.100      # seconds; under this the wall/CPU ratio is startup noise
PROTOCOL_RUNS = 10      # the protocol asks for at least this many runs per arm

# ---------------------------------------------------------------- phase-1 pure functions


def summarize(xs):
    """Summary dict of wall times: runs, median, q1, q3, iqr, min, max."""
    q1, q3 = quartiles(xs)
    return {"runs": len(xs), "median": median(xs), "q1": q1, "q3": q3, "iqr": q3 - q1,
            "min": float(min(xs)), "max": float(max(xs))}


def with_cpu(s, cpus):
    """Add cpu_median, wall_over_cpu and waiting (wall >= WAIT_FLOOR and > WAIT_RATIO x CPU)."""
    if not cpus or any(c is None for c in cpus):
        s["cpu_median"] = None
        s["wall_over_cpu"] = None
        s["waiting"] = False
        return s
    cm = median(cpus)
    s["cpu_median"] = cm
    s["wall_over_cpu"] = round(s["median"] / cm, 2) if cm > 0 else None
    s["waiting"] = bool(s["median"] >= WAIT_FLOOR and s["median"] > WAIT_RATIO * cm)
    return s


def summarize_arm(walls, cpus):
    return with_cpu(summarize(walls), cpus)


def verdict(before, after):
    """Phase-1 IQR noise rule: (verdict, speedup, reason); the hand rule in measurement.md."""
    widest = 2 * max(before["iqr"], after["iqr"])
    if (after["median"] < before["median"] and after["q3"] < before["q1"]
            and before["median"] - after["median"] > widest):
        return "faster", round(before["median"] / after["median"], 2), ""
    if (before["median"] < after["median"] and before["q3"] < after["q1"]
            and after["median"] - before["median"] > widest):
        return "slower", round(before["median"] / after["median"], 2), ""
    if after["median"] == before["median"]:
        reason = "medians equal"
    elif not (after["q3"] < before["q1"] or before["q3"] < after["q1"]):
        reason = "IQRs overlap"
    else:
        reason = "gap %s is under twice the larger IQR (%s)" % (
            fmt_time(abs(before["median"] - after["median"])), fmt_time(widest))
    return "inconclusive", None, reason


def gate1_line(s):
    """The 'CPU x of y wall' fragment of the report's Gate 1 line, with the waiting verdict."""
    if s.get("cpu_median") is None:
        return "CPU n/a of %s wall (CPU time unavailable on this platform)" % fmt_time(s["median"])
    if s["median"] < WAIT_FLOOR:
        tail = "ratio unreliable under %s" % fmt_time(WAIT_FLOOR)
    elif s["waiting"]:
        tail = "appears to be waiting (wall > %.1fx CPU)" % WAIT_RATIO
    else:
        tail = "CPU-bound"
    return "CPU %s of %s wall -> %s" % (fmt_time(s["cpu_median"]), fmt_time(s["median"]), tail)


def before_after_line(before, after):
    return "Before: median %s (IQR %s, runs=%d); After: median %s (IQR %s, runs=%d)." % (
        fmt_time(before["median"]), fmt_time(before["iqr"]), before["runs"],
        fmt_time(after["median"]), fmt_time(after["iqr"]), after["runs"])


def report_lines(before, after, verdict_name, speedup):
    """Phase-1 paste-ready lines for the IQR rule."""
    if verdict_name == "faster":
        second = "Verdict: %.2fx faster" % speedup
    elif verdict_name == "slower":
        second = "Verdict: %.2fx slower" % (1.0 / speedup)
    else:
        second = "Verdict: no measurable change"
    return [before_after_line(before, after), second]


def compare_warnings(before, after):
    """Phase-1 protocol warnings: run counts by tier, startup-dominated medians."""
    out = []
    runs = min(before["runs"], after["runs"])
    hi, lo = max(before["median"], after["median"]), min(before["median"], after["median"])
    ratio = hi / lo if lo > 0 else float("inf")
    if runs < 7:
        out.append("only %d runs per arm; the protocol needs at least 7" % runs)
    if ratio < 1.2 and runs < 30:
        out.append("medians differ by under 1.2x; resolving that needs 30 runs per arm (have %d)"
                   % runs)
    elif ratio < 2.0 and runs < 15:
        out.append("medians differ by under 2x; resolving that needs 15 runs per arm (have %d)"
                   % runs)
    if lo < STARTUP_FLOOR:
        out.append("a median is under %s: dominated by process startup; increase n or loop "
                   "inside the process" % fmt_time(STARTUP_FLOOR))
    return out


def default_baseline(argv):
    """A startup-only command matching argv[0], or None when none is known."""
    exe = os.path.basename(argv[0])
    if re.match(r"python[0-9.]*(w)?$", exe) or exe in ("pypy", "pypy3"):
        return [argv[0], "-c", "pass"]
    if exe in ("node", "nodejs", "ruby", "perl"):
        return [argv[0], "-e", "0"]
    return None


# ---------------------------------------------------------------- timing core


def cpu_accounting():
    if resource is None:
        return "unavailable"
    return "wait4" if hasattr(os, "wait4") else "rusage-children"


def _exit_code(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return -os.WTERMSIG(status) if os.WIFSIGNALED(status) else 1


def _children_cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def time_once(cmd, shell=False, capture_stdout=False, timeout=None):
    """Run cmd once; return {wall, cpu, rss, exit, stdout_sha256}.

    Raises StartError when it cannot start and CommandFailed (stderr tail in details) when
    it exits non-zero or is killed by the timeout.
    """
    shown = cmd if shell else " ".join(cmd)
    mode = cpu_accounting()
    out = tempfile.TemporaryFile() if capture_stdout else open(os.devnull, "wb")
    with out, tempfile.TemporaryFile() as err:
        cpu0 = _children_cpu() if mode == "rusage-children" else None
        start = time.perf_counter()
        try:
            proc = start_group(cmd, shell=shell, stdout=out, stderr=err,
                               stdin=subprocess.DEVNULL)
        except OSError as e:
            raise StartError("cannot run %s: %s" % (shown, e.strerror or e))
        timer = GroupTimer(proc, timeout)
        cpu = rss = None
        try:
            waited = wait_unreaped(proc)
            if waited:
                wall = time.perf_counter() - start
                timer.exited()
            if mode == "wait4":
                _, status, ru = os.wait4(proc.pid, 0)
                proc.returncode = _exit_code(status)
                cpu = ru.ru_utime + ru.ru_stime
                rss = ru.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
            else:
                proc.wait()
                if mode == "rusage-children":
                    cpu = _children_cpu() - cpu0
            if not waited:
                wall = time.perf_counter() - start
                timer.exited()
        except BaseException:           # Ctrl-C reaches only this process: take the group too
            if proc.returncode is None:
                kill_group(proc)
            raise
        code = proc.returncode
        if code != 0:
            err.seek(0)
            tail = err.read().decode("utf-8", "replace").splitlines()[-20:]
            what = ("timed out after %g s" % timeout) if timer.fired else (
                "failed with exit %d" % code)
            raise CommandFailed("measured command %s: %s" % (what, shown), details=tail)
        digest = None
        if capture_stdout:
            out.seek(0)
            digest = hashlib.sha256(out.read()).hexdigest()
    return {"wall": wall, "cpu": cpu, "rss": rss, "exit": code, "stdout_sha256": digest}


def time_many(cmd, runs, warmup, shell=False, **kw):
    """Warm up, then time `runs` runs. Returns (walls, cpus) as phase 1 did."""
    for _ in range(warmup):
        time_once(cmd, shell, **kw)
    results = [time_once(cmd, shell, **kw) for _ in range(runs)]
    return [r["wall"] for r in results], [r["cpu"] for r in results]


def run_order(names, runs, order, seed):
    """interleave: A B A B ...; random: each round shuffled with the seed; blocked: AAA BBB."""
    if order == "blocked":
        return [n for n in names for _ in range(runs)]
    rng = random.Random(seed)
    seq = []
    for _ in range(runs):
        rnd = list(names)
        if order == "random":
            rng.shuffle(rnd)
        seq.extend(rnd)
    return seq


def run_arms(arms, runs, warmup, order="interleave", seed=0, shell=False,
             check_output=False, timeout=None):
    """arms: list of (name, cmd). Returns (bench dict without meta, warnings)."""
    data = {}
    for name, cmd in arms:
        data[name] = {"command": cmd, "shell": shell, "wall": [], "cpu": [], "rss": [],
                      "exit": [], "stdout_sha256": []}
        for _ in range(warmup):
            time_once(cmd, shell, check_output, timeout)
    sequence = run_order([n for n, _ in arms], runs, order, seed)
    cmds = dict(arms)
    for name in sequence:
        r = time_once(cmds[name], shell, check_output, timeout)
        for key in ("wall", "cpu", "rss", "exit", "stdout_sha256"):
            data[name][key].append(r[key])
    return {"arms": data, "order": sequence}, output_warnings(data) if check_output else []


def output_warnings(arms):
    out = []
    sets = {}
    for name, arm in arms.items():
        hashes = set(h for h in arm["stdout_sha256"] if h)
        sets[name] = hashes
        if len(hashes) > 1:
            out.append("arm %s: stdout differs between its own runs (%d distinct outputs): "
                       "nondeterministic output" % (name, len(hashes)))
    names = list(sets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if sets[a] and sets[b] and not (sets[a] & sets[b]):
                out.append("outputs differ between arms %s and %s: check the oracle before "
                           "trusting the timing" % (a, b))
    return out


def metadata(inputs, seed, repo=None):
    meta = environment()
    root, _ = repo_root(repo)
    meta.update(git_info(root))
    meta["seed"] = seed
    meta["cpu_accounting"] = cpu_accounting()
    meta["inputs"] = {}
    for path in inputs or []:
        try:
            meta["inputs"][path.replace(os.sep, "/")] = sha256_file(path)
        except OSError as e:
            raise UsageError("cannot hash --input %s: %s" % (path, e.strerror or e))
    return meta


def parse_arm(text):
    if "=" not in text:
        raise UsageError("--arm needs NAME=COMMAND, got %r" % text)
    name, cmd = text.split("=", 1)
    name, cmd = name.strip(), cmd.strip()
    if not name or not cmd:
        raise UsageError("--arm needs a non-empty NAME and COMMAND, got %r" % text)
    return name, cmd


def arm_command(text, shell):
    if shell:
        return text
    argv = shlex.split(text)
    if not argv:
        raise UsageError("empty command in %r" % text)
    return argv


def split_dashes(raw):
    if "--" in raw:
        i = raw.index("--")
        return raw[:i], raw[i + 1:]
    return raw, []


def _parser(name, description):
    return Parser(prog="dev.py " + name, description=description)


def _check_runs(runs, warmup):
    if runs < 1 or warmup < 0:
        raise UsageError("--runs must be >= 1 and --warmup >= 0")


# ---------------------------------------------------------------- bench


def cmd_bench(args):
    p = _parser("bench", "Time one or more commands (arms) with interleaving, CPU and RSS.")
    p.add_argument("--arm", action="append", default=[], metavar="NAME=CMD", help="an arm; repeatable")
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                   help="timed runs per arm (default %d)" % DEFAULT_RUNS)
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                   help="untimed runs per arm first (default %d)" % DEFAULT_WARMUP)
    p.add_argument("--order", choices=("interleave", "random", "blocked"), default="interleave",
                   help="run order across arms (default interleave)")
    p.add_argument("--seed", type=int, default=0, help="seed for --order random (default 0)")
    p.add_argument("--shell", action="store_true", help="run arms through /bin/sh -c")
    p.add_argument("--check-output", action="store_true", help="hash stdout and compare")
    p.add_argument("--timeout", type=float, default=None, help="seconds per run")
    p.add_argument("--input", action="append", default=[], metavar="FILE",
                   help="input file whose SHA-256 goes into meta; one per flag, repeatable")
    p.add_argument("--min-effect", type=float, default=None, metavar="X",
                   help="smallest ratio change that counts, e.g. 0.05 for 5%% (default: "
                        ".devkit/aa.json from aa --save when under 7 days old, else 0.05)")
    p.add_argument("--out", default=None, help="write the devkit-bench/1 JSON here")
    p.add_argument("--repo", default=None,
                   help="repository root for .devkit/ and git metadata (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if not a.arm:
        raise UsageError("give at least one --arm NAME=CMD")
    _check_runs(a.runs, a.warmup)
    arms = [parse_arm(t) for t in a.arm]
    names = [n for n, _ in arms]
    if len(set(names)) != len(names):
        raise UsageError("arm names must be distinct: %s" % ", ".join(names))
    arms = [(n, arm_command(c, a.shell)) for n, c in arms]
    if a.shell:
        note("--shell: /bin/sh startup is inside every timing")
    bench, warnings = run_arms(arms, a.runs, a.warmup, a.order, a.seed, a.shell,
                               a.check_output, a.timeout)
    bench["schema"] = "devkit-bench/1"
    bench["runs"], bench["warmup"], bench["order_mode"] = a.runs, a.warmup, a.order
    bench["meta"] = metadata(a.input, a.seed, a.repo)
    bench["warnings"] = warnings
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(bench, f, indent=1)
    result = None
    if len(arms) >= 2:
        from devkit import compare  # measure is imported by compare; keep the import local
        min_effect, source = compare.resolve_min_effect(a.min_effect, a.repo)
        b, af = bench["arms"][names[0]], bench["arms"][names[1]]
        result = compare.analyze(b["wall"], af["wall"], min_effect, seed=a.seed)
        warnings = warnings + compare.protocol_warnings(
            result, {"before": b["cpu"], "after": af["cpu"]}, [bench["meta"]])
    for w in warnings:
        warn(w)
    if a.json:
        if result is not None:
            bench["compare"] = compare.compare_json(result, source, warnings, [bench["meta"]],
                                                    names=names[:2])
        print_json(bench)
        return 0
    if result is None:
        arm = bench["arms"][names[0]]
        s = summarize_arm(arm["wall"], arm["cpu"])
        print("runs    %d" % s["runs"])
        for k in ("median", "q1", "q3", "iqr", "min", "max"):
            print("%-7s %s" % (k, fmt_time(s[k])))
        if s["cpu_median"] is not None:
            print("cpu     %s (wall/CPU %s)" % (fmt_time(s["cpu_median"]),
                                                s["wall_over_cpu"] if s["wall_over_cpu"] is not None
                                                else "n/a"))
        rss = [r for r in arm["rss"] if r is not None]
        print("rss     %s" % (fmt_bytes(max(rss)) if rss else "n/a"))
        print("Gate 1: " + gate1_line(s))
    else:
        compare.print_compare(result, source, names[:2])
    if a.out:
        print("wrote %s" % a.out)
    return 0


# ---------------------------------------------------------------- aa


def cmd_aa(args):
    p = _parser("aa", "A/A noise floor: the same command as two interleaved arms. "
                "Usage: dev.py aa [options] -- CMD [ARGS...]")
    p.add_argument("--runs", type=int, default=20, help="timed runs per arm (default 20)")
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                   help="untimed runs first (default %d)" % DEFAULT_WARMUP)
    p.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    p.add_argument("--resamples", type=int, default=5000,
                   help="bootstrap resamples for the ratio CI (default 5000)")
    p.add_argument("--shell", action="store_true", help="run the command through /bin/sh -c")
    p.add_argument("--save", action="store_true", help="write .devkit/aa.json for compare")
    p.add_argument("--repo", default=None,
                   help="repository root for .devkit/ and git metadata (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    opts, argv = split_dashes(args)
    a = p.parse_args(opts)
    if not argv:
        raise UsageError("give the command after --, for example: dev.py aa -- python3 bench.py")
    _check_runs(a.runs, a.warmup)
    cmd = " ".join(argv) if a.shell else argv
    if a.shell:
        note("--shell: /bin/sh startup is inside every timing")
    bench, _ = run_arms([("a1", cmd), ("a2", cmd)], a.runs, a.warmup, "interleave", a.seed, a.shell)
    w1, w2 = bench["arms"]["a1"]["wall"], bench["arms"]["a2"]["wall"]
    ci = stats.bootstrap_ratio_ci(w1, w2, resamples=a.resamples, seed=a.seed)
    half = (ci["hi"] - ci["lo"]) / 2.0
    min_effect = max(0.02, 2.0 * half)
    sentence = "A/A noise floor: ratio CI %.3f to %.3f on %d runs; minimum effect %.1f%%" % (
        ci["lo"], ci["hi"], a.runs, min_effect * 100)
    record = {"schema": "devkit-aa/1", "command": cmd, "min_effect": min_effect,
              "ci": [ci["lo"], ci["hi"]], "runs": a.runs, "timestamp": environment()["timestamp"],
              "seed": a.seed, "resamples": a.resamples, "medians": [median(w1), median(w2)],
              "cv_percent": [stats.cv(w1), stats.cv(w2)], "sentence": sentence, "saved": None}
    if a.save:
        root, root_note = repo_root(a.repo)
        if root_note:
            note(root_note)
        path = os.path.join(devkit_dir(root, create=True), "aa.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: record[k] for k in ("command", "min_effect", "ci", "runs", "timestamp")},
                      f, indent=1)
        record["saved"] = path.replace(os.sep, "/")
    if a.json:
        print_json(record)
        return 0
    print("a1: median %s (CV %.1f%%); a2: median %s (CV %.1f%%)" % (
        fmt_time(record["medians"][0]), record["cv_percent"][0],
        fmt_time(record["medians"][1]), record["cv_percent"][1]))
    print(sentence)
    if record["saved"]:
        print("saved %s; dev.py compare uses it for 7 days" % record["saved"])
    return 0


# ---------------------------------------------------------------- scale


def parse_sizes(text):
    try:
        sizes = [int(x) for x in text.split(",") if x.strip()]
    except ValueError:
        raise UsageError("--sizes must be a comma-separated list of integers")
    if any(n <= 0 for n in sizes):
        raise UsageError("--sizes must be positive integers")
    if len(set(sizes)) < 2:
        raise UsageError("--sizes needs at least two distinct sizes")
    return sizes


def scale_core(argv, sizes, runs, warmup, baseline, no_baseline, seed=0):
    """Shared by dev.py scale and bench.py scale. Returns the result dict."""
    if baseline and no_baseline:
        raise UsageError("--baseline and --no-baseline contradict each other; pass one")
    if not any("{n}" in x for x in argv):
        raise UsageError("the command must contain {n} somewhere for scale to substitute the size")
    warnings = []
    if len(sizes) < 3:
        warnings.append("only %d sizes; use at least three (four recommended) so the fit has "
                        "a residual" % len(sizes))
    baseline_cmd = None
    if baseline:
        baseline_cmd = shlex.split(baseline)
        if not baseline_cmd:
            raise UsageError("--baseline needs a non-empty command")
    elif not no_baseline:
        baseline_cmd = default_baseline(argv)
        if baseline_cmd is None:
            warnings.append("no startup baseline known for %r; pass --baseline 'CMD' or make "
                            "the smallest median at least 0.5 s" % argv[0])
    base = None
    if baseline_cmd is not None:
        bw, bc = time_many(baseline_cmd, runs, warmup)
        base = summarize_arm(bw, bc)
        base["command"] = baseline_cmd
    rows, per_size = [], []
    for n in sizes:
        cmd = [x.replace("{n}", str(n)) for x in argv]
        w, c = time_many(cmd, runs, warmup)
        s = summarize_arm(w, c)
        s["n"] = n
        s["times"] = w
        rows.append(s)
        per_size.append(w)
    base_median = base["median"] if base else 0.0
    exponent, more = scale_exponent(sizes, [r["median"] for r in rows], base_median)
    warnings.extend(more)
    ci = stats.exponent_ci(sizes, per_size, base_median, seed=seed) if exponent is not None else None
    largest = max(rows, key=lambda r: r["n"])
    return {"sizes": rows, "baseline": base, "exponent": exponent,
            "exponent_ci": list(ci) if ci else None,
            "class": classify_exponent(exponent) if exponent is not None else None,
            "warnings": warnings, "command": argv, "gate1": gate1_line(largest), "seed": seed}


def print_scale(res):
    for r in res["sizes"]:
        cpu = fmt_time(r["cpu_median"]) if r["cpu_median"] is not None else "n/a"
        print("n=%-8d median %-10s IQR %-10s CPU %-10s (runs %d)" % (
            r["n"], fmt_time(r["median"]), fmt_time(r["iqr"]), cpu, r["runs"]))
    if res["baseline"]:
        print("Baseline: median %s for %s, subtracted before the fit" % (
            fmt_time(res["baseline"]["median"]), " ".join(res["baseline"]["command"])))
    if res["exponent"] is None:
        print("Exponent: not fitted")
    elif res["exponent_ci"]:
        print("Exponent: %.2f (%s; 95%% CI %.2f..%.2f)" % (
            res["exponent"], res["class"], res["exponent_ci"][0], res["exponent_ci"][1]))
    else:
        print("Exponent: %.2f (%s)" % (res["exponent"], res["class"]))
    largest = max(res["sizes"], key=lambda r: r["n"])
    print("Gate 1 at n=%d: %s" % (largest["n"], res["gate1"]))


def cmd_scale(args):
    p = _parser("scale", "Doubling test: time the command at each size, fit the exponent "
                "after the startup baseline. Usage: dev.py scale --sizes ... [opts] -- CMD")
    p.add_argument("--sizes", required=True, help="comma-separated sizes, e.g. 1000,2000,4000,8000")
    p.add_argument("--baseline", default=None, help="startup-only command to subtract")
    p.add_argument("--no-baseline", action="store_true", help="fit the raw medians")
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                   help="timed runs per size (default %d)" % DEFAULT_RUNS)
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                   help="untimed runs per size first (default %d)" % DEFAULT_WARMUP)
    p.add_argument("--seed", type=int, default=0, help="bootstrap seed for the CI (default 0)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    opts, argv = split_dashes(args)
    a = p.parse_args(opts)
    if not argv:
        raise UsageError("give the command after --; every {n} in it is replaced by the size")
    _check_runs(a.runs, a.warmup)
    res = scale_core(argv, parse_sizes(a.sizes), a.runs, a.warmup, a.baseline, a.no_baseline, a.seed)
    for w in res["warnings"]:
        warn(w)
    if a.json:
        res["schema"] = "devkit-scale/1"
        res["meta"] = metadata([], a.seed)
        print_json(res)
        return 0
    print_scale(res)
    return 0


# ---------------------------------------------------------------- changepoint


def load_series(path):
    """[(label, value)] from one-number-per-line, label,value CSV, or JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise UsageError("cannot read %s: %s" % (path, e.strerror or e))
    stripped = text.strip()
    if not stripped:
        raise UsageError("%s is empty" % path)
    if stripped[0] in "[{" or path.endswith(".json"):
        try:
            data = json.loads(stripped)
        except ValueError as e:
            raise UsageError("%s is not valid JSON: %s" % (path, e))
        if isinstance(data, dict):
            data = data.get("series", data.get("values"))
        if not isinstance(data, list):
            raise UsageError("%s: expected a JSON list of numbers or of {label, value}" % path)
        out = []
        for i, item in enumerate(data):
            try:
                if isinstance(item, dict):
                    out.append((str(item.get("label", i)), float(item["value"])))
                else:
                    out.append((str(i), float(item)))
            except (KeyError, TypeError, ValueError):
                raise UsageError("%s item %d: expected a number or {\"label\", \"value\"}, got %r"
                                 % (path, i, item))
        return out
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        try:
            if len(parts) == 1:
                out.append((str(len(out)), float(parts[0])))
            else:
                out.append((parts[0], float(parts[-1])))
        except ValueError:
            if lineno == 1 and len(parts) > 1:
                continue  # header line
            raise UsageError("%s line %d: cannot read a number from %r" % (path, lineno, line))
    return out


def cmd_changepoint(args):
    p = _parser("changepoint", "Step changes in a series by deterministic binary segmentation "
                "(Welch t, Bonferroni over candidate splits).")
    p.add_argument("series", nargs="?", default=None,
                   help="CSV (one number per line or label,value) or JSON file")
    p.add_argument("--perf", default=None, metavar="NAME",
                   help="read the medians of .devkit/perf/NAME.jsonl (from dev.py perf run)")
    p.add_argument("--min-size", type=int, default=5,
                   help="fewest points on each side of a split (default 5)")
    p.add_argument("--alpha", type=float, default=0.01,
                   help="significance level before Bonferroni (default 0.01)")
    p.add_argument("--repo", default=None, help="repository root for --perf")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if a.min_size < 2:
        raise UsageError("--min-size must be at least 2")
    if (a.series is None) == (a.perf is None):
        raise UsageError("give a SERIES file or --perf NAME, not both; run "
                         "`dev.py changepoint --help` for the options")
    if a.perf is not None:
        from devkit import perf
        root, root_note = repo_root(a.repo)
        if root_note:
            note(root_note)
        pairs = perf.changepoint_series(root, a.perf)
    else:
        pairs = load_series(a.series)
    values = [v for _, v in pairs]
    if len(values) < 2 * a.min_size:
        raise UsageError("series has %d points; need at least 2 x min-size (%d)" % (
            len(values), 2 * a.min_size))
    points = stats.changepoint(values, a.min_size, a.alpha)
    for pt in points:
        pt["label"] = pairs[pt["index"]][0]
    triage = ("Triage before acting: in published CI practice under half of detected change "
              "points are real regressions; check the commit range and rerun the benchmark.")
    if a.json:
        print_json({"schema": "devkit-changepoint/1", "source": a.series or "perf:" + a.perf,
                    "n": len(values), "min_size": a.min_size, "alpha": a.alpha,
                    "points": points, "note": triage})
        return 0
    if not points:
        print("no change point at alpha %g over %d points (min segment %d)" % (
            a.alpha, len(values), a.min_size))
    for pt in points:
        change = ("%+.1f%%" % pt["percent_change"]) if pt["percent_change"] is not None else "n/a"
        print("change point at index %d (label %s): median %.6g before -> %.6g after (%s), "
              "p=%.3g" % (pt["index"], pt["label"], pt["before_median"], pt["after_median"],
                          change, pt["p"]))
    print(triage)
    return 0
