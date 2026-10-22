"""Timing engine and the evidence commands bench, aa, scale and changepoint. The before/after
analysis is devkit.compare; the deprecated bench.py subcommands are devkit.legacy.

Timing: wall via time.perf_counter around the child's life; the child's user+sys CPU and
max RSS from the rusage that os.wait4 returns for that child (POSIX). Without wait4 the
RUSAGE_CHILDREN deltas give CPU and RSS is null; without `resource` both are null.
"""
import hashlib
import json
import math
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

DEFAULT_RUNS = 7        # scale, perf and the legacy bench.py; bench uses PROTOCOL_RUNS
DEFAULT_WARMUP = 1
STARTUP_FLOOR = 0.020   # seconds; medians below this are mostly process startup
WAIT_RATIO = 1.5        # wall > WAIT_RATIO x CPU means the process is waiting
WAIT_FLOOR = 0.100      # seconds; under this the wall/CPU ratio is startup noise
PROTOCOL_RUNS = 10      # the protocol asks for at least this many runs per arm
MAX_TOTAL = 300.0       # seconds; bench, aa and scale stop before a projected total over this
RISING_RATIO = 1.3      # consecutive-pair slopes each this much above the last: exponential

# ---------------------------------------------------------------- pure functions (also bench.py)


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
    """The IQR noise rule: (verdict, speedup, reason); the hand rule in measurement.md."""
    widest = 2 * max(before["iqr"], after["iqr"])
    if (after["median"] < before["median"] and after["q3"] < before["q1"]
            and before["median"] - after["median"] > widest):
        return "faster", before["median"] / after["median"], ""
    if (before["median"] < after["median"] and before["q3"] < after["q1"]
            and after["median"] - before["median"] > widest):
        return "slower", before["median"] / after["median"], ""
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


def fmt_metric(value, metric="wall"):
    """A value of the compared metric in its unit: bytes for rss, seconds otherwise."""
    return fmt_bytes(value) if metric == "rss" else fmt_time(value)


def before_after_line(before, after, metric="wall"):
    f = lambda v: fmt_metric(v, metric)
    return "Before: median %s (IQR %s, runs=%d); After: median %s (IQR %s, runs=%d)." % (
        f(before["median"]), f(before["iqr"]), before["runs"],
        f(after["median"]), f(after["iqr"]), after["runs"])


def report_lines(before, after, verdict_name, speedup):
    """Paste-ready lines for the IQR rule (bench.py)."""
    if verdict_name == "faster":
        second = "Verdict: %.2fx faster" % speedup
    elif verdict_name == "slower":
        second = "Verdict: %.2fx slower" % (1.0 / speedup)
    else:
        second = "Verdict: no measurable change"
    return [before_after_line(before, after), second]


def compare_warnings(before, after):
    """bench.py protocol warnings: run counts by step, startup-dominated medians."""
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
        leftover = False
        if os.name == "posix":
            try:
                os.killpg(proc.pid, 0)       # anyone left in the measured process group?
                leftover = True
                kill_group(proc)
            except OSError:
                pass
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
    return {"wall": wall, "cpu": cpu, "rss": rss, "exit": code, "stdout_sha256": digest,
            "leftover": leftover}


def time_many(cmd, runs, warmup, shell=False, **kw):
    """Warm up, then time `runs` runs. Returns (walls, cpus)."""
    for _ in range(warmup):
        time_once(cmd, shell, **kw)
    results = [time_once(cmd, shell, **kw) for _ in range(runs)]
    return [r["wall"] for r in results], [r["cpu"] for r in results]


class CostCap(object):
    """--max-total for bench and aa: once every arm has run once, project the whole run from
    those first times and stop (exit 1) before going on when the projection is over the cap.
    cap 0 or None disables it."""

    def __init__(self, cap, names, per_arm_runs, what):
        self.cap = cap or 0
        self.names = list(names)
        self.per_arm = per_arm_runs
        self.what = what
        self.first = {}
        self.start = time.perf_counter()
        self.checked = not self.cap

    def ran(self, name, wall):
        if self.checked:
            return
        self.first.setdefault(name, wall)
        if len(self.first) < len(self.names):
            return
        self.checked = True
        projected = sum(self.first.values()) * self.per_arm
        if projected > self.cap:
            raise CommandFailed(
                "%s would take about %s in all (%d runs per arm; the first runs took %s), over "
                "--max-total %s; stopped after %s. Pass --max-total %d to allow it, use fewer "
                "--runs or a smaller input, or follow measurement.md section 4 for expensive "
                "runs" % (self.what, fmt_time(projected), self.per_arm,
                          ", ".join(fmt_time(self.first[n]) for n in self.names),
                          fmt_time(self.cap), fmt_time(time.perf_counter() - self.start),
                          int(projected * 1.2) + 1))


