#!/usr/bin/env python3
"""dev.py: measurement and repository tooling for the competitive-systems skill (3.8+ stdlib).

    python3 <skill-dir>/scripts/dev.py <command> [options]
    python3 <skill-dir>/scripts/dev.py <command> --help

Exit codes: 0 success or verdict produced; 1 a gate failed or the measured command failed;
2 usage error; 3 a command or external tool could not be started; 70 an internal error (a
bug in dev.py: report it with the traceback); 130/143/129 interrupted by SIGINT, SIGTERM or
SIGHUP (measured process groups are killed first). Every expected error is one
line starting `dev.py <command>:`; `--json` prints exactly one JSON object on stdout and
sends warnings to stderr; every random draw takes `--seed` (default 0).
"""
import importlib
import os
import signal
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# (name, module, one-line help), grouped as `dev.py help` prints them.
GROUPS = [
    ("Evidence", [
        ("bench", "devkit.measure", "time one or more commands (arms): interleaved runs, CPU, RSS"),
        ("compare", "devkit.compare", "before/after verdict from bench JSON: bootstrap CI and Mann-Whitney p"),
        ("aa", "devkit.measure", "A/A noise floor of one command; --save sets compare's minimum effect"),
        ("scale", "devkit.measure", "doubling test: exponent with a CI after the startup baseline"),
        ("bound", "devkit.bound", "arithmetic bounds: amdahl, makespan (LPT), ops (Gate 2)"),
        ("changepoint", "devkit.measure", "step changes in a series by binary segmentation"),
        ("bisect", "devkit.perfbisect", "first slower commit: timed binary search in a throwaway worktree"),
        ("catalog", "devkit.catalog", "technique cards by symptom: precondition, reject_when, facts"),
        ("report", "devkit.report", "fill the report template from compare, scale, aa and oracle"),
        ("oracle", "devkit.oracle", "write a differential unittest: reference vs new, anti-cache check"),
    ]),
    ("Repository", [
        ("detect", "devkit.detect", "languages, package manager and the command each step runs"),
        ("check", "devkit.checks", "fail-fast lint, typecheck and affected tests on changed files"),
        ("lint", "devkit.checks", "lint changed (or given) files"),
        ("typecheck", "devkit.checks", "typecheck changed (or given) files"),
        ("test", "devkit.checks", "full test suite per language; --record writes .devkit/runs"),
        ("affected", "devkit.affected", "changed files to test targets; fails closed to a full run"),
    ]),
    ("Tests", [
        ("slowest", "devkit.history", "recorded test targets by p50 duration, with max and runs"),
        ("flakes", "devkit.history", "targets that both passed and failed; --write-quarantine"),
        ("smart", "devkit.history", "affected + always_run, then recent failures by knapsack in a budget"),
        ("shards", "devkit.history", "LPT split of targets into N shards; LB and makespan/LB"),
    ]),
    ("Navigation", [
        ("map", "devkit.repomap", "write .devkit/repo-map.json: exports, imports, importers, tests"),
        ("outline", "devkit.repomap", "definitions of one file with line numbers and summaries"),
        ("where", "devkit.repomap", "definition path:line of a symbol and who imports it"),
        ("context", "devkit.context", "token-budgeted context packet for a task: exact names first"),
    ]),
    ("Setup", [
        ("init", "devkit.init", "write the tools/dev.py shim, tools/dev.json and .devkit/"),
        ("size-audit", "devkit.sizeaudit", "files and Python functions over the line limits"),
        ("perf", "devkit.perf", "perf run: time tools/dev.json benchmarks into .devkit/perf; perf history"),
        ("selftest", "devkit.selftest", "offline end-to-end check on embedded fixtures: PASS or the first failure"),
    ]),
]


def print_help(stream):
    from devkit import VERSION
    stream.write("dev.py %s: usage: dev.py <command> [options]; dev.py <command> --help\n\n" % VERSION)
    for group, commands in GROUPS:
        stream.write("%s\n" % group)
        for name, _, text in commands:
            stream.write("  %-12s %s\n" % (name, text))
        stream.write("\n")


def find(name):
    for _, commands in GROUPS:
        for cmd, module, _ in commands:
            if cmd == name:
                return module
    return None


INTERNAL_ERROR = 70


class Terminated(KeyboardInterrupt):
    """SIGTERM or SIGHUP, raised like Ctrl-C so the measured process group is killed."""

    def __init__(self, signum):
        KeyboardInterrupt.__init__(self)
        self.signum = signum


def _on_signal(signum, _frame):
    raise Terminated(signum)


def install_signal_handlers():
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):   # not the main thread, or unsupported
                pass


def main(argv):
    if not argv:
        print_help(sys.stderr)
        return 2
    name = argv[0]
    if name in ("help", "-h", "--help"):
        print_help(sys.stdout)
        return 0
    if name == "--version":
        from devkit import VERSION
        print(VERSION)
        return 0
    module = find(name)
    if module is None:
        sys.stderr.write("dev.py: unknown command %r; run `dev.py help` for the list\n" % name)
        return 2
    from devkit.common import DevError
    fn = getattr(importlib.import_module(module), "cmd_" + name.replace("-", "_"))
    install_signal_handlers()
    try:
        return fn(argv[1:])
    except DevError as e:
        sys.stderr.write("dev.py %s: %s\n" % (name, e.message))
        if e.details:
            sys.stderr.write("dev.py %s: last %d line(s) of its stderr:\n" % (name, len(e.details)))
            for line in e.details:
                sys.stderr.write("  " + line + "\n")
        return e.code
    except Terminated as e:
        sys.stderr.write("dev.py %s: terminated by signal %d\n" % (name, e.signum))
        return 128 + e.signum
    except KeyboardInterrupt:
        sys.stderr.write("dev.py %s: interrupted\n" % name)
        return 130
    except Exception as e:  # a bug: say so, with a distinct exit code CI can tell from a gate
        traceback.print_exc()
        sys.stderr.write("dev.py %s: internal error: %s: %s (exit %d; please report it)\n" % (
            name, type(e).__name__, e, INTERNAL_ERROR))
        return INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
