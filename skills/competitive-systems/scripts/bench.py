#!/usr/bin/env python3
"""Time commands the way the measurement protocol requires: warm-up, several runs,
median and IQR, CPU time next to wall time, interleaved A/B, a noise-rule verdict, and a
doubling-test exponent fitted after the process-startup baseline is subtracted.

Subcommands:
  run      [--runs N] [--warmup W] [--json] -- CMD [ARG...]
  compare  [--runs N] [--warmup W] [--json] [--no-interleave] --before 'CMD' --after 'CMD'
  scale    --sizes n1,n2[,n3...] [--baseline 'CMD' | --no-baseline]
           [--runs N] [--warmup W] [--json] -- CMD [ARG...]

`run` and `scale` take an argv list after `--` (no shell). `compare` takes two shell strings
because before/after are usually pipelines. In `scale`, every `{n}` in every argv element is
replaced by the size. `scale` also times a startup baseline (`<python> -c pass` when the
command is python, `node -e 0` for node, or the command given with --baseline) and subtracts
its median from every size before fitting the exponent; without that, startup pulls the
exponent toward 0. CPU time is the child's user+sys from getrusage (macOS and Linux).
Exit status: 0 success, 2 usage error, 3 the measured command failed or could not be run
(its last stderr lines are shown). Python 3.8+, standard library only.
"""
import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

try:
    import resource
except ImportError:  # not POSIX: CPU time is reported as unavailable
    resource = None

DEFAULT_RUNS = 7
DEFAULT_WARMUP = 1
STARTUP_FLOOR = 0.020   # seconds; medians below this are mostly process startup
SCALE_FLOOR = 0.200     # seconds; the smallest scale median should be at least this
BASELINE_RATIO = 10.0   # the smallest scale median should be >= this x the startup baseline
WAIT_RATIO = 1.5        # wall > WAIT_RATIO x CPU means the process is waiting
WAIT_FLOOR = 0.100      # seconds; under this the wall/CPU ratio is startup noise

# ---------------------------------------------------------------- statistics


def median(xs):
    """Median of a non-empty list."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def quartiles(xs):
    """(q1, q3) by the median-of-halves method; the middle element is excluded for odd n."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("quartiles of empty list")
    if n == 1:
        return float(s[0]), float(s[0])
    half = n // 2
    lower = s[:half]
    upper = s[half + 1:] if n % 2 else s[half:]
    return median(lower), median(upper)


def summarize(xs):
    """Summary dict of wall times: runs, median, q1, q3, iqr, min, max."""
    q1, q3 = quartiles(xs)
    return {
        "runs": len(xs),
        "median": median(xs),
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "min": float(min(xs)),
        "max": float(max(xs)),
    }


def with_cpu(s, cpus):
    """Add CPU fields to a wall-time summary: cpu_median, wall_over_cpu, waiting.

    waiting is True when the median wall time is at least WAIT_FLOOR and exceeds
    WAIT_RATIO x the median CPU time: the process spent its time waiting, not computing.
    """
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
    """Noise rule on two summaries.

    Returns (verdict, speedup, reason). verdict is "faster", "slower" or "inconclusive";
    speedup is before.median / after.median rounded to 2 decimals, or None when
    inconclusive; reason explains an inconclusive result.
    """
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


def fit_exponent(sizes, medians):
    """Least-squares slope of log(median) on log(size), rounded to 2 decimals."""
    if len(sizes) != len(medians) or len(set(sizes)) < 2:
        raise ValueError("fit_exponent needs at least two distinct sizes")
    if any(s <= 0 for s in sizes) or any(m <= 0 for m in medians):
        raise ValueError("fit_exponent needs positive sizes and medians")
    xs = [math.log(float(s)) for s in sizes]
    ys = [math.log(float(m)) for m in medians]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / sxx, 2)


