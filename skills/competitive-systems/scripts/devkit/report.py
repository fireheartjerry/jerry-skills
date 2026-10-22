"""`dev.py report`: fill references/report-template.md from measurement JSON.

Filled from files: Before and Verdict (a devkit-compare/1 file, or a devkit-bench/1 file that
holds a `compare` object), Class (two devkit-scale/1 files), Noise floor (a saved A/A file),
Oracle (runs --oracle-cmd and records its exit code and last output line), Card and Card
facts (--set card=<id>, looked up in the technique catalog), Environment and Protocol (the
compare metadata). Every other field comes from --set FIELD=TEXT or stays `<...>` and is
named under `Not verified:`. A ratio is never printed without the two absolute medians.
"""
import os
import re
import subprocess

from devkit import catalog
from devkit.common import (Parser, UsageError, fmt_time, load_json_file, note, print_json,
                           warn)
from devkit.measure import fmt_metric

PLACEHOLDER = re.compile(r"<[^<>]*>")


def template_path():
    return os.path.join(os.path.dirname(catalog.catalog_path()), "report-template.md")


def load_template(path=None):
    """[(field, template line)] from the first fenced block of report-template.md."""
    path = path or template_path()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise UsageError("cannot read the report template %s: %s" % (path, e.strerror or e))
    m = re.search(r"^```[a-z]*\n(.*?)^```", text, re.S | re.M)
    if not m:
        raise UsageError("report template %s has no fenced block" % path)
    out = []
    for line in m.group(1).splitlines():
        if ":" in line:
            out.append((line.split(":", 1)[0].strip(), line))
    return out


def norm(field):
    return re.sub(r"[^a-z0-9]", "", field.lower())


def load_compare(path):
    data = load_json_file(path, "compare file")
    if isinstance(data, dict) and data.get("schema") == "devkit-bench/1" and data.get("compare"):
        data = data["compare"]
    if not isinstance(data, dict) or data.get("schema") != "devkit-compare/1":
        raise UsageError("%s is not a devkit-compare/1 file; write one with "
                         "dev.py compare bench.json --json > compare.json" % path)
    for key in ("before", "after", "effect", "verdict"):
        if key not in data:
            raise UsageError("%s lacks %r; regenerate it with dev.py compare --json" % (path, key))
    return data


def before_line(c):
    b, a = c["before"], c["after"]
    f = lambda v: fmt_metric(v, c.get("metric", "wall"))
    return "Before: median %s (IQR %s, runs=%d); After: median %s (IQR %s, runs=%d)." % (
        f(b["median"]), f(b["iqr"]), b["runs"], f(a["median"]), f(a["iqr"]), a["runs"])


def verdict_line(c):
    e, name = c["effect"], c["verdict"]
    f = lambda v: fmt_metric(v, c.get("metric", "wall"))
    medians = "medians %s -> %s" % (f(c["before"]["median"]), f(c["after"]["median"]))
    if name == "INVALID":
        return "Verdict: invalid: outputs differ between arms, so no speedup is claimed (%s)." % (
            medians)
    ci = "95%% CI %.2fx..%.2fx" % (e["ci_lo"], e["ci_hi"])
    if name == "FASTER":
        return "Verdict: %.2fx faster (%s, p=%.3g; %s)." % (e["ratio"], ci, c["p"], medians)
    if name == "SLOWER":
        return "Verdict: %.2fx slower (%s, p=%.3g; %s)." % (1.0 / e["ratio"], ci, c["p"], medians)
    if name == "NO DETECTABLE CHANGE":
        return "Verdict: no detectable change (ratio %.2fx, %s, min effect %.3g%%; %s)." % (
            e["ratio"], ci, c["min_effect"] * 100, medians)
    return "Verdict: inconclusive (ratio %.2fx, %s, p=%.3g; %s); add runs." % (
        e["ratio"], ci, c["p"], medians)


def load_scale(path):
    data = load_json_file(path, "scale file")
    if not isinstance(data, dict) or data.get("schema") != "devkit-scale/1":
        raise UsageError("%s is not a devkit-scale/1 file; write one with dev.py scale ... --json"
                         % path)
    return data


def exponent_text(s):
    if s.get("exponent") is None:
        return "not fitted"
    ci = s.get("exponent_ci")
    sizes = [r["n"] for r in s.get("sizes", [])]
    span = " at n=%d..%d" % (min(sizes), max(sizes)) if sizes else ""
    if ci:
        return "%.2f (95%% CI %.2f..%.2f%s)" % (s["exponent"], ci[0], ci[1], span)
    return "%.2f%s" % (s["exponent"], span)


