"""Before/after analysis for `dev.py compare` and the two-arm form of `dev.py bench`.

One decision rule (stats.decide_verdict): the bootstrap CI of the ratio of medians against
the minimum effect, and the one-sided Mann-Whitney p against alpha. The phase-1 IQR rule
survives only as the hand rule in measurement.md and inside bench.py.
"""
import datetime
import os

from devkit import stats
from devkit.common import (Parser, UsageError, devkit_dir, fmt_time, load_json_file, note,
                           print_json, repo_root, warn)
from devkit.measure import (PROTOCOL_RUNS, STARTUP_FLOOR, WAIT_RATIO, before_after_line,
                            summarize, with_cpu)

DEFAULT_MIN_EFFECT = 0.05
AA_MAX_AGE_DAYS = 7
AA_FILE = "aa.json"


def arm_stats(values):
    s = summarize(values)
    s["mad"] = stats.mad(values)
    s["cv_percent"] = stats.cv(values)
    s["outliers_mild"], s["outliers_severe"] = stats.tukey_counts(values)
    s["spearman_rho"] = stats.spearman(list(range(len(values))), values)
    return s


def analyze(before, after, min_effect, alpha=0.05, resamples=5000, seed=0):
    """Effect, p values and the verdict for two series (before and after)."""
    b, a = arm_stats(before), arm_stats(after)
    ci = stats.bootstrap_ratio_ci(before, after, resamples=resamples, seed=seed)
    faster = stats.mann_whitney(after, before, "less")
    slower = stats.mann_whitney(after, before, "greater")
    name, reason = stats.decide_verdict(ci["lo"], ci["hi"], faster["p"], slower["p"],
                                        min_effect, alpha)
    half = (ci["hi"] - ci["lo"]) / 2.0
    runs = min(len(before), len(after))
    p = {"FASTER": faster["p"], "SLOWER": slower["p"]}.get(name, faster["p_two_sided"])
    return {
        "before": b, "after": a, "verdict": name, "reason": reason, "p": p,
        "effect": {"ratio": ci["ratio"], "ci_lo": ci["lo"], "ci_hi": ci["hi"],
                   "ci_level": ci["level"], "resamples": resamples, "seed": seed,
                   "hodges_lehmann_shift": stats.hodges_lehmann(before, after),
                   "p_faster": faster["p"], "p_slower": slower["p"],
                   "p_two_sided": faster["p_two_sided"], "u": faster["u"],
                   "method": faster["method"]},
        "min_effect": min_effect, "alpha": alpha,
        "suggested_runs": (stats.suggest_runs(runs, half, min_effect)
                           if name == "INCONCLUSIVE" else None),
    }


def ci_report_lines(result):
    """The two paste-ready lines for the CI rule."""
    b, a, e = result["before"], result["after"], result["effect"]
    ci = "95%% CI %.2fx..%.2fx" % (e["ci_lo"], e["ci_hi"])
    name = result["verdict"]
    if name == "FASTER":
        second = "Verdict: %.2fx faster (%s, p=%.3g)" % (e["ratio"], ci, result["p"])
    elif name == "SLOWER":
        second = "Verdict: %.2fx slower (%s, p=%.3g)" % (1.0 / e["ratio"], ci, result["p"])
    elif name == "NO DETECTABLE CHANGE":
        second = "Verdict: no detectable change (%s, min effect %.3g%%)" % (
            ci, result["min_effect"] * 100)
    else:
        second = ("Verdict: inconclusive (%s, p=%.3g); add runs: the CI is wider than the "
                  "minimum effect; try %d runs per arm" % (ci, result["p"], result["suggested_runs"]))
    return [before_after_line(b, a), second]