def scale_exponent(sizes, medians, baseline):
    """Exponent fitted after subtracting the startup baseline from every median.

    Returns (exponent or None, warnings). None means the medians sit within the baseline
    and nothing can be fitted; the warnings say what to do.
    """
    warnings = []
    smallest = min(medians)
    if baseline > 0 and smallest < BASELINE_RATIO * baseline:
        warnings.append("smallest median %s is under %dx the startup baseline %s; raise the "
                        "sizes until it is at least 0.5 s and refit"
                        % (fmt_time(smallest), int(BASELINE_RATIO), fmt_time(baseline)))
    elif smallest < SCALE_FLOOR:
        warnings.append("smallest median %s is under %s; raise the sizes until it is at "
                        "least 0.5 s and refit" % (fmt_time(smallest), fmt_time(SCALE_FLOOR)))
    adjusted = [m - baseline for m in medians]
    if any(a <= 0 for a in adjusted):
        warnings.append("a median is within the startup baseline; exponent not fitted")
        return None, warnings
    return fit_exponent(sizes, adjusted), warnings


def classify_exponent(e):
    if e < 1.3:
        return "about linear"
    if e < 1.7:
        return "between linear and quadratic"
    if e < 2.5:
        return "about quadratic"
    if e < 3.5:
        return "about cubic"
    return "worse than cubic"


# ---------------------------------------------------------------- formatting


def fmt_time(seconds):
    if seconds >= 1.0:
        return "%.2f s" % seconds
    if seconds >= 0.001:
        return "%.1f ms" % (seconds * 1000.0)
    return "%.0f us" % (seconds * 1e6)


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


def report_lines(before, after, verdict_name, speedup):
    first = "Before: median %s (IQR %s, runs=%d); After: median %s (IQR %s, runs=%d)." % (
        fmt_time(before["median"]), fmt_time(before["iqr"]), before["runs"],
        fmt_time(after["median"]), fmt_time(after["iqr"]), after["runs"])
    if verdict_name == "faster":
        second = "Verdict: %.2fx faster" % speedup
    elif verdict_name == "slower":
        second = "Verdict: %.2fx slower" % (1.0 / speedup)
    else:
        second = "Verdict: no measurable change"
    return [first, second]


def compare_warnings(before, after):
    """Protocol warnings for a compare: run counts by tier, startup-dominated medians."""
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


# ---------------------------------------------------------------- timing


def child_cpu():
    """Cumulative user+sys seconds of waited-for children, or None when unavailable."""
    if resource is None:
        return None
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def time_once(cmd, shell):
    """Run cmd once; return (wall_seconds, cpu_seconds or None).

    Exit 3 with a message when the command cannot be started or exits nonzero; in the
    latter case the last 20 lines of its stderr are shown.
    """
    shown = cmd if shell else " ".join(cmd)
    cpu0 = child_cpu()
    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, shell=shell, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
    except OSError as e:
        sys.stderr.write("bench.py: cannot run %s: %s\n" % (shown, e))
        sys.exit(3)
    wall = time.perf_counter() - start
    cpu1 = child_cpu()
    cpu = None if cpu0 is None else cpu1 - cpu0
    if proc.returncode != 0:
        sys.stderr.write("bench.py: measured command failed with exit %d: %s\n"
                         % (proc.returncode, shown))
        tail = proc.stderr.decode("utf-8", "replace").splitlines()[-20:]
        if tail:
            sys.stderr.write("bench.py: last %d line(s) of its stderr:\n" % len(tail))
            for line in tail:
                sys.stderr.write("  " + line + "\n")
        sys.exit(3)
    return wall, cpu


def time_many(cmd, runs, warmup, shell=False):
    """Warm up, then time `runs` runs. Returns (walls, cpus)."""
    for _ in range(warmup):
        time_once(cmd, shell)
    walls, cpus = [], []
    for _ in range(runs):
        w, c = time_once(cmd, shell)
        walls.append(w)
        cpus.append(c)
    return walls, cpus


def default_baseline(argv):
    """A startup-only command matching argv[0], or None when none is known."""
    exe = os.path.basename(argv[0])
    if re.match(r"python[0-9.]*(w)?$", exe) or exe in ("pypy", "pypy3"):
        return [argv[0], "-c", "pass"]
    if exe in ("node", "nodejs", "ruby", "perl"):
        return [argv[0], "-e", "0"]
    return None


# ---------------------------------------------------------------- subcommands