def runs_note(what, cmd_text, count):
    """The one line printed before any measured run: how many times what runs, and that each
    run repeats the command's side effects."""
    note("%s will run %s %d times; every run repeats its side effects (writes, network calls, "
         "spend), so measure a pure command or a stub (SKILL.md step 0)" % (
             what, cmd_text, count))


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
             check_output=False, timeout=None, max_total=None, what="bench"):
    """arms: list of (name, cmd). Returns (bench dict without meta, warnings). max_total:
    seconds; stop (CommandFailed) once the first run of every arm projects past it."""
    data = {}
    cap = CostCap(max_total, [n for n, _ in arms], runs + warmup, what)
    for name, cmd in arms:
        data[name] = {"command": cmd, "shell": shell, "wall": [], "cpu": [], "rss": [],
                      "exit": [], "stdout_sha256": []}
        for _ in range(warmup):
            cap.ran(name, time_once(cmd, shell, check_output, timeout)["wall"])
    sequence = run_order([n for n, _ in arms], runs, order, seed)
    cmds = dict(arms)
    leftovers = set()
    for name in sequence:
        r = time_once(cmds[name], shell, check_output, timeout)
        cap.ran(name, r["wall"])
        for key in ("wall", "cpu", "rss", "exit", "stdout_sha256"):
            data[name][key].append(r[key])
        if r["leftover"]:
            leftovers.add(name)
    warnings = output_warnings(data) if check_output else []
    for name in sorted(leftovers):
        warnings.append("arm %s left processes running after it exited; dev.py killed them, "
                        "but background work outside the timed region skews later runs"
                        % name)
    out = {"arms": data, "order": sequence}
    if check_output:
        out["outputs_differ"] = differing_arms(data)
    return out, warnings


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def differing_arms(arms):
    """[[a, b], ...]: pairs of arms whose stdout hashes share no value."""
    sets = dict((n, set(h for h in a["stdout_sha256"] if h)) for n, a in arms.items())
    names = list(sets)
    return [[a, b] for i, a in enumerate(names) for b in names[i + 1:]
            if sets[a] and sets[b] and not (sets[a] & sets[b])]


def output_warnings(arms):
    out = []
    sets = {}
    for name, arm in arms.items():
        hashes = set(h for h in arm["stdout_sha256"] if h)
        sets[name] = hashes
        if len(hashes) > 1:
            out.append("arm %s: stdout differs between its own runs (%d distinct outputs): "
                       "nondeterministic output" % (name, len(hashes)))
    for a, b in differing_arms(arms):
        out.append("outputs differ between arms %s and %s: the arms do different work, so "
                   "the timing is not a speedup; check the oracle" % (a, b))
    if sets and all(h == {EMPTY_SHA256} for h in sets.values()):
        out.append("--check-output compared empty outputs: every arm printed nothing, so the "
                   "check proves nothing; make the driver print a result hash")
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
    try:
        argv = shlex.split(text)
    except ValueError as e:
        raise UsageError("cannot split %r into arguments: %s (check the quotes)" % (text, e))
    if not argv:
        raise UsageError("empty command in %r" % text)
    return argv


def split_dashes(raw):
    if "--" in raw:
        i = raw.index("--")
        return raw[:i], raw[i + 1:]
    return raw, []


def run_oracle_first(cmd, timeout=600):
    """Run the oracle before any timing: {command, exit, last_line}; CommandFailed when it
    fails, so no timing of an unverified candidate is ever produced."""
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CommandFailed("--oracle-cmd timed out after %d s; not timing an unverified "
                            "candidate" % timeout)
    lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    last = lines[-1] if lines else "(no output)"
    if p.returncode != 0:
        raise CommandFailed("--oracle-cmd failed with exit %d (last line: %s); not timing an "
                            "unverified candidate" % (p.returncode, last),
                            details=lines[-20:])
    note("oracle passed before timing: %s (last line: %s)" % (cmd, last))
    return {"command": cmd, "exit": p.returncode, "last_line": last}


