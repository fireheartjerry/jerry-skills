#!/usr/bin/env python3
"""Deprecated: kept so existing scripts keep working. New work uses dev.py bench, compare and
scale, whose verdict (a bootstrap CI against a measured noise floor) replaces this one.

Times commands with warm-up, several runs, median and IQR, CPU time next to wall time,
interleaved A/B, the IQR noise-rule verdict, and a doubling-test exponent fitted after the
process-startup baseline is subtracted. The engine lives in devkit.

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

New work should use dev.py (bench, compare, aa, scale, bound, changepoint).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from devkit.common import DevError, UsageError, fmt_time  # noqa: E402,F401
from devkit.legacy import legacy_compare, legacy_run, legacy_scale  # noqa: E402
from devkit.measure import (BASELINE_RATIO, DEFAULT_RUNS, DEFAULT_WARMUP,  # noqa: E402,F401
                            SCALE_FLOOR, STARTUP_FLOOR, WAIT_FLOOR, WAIT_RATIO,
                            classify_exponent, compare_warnings, default_baseline,
                            fit_exponent, gate1_line, median, quartiles, report_lines,
                            scale_exponent, summarize, summarize_arm, verdict, with_cpu)


def build_parser():
    p = argparse.ArgumentParser(prog="bench.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="sub")

    def common(sp):
        sp.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help="measured runs per arm (default %d)" % DEFAULT_RUNS)
        sp.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                        help="discarded warm-up runs per arm (default %d)" % DEFAULT_WARMUP)
        sp.add_argument("--json", action="store_true", help="print one JSON object")

    common(sub.add_parser("run", help="time one command given after --"))
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
    split = raw.index("--") if "--" in raw else len(raw)
    opts, argv = raw[:split], raw[split + 1:]
    parser = build_parser()
    args = parser.parse_args(opts)
    if args.sub is None:
        parser.print_help()
        return 2
    try:
        if args.runs < 1 or args.warmup < 0:
            raise UsageError("--runs must be >= 1 and --warmup >= 0")
        if args.sub in ("run", "scale") and not argv:
            raise UsageError("%s needs a command after --" % args.sub)
        return {"run": legacy_run, "compare": legacy_compare, "scale": legacy_scale}[args.sub](args, argv)
    except DevError as e:
        sys.stderr.write("bench.py: %s\n" % e.message)
        if e.details:
            sys.stderr.write("bench.py: last %d line(s) of its stderr:\n" % len(e.details))
            for line in e.details:
                sys.stderr.write("  " + line + "\n")
        return 2 if isinstance(e, UsageError) else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