def cmd_run(args, argv):
    walls, cpus = time_many(argv, args.runs, args.warmup)
    s = summarize_arm(walls, cpus)
    if args.json:
        out = dict(s)
        out["command"] = argv
        out["times"] = walls
        out["cpu_times"] = cpus
        out["gate1"] = gate1_line(s)
        print(json.dumps(out))
        return
    print("runs    %d" % s["runs"])
    for k in ("median", "q1", "q3", "iqr", "min", "max"):
        print("%-7s %s" % (k, fmt_time(s[k])))
    if s["cpu_median"] is not None:
        print("cpu     %s (wall/CPU %s)" % (fmt_time(s["cpu_median"]),
                                            s["wall_over_cpu"] if s["wall_over_cpu"] is not None
                                            else "n/a"))
    print("Gate 1: " + gate1_line(s))


def cmd_compare(args, argv):
    if argv:
        usage("compare takes --before and --after shell strings, not a trailing command")
    before_w, before_c, after_w, after_c = [], [], [], []
    for _ in range(args.warmup):
        time_once(args.before, True)
        time_once(args.after, True)
    if args.no_interleave:
        before_w, before_c = time_many(args.before, args.runs, 0, shell=True)
        after_w, after_c = time_many(args.after, args.runs, 0, shell=True)
    else:
        for _ in range(args.runs):
            w, c = time_once(args.before, True)
            before_w.append(w)
            before_c.append(c)
            w, c = time_once(args.after, True)
            after_w.append(w)
            after_c.append(c)
    b, a = summarize_arm(before_w, before_c), summarize_arm(after_w, after_c)
    name, speedup, reason = verdict(b, a)
    lines = report_lines(b, a, name, speedup)
    warnings = compare_warnings(b, a)
    if args.json:
        print(json.dumps({
            "before": b, "after": a, "verdict": name, "speedup": speedup,
            "reason": reason, "interleaved": not args.no_interleave,
            "report_lines": lines, "warnings": warnings,
            "before_times": before_w, "after_times": after_w,
            "before_cpu_times": before_c, "after_cpu_times": after_c,
        }))
        return
    print("%-8s %-11s %-11s %-11s %-11s %-11s %-9s %s" % (
        "arm", "median", "IQR", "min", "max", "CPU", "wall/CPU", "runs"))
    for label, s in (("before", b), ("after", a)):
        cpu = fmt_time(s["cpu_median"]) if s["cpu_median"] is not None else "n/a"
        ratio = "%.2f" % s["wall_over_cpu"] if s["wall_over_cpu"] is not None else "n/a"
        print("%-8s %-11s %-11s %-11s %-11s %-11s %-9s %d" % (
            label, fmt_time(s["median"]), fmt_time(s["iqr"]), fmt_time(s["min"]),
            fmt_time(s["max"]), cpu, ratio, s["runs"]))
    if name == "inconclusive":
        print("Verdict: inconclusive (within noise): medians %s vs %s, %s" % (
            fmt_time(b["median"]), fmt_time(a["median"]), reason))
    elif name == "faster":
        print("Verdict: faster (%.2fx)" % speedup)
    else:
        print("Verdict: slower (%.2fx)" % (1.0 / speedup))
    for s, label in ((b, "before"), (a, "after")):
        if s["waiting"]:
            print("note: the %s arm appears to be waiting (wall > %.1fx CPU)" % (label, WAIT_RATIO))
    for w in warnings:
        print("warning: " + w)
    for line in lines:
        print(line)