def protocol_warnings(result, cpus=None, metas=None):
    """Warnings the protocol asks for, over the analysis result and optional CPU and metadata."""
    out = []
    arms = (("before", result["before"]), ("after", result["after"]))
    for label, s in arms:
        n = s["runs"]
        if n < PROTOCOL_RUNS:
            out.append("%s: only %d runs; the protocol asks for at least %d per arm" % (
                label, n, PROTOCOL_RUNS))
        if s["cv_percent"] > 5.0:
            out.append("%s: CV %.1f%% is over 5%%; the machine is noisy, close other work or "
                       "add runs" % (label, s["cv_percent"]))
        outliers = s["outliers_mild"] + s["outliers_severe"]
        if outliers > 0.1 * n:
            out.append("%s: %d of %d runs are Tukey outliers (over 10%%); kept, check for "
                       "interference" % (label, outliers, n))
        if n >= 7 and abs(s["spearman_rho"]) > 0.5:
            out.append("%s: times trend with run index (Spearman rho %.2f): warmup unfinished "
                       "or drift" % (label, s["spearman_rho"]))
        if s["median"] < STARTUP_FLOOR:
            out.append("%s: median %s is under %s; process startup dominates, loop inside the "
                       "process" % (label, fmt_time(s["median"]), fmt_time(STARTUP_FLOOR)))
    if cpus:
        for label, s in arms:
            c = cpus.get(label)
            if c and all(x is not None for x in c):
                if with_cpu(dict(s), c)["waiting"]:
                    out.append("%s: appears to be waiting (wall > %.1fx CPU); measure the "
                               "waiting, not the computing" % (label, WAIT_RATIO))
    if metas:
        for m in metas:
            load, cpu = m.get("loadavg"), m.get("cpu_count")
            if load and cpu and load[0] > cpu / 2.0:
                out.append("load average %.1f exceeds half the CPU count (%d) at measurement "
                           "time" % (load[0], cpu))
                break
        if len(metas) == 2:
            for key in ("python", "platform", "rev"):
                if metas[0].get(key) != metas[1].get(key):
                    out.append("metadata differs between the files: %s %r vs %r" % (
                        key, metas[0].get(key), metas[1].get(key)))
    return out


def resolve_min_effect(arg, repo=None):
    """(min_effect, source line) from --min-effect, a fresh .devkit/aa.json, or the default."""
    if arg is not None:
        if arg <= 0:
            raise UsageError("--min-effect must be positive, for example 0.05 for 5%")
        return arg, "min effect %g%% (--min-effect)" % (arg * 100)
    root, _ = repo_root(repo)
    path = os.path.join(devkit_dir(root), AA_FILE)
    rel = os.path.relpath(path, root)
    if os.path.isfile(path):
        try:
            data = load_json_file(path, "A/A file")
            stamp = datetime.datetime.fromisoformat(data["timestamp"])
            age = datetime.datetime.now(datetime.timezone.utc) - stamp.astimezone(
                datetime.timezone.utc)
            if age.days < AA_MAX_AGE_DAYS:
                return float(data["min_effect"]), "min effect %.1f%% (from %s measured %s)" % (
                    data["min_effect"] * 100, rel, data["timestamp"])
            note("%s is %d days old; rerun dev.py aa --save" % (rel, age.days))
        except (KeyError, ValueError, TypeError, UsageError):
            note("%s is unreadable; rerun dev.py aa --save" % rel)
    return DEFAULT_MIN_EFFECT, "min effect 5% (default; run dev.py aa to measure the noise floor)"


def print_compare(result, source_line, names=("before", "after")):
    e = result["effect"]
    print("%-8s %-4s %-10s %-10s %-10s %-6s %-10s %-10s %-8s %s" % (
        "arm", "n", "median", "IQR", "MAD", "CV", "min", "max", "outliers", "rho"))
    for label, s in zip(names, (result["before"], result["after"])):
        print("%-8s %-4d %-10s %-10s %-10s %-6s %-10s %-10s %-8s %.2f" % (
            label, s["runs"], fmt_time(s["median"]), fmt_time(s["iqr"]), fmt_time(s["mad"]),
            "%.1f%%" % s["cv_percent"], fmt_time(s["min"]), fmt_time(s["max"]),
            "%d/%d" % (s["outliers_mild"], s["outliers_severe"]), s["spearman_rho"]))
    shift = e["hodges_lehmann_shift"]
    print("Effect: ratio of medians %.3fx (95%% CI %.3fx..%.3fx, %d resamples, seed %d); "
          "Hodges-Lehmann shift %s %s; Mann-Whitney one-sided p %.3g (faster) / %.3g (slower), "
          "two-sided %.3g, %s" % (
              e["ratio"], e["ci_lo"], e["ci_hi"], e["resamples"], e["seed"],
              fmt_time(abs(shift)), "saved" if shift >= 0 else "lost",
              e["p_faster"], e["p_slower"], e["p_two_sided"], e["method"]))
    print(source_line + "; alpha %g" % result["alpha"])
    print("verdict: %s: %s" % (result["verdict"], result["reason"]))
    print("Paste into the report:")
    for line in ci_report_lines(result):
        print(line)


