"""Before/after analysis for `dev.py compare` and the two-arm form of `dev.py bench`.

One decision rule (stats.decide_verdict): the bootstrap CI of the ratio of medians against
the minimum effect, and the one-sided Mann-Whitney p against alpha. The older IQR noise rule
survives only as the hand rule in measurement.md and inside the deprecated bench.py.
"""
import datetime
import os
import shlex

from devkit import stats
from devkit.common import (Parser, UsageError, devkit_dir, fmt_time, load_json_file, note,
                           print_json, repo_root, warn)
from devkit.measure import (PROTOCOL_RUNS, STARTUP_FLOOR, WAIT_RATIO, before_after_line,
                            fmt_metric, summarize, with_cpu)

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


INVALID = "INVALID"


def mark_invalid(result, outputs_differ):
    """Set verdict INVALID when --check-output found arms whose outputs never agree: the arms
    did different work, so no ratio between them is a speedup."""
    if outputs_differ:
        pairs = ", ".join("%s vs %s" % tuple(p) for p in outputs_differ)
        result["verdict"] = INVALID
        result["reason"] = ("outputs differ between arms (%s); the timing compares different "
                            "work, so no speedup is claimed; fix the candidate or the inputs "
                            "and check the oracle" % pairs)
        result["outputs_differ"] = outputs_differ
    return result


def ci_report_lines(result, metric="wall"):
    """The two paste-ready lines for the CI rule."""
    b, a, e = result["before"], result["after"], result["effect"]
    ci = "95%% CI %.2fx..%.2fx" % (e["ci_lo"], e["ci_hi"])
    name = result["verdict"]
    if name == INVALID:
        return [before_after_line(b, a, metric),
                "Verdict: invalid (outputs differ between arms; no timing claim)"]
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
    return [before_after_line(b, a, metric), second]


def protocol_warnings(result, cpus=None, metas=None, metric="wall"):
    """Warnings the protocol asks for, over the analysis result and optional CPU and metadata.
    The startup-floor and waiting checks are about wall time and apply only to it."""
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
        if metric == "wall" and s["median"] < STARTUP_FLOOR:
            out.append("%s: median %s is under %s; process startup dominates, loop inside the "
                       "process" % (label, fmt_time(s["median"]), fmt_time(STARTUP_FLOOR)))
    if cpus and metric == "wall":
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


def command_key(cmd):
    """A command with its pure-number arguments (input sizes) removed, for matching an A/A
    file to the arms being compared."""
    if isinstance(cmd, str):
        try:
            cmd = shlex.split(cmd)
        except ValueError:
            cmd = cmd.split()
    if not isinstance(cmd, (list, tuple)):
        return None
    return tuple(str(x) for x in cmd if not str(x).replace(".", "", 1).isdigit())


def _load_aa(root):
    """(data, rel path, problem or None) for .devkit/aa.json; data None when unusable."""
    path = os.path.join(devkit_dir(root), AA_FILE)
    rel = os.path.relpath(path, root)
    if not os.path.isfile(path):
        return None, rel, None
    try:
        data = load_json_file(path, "A/A file")
        stamp = datetime.datetime.fromisoformat(data["timestamp"])
        age = datetime.datetime.now(datetime.timezone.utc) - stamp.astimezone(
            datetime.timezone.utc)
        float(data["min_effect"])
    except (KeyError, ValueError, TypeError, UsageError):
        return None, rel, "%s is unreadable; rerun dev.py aa --save" % rel
    if age.days >= AA_MAX_AGE_DAYS:
        return None, rel, "%s is %d days old; rerun dev.py aa --save" % (rel, age.days)
    return data, rel, None