def cmd_scale(args, argv):
    try:
        sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    except ValueError:
        usage("--sizes must be a comma-separated list of integers")
    if any(n <= 0 for n in sizes):
        usage("--sizes must be positive integers")
    if len(set(sizes)) < 2:
        usage("--sizes needs at least two distinct sizes")
    if not any("{n}" in a for a in argv):
        usage("the command must contain {n} somewhere for scale to substitute the size")
    warnings = []
    baseline_cmd = None
    if args.baseline:
        baseline_cmd = shlex.split(args.baseline)
        if not baseline_cmd:
            usage("--baseline needs a non-empty command")
    elif not args.no_baseline:
        baseline_cmd = default_baseline(argv)
        if baseline_cmd is None:
            warnings.append("no startup baseline known for %r; pass --baseline 'CMD' or make "
                            "the smallest median at least 0.5 s" % argv[0])
    baseline = None
    if baseline_cmd is not None:
        bw, bc = time_many(baseline_cmd, args.runs, args.warmup)
        baseline = summarize_arm(bw, bc)
        baseline["command"] = baseline_cmd
    rows = []
    for n in sizes:
        cmd = [a.replace("{n}", str(n)) for a in argv]
        w, c = time_many(cmd, args.runs, args.warmup)
        s = summarize_arm(w, c)
        s["n"] = n
        rows.append(s)
    base_median = baseline["median"] if baseline else 0.0
    exponent, more = scale_exponent([r["n"] for r in rows], [r["median"] for r in rows],
                                    base_median)
    warnings.extend(more)
    label = classify_exponent(exponent) if exponent is not None else None
    largest = max(rows, key=lambda r: r["n"])
    if args.json:
        print(json.dumps({"sizes": rows, "baseline": baseline, "exponent": exponent,
                          "class": label, "warnings": warnings, "command": argv,
                          "gate1": gate1_line(largest)}))
        return
    for r in rows:
        cpu = fmt_time(r["cpu_median"]) if r["cpu_median"] is not None else "n/a"
        print("n=%-8d median %-10s IQR %-10s CPU %-10s (runs %d)" % (
            r["n"], fmt_time(r["median"]), fmt_time(r["iqr"]), cpu, r["runs"]))
    if baseline:
        print("Baseline: median %s for %s, subtracted before the fit" % (
            fmt_time(baseline["median"]), " ".join(baseline["command"])))
    if exponent is None:
        print("Exponent: not fitted")
    else:
        print("Exponent: %.2f (%s)" % (exponent, label))
    print("Gate 1 at n=%d: %s" % (largest["n"], gate1_line(largest)))
    for w in warnings:
        print("warning: " + w)


# ---------------------------------------------------------------- CLI


def usage(msg):
    sys.stderr.write("bench.py: %s\n" % msg)
    sys.exit(2)


def build_parser():
    p = argparse.ArgumentParser(
        prog="bench.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="sub")

    def common(sp):
        sp.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help="measured runs per arm (default %d)" % DEFAULT_RUNS)
        sp.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                        help="discarded warm-up runs per arm (default %d)" % DEFAULT_WARMUP)
        sp.add_argument("--json", action="store_true", help="print one JSON object")

    sp = sub.add_parser("run", help="time one command given after --")
    common(sp)
    sp = sub.add_parser("compare", help="interleaved A/B of two shell strings")
    common(sp)
    sp.add_argument("--before", required=True, help="shell string for the before arm")
    sp.add_argument("--after", required=True, help="shell string for the after arm")
    sp.add_argument("--no-interleave", action="store_true",
                    help="run all before runs, then all after runs")
    sp = sub.add_parser("scale", help="doubling test: time the command at each size")
    common(sp)
    sp.add_argument("--sizes", required=True, help="comma-separated sizes, e.g. 1000,2000,4000")
    sp.add_argument("--baseline", default=None,
                    help="startup-only command to subtract (default: '<argv0> -c pass' for "
                         "python, '<argv0> -e 0' for node, ruby, perl)")
    sp.add_argument("--no-baseline", action="store_true",
                    help="fit the raw medians without subtracting a startup baseline")
    return p


def main(raw):
    if "--" in raw:
        split = raw.index("--")
        opts, argv = raw[:split], raw[split + 1:]
    else:
        opts, argv = raw, []
    parser = build_parser()
    args = parser.parse_args(opts)
    if args.sub is None:
        parser.print_help()
        sys.exit(2)
    if args.runs < 1 or args.warmup < 0:
        usage("--runs must be >= 1 and --warmup >= 0")
    if args.sub in ("run", "scale") and not argv:
        usage("%s needs a command after --" % args.sub)
    {"run": cmd_run, "compare": cmd_compare, "scale": cmd_scale}[args.sub](args, argv)


if __name__ == "__main__":
    main(sys.argv[1:])
