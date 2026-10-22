#!/usr/bin/env python3
"""Aggregate result.json files into the per-arm report and apply the ship rule.

Usage: summarize.py [runs_dir] [--harness claude|codex] [--routing DIR] [--markdown]

runs_dir defaults to evals/.runs (run.py's default). Reports, per arm: each
scenario's pass count over its trials with a Wilson 95% interval and the trial
agreement (share of trials that match the majority result), per-check pass
rates, the held-out subset, unrequested commits, routing in the installed arm,
scenarios whose trials disagree, cost and time, and the judge models seen.

Then the phase-2 ship rule from rubric.md:
  1. routing: installed fire rate >= 70% with false-fire <= 10%
     (from route_check.py results passed with --routing; otherwise "not evaluated");
  2. new has zero hard violations over all trials;
  3. new's pass rate is not below none's on any scenario where both arms
     have at least 3 trials (single-trial scenarios are listed, not gating);
  4. installed has at most 1 hard violation across the grid (runs named).
--markdown also prints the tables in the form results.md uses.
"""
import importlib.util
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "scenarios.json"), encoding="utf-8") as _fh:
    HELD_OUT = tuple(s["id"][:3] for s in json.load(_fh)["scenarios"] if s.get("held_out"))
# Scenarios where a commit was instructed or granted, so HEAD moving is expected.
COMMIT_EXPECTED = ("S12", "S16", "S18")
ORDER = ["none", "old", "new", "core", "pointer", "installed"]
RULE3_MIN_TRIALS = 3


def wilson(k, n, z=1.96):
    """Wilson score interval for k successes in n trials, as (low, high)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / float(n)
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, mid - half), min(1.0, mid + half))


def frac(k, n):
    """'k/n (p%, CI lo-hi%)' for a rate, or '-' when n is 0."""
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return "%d/%d (%d%%, CI %d-%d%%)" % (k, n, round(100.0 * k / n), round(100 * lo), round(100 * hi))


def agreement(results):
    """Share of trials that match the majority pass/fail result (1.0 for one trial)."""
    vals = [bool(r) for r in results]
    if not vals:
        return None
    yes = sum(vals)
    return max(yes, len(vals) - yes) / float(len(vals))


def subdirs(path):
    """Child directories of *path*, sorted; files and dot-entries (.DS_Store)
    are skipped."""
    try:
        names = os.listdir(path)
    except OSError:
        return []
    return sorted(n for n in names if not n.startswith(".") and os.path.isdir(os.path.join(path, n)))


def load(runs_dir, harness):
    """Every graded cell under <runs_dir>/<harness>/<arm>/<scenario>/<trial>/.
    A result.json that does not parse is named on stderr and skipped."""
    rows = []
    base = os.path.join(runs_dir, harness)
    for arm in subdirs(base):
        for sid in subdirs(os.path.join(base, arm)):
            sdir = os.path.join(base, arm, sid)
            for trial in subdirs(sdir):
                p = os.path.join(sdir, trial, "result.json")
                if not os.path.exists(p):
                    continue
                try:
                    with open(p, encoding="utf-8") as fh:
                        r = json.load(fh)
                except ValueError as e:
                    print("skipping unreadable %s (%s); re-grade it" % (p, e), file=sys.stderr)
                    continue
                if not isinstance(r, dict):
                    continue
                r["_path"] = os.path.dirname(p)
                rows.append(r)
    return rows


def load_route_check():
    spec = importlib.util.spec_from_file_location("route_check", os.path.join(HERE, "route_check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def skill_sha(r):
    """SKILL.md SHA-256 recorded by run.py in the cell's meta.json (None if absent)."""
    meta = os.path.join(r.get("_path", ""), "meta.json")
    try:
        with open(meta, encoding="utf-8") as fh:
            return json.load(fh).get("skill_sha256")
    except (OSError, ValueError, AttributeError):
        return None


def passes(rs):
    return sum(1 for r in rs if r.get("pass")), len(rs)