def startup_warnings(arms_data, runs=5):
    """Warn when an arm's median is mostly interpreter startup: time the matching startup
    command (python3 -c pass, node -e 0) once per interpreter and compare."""
    out, cache = [], {}
    for name, arm in arms_data.items():
        cmd = arm["command"]
        if isinstance(cmd, str) or not arm["wall"]:
            continue
        base = default_baseline(cmd)
        if base is None:
            continue
        m = median(arm["wall"])
        if m > 0.5:
            continue
        key = tuple(base)
        if key not in cache:
            try:
                cache[key] = median(time_many(base, runs, 1)[0])
            except (StartError, CommandFailed):
                cache[key] = None
        b = cache[key]
        if b and m < 3 * b:
            out.append("%s: median %s is under 3x the startup time of `%s` (%s): most of it is "
                       "process startup, so a ratio understates the change in the computation; "
                       "subtract it (startup-corrected median about %s) or raise n"
                       % (name, fmt_time(m), " ".join(base), fmt_time(b),
                          fmt_time(max(m - b, 0.0))))
    return out


def _parser(name, description):
    return Parser(prog="dev.py " + name, description=description)


def _check_runs(runs, warmup):
    if runs < 1 or warmup < 0:
        raise UsageError("--runs must be >= 1 and --warmup >= 0")


# ---------------------------------------------------------------- bench