def class_line(before, after):
    """The Class line; says plainly when the exponent did not drop (a constant-factor gain)
    and when the two fits cover different sizes."""
    text = "Class: exponent %s -> %s" % (exponent_text(before), exponent_text(after))
    notes = []
    cb, ca = before.get("exponent_ci"), after.get("exponent_ci")
    eb, ea = before.get("exponent"), after.get("exponent")
    if cb and ca and not ca[0] < cb[1]:
        notes.append("the exponent did not drop: a constant-factor gain, not an algorithmic one")
    elif not (cb and ca) and eb is not None and ea is not None and ea >= eb - 0.2:
        notes.append("the exponent did not clearly drop: a constant-factor gain, not an "
                     "algorithmic one")
    sb = [r["n"] for r in before.get("sizes", [])]
    sa = [r["n"] for r in after.get("sizes", [])]
    if sb and sa and (min(sb), max(sb)) != (min(sa), max(sa)):
        notes.append("the two fits cover different sizes")
    return text + ("; " + "; ".join(notes) if notes else "") + "."


def noise_line(path):
    data = load_json_file(path, "A/A file")
    try:
        lo, hi = data["ci"]
        runs, eff = data["runs"], data["min_effect"]
    except (KeyError, TypeError, ValueError):
        raise UsageError("%s is not an A/A file (needs ci, runs, min_effect); write one with "
                         "dev.py aa --save or dev.py aa --json" % path)
    return "Noise floor: A/A ratio CI %.3f..%.3f on %d runs; min effect %.1f%%." % (
        lo, hi, runs, eff * 100)


def run_oracle(cmd, timeout=600):
    """(line, passed) after running cmd through the shell."""
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "Oracle: `%s`: timed out after %d s (fail)." % (cmd, timeout), False
    lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    last = lines[-1] if lines else "(no output)"
    status = "pass" if p.returncode == 0 else "FAIL"
    return "Oracle: `%s`: exit %d (%s); last line: %s." % (cmd, p.returncode, status, last), \
        p.returncode == 0


def environment_line(c):
    metas = c.get("meta") or []
    if not metas:
        return None
    m = metas[0]
    if not m.get("python"):
        return None                      # hand-built JSON (CI history): fill with --set
    rev = m.get("rev")
    git = "git %s%s" % (rev[:7], " (dirty)" if m.get("dirty") else "") if rev else "no git revision"
    load = m.get("loadavg")
    return "Environment: Python %s on %s; %s CPUs; load %s; %s." % (
        m.get("python", "?"), m.get("platform", "?"), m.get("cpu_count", "?"),
        "%.1f" % load[0] if load else "n/a", git)


def protocol_line(c):
    e = c["effect"]
    return "Protocol: metric %s; runs %d/%d; %s; alpha %g; %d bootstrap resamples, seed %d; " \
           "Mann-Whitney %s." % (
               c.get("metric", "wall"), c["before"]["runs"], c["after"]["runs"],
               c.get("min_effect_source", "min effect %.3g%%" % (c["min_effect"] * 100)),
               c.get("alpha", 0.05), e.get("resamples", 0), e.get("seed", 0), e.get("method", "?"))


def card_lines(ident):
    entry = catalog.find_entry(ident)
    if entry is None:
        raise UsageError("unknown card id %r; list them with dev.py catalog --list" % ident)
    card = "Card: %s (%s). Precondition: <test and result>." % (entry["title"], entry["file"])
    facts = "Card facts: %s." % ("; ".join(entry["facts"]) if entry["facts"]
                                 else "none listed for this card")
    return card, facts


def parse_sets(items, fields):
    """{normalised field: text} from FIELD=TEXT items; the 'card' key is kept for the catalog."""
    known = dict((norm(f), f) for f in fields)
    out = {}
    for item in items:
        if "=" not in item:
            raise UsageError("--set needs FIELD=TEXT, got %r" % item)
        key, text = item.split("=", 1)
        k = norm(key)
        if k not in known:
            raise UsageError("--set: unknown field %r; fields are: %s" % (
                key, ", ".join(fields)))
        out[k] = text.strip()
    return out