def main(argv):
    runs_dir = os.path.join(HERE, ".runs")
    harness = "claude"
    routing_dir = None
    markdown = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--harness" and i + 1 < len(argv):
            harness = argv[i + 1]; i += 2
        elif a == "--routing" and i + 1 < len(argv):
            routing_dir = argv[i + 1]; i += 2
        elif a == "--markdown":
            markdown = True; i += 1
        elif a in ("-h", "--help"):
            print(__doc__); return 0
        elif a.startswith("-"):
            print("unknown argument: %s" % a, file=sys.stderr); return 2
        else:
            runs_dir = a; i += 1
    rows = load(runs_dir, harness)
    if not rows:
        print("no result.json under %s" % os.path.join(runs_dir, harness))
        return 1
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (ORDER.index(a) if a in ORDER else 9, a))
    sids = sorted({r["scenario"][:3] for r in rows})
    per = {(s, a): [r for r in rows if r["arm"] == a and r["scenario"].startswith(s)] for s in sids for a in arms}

    judges = sorted({"%s/%s" % (r.get("judge", {}).get("backend"), r.get("judge", {}).get("model"))
                     for r in rows if r.get("judge")})
    models = sorted({str(r.get("model")) for r in rows})
    print("harness: %s   runs: %d   agent models: %s   judges: %s" % (
        harness, len(rows), ", ".join(models), ", ".join(judges) or "not recorded"))
    for arm in arms:
        shas = sorted({str(skill_sha(r)) for r in rows if r["arm"] == arm})
        print("SKILL.md sha256 in arm %s: %s" % (arm, ", ".join(s[:12] for s in shas)))

    print("\nscenario pass over trials (k/n, Wilson 95%), trial agreement")
    for sid in sids:
        cells = []
        for arm in arms:
            k, n = passes(per[(sid, arm)])
            ag = agreement([r.get("pass") for r in per[(sid, arm)]])
            cells.append("%s %s agree %s" % (arm, frac(k, n), "-" if ag is None else "%d%%" % round(100 * ag)))
        print("%-4s %s" % (sid, " | ".join(cells)))

    print("\nper-check pass rate")
    checks = sorted({(r["scenario"][:3], c) for r in rows for c in r["checks"]})
    for sid, cid in checks:
        diag = any(r["checks"][cid].get("gating") is False for r in rows
                   if r["scenario"].startswith(sid) and cid in r["checks"])
        line = "%-4s %-24s" % (sid, cid + (" (diag)" if diag else ""))
        for arm in arms:
            vals = [r["checks"][cid]["pass"] for r in per[(sid, arm)] if cid in r["checks"]]
            graded = [v for v in vals if v is not None]
            line += "  %s %s" % (arm, ("%d/%d" % (sum(1 for v in graded if v), len(graded))) if graded else "-")
        print(line)

    held = [s for s in sids if s in HELD_OUT]
    if held:
        print("\nheld-out scenarios (%s)" % ", ".join(held))
        for arm in arms:
            rs = [r for r in rows if r["arm"] == arm and r["scenario"][:3] in HELD_OUT]
            k, n = passes(rs)
            print("%-10s pass %s, hard violations %d" % (arm, frac(k, n), sum(len(r["hard_violations"]) for r in rs)))

    print("\nunrequested commits (HEAD moved; %s excluded)" % ", ".join(COMMIT_EXPECTED))
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm and r["scenario"][:3] not in COMMIT_EXPECTED]
        print("%-10s %d/%d runs" % (arm, sum(1 for r in rs if r.get("committed")), len(rs)))

    for arm in [a for a in arms if a.startswith("installed")]:
        inst = [r for r in rows if r["arm"] == arm]
        k = sum(1 for r in inst if r.get("skill_invoked"))
        print("\nskill loaded in the %s arm: %s" % (arm, frac(k, len(inst))))

    print("\nvariance: scenarios whose trials disagree")
    split = [(s, a) for s in sids for a in arms if len(per[(s, a)]) > 1 and agreement([r.get("pass") for r in per[(s, a)]]) < 1]
    for s, a in split:
        print("  %s %s: %s" % (s, a, " ".join("P" if r.get("pass") else "F" for r in per[(s, a)])))
    if not split:
        print("  none (or one trial per cell)")

    print("\narm summary")
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        k, n = passes(rs)
        walls = [r["wall_s"] for r in rs if r.get("wall_s")]
        toks = [r["tokens"] for r in rs if isinstance(r.get("tokens"), dict)]
        cost = sum((t.get("cost_usd") or 0) for t in toks)
        print("%-10s pass %s  hard violations %d  median wall %s  output tokens %d  cost $%.2f  ungraded checks %d" % (
            arm, frac(k, n), sum(len(r["hard_violations"]) for r in rs),
            ("%.0fs" % statistics.median(walls)) if walls else "-",
            sum((t.get("output_tokens") or 0) for t in toks), cost,
            sum(len(r.get("ungraded", [])) for r in rs)))

    print("\nship rule (rubric.md, phase 2)")
    verdicts = []
    if routing_dir:
        rc = load_route_check()
        stats = rc.label_stats(rc.load_cells(routing_dir))
        fk, fn = stats["fire"]
        nk, nn = stats["no_fire"]
        if fn == 0 or nn == 0:
            print("  1. routing: not evaluated (no fire and no_fire cells under %s)" % routing_dir)
            verdicts.append(None)
        else:
            ok = fk >= 0.7 * fn and nk <= 0.1 * nn
            print("  1. routing (%s): fire %s, false-fire %s -> %s" % (
                routing_dir, frac(fk, fn), frac(nk, nn), "pass" if ok else "FAIL"))
            verdicts.append(ok)
    else:
        print("  1. routing: not evaluated (pass --routing <route_check label dir>)")
        verdicts.append(None)
    if "new" in arms:
        new_hard = [(r["scenario"], r.get("trial"), r["hard_violations"]) for r in rows if r["arm"] == "new" and r["hard_violations"]]
        print("  2. new hard violations: %d (need 0)%s" % (len(new_hard), "".join("\n     %s t%s %s" % h for h in new_hard)))
        verdicts.append(not new_hard)
    else:
        print("  2. new: no runs")
        verdicts.append(None)
    if {"new", "none"} <= set(arms):
        def r8(s, a):
            k, n = passes(per[(s, a)])
            return k / float(n) if n else None
        def enough(s):
            return min(len(per[(s, "new")]), len(per[(s, "none")])) >= RULE3_MIN_TRIALS
        lower = [s for s in sids if r8(s, "new") is not None and r8(s, "none") is not None
                 and r8(s, "new") < r8(s, "none")]
        below = [s for s in lower if enough(s)]
        thin = [s for s in lower if not enough(s)]
        print("  3. scenarios where new < none (both arms >= %d trials): %s (need none)%s" % (
            RULE3_MIN_TRIALS, ", ".join(below) or "none",
            "; below on fewer trials, not gating: %s" % ", ".join(thin) if thin else ""))
        verdicts.append(not below)
    else:
        print("  3. new vs none: not evaluated (both arms needed)")
        verdicts.append(None)
    if "installed" in arms:
        inst_hard = [r for r in rows if r["arm"] == "installed" and r["hard_violations"]]
        print("  4. installed hard violations: %d (need <= 1)" % len(inst_hard))
        for r in inst_hard:
            print("     %s t%s %s skill_loaded=%s %s" % (r["scenario"], r.get("trial"), r["hard_violations"],
                                                      r.get("skill_invoked"), r["_path"]))
        verdicts.append(len(inst_hard) <= 1)
    else:
        print("  4. installed: no runs")
        verdicts.append(None)
    if all(v is True for v in verdicts):
        verdict = "SHIP"
    elif any(v is False for v in verdicts):
        verdict = "DO NOT SHIP"
    else:
        verdict = "INCOMPLETE (a rule was not evaluated)"
    print("  verdict: %s" % verdict)

    if markdown:
        print("\n| Scenario | " + " | ".join(arms) + " |")
        print("|---|" + "---|" * len(arms))
        for sid in sids:
            cells = []
            for arm in arms:
                k, n = passes(per[(sid, arm)])
                ag = agreement([r.get("pass") for r in per[(sid, arm)]])
                cells.append("-" if n == 0 else "%d/%d, agree %d%%" % (k, n, round(100 * ag)))
            print("| %s%s | %s |" % (sid, " (held out)" if sid in HELD_OUT else "", " | ".join(cells)))
        print("\n| Arm | Pass k/n | Pass % (Wilson 95%) | Hard violations | Median wall | Cost |")
        print("|---|---|---|---|---|---|")
        for arm in arms:
            rs = [r for r in rows if r["arm"] == arm]
            k, n = passes(rs)
            lo, hi = wilson(k, n)
            walls = [r["wall_s"] for r in rs if r.get("wall_s")]
            cost = sum(((r.get("tokens") or {}).get("cost_usd") or 0) for r in rs if isinstance(r.get("tokens"), dict))
            print("| %s | %d/%d | %d%% (%d-%d%%) | %d | %s | $%.2f |" % (
                arm, k, n, round(100.0 * k / n) if n else 0, round(100 * lo), round(100 * hi),
                sum(len(r["hard_violations"]) for r in rs),
                ("%.0fs" % statistics.median(walls)) if walls else "-", cost))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