def cmd_bench(args):
    p = _parser("bench", "Time one or more commands (arms) with interleaving, CPU and RSS.")
    p.add_argument("--arm", action="append", default=[], metavar="NAME=CMD", help="an arm; repeatable")
    p.add_argument("--runs", type=int, default=PROTOCOL_RUNS,
                   help="timed runs per arm (default %d, the protocol minimum)" % PROTOCOL_RUNS)
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                   help="untimed runs per arm first (default %d)" % DEFAULT_WARMUP)
    p.add_argument("--order", choices=("interleave", "random", "blocked"), default="interleave",
                   help="run order across arms (default interleave)")
    p.add_argument("--seed", type=int, default=0, help="seed for --order random (default 0)")
    p.add_argument("--shell", action="store_true", help="run arms through /bin/sh -c")
    p.add_argument("--check-output", action="store_true",
                   help="hash stdout per run; arms whose outputs differ get verdict INVALID "
                        "and exit 1")
    p.add_argument("--timeout", type=float, default=None, help="seconds per run")
    p.add_argument("--max-total", type=float, default=MAX_TOTAL, metavar="S",
                   help="stop before the rest when the first run of each arm projects the "
                        "whole bench past S seconds (default %d; 0 disables)" % MAX_TOTAL)
    p.add_argument("--oracle-cmd", default=None, metavar="CMD",
                   help="run this correctness check (through the shell) before any timing; a "
                        "non-zero exit stops the bench with exit 1, and the result is stored "
                        "for compare and report")
    p.add_argument("--input", action="append", default=[], metavar="FILE",
                   help="input file whose SHA-256 goes into meta; one per flag, repeatable")
    p.add_argument("--min-effect", type=float, default=None, metavar="X",
                   help="smallest ratio change that counts, e.g. 0.05 for 5%% (default: "
                        ".devkit/aa.json from aa --save when under 7 days old and measured on "
                        "one of these arms, else 0.05; never below that saved floor)")
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
    # Everything that can fail on a typo fails now, before the expensive timed runs.
    metadata(a.input, a.seed, a.repo)
    if a.out:
        parent = os.path.dirname(os.path.abspath(a.out))
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            raise UsageError("--out %s: directory %s does not exist or is not writable"
                             % (a.out, parent))
    min_effect = source = None
    if len(arms) >= 2:
        from devkit import compare  # measure is imported by compare; keep the import local
        min_effect, source = compare.resolve_min_effect(a.min_effect, a.repo,
                                                        [c for _, c in arms])
    if a.max_total < 0:
        raise UsageError("--max-total must be >= 0 (0 disables the cap)")
    oracle = run_oracle_first(a.oracle_cmd) if a.oracle_cmd else None
    if a.shell:
        note("--shell: /bin/sh startup is inside every timing")
    runs_note("bench", "each of %d arm(s)" % len(arms), a.runs + a.warmup)
    bench, warnings = run_arms(arms, a.runs, a.warmup, a.order, a.seed, a.shell,
                               a.check_output, a.timeout, a.max_total, "bench")
    bench["schema"] = "devkit-bench/1"
    if oracle is not None:
        bench["oracle"] = oracle
    bench["runs"], bench["warmup"], bench["order_mode"] = a.runs, a.warmup, a.order
    bench["meta"] = metadata(a.input, a.seed, a.repo)
    warnings += startup_warnings(bench["arms"])
    bench["warnings"] = warnings
    if a.out:
        try:
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(bench, f, indent=1)
        except OSError as e:
            warn("could not write %s (%s); the timings are printed below" % (
                a.out, e.strerror or e))
            a.out = None
    result = None
    if len(arms) >= 2:
        b, af = bench["arms"][names[0]], bench["arms"][names[1]]
        result = compare.analyze(b["wall"], af["wall"], min_effect, seed=a.seed)
        compare.mark_invalid(result, bench.get("outputs_differ"))
        warnings = warnings + compare.protocol_warnings(
            result, {"before": b["cpu"], "after": af["cpu"]}, [bench["meta"]])
    for w in warnings:
        warn(w)
    if a.json:
        if result is not None:
            bench["compare"] = compare.compare_json(result, source, warnings, [bench["meta"]],
                                                    names=names[:2])
            if oracle is not None:
                bench["compare"]["oracle"] = oracle
        print_json(bench)
        return 1 if result is not None and result["verdict"] == "INVALID" else 0
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
    return 1 if result is not None and result["verdict"] == "INVALID" else 0


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
    p.add_argument("--max-total", type=float, default=MAX_TOTAL, metavar="S",
                   help="stop when the first runs project the A/A past S seconds (default %d; "
                        "0 disables)" % MAX_TOTAL)
    p.add_argument("--save", action="store_true", help="write .devkit/aa.json for compare")
    p.add_argument("--force", action="store_true",
                   help="with --save, overwrite a saved floor measured with more runs")
    p.add_argument("--repo", default=None,
                   help="repository root for .devkit/ and git metadata (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    opts, argv = split_dashes(args)
    a = p.parse_args(opts)
    if not argv:
        raise UsageError("give the command after --, for example: dev.py aa -- python3 bench.py")
    _check_runs(a.runs, a.warmup)
    if a.max_total < 0:
        raise UsageError("--max-total must be >= 0 (0 disables the cap)")
    cmd = " ".join(shlex.quote(x) for x in argv) if a.shell else argv
    if a.shell:
        note("--shell: /bin/sh startup is inside every timing")
    if a.runs < 10:
        warn("--runs %d is under 10; the noise floor from so few runs is itself noisy"
             % a.runs)
    root = path = None
    if a.save:
        root, root_note = repo_root(a.repo)
        if root_note:
            note(root_note)
        path = os.path.join(devkit_dir(root, create=True), "aa.json")
        old_runs = _saved_runs(path)
        if old_runs is not None and old_runs > a.runs and not a.force:
            raise UsageError("%s holds a floor measured with %d runs; --runs %d would replace "
                             "it with a noisier one (pass --force to do it anyway)"
                             % (path, old_runs, a.runs))
    note("aa runs the command %d times (2 arms x (%d runs + %d warmup))" % (
        2 * (a.runs + a.warmup), a.runs, a.warmup))
    runs_note("aa", "`%s`" % (cmd if a.shell else " ".join(cmd)), 2 * (a.runs + a.warmup))
    bench, _ = run_arms([("a1", cmd), ("a2", cmd)], a.runs, a.warmup, "interleave", a.seed,
                        a.shell, max_total=a.max_total, what="aa")
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
        env = environment()
        saved = {k: record[k] for k in ("command", "min_effect", "ci", "runs", "timestamp")}
        saved.update(platform=env["platform"], cpu_count=env["cpu_count"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=1)
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


def _saved_runs(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(json.load(f).get("runs"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


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


def pair_slopes(sizes, medians, baseline=0.0):
    """log-log slope between each pair of consecutive sizes after the baseline; None for a
    pair with a median inside the baseline."""
    out = []
    pts = sorted(zip(sizes, medians))
    for (n1, m1), (n2, m2) in zip(pts, pts[1:]):
        a, b = m1 - baseline, m2 - baseline
        out.append(math.log(b / a) / math.log(float(n2) / n1) if a > 0 and b > 0 else None)
    return out


def slopes_rising(slopes):
    """True when every consecutive-pair slope is at least RISING_RATIO x the one before, over
    at least three pairs, and the last is above 2.5: growth no polynomial fit describes."""
    if len(slopes) < 3 or any(x is None or x <= 0 for x in slopes):
        return False
    return all(b >= RISING_RATIO * a for a, b in zip(slopes, slopes[1:])) and slopes[-1] > 2.5


def slope_warnings(slopes, exponent):
    if slopes_rising(slopes):
        return ["pairwise slopes keep rising (%s): the growth looks exponential, so one fitted "
                "exponent means nothing; stop fitting and look for repeated subproblems "
                "(dev.py catalog --id recursion-dp-memo) or an uncapped search "
                "(SKILL.md step 1)" % ", ".join("%.2f" % x for x in slopes)]
    known = [x for x in slopes if x is not None]
    if exponent is not None and 1.3 <= exponent < 1.7 and len(known) >= 2 and \
            all(b > a for a, b in zip(known, known[1:])):
        return ["exponent %.2f with slopes rising toward the largest size (%s): a mix of terms "
                "such as a*n + b*n^2; measure larger sizes, or profile at the production n and "
                "take the hot frame's share there (measurement.md section 2)"
                % (exponent, ", ".join("%.2f" % x for x in known))]
    return []


def _projected_exponent(sizes_done, medians, baseline):
    """Exponent for projecting the next size: 2 with one size; else the last pair's slope
    (at least 1), extrapolated by its rise when slopes are rising."""
    if len(sizes_done) < 2:
        return 2.0
    slopes = [x for x in pair_slopes(sizes_done, medians, baseline) if x is not None]
    if not slopes:
        return 2.0
    e = max(slopes[-1], 1.0)
    if len(slopes) >= 2 and slopes[-1] > slopes[-2]:
        e += slopes[-1] - slopes[-2]
    return e


def scale_core(argv, sizes, runs, warmup, baseline, no_baseline, seed=0, timeout=None,
               progress=False, max_total=None):
    """Shared by dev.py scale and bench.py scale. Returns the result dict. With progress, a
    line per size goes to stderr, and an estimate of the rest after each size (quadratic
    after the first, the slope fitted so far after that). With max_total (seconds), the
    next size is not started when its projected cost would pass the cap: CommandFailed names
    the largest affordable size."""
    if baseline and no_baseline:
        raise UsageError("--baseline and --no-baseline contradict each other; pass one")
    if not any("{n}" in x for x in argv):
        raise UsageError("the command has no {n} for scale to substitute the size; quote it "
                         "as '{n}' (zsh drops an unquoted {n} that comes before a redirect such "
                         "as > out.json)")
    warnings = []
    if len(sizes) < 3:
        warnings.append("only %d sizes; use at least three (four recommended) so the fit has "
                        "a residual" % len(sizes))
    baseline_cmd = None
    if baseline:
        try:
            baseline_cmd = shlex.split(baseline)
        except ValueError as e:
            raise UsageError("cannot split --baseline %r: %s (check the quotes)" % (baseline, e))
        if not baseline_cmd:
            raise UsageError("--baseline needs a non-empty command")
    elif not no_baseline:
        baseline_cmd = default_baseline(argv)
        if baseline_cmd is None:
            warnings.append("no startup baseline known for %r; pass --baseline 'CMD' or make "
                            "the smallest median at least 0.5 s" % argv[0])
    if progress:
        runs_note("scale", "`%s`" % " ".join(argv), (runs + warmup) * len(sizes))
    base = None
    if baseline_cmd is not None:
        bw, bc = time_many(baseline_cmd, runs, warmup, timeout=timeout)
        base = summarize_arm(bw, bc)
        base["command"] = baseline_cmd
    base_median = base["median"] if base else 0.0
    rows, per_size = [], []
    t0 = time.perf_counter()
    per = runs + warmup
    for i, n in enumerate(sizes):
        if i and max_total:
            done = [r["n"] for r in rows]
            meds = [r["median"] for r in rows]
            e = _projected_exponent(done, meds, base_median)
            last_n, last_m = done[-1], meds[-1]
            cost = last_m * (float(n) / last_n) ** e * per
            spent = time.perf_counter() - t0
            if spent + cost > max_total:
                room = max(max_total - spent, 0.0) / (max(last_m, 1e-9) * per)
                fit = int(last_n * room ** (1.0 / e)) if room > 0 else 0
                so_far = ", ".join("n=%d %s" % (r["n"], fmt_time(r["median"])) for r in rows)
                raise CommandFailed(
                    "n=%d would take about %s (%d runs at exponent %.2f fitted so far), past "
                    "--max-total %s with %s spent; measured so far: %s. The largest size that "
                    "fits is about n=%d; pass --runs 3, smaller sizes or a larger --max-total "
                    "(run it in the background with --json to a file)"
                    % (n, fmt_time(cost), per, e, fmt_time(max_total), fmt_time(spent), so_far,
                       fit))
        cmd = [x.replace("{n}", str(n)) for x in argv]
        w, c = time_many(cmd, runs, warmup, timeout=timeout)
        s = summarize_arm(w, c)
        s["n"] = n
        s["times"] = w
        rows.append(s)
        per_size.append(w)
        if progress:
            sys.stderr.write("scale: n=%d median %s (%d of %d sizes, %s elapsed)\n" % (
                n, fmt_time(s["median"]), i + 1, len(sizes), fmt_time(time.perf_counter() - t0)))
            rest = sizes[i + 1:]
            if rest:
                e = _projected_exponent([r["n"] for r in rows], [r["median"] for r in rows],
                                        base_median)
                est = sum(s["median"] * (m / float(n)) ** e for m in rest) * per
                basis = ("if time grows quadratically" if i == 0 else
                         "at the exponent %.2f fitted so far" % e)
                sys.stderr.write("scale: the remaining sizes take about %s %s (%d runs each); "
                                 "for slow sizes use --runs 3 or smaller sizes\n"
                                 % (fmt_time(est), basis, per))
            sys.stderr.flush()
    exponent, more = scale_exponent(sizes, [r["median"] for r in rows], base_median)
    warnings.extend(more)
    raw = None
    if exponent is not None:
        raw = stats.loglog_slope(sizes, [r["median"] - base_median for r in rows])
    ci = stats.exponent_ci(sizes, per_size, base_median, seed=seed) if exponent is not None else None
    slopes = pair_slopes(sizes, [r["median"] for r in rows], base_median)
    warnings.extend(slope_warnings(slopes, raw))
    largest = max(rows, key=lambda r: r["n"])
    return {"sizes": rows, "baseline": base, "exponent": exponent, "exponent_raw": raw,
            "exponent_ci": list(ci) if ci else None,
            "class": classify_exponent(raw) if raw is not None else None,
            "pair_slopes": slopes, "slopes_rising": slopes_rising(slopes),
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
    slopes = res.get("pair_slopes") or []
    if slopes:
        print("Pairwise slopes: %s%s" % (", ".join("n/a" if x is None else "%.2f" % x
                                                     for x in slopes),
                                          " (rising: exponential, see the warning)"
                                          if res.get("slopes_rising") else ""))
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
    p.add_argument("--timeout", type=float, default=None, help="seconds per run before it is "
                   "killed (the scale fails with exit 1)")
    p.add_argument("--max-total", type=float, default=MAX_TOTAL, metavar="S",
                   help="do not start a size whose projected cost would pass S seconds in all "
                        "(default %d; 0 disables)" % MAX_TOTAL)
    p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    opts, argv = split_dashes(args)
    a = p.parse_args(opts)
    if not argv:
        raise UsageError("give the command after --; every {n} in it is replaced by the size "
                         "(quote it as '{n}' in zsh)")
    _check_runs(a.runs, a.warmup)
    if a.max_total < 0:
        raise UsageError("--max-total must be >= 0 (0 disables the cap)")
    res = scale_core(argv, parse_sizes(a.sizes), a.runs, a.warmup, a.baseline, a.no_baseline,
                     a.seed, timeout=a.timeout, progress=not a.quiet, max_total=a.max_total)
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
    """[(label, value)] from one-number-per-line, label,value CSV, or JSON. A non-finite value
    (nan, inf) is a usage error naming its position: one NaN would hide every step."""
    pairs = _load_series(path)
    for i, (label, v) in enumerate(pairs):
        if v != v or v in (float("inf"), float("-inf")):
            raise UsageError("%s: value %d (label %s) is %r; a non-finite value hides every "
                             "change point, so remove or fix it" % (path, i, label, v))
    return pairs


def _load_series(path):
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
