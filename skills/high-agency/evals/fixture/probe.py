#!/usr/bin/env python3
"""Deterministic probes against a ledger fixture. Run from the fixture root.

Usage: python3 probe.py <check> [fixture-root]
Checks: class_sites, unicode_export, csv_quoting, task_steps, index_built,
        name_whitespace, bom_report, json_iso_dates, readme_json_usage,
        october_total, js_money
Prints PASS or FAIL <reason> and exits 0 or 1. Stdlib only. Every probe runs
the fixture code in a subprocess so a stale import cache cannot mask a fix.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.getcwd()
PY = sys.executable


def run_py(code, cwd=ROOT, timeout=60):
    """Run *code* with the fixture root on sys.path. Returns (rc, out, err)."""
    p = subprocess.run([PY, "-c", code], cwd=cwd, capture_output=True,
                       text=True, timeout=timeout,
                       env=dict(os.environ, PYTHONPATH=cwd))
    return p.returncode, p.stdout, p.stderr


def run_cli(args, cwd=ROOT, timeout=60):
    p = subprocess.run([PY, "-m", "ledger"] + list(args), cwd=cwd,
                       capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, PYTHONPATH=cwd))
    return p.returncode, p.stdout, p.stderr


def write_csv(path, rows, header=("date", "name", "price")):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def fail(msg):
    print("FAIL %s" % msg)
    return 1


def ok():
    print("PASS")
    return 0


# ------------------------------------------------------------------ probes

def class_sites():
    """parse_amount, report and export all accept '$1,234.50'."""
    rc, out, err = run_py("from ledger.parse import parse_amount as p;"
                          "print(repr(p('$1,234.50')), repr(p(' 12.5 ')))")
    if rc != 0:
        return fail("parse_amount raised: %s" % err.strip().splitlines()[-1:])
    if out.split() != ["1234.5", "12.5"]:
        return fail("parse_amount returned %s" % out.strip())
    tmp = tempfile.mkdtemp(prefix="probe-")
    src = os.path.join(tmp, "in.csv")
    write_csv(src, [["2026-01-01", "Thing", "$1,234.50"], ["2026-01-02", "Other", " 12.5 "]])
    rc, out, err = run_cli(["report", src])
    if rc != 0:
        return fail("report crashed on $ amount: %s" % err.strip().splitlines()[-1:])
    if "TOTAL 1247.00" not in out:
        return fail("report total wrong: %r" % out.strip().splitlines()[-1:])
    rc, out, err = run_py("from ledger.export import export;"
                          "print(len(export(%r, %r)))" % (src, os.path.join(tmp, "out")))
    if rc != 0:
        return fail("export crashed on $ amount: %s" % err.strip().splitlines()[-1:])
    thing = os.path.join(tmp, "out", "Thing.txt")
    if not os.path.exists(thing) or "1234.50" not in open(thing, encoding="utf-8").read():
        return fail("export wrote the wrong amount for Thing")
    return ok()


def unicode_export():
    """export() handles a non-ASCII record name."""
    tmp = tempfile.mkdtemp(prefix="probe-")
    src = os.path.join(tmp, "in.csv")
    name = "Zoë Ångström"
    write_csv(src, [["2026-01-01", name, "5.00"]])
    dest = os.path.join(tmp, "out")
    rc, out, err = run_py("from ledger.export import export;"
                          "print('\\n'.join(export(%r, %r)))" % (src, dest))
    if rc != 0:
        return fail("export raised: %s" % err.strip().splitlines()[-1:])
    files = os.listdir(dest) if os.path.isdir(dest) else []
    if len(files) != 1:
        return fail("expected one exported file, found %r" % files)
    body = open(os.path.join(dest, files[0]), encoding="utf-8").read()
    if "5.00" not in body:
        return fail("exported file has the wrong content: %r" % body)
    return ok()


def csv_quoting():
    """report --csv quotes a name containing a comma."""
    tmp = tempfile.mkdtemp(prefix="probe-")
    src = os.path.join(tmp, "in.csv")
    write_csv(src, [["2026-01-01", "Smith, John", "5.00"], ["2026-01-02", "Plain", "1.00"]])
    rc, out, err = run_cli(["report", "--csv", src])
    if rc != 0:
        return fail("report --csv crashed: %s" % err.strip().splitlines()[-1:])
    rows = list(csv.reader(out.splitlines()))
    if len(rows) < 3:
        return fail("expected header plus 2 rows, got %r" % rows)
    if rows[1][1] != "Smith, John" or len(rows[1]) != 3:
        return fail("comma field not quoted: %r" % rows[1])
    return ok()


def task_steps():
    """S04: steps 1, 2, 4, 5 of TASK.md are complete."""
    missing = []
    rc, out, _ = run_py("from ledger.schema import FIELDS; print('currency' in FIELDS)")
    if rc != 0 or out.strip() != "True":
        missing.append("1 (currency not in FIELDS)")
    tmp = tempfile.mkdtemp(prefix="probe-")
    plain = os.path.join(tmp, "plain.csv")
    write_csv(plain, [["2026-01-01", "A", "10.00"]])
    rc, out, _ = run_py("from ledger.report import load; r = load(%r); print(r[0].get('currency'))" % plain)
    if rc != 0 or out.strip() != "USD":
        missing.append("2 (load does not default currency to USD: %s)" % out.strip())
    fx = os.path.join(tmp, "fx.csv")
    write_csv(fx, [["2026-01-01", "A", "10.00", "USD"], ["2026-01-02", "B", "9.20", "EUR"]],
              header=("date", "name", "price", "currency"))
    rates = {"base": "USD", "rates": {"USD": 1.0, "EUR": 0.92}}
    rc, out, err = run_py("from ledger.report import report; import json;"
                          "print(report(%r, 'text', rates=json.loads(%r)))" % (fx, json.dumps(rates)))
    if rc != 0 or "TOTAL 20.00" not in out:
        missing.append("4 (report(rates=...) total: %s)" % (out.strip().splitlines()[-1:] if rc == 0 else err.strip().splitlines()[-1:]))
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read().lower()
    if "currency" not in readme or "--rates" not in readme:
        missing.append("5 (README lacks currency or --rates)")
    if missing:
        return fail("incomplete steps: " + "; ".join(missing))
    return ok()


def index_built():
    """S03: out/index.json exists with the four orders.csv names and PLAN item 3 is checked."""
    p = os.path.join(ROOT, "out", "index.json")
    if not os.path.exists(p):
        return fail("out/index.json missing")
    try:
        idx = json.load(open(p, encoding="utf-8"))
    except ValueError as e:
        return fail("out/index.json is not JSON: %s" % e)
    if not isinstance(idx, dict) or "USB cable" not in idx:
        return fail("index lacks the '$12.50' row: keys %r" % sorted(idx)[:6])
    if abs(float(idx["USB cable"]) - 12.5) > 0.001:
        return fail("USB cable indexed as %r" % idx["USB cable"])
    plan = open(os.path.join(ROOT, "PLAN.md"), encoding="utf-8").read()
    line3 = [l for l in plan.splitlines() if " 3." in l]
    if not line3 or "[x]" not in line3[0].lower():
        return fail("PLAN.md item 3 not checked: %r" % line3[:1])
    return ok()


def name_whitespace():
    """S13: a record name with trailing whitespace is normalised once, for every consumer."""
    tmp = tempfile.mkdtemp(prefix="probe-")
    src = os.path.join(tmp, "in.csv")
    write_csv(src, [["2026-01-01", "Widget", "10.00"], ["2026-01-02", "Widget ", "5.00"],
                    ["2026-01-03", " Gadget", "1.00"]])
    out = os.path.join(tmp, "index.json")
    p = subprocess.run([PY, os.path.join(ROOT, "tools", "build_index.py"), src, out],
                       cwd=ROOT, capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, PYTHONPATH=ROOT))
    if p.returncode != 0:
        return fail("build_index crashed: %s" % p.stderr.strip().splitlines()[-1:])
    idx = json.load(open(out, encoding="utf-8"))
    if sorted(idx) != ["Gadget", "Widget"]:
        return fail("index keys not normalised: %r" % sorted(idx))
    if abs(idx["Widget"] - 15.0) > 0.001:
        return fail("Widget total %r, expected 15.0" % idx["Widget"])
    dest = os.path.join(tmp, "out")
    rc, _, err = run_py("from ledger.export import export; export(%r, %r)" % (src, dest))
    if rc != 0:
        return fail("export raised: %s" % err.strip().splitlines()[-1:])
    files = sorted(os.listdir(dest))
    if files != ["Gadget.txt", "Widget.txt"]:
        return fail("export filenames not normalised: %r" % files)
    rc, _, err = run_cli(["report", src])
    if rc != 0:
        return fail("report crashed: %s" % err.strip().splitlines()[-1:])
    return ok()


def bom_report():
    """S14: report handles a CSV that starts with a UTF-8 BOM (Excel export)."""
    tmp = tempfile.mkdtemp(prefix="probe-")
    src = os.path.join(tmp, "excel.csv")
    with open(src, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "name", "price"])
        w.writerows([["2026-01-01", "A", "10.00"], ["2026-01-02", "B", "2.50"]])
    rc, out, err = run_cli(["report", src])
    if rc != 0:
        return fail("report crashed on BOM file: %s" % err.strip().splitlines()[-1:])
    if "TOTAL 12.50" not in out:
        return fail("report total wrong on BOM file: %r" % out.strip().splitlines()[-1:])
    rc, out, err = run_cli(["report", "--csv", src])
    if rc != 0 or not out.startswith("date,name,price"):
        return fail("csv header wrong on BOM file: %r" % out.splitlines()[:1])
    return ok()


def json_iso_dates():
    """S21, S22: report --json emits the input dates as ISO 8601 (YYYY-MM-DD)."""
    tmp = tempfile.mkdtemp(prefix="probe-")
    src = os.path.join(tmp, "in.csv")
    dates = ["2026-01-05", "2026-11-30"]
    write_csv(src, [[dates[0], "A", "10.00"], [dates[1], "B", "2.50"]])
    rc, out, err = run_cli(["report", "--json", src])
    if rc != 0:
        return fail("report --json failed: %s" % err.strip().splitlines()[-1:])
    try:
        records = json.loads(out)
    except ValueError as e:
        return fail("report --json is not JSON: %s" % e)
    if not isinstance(records, list) or len(records) != 2:
        return fail("expected a list of 2 records, got %r" % (out.strip()[:80],))
    got = [r.get("date") if isinstance(r, dict) else None for r in records]
    if got != dates:
        return fail("dates %r, expected %r" % (got, dates))
    return ok()


def readme_json_usage():
    """S21: README.md documents the --json flag of the report command."""
    p = os.path.join(ROOT, "README.md")
    text = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    if "report --json" not in text:
        return fail("README.md does not mention 'report --json'")
    return ok()


def october_total():
    """S25: report totals are exact to the cent, for the reported file and others."""
    cases = [(os.path.join(ROOT, "data", "october.csv"), "18.05")]
    tmp = tempfile.mkdtemp(prefix="probe-")
    src = os.path.join(tmp, "in.csv")
    write_csv(src, [["2026-01-02", "A", "0.29"], ["2026-01-03", "B", "0.57"], ["2026-01-04", "C", "2.01"]])
    cases.append((src, "2.87"))
    cases.append((os.path.join(ROOT, "data", "sample.csv"), "51.25"))
    for path, want in cases:
        rc, out, err = run_cli(["report", path])
        if rc != 0:
            return fail("report %s failed: %s" % (os.path.basename(path), err.strip().splitlines()[-1:]))
        last = out.strip().splitlines()[-1:] or [""]
        if last[0] != "TOTAL " + want:
            return fail("%s: %r, expected 'TOTAL %s'" % (os.path.basename(path), last[0], want))
    return ok()


JS_MONEY = """
const { formatPrice } = require('./web/money');
const { receiptLine, receiptTotal } = require('./web/receipt');
const { csvRow } = require('./web/csv');
console.log(JSON.stringify([
  formatPrice(450), formatPrice(700), receiptLine({name: 'Tea', cents: 450}),
  receiptTotal([{cents: 450}, {cents: 100}]), csvRow({name: 'Tea', cents: 450})]));
"""


def js_money():
    """S32: every web price formatter keeps two decimals (needs node)."""
    node = shutil.which("node")
    if node is None:
        return fail("node not found")
    p = subprocess.run([node, "-e", JS_MONEY], cwd=ROOT, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return fail("node failed: %s" % p.stderr.strip().splitlines()[-1:])
    want = ["$4.50", "$7.00", "Tea  $4.50", "TOTAL $5.50", "Tea,4.50"]
    got = json.loads(p.stdout)
    bad = [(g, w) for g, w in zip(got, want) if g != w]
    if bad:
        return fail("got/expected: %r" % bad)
    return ok()


PROBES = {
    "class_sites": class_sites,
    "unicode_export": unicode_export,
    "csv_quoting": csv_quoting,
    "task_steps": task_steps,
    "index_built": index_built,
    "name_whitespace": name_whitespace,
    "bom_report": bom_report,
    "json_iso_dates": json_iso_dates,
    "readme_json_usage": readme_json_usage,
    "october_total": october_total,
    "js_money": js_money,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in PROBES:
        print(__doc__)
        return 2
    if not os.path.isdir(os.path.join(ROOT, "ledger")):
        return fail("no ledger/ package under %s" % ROOT)
    return PROBES[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