def build(template, compare=None, scale_before=None, scale_after=None, aa=None,
          oracle_cmd=None, sets=None):
    """(lines, unfilled field names, oracle_passed or None)."""
    filled = {}
    oracle_ok = None
    if compare is not None:
        filled["before"] = before_line(compare)
        filled["verdict"] = verdict_line(compare)
        env = environment_line(compare)
        if env:
            filled["environment"] = env
        filled["protocol"] = protocol_line(compare)
    if scale_before is not None and scale_after is not None:
        filled["class"] = class_line(scale_before, scale_after)
    if aa:
        filled["noisefloor"] = noise_line(aa)
    if oracle_cmd:
        filled["oracle"], oracle_ok = run_oracle(oracle_cmd)
    elif compare is not None and isinstance(compare.get("oracle"), dict):
        o = compare["oracle"]
        filled["oracle"] = ("Oracle: `%s`: exit %d (pass, run by bench before timing); last "
                            "line: %s." % (o.get("command"), o.get("exit", 0),
                                           o.get("last_line")))
    sets = dict(sets or {})
    if "card" in sets:
        value = sets.pop("card")
        if catalog.find_entry(value) is not None or re.match(r"^[a-z0-9-]+$", value):
            filled["card"], filled["cardfacts"] = card_lines(value)
        else:
            filled["card"] = "Card: %s" % value
    precondition = sets.pop("precondition", None)
    if precondition is not None:
        card = filled.get("card", "Card: <name> (<file>). Precondition: <test and result>.")
        filled["card"] = card.replace("<test and result>", precondition)
    extra_not_verified = sets.pop("notverified", None)
    for k, text in sets.items():
        filled[k] = text
    lines, unfilled = [], []
    for field, tline in template:
        k = norm(field)
        if k == "notverified":
            continue
        text = filled.get(k, tline)
        if not text.lower().startswith(field.lower() + ":"):
            text = "%s: %s" % (field, text)
        if PLACEHOLDER.search(text):
            unfilled.append(field)
        lines.append(text)
    parts = []
    if unfilled:
        parts.append("%s (still <...>: fill each or say why it was not run)" % ", ".join(unfilled))
    if compare is not None and "default" in compare.get("min_effect_source", ""):
        parts.append("noise floor not measured (compare used the default 5% minimum effect)")
    if oracle_ok is False:
        parts.append("the oracle failed, so the timing is not evidence")
    if compare is not None and compare.get("verdict") == "INVALID":
        parts.append("bench found the arms' outputs differ, so the timing is not evidence")
    if extra_not_verified:
        parts.append(extra_not_verified)
    lines.append("Not verified: %s." % ("; ".join(parts) if parts else "nothing listed"))
    return lines, unfilled, oracle_ok


def cmd_report(args):
    p = Parser(prog="dev.py report",
               description="Fill the report template from compare, scale and A/A JSON and an "
                           "oracle command; unfilled fields are listed under Not verified.")
    p.add_argument("--compare", default=None, help="devkit-compare/1 JSON (dev.py compare --json)")
    p.add_argument("--scale-before", default=None, help="devkit-scale/1 JSON for the before arm")
    p.add_argument("--scale-after", default=None, help="devkit-scale/1 JSON for the after arm")
    p.add_argument("--oracle-cmd", default=None, help="command whose exit code decides the oracle")
    p.add_argument("--aa", default=None, help="A/A JSON (dev.py aa --save writes .devkit/aa.json)")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="FIELD=TEXT",
                   help="fill a field by hand; card=<id> fills Card and Card facts, "
                        "precondition=TEXT fills the Card line's precondition")
    p.add_argument("--out", default=None, help="write the report here as well as stdout")
    p.add_argument("--json", action="store_true",
                   help="print one JSON object (the report text, unfilled fields, oracle result)")
    a = p.parse_args(args)
    if bool(a.scale_before) != bool(a.scale_after):
        raise UsageError("--scale-before and --scale-after go together")
    if not (a.compare or a.sets or a.oracle_cmd or a.aa or a.scale_before):
        raise UsageError("give --compare FILE and any of --scale-before/--scale-after, --aa, "
                         "--oracle-cmd, --set FIELD=TEXT")
    template = load_template()
    compare = load_compare(a.compare) if a.compare else None
    sb = load_scale(a.scale_before) if a.scale_before else None
    sa = load_scale(a.scale_after) if a.scale_after else None
    sets = parse_sets(a.sets, [f for f, _ in template] + ["precondition"])
    lines, unfilled, oracle_ok = build(template, compare, sb, sa, a.aa, a.oracle_cmd, sets)
    if compare is not None:
        for w in compare.get("warnings", []):
            warn(w)
    text = "\n".join(lines) + "\n"
    if a.json:
        print_json({"schema": "devkit-report/1", "report": text, "unfilled": unfilled,
                    "oracle_ok": oracle_ok, "out": a.out})
    else:
        print(text, end="")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        note("wrote %s" % a.out)
    if unfilled:
        note("%d field(s) still <...>: %s" % (len(unfilled), ", ".join(unfilled)))
    return 1 if oracle_ok is False else 0
