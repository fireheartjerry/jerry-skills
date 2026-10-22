"""The deprecated `bench.py` subcommands (run, compare, scale) on the shared timing core.

Output lines and JSON keys are unchanged for existing scripts; `scale` also has the later
fixes (SCALE_FLOOR 0.5 s, the baseline/no-baseline usage error, an exponent CI as an extra
key).
"""
import json

from devkit.common import UsageError, fmt_time
from devkit.measure import (WAIT_RATIO, compare_warnings, gate1_line, parse_sizes, print_scale,
                            report_lines, scale_core, summarize_arm, time_many, time_once,
                            verdict)


def legacy_run(args, argv):
    walls, cpus = time_many(argv, args.runs, args.warmup)
    s = summarize_arm(walls, cpus)
    if args.json:
        out = dict(s)
        out.update({"command": argv, "times": walls, "cpu_times": cpus, "gate1": gate1_line(s)})
        print(json.dumps(out))
        return 0
    print("runs    %d" % s["runs"])
    for k in ("median", "q1", "q3", "iqr", "min", "max"):
        print("%-7s %s" % (k, fmt_time(s[k])))
    if s["cpu_median"] is not None:
        print("cpu     %s (wall/CPU %s)" % (fmt_time(s["cpu_median"]),
                                            s["wall_over_cpu"] if s["wall_over_cpu"] is not None
                                            else "n/a"))
    print("Gate 1: " + gate1_line(s))
    return 0


def legacy_compare(args, argv):
    if argv:
        raise UsageError("compare takes --before and --after shell strings, not a trailing command")
    for _ in range(args.warmup):
        time_once(args.before, True)
        time_once(args.after, True)
    if args.no_interleave:
        before_w, before_c = time_many(args.before, args.runs, 0, shell=True)
        after_w, after_c = time_many(args.after, args.runs, 0, shell=True)
    else:
        before_w, before_c, after_w, after_c = [], [], [], []
        for _ in range(args.runs):
            r = time_once(args.before, True)
            before_w.append(r["wall"])
            before_c.append(r["cpu"])
            r = time_once(args.after, True)
            after_w.append(r["wall"])
            after_c.append(r["cpu"])
    b, a = summarize_arm(before_w, before_c), summarize_arm(after_w, after_c)
    name, speedup, reason = verdict(b, a)
    lines = report_lines(b, a, name, speedup)
    warnings = compare_warnings(b, a)
    if args.json:
        print(json.dumps({
            "before": b, "after": a, "verdict": name, "speedup": speedup, "reason": reason,
            "interleaved": not args.no_interleave, "report_lines": lines, "warnings": warnings,
            "before_times": before_w, "after_times": after_w,
            "before_cpu_times": before_c, "after_cpu_times": after_c}))
        return 0
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
    return 0


def legacy_scale(args, argv):
    res = scale_core(argv, parse_sizes(args.sizes), args.runs, args.warmup, args.baseline,
                     args.no_baseline)
    if args.json:
        print(json.dumps({k: res[k] for k in ("sizes", "baseline", "exponent", "exponent_ci",
                                              "class", "warnings", "command", "gate1")}))
        return 0
    print_scale(res)
    for w in res["warnings"]:
        print("warning: " + w)
    return 0