def compare_json(result, source_line, warnings, metas=None, metric="wall", names=None):
    out = {"schema": "devkit-compare/1", "metric": metric,
           "arms": list(names) if names else ["before", "after"]}
    out.update(result)
    out["min_effect_source"] = source_line
    out["report_lines"] = ci_report_lines(result)
    out["warnings"] = warnings
    out["meta"] = metas or []
    return out


def load_bench(path):
    data = load_json_file(path, "bench file")
    if not isinstance(data, dict) or data.get("schema") != "devkit-bench/1":
        raise UsageError("%s is not a devkit-bench/1 file; produce one with dev.py bench --out" % path)
    if not data.get("arms"):
        raise UsageError("%s has no arms" % path)
    return data


def pick_arm(data, name, path, position):
    arms = data["arms"]
    if name is None:
        names = list(arms)
        if len(names) <= position:
            raise UsageError("%s has %d arm(s); name them with --before and --after or give "
                             "two files" % (path, len(names)))
        name = names[position]
    if name not in arms:
        raise UsageError("arm %r is not in %s (arms: %s)" % (name, path, ", ".join(arms)))
    return name, arms[name]


def cmd_compare(args):
    p = Parser(
        prog="dev.py compare",
        description="Before/after verdict: bootstrap CI of the median ratio and Mann-Whitney p "
                    "against a minimum effect.")
    p.add_argument("files", nargs="*", help="one bench JSON (two arms) or two bench JSON files")
    p.add_argument("--before", default=None, help="arm name for the before side")
    p.add_argument("--after", default=None, help="arm name for the after side")
    p.add_argument("--metric", choices=("wall", "cpu", "rss"), default="wall",
                   help="which per-run measurement to compare (default wall)")
    p.add_argument("--min-effect", type=float, default=None, help="fraction, e.g. 0.05")
    p.add_argument("--alpha", type=float, default=0.05,
                   help="significance level for the Mann-Whitney p (default 0.05)")
    p.add_argument("--resamples", type=int, default=5000,
                   help="bootstrap resamples for the ratio CI (default 5000)")
    p.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    p.add_argument("--fail-on", default="", help="comma list of slower,inconclusive to exit 1 on")
    p.add_argument("--repo", default=None,
                   help="repository root holding .devkit/aa.json (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if not a.files or len(a.files) > 2:
        raise UsageError("give one bench JSON file (from dev.py bench --out FILE) or two")
    if a.resamples < 100:
        raise UsageError("--resamples must be at least 100")
    fail_on = set(x.strip().upper() for x in a.fail_on.split(",") if x.strip())
    bad = fail_on - {"SLOWER", "INCONCLUSIVE"}
    if bad:
        raise UsageError("--fail-on accepts slower,inconclusive; got %s" % ", ".join(sorted(bad)))
    first = load_bench(a.files[0])
    warnings = list(first.get("warnings", []))
    if len(a.files) == 1:
        bname, barm = pick_arm(first, a.before, a.files[0], 0)
        aname, aarm = pick_arm(first, a.after, a.files[0], 1)
        metas = [first.get("meta", {})]
    else:
        second = load_bench(a.files[1])
        bname, barm = pick_arm(first, a.before, a.files[0], 0)
        aname, aarm = pick_arm(second, a.after, a.files[1], 0)
        metas = [first.get("meta", {}), second.get("meta", {})]
        warnings += second.get("warnings", [])
    before, after = barm.get(a.metric) or [], aarm.get(a.metric) or []
    if not before or not after or any(v is None for v in before + after):
        raise UsageError("metric %s is unavailable (null or missing values) in the chosen arms"
                         % a.metric)
    min_effect, source = resolve_min_effect(a.min_effect, a.repo)
    result = analyze(before, after, min_effect, a.alpha, a.resamples, a.seed)
    warnings += protocol_warnings(result, {"before": barm.get("cpu"), "after": aarm.get("cpu")},
                                  metas)
    for w in warnings:
        warn(w)
    if a.json:
        print_json(compare_json(result, source, warnings, metas, a.metric, (bname, aname)))
    else:
        print_compare(result, source, (bname, aname))
    return 1 if result["verdict"] in fail_on else 0