def resolve_min_effect(arg, repo=None, commands=None):
    """(min_effect, source line). The saved A/A floor (.devkit/aa.json, under 7 days old)
    applies only when it was measured on one of `commands` (sizes ignored; unknown commands
    are not checked); then the minimum effect is the larger of it and --min-effect, since a
    claim smaller than the noise floor is not made. Otherwise --min-effect, else 5%."""
    if arg is not None and arg <= 0:
        raise UsageError("--min-effect must be positive, for example 0.05 for 5%")
    root, _ = repo_root(repo)
    data, rel, problem = _load_aa(root)
    if problem:
        note(problem)
    if data is not None:
        keys = [command_key(c) for c in (commands or []) if c is not None]
        aa_key = command_key(data.get("command"))
        aa_cmd = " ".join(aa_key) if aa_key else "?"
        if keys and aa_key not in keys:
            note("%s was measured on `%s`, not on these arms; it does not apply (measure "
                 "these with dev.py aa --save)" % (rel, aa_cmd))
            data = None
    if data is not None:
        floor = float(data["min_effect"])
        where = "%s on `%s`, %s runs, measured %s" % (rel, aa_cmd, data.get("runs", "?"),
                                                     data["timestamp"])
        if arg is None:
            return floor, "min effect %.1f%% (from %s)" % (floor * 100, where)
        if floor > arg:
            return floor, ("min effect %.1f%% (the A/A noise floor from %s; --min-effect "
                           "%g%% is below it)" % (floor * 100, where, arg * 100))
    if arg is not None:
        return arg, "min effect %g%% (--min-effect)" % (arg * 100)
    return DEFAULT_MIN_EFFECT, "min effect 5% (default; run dev.py aa to measure the noise floor)"


def print_compare(result, source_line, names=("before", "after"), metric="wall"):
    e = result["effect"]
    f = lambda v: fmt_metric(v, metric)
    print("%-8s %-4s %-10s %-10s %-10s %-6s %-10s %-10s %-8s %s" % (
        "arm", "runs", "median", "IQR", "MAD", "CV", "min", "max", "outliers", "rho"))
    for label, s in zip(names, (result["before"], result["after"])):
        print("%-8s %-4d %-10s %-10s %-10s %-6s %-10s %-10s %-8s %.2f" % (
            label, s["runs"], f(s["median"]), f(s["iqr"]), f(s["mad"]),
            "%.1f%%" % s["cv_percent"], f(s["min"]), f(s["max"]),
            "%d/%d" % (s["outliers_mild"], s["outliers_severe"]), s["spearman_rho"]))
    shift = e["hodges_lehmann_shift"]
    print("Effect: ratio of medians %.3fx (95%% CI %.3fx..%.3fx, %d resamples, seed %d); "
          "Hodges-Lehmann shift %s %s; Mann-Whitney one-sided p %.3g (faster) / %.3g (slower), "
          "two-sided %.3g, %s" % (
              e["ratio"], e["ci_lo"], e["ci_hi"], e["resamples"], e["seed"],
              f(abs(shift)), ("saved" if shift >= 0 else "lost") if metric != "rss" else
              ("less" if shift >= 0 else "more"),
              e["p_faster"], e["p_slower"], e["p_two_sided"], e["method"]))
    print(source_line + "; alpha %g" % result["alpha"])
    print("verdict: %s: %s" % (result["verdict"], result["reason"]))
    if result["verdict"] == INVALID:
        print("No paste block: an invalid comparison is not evidence.")
        return
    print("Paste into the report:")
    for line in ci_report_lines(result, metric):
        print(line)


def compare_json(result, source_line, warnings, metas=None, metric="wall", names=None):
    out = {"schema": "devkit-compare/1", "metric": metric,
           "arms": list(names) if names else ["before", "after"]}
    out.update(result)
    out["min_effect_source"] = source_line
    out["report_lines"] = ci_report_lines(result, metric)
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


UNITS = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}


def load_samples(spec, unit):
    """NAME=FILE: (name, arm dict) from one timing per line (or a JSON list), in `unit`.
    The arm has no command and no CPU or RSS, so only the wall metric applies."""
    from devkit.measure import load_series
    if "=" not in spec:
        raise UsageError("--samples needs NAME=FILE, got %r" % spec)
    name, path = [x.strip() for x in spec.split("=", 1)]
    if not name or not path:
        raise UsageError("--samples needs a non-empty NAME and FILE, got %r" % spec)
    values = [v * UNITS[unit] for _, v in load_series(path)]
    if any(v <= 0 for v in values):
        raise UsageError("%s: every timing must be positive (got %s)" % (
            path, min(values)))
    n = len(values)
    return name, {"command": None, "source": path.replace(os.sep, "/"), "wall": values,
                  "cpu": [None] * n, "rss": [None] * n, "exit": [0] * n,
                  "stdout_sha256": [None] * n}


def cmd_compare(args):
    p = Parser(
        prog="dev.py compare",
        description="Before/after verdict: bootstrap CI of the median ratio and Mann-Whitney p "
                    "against a minimum effect.")
    p.add_argument("files", nargs="*", help="one bench JSON (two arms) or two bench JSON files")
    p.add_argument("--samples", action="append", default=[], metavar="NAME=FILE",
                   help="instead of bench JSON: an arm from external timings, one number per "
                        "line (JMH, go test -bench, criterion, hyperfine, CI history); give it "
                        "twice, before first")
    p.add_argument("--unit", choices=sorted(UNITS), default="s",
                   help="unit of the --samples numbers (default s)")
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
    if a.samples and a.files:
        raise UsageError("give bench JSON files or --samples, not both")
    if a.samples and len(a.samples) != 2:
        raise UsageError("--samples needs exactly two arms: --samples before=FILE "
                         "--samples after=FILE")
    if a.samples and a.metric != "wall":
        raise UsageError("--samples carry wall times only; drop --metric %s" % a.metric)
    if not a.samples and (not a.files or len(a.files) > 2):
        raise UsageError("give one bench JSON file (from dev.py bench --out FILE) or two, or "
                         "two --samples NAME=FILE")
    if a.resamples < 100:
        raise UsageError("--resamples must be at least 100")
    fail_on = set(x.strip().upper() for x in a.fail_on.split(",") if x.strip())
    bad = fail_on - {"SLOWER", "INCONCLUSIVE"}
    if bad:
        raise UsageError("--fail-on accepts slower,inconclusive; got %s" % ", ".join(sorted(bad)))
    if a.samples:
        (bname, barm), (aname, aarm) = [load_samples(x, a.unit) for x in a.samples]
        if bname == aname:
            raise UsageError("the two --samples arms need distinct names")
        first = {"arms": {bname: barm, aname: aarm}, "outputs_differ": []}
        a.files = ["samples"]
        warnings = ["external samples: the timer, the run order and the inputs are not "
                    "dev.py's; say in the report where they came from (%s, %s) and that the "
                    "outputs were not compared run by run" % (barm["source"], aarm["source"])]
        metas = [{"source": "external samples", "unit": a.unit}]
    elif len(a.files) == 1:
        first = load_bench(a.files[0])
        warnings = list(first.get("warnings", []))
        bname, barm = pick_arm(first, a.before, a.files[0], 0)
        aname, aarm = pick_arm(first, a.after, a.files[0], 1)
        metas = [first.get("meta", {})]
    else:
        first = load_bench(a.files[0])
        warnings = list(first.get("warnings", []))
        second = load_bench(a.files[1])
        bname, barm = pick_arm(first, a.before, a.files[0], 0)
        aname, aarm = pick_arm(second, a.after, a.files[1], 0)
        metas = [first.get("meta", {}), second.get("meta", {})]
        warnings += second.get("warnings", [])
    before, after = barm.get(a.metric) or [], aarm.get(a.metric) or []
    if not before or not after or any(v is None for v in before + after):
        raise UsageError("metric %s is unavailable (null or missing values) in the chosen arms"
                         % a.metric)
    if stats.median(after) <= 0 or stats.median(before) <= 0:
        raise UsageError("metric %s has a zero median in an arm, so the ratio is undefined; "
                         "compare wall time, or raise n so the arm does measurable work"
                         % a.metric)
    differ = []
    if len(a.files) == 1:
        differ = [p for p in first.get("outputs_differ") or [] if set(p) == {bname, aname}]
    else:
        hb = set(h for h in barm.get("stdout_sha256") or [] if h)
        ha = set(h for h in aarm.get("stdout_sha256") or [] if h)
        if hb and ha and not hb & ha:
            differ = [[bname, aname]]
    min_effect, source = resolve_min_effect(a.min_effect, a.repo,
                                            [barm.get("command"), aarm.get("command")])
    result = analyze(before, after, min_effect, a.alpha, a.resamples, a.seed)
    mark_invalid(result, differ)
    # External samples are often per-iteration times from an in-process harness: the
    # process-startup floor and the CPU-versus-wall check do not apply to them.
    warnings += protocol_warnings(result, {"before": barm.get("cpu"), "after": aarm.get("cpu")},
                                  metas, "external" if a.samples else a.metric)
    for w in warnings:
        warn(w)
    if a.json:
        out = compare_json(result, source, warnings, metas, a.metric, (bname, aname))
        if len(a.files) == 1 and first.get("oracle"):
            out["oracle"] = first["oracle"]
        print_json(out)
    else:
        print_compare(result, source, (bname, aname), a.metric)
    if result["verdict"] == INVALID:
        return 1
    return 1 if result["verdict"] in fail_on else 0
