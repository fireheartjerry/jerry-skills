"""Arithmetic bounds: `dev.py bound amdahl | makespan | ops`. No timing; standard library only.

amdahl: overall speedup 1/((1-p) + p/s) and the cap 1/(1-p) for a share p sped up s times.
makespan: lower bound, LPT plan and Graham's (4/3 - 1/(3m)) guarantee for a job list.
ops: Gate 2 arithmetic, an operation count or scaling law against a time budget.
"""
import ast
import math
import os

from devkit.common import Parser, UsageError, fmt_time, load_json_file, print_json
from devkit.graph import lpt  # noqa: F401  (the card algorithm, shared with shards)
from devkit.stats import median

# Simple operations per second. cpython: one interpreted step (a Python-level call or
# comparison); c-builtin: one element of a scan done in C (`x in list`, sum, sorted,
# str.find), measured at 1.5e8 to 3.3e8 per second on CPython 3.9; compiled: native code.
BUDGETS = {"cpython": 1e7, "c-builtin": 2e8, "compiled": 1e9}
THRESHOLDS = {"request": 0.100, "batch": 1.0}        # seconds per call before it matters
HELP = ("usage: dev.py bound {amdahl,makespan,ops} ...\n"
        "  amdahl   --p FRACTION --s LOCAL_SPEEDUP\n"
        "  makespan --durations FILE [--workers M] [--pinned NAME ...] "
        "[--current alphabetical|roundrobin|FILE]\n"
        "  ops      --n N (--time-at-n S --exponent E | --ops EXPR [--runtime "
        "cpython|c-builtin|compiled | --rate OPS_PER_S]) [--path request|batch]\n")


def cmd_bound(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    if not args:
        import sys
        sys.stderr.write(HELP)
        raise UsageError("name a bound: amdahl, makespan or ops")
    sub = {"amdahl": bound_amdahl, "makespan": bound_makespan, "ops": bound_ops}.get(args[0])
    if sub is None:
        raise UsageError("unknown bound %r; one of amdahl, makespan, ops" % args[0])
    return sub(args[1:])


# ---------------------------------------------------------------- amdahl


def amdahl(p, s):
    """(overall, cap): 1/((1-p) + p/s) and 1/(1-p); cap is inf when p == 1."""
    overall = 1.0 / ((1.0 - p) + p / s)
    cap = math.inf if p >= 1.0 else 1.0 / (1.0 - p)
    return overall, cap


def bound_amdahl(args):
    p = Parser(prog="dev.py bound amdahl", description=amdahl.__doc__)
    p.add_argument("--p", type=float, required=True, help="share of total time, 0 < p <= 1")
    p.add_argument("--s", type=float, required=True, help="local speedup of that share, > 1")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if not 0 < a.p <= 1:
        raise UsageError("--p is a fraction of total time in (0, 1], for example 0.12 for 12%")
    if a.s <= 0:
        raise UsageError("--s must be a positive speedup factor")
    overall, cap = amdahl(a.p, a.s)
    sentence = ("Amdahl: speeding up a %g%% share by %gx gives %.2fx end to end; editing a %g%% "
                "share caps the end-to-end gain at %s." % (
                    a.p * 100, a.s, overall, a.p * 100,
                    "%.2fx" % cap if cap != math.inf else "no cap (p = 1)"))
    if a.json:
        print_json({"schema": "devkit-bound/1", "kind": "amdahl", "p": a.p, "s": a.s,
                    "overall": overall, "cap": (None if cap == math.inf else cap),
                    "sentence": sentence})
        return 0
    print(sentence)
    return 0


# ---------------------------------------------------------------- makespan


def lower_bound(jobs, m, pinned=()):
    """(LB, binding term): max of total/m, the longest job, the pinned total."""
    total = sum(d for d, _ in jobs)
    longest = max((d for d, _ in jobs), default=0.0)
    pinned_total = sum(d for d, n in jobs if n in pinned)
    terms = [(total / m, "total/workers"), (longest, "longest job"),
             (pinned_total, "pinned jobs on one worker")]
    return max(terms, key=lambda t: t[0])


def load_jobs(spec):
    """[(name, raw)] and runners from a JSON file or an inline comma list of durations."""
    runners = None
    if os.path.isfile(spec):
        data = load_json_file(spec, "durations file")
    else:
        try:
            data = [float(x) for x in spec.split(",") if x.strip()]
        except ValueError:
            raise UsageError("--durations must be a JSON file or a comma list of seconds; %r is "
                             "neither (file not found)" % spec)
    if isinstance(data, dict) and "jobs" in data:
        runners = data.get("runners")
        data = data["jobs"]
    if isinstance(data, dict):
        items = list(data.items())
    elif isinstance(data, list):
        items = []
        for i, item in enumerate(data):
            if isinstance(item, dict):
                items.append((str(item.get("name", "job%d" % (i + 1))),
                              item.get("seconds", item.get("duration"))))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                items.append((str(item[0]), item[1]))
            else:
                items.append(("job%d" % (i + 1), item))
    else:
        raise UsageError("durations must be {name: seconds}, {\"jobs\": ..., \"runners\": M} or a list")
    if not items:
        raise UsageError("no jobs in %s" % spec)
    return items, runners


def validate_jobs(items):
    """(jobs, replaced): bad durations replaced by the p50 of valid ones; a duplicate name
    keeps its own duration and gets a #k suffix (reported under replaced)."""
    valid = []
    for _, raw in items:
        try:
            v = float(raw)
            if v > 0 and math.isfinite(v):
                valid.append(v)
        except (TypeError, ValueError):
            pass
    if not valid:
        raise UsageError("no job has a positive numeric duration")
    p50 = median(valid)
    jobs, replaced, seen = [], [], set()
    for name, raw in items:
        why = None
        try:
            v = float(raw)
            if not (v > 0 and math.isfinite(v)):
                why = "non-positive"
        except (TypeError, ValueError):
            why = "missing" if raw is None else "not a number"
        if name in seen:
            k = 2
            while "%s#%d" % (name, k) in seen:
                k += 1
            new = "%s#%d" % (name, k)
            replaced.append("%s (duplicate name) -> renamed %s" % (name, new))
            name = new
        seen.add(name)
        if why:
            replaced.append("%s (%s) -> p50 %g" % (name, why, p50))
            v = p50
        jobs.append((v, name))
    return jobs, replaced


def replay(plan_spec, jobs, m):
    """[(load, [names])] for the current plan.

    alphabetical: sorted names cut into m contiguous slices of near-equal count (the first
    n mod m slices take one extra job); roundrobin: jobs dealt in file order; else a plan file.
    """
    dur = dict((n, d) for d, n in jobs)
    if plan_spec == "alphabetical":
        names = sorted(n for _, n in jobs)
        q, r = divmod(len(names), m)
        buckets, i = [], 0
        for w in range(m):
            k = q + (1 if w < r else 0)
            buckets.append(names[i:i + k])
            i += k
    elif plan_spec == "roundrobin":
        buckets = [[] for _ in range(m)]
        for i, (_, n) in enumerate(jobs):
            buckets[i % m].append(n)
    else:
        data = load_json_file(plan_spec, "current plan")
        buckets = list(data.values()) if isinstance(data, dict) else data
        if not isinstance(buckets, list) or not all(isinstance(b, list) for b in buckets):
            raise UsageError("current plan must be {worker: [names]} or [[names], ...]")
        unknown = [n for b in buckets for n in b if n not in dur]
        if unknown:
            raise UsageError("current plan names unknown jobs: %s" % ", ".join(unknown[:5]))
    return [(sum(dur[n] for n in b), list(b)) for b in buckets]


def bound_makespan(args):
    p = Parser(prog="dev.py bound makespan",
                                description="Lower bound, LPT plan and Graham bound for a job list.")
    p.add_argument("--durations", required=True, help="JSON file or comma list of seconds")
    p.add_argument("--workers", type=int, default=None, help="m; else 'runners' from the file")
    p.add_argument("--pinned", nargs="*", default=[], help="job names kept on one worker")
    p.add_argument("--current", default=None, help="alphabetical | roundrobin | plan FILE")
    p.add_argument("--brief", action="store_true",
                   help="per worker: load, job count and the first two names only")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    items, runners = load_jobs(a.durations)
    m = a.workers if a.workers is not None else runners
    if m is None:
        raise UsageError("give --workers M or put \"runners\" in the durations file")
    if m < 1:
        raise UsageError("--workers must be at least 1")
    jobs, replaced = validate_jobs(items)
    names = set(n for _, n in jobs)
    missing = [x for x in a.pinned if x not in names]
    if missing:
        raise UsageError("--pinned names unknown jobs: %s" % ", ".join(missing))
    total = sum(d for d, _ in jobs)
    longest_d, longest_n = max(jobs)
    lb, binding = lower_bound(jobs, m, a.pinned)
    plan = lpt(jobs, m, a.pinned)
    makespan = max(load for load, _ in plan)
    graham = 4.0 / 3.0 - 1.0 / (3.0 * m)
    binds_from = int(math.ceil(total / longest_d)) if longest_d > 0 else None
    current = None
    if a.current:
        cur = replay(a.current, jobs, m)
        cur_ms = max(load for load, _ in cur)
        worst = max(range(len(cur)), key=lambda i: cur[i][0])
        current = {"plan": a.current, "makespan": cur_ms, "binding_worker": worst + 1,
                   "assignment": [{"worker": i + 1, "load": l, "jobs": b} for i, (l, b) in enumerate(cur)]}
    out = {"schema": "devkit-bound/1", "kind": "makespan", "jobs": len(jobs), "workers": m,
           "total": total, "longest": {"name": longest_n, "seconds": longest_d},
           "pinned": list(a.pinned), "replaced": replaced, "lb": lb, "lb_binding": binding,
           "longest_binds_from_m": binds_from, "lpt_makespan": makespan,
           "lpt_over_lb": makespan / lb if lb else None, "graham_bound": graham,
           "plan": [{"worker": i + 1, "load": l, "jobs": b} for i, (l, b) in enumerate(plan)],
           "current": current}
    if a.json:
        print_json(out)
        return 0
    print("jobs %d, total %s, longest %s (%s), workers %d%s" % (
        len(jobs), fmt_time(total), fmt_time(longest_d), longest_n, m,
        ", pinned: " + ", ".join(a.pinned) if a.pinned else ""))
    if replaced:
        print("replaced: " + "; ".join(replaced))
    if m == 1:
        print("m = 1: the makespan is the total; scheduling cannot help")
    if current:
        print("current plan (%s): makespan %s (worker %d binds)" % (
            a.current, fmt_time(current["makespan"]), current["binding_worker"]))
    print("LB %s (%s binds; the longest job binds from m = %s)" % (
        fmt_time(lb), binding, binds_from))
    print("LPT plan: makespan %s = %.2fx LB; guarantee <= %.2fx optimal (4/3 - 1/(3m), m = %d)" % (
        fmt_time(makespan), makespan / lb if lb else float("nan"), graham, m))
    for i, (load, bucket) in enumerate(plan):
        if a.brief and len(bucket) > 2:
            names = "%s, ... (%d jobs)" % (", ".join(bucket[:2]), len(bucket))
        else:
            names = ", ".join(bucket)
        print("  worker %d: %-10s %s" % (i + 1, fmt_time(load), names))
    return 0


# ---------------------------------------------------------------- ops


def _pow(x, y):
    r = float(x) ** float(y)
    if isinstance(r, complex):
        raise ValueError("a negative base with a fractional power")
    return r


_BIN = {ast.Add: lambda x, y: x + y, ast.Sub: lambda x, y: x - y, ast.Mult: lambda x, y: x * y,
        ast.Div: lambda x, y: x / y, ast.Pow: _pow,
        ast.FloorDiv: lambda x, y: x // y, ast.Mod: lambda x, y: x % y}
_FUNCS = {"log": math.log, "log2": math.log2, "sqrt": math.sqrt}
_OP_WORDS = {ast.BitXor: "^ (use ** for powers, as in n**2)", ast.BitAnd: "&", ast.BitOr: "|",
             ast.LShift: "<<", ast.RShift: ">>", ast.MatMult: "@"}


def _rejected(node):
    if isinstance(node, ast.BinOp):
        return "operator %s" % _OP_WORDS.get(type(node.op), type(node.op).__name__)
    if isinstance(node, ast.Name):
        return "name %r (the only name is n)" % node.id
    if isinstance(node, ast.Call):
        fn = getattr(node.func, "id", None) or "?"
        return "function %s (log, log2 and sqrt take one argument)" % fn
    if isinstance(node, ast.Constant):
        return "constant %r" % (node.value,)
    return "a %s expression" % type(node).__name__.lower()


def evaluate(expr, n):
    """Evaluate an arithmetic expression in n (in floats): numbers, n, + - * / // % ** (),
    log, log2, sqrt. A result too large for a float is inf, never a hang or a traceback."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise UsageError("cannot parse --ops %r: %s" % (expr, e.msg))

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id == "n":
            return float(n)
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            return _BIN[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCS and len(node.args) == 1 and not node.keywords):
            return _FUNCS[node.func.id](ev(node.args[0]))
        raise UsageError("--ops may use numbers, n, + - * / // %% ** (), log, log2, sqrt; "
                         "not %s" % _rejected(node))
    try:
        return float(ev(tree))
    except OverflowError:
        return math.inf
    except (ValueError, ZeroDivisionError) as e:
        raise UsageError("--ops %r fails at n=%g: %s" % (expr, n, e))


FLIP_MAX = 10 ** 15


def flip_size(time_at, threshold, lo=1, hi=FLIP_MAX):
    """Smallest integer n in [lo, hi] with time_at(n) >= threshold, assuming monotone growth.
    The upper end is found by doubling from lo, so a fast-growing model (2**n) is probed
    only at small n; a probe that overflows counts as crossing."""
    def t(n):
        try:
            return time_at(n)
        except OverflowError:
            return math.inf
    if t(lo) >= threshold:
        return lo
    top = lo
    while t(top) < threshold:
        if top >= hi:
            return None
        lo, top = top, min(top * 2, hi)
    while top - lo > 1:
        mid = (lo + top) // 2
        if t(mid) >= threshold:
            top = mid
        else:
            lo = mid
    return top


def bound_ops(args):
    p = Parser(prog="dev.py bound ops",
                                description="Gate 2: operation count or scaling law against the budget.")
    p.add_argument("--n", type=float, required=True, help="expected input size")
    p.add_argument("--ops", default=None, help="operations as an expression in n, e.g. '40*n*n'")
    p.add_argument("--time-at-n", type=float, default=None, help="measured seconds at n")
    p.add_argument("--exponent", type=float, default=None, help="scaling exponent for --time-at-n")
    p.add_argument("--runtime", choices=sorted(BUDGETS), default="cpython",
                   help="operations per second for --ops: cpython 1e7 (an interpreted step), "
                        "c-builtin 2e8 (an element scanned in C: x in list, sum, sorted), "
                        "compiled 1e9 (default cpython)")
    p.add_argument("--rate", type=float, default=None,
                   help="operations per second for --ops, overriding --runtime (measure it)")
    p.add_argument("--path", choices=sorted(THRESHOLDS), default="request",
                   help="time that matters per call: request 100 ms, batch 1 s (default request)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if a.n <= 0:
        raise UsageError("--n must be positive")
    budget, threshold = BUDGETS[a.runtime], THRESHOLDS[a.path]
    rate_name = a.runtime
    if a.rate is not None:
        if a.rate <= 0:
            raise UsageError("--rate must be positive operations per second")
        budget, rate_name = a.rate, "--rate"
    if a.ops is not None:
        if a.time_at_n is not None or a.exponent is not None:
            raise UsageError("give --ops or --time-at-n with --exponent, not both")
        ops_at = lambda n: evaluate(a.ops, n)  # noqa: E731
        time_at = lambda n: ops_at(n) / budget  # noqa: E731
        model = "ops = %s" % a.ops
    elif a.time_at_n is not None and a.exponent is not None:
        if a.time_at_n <= 0 or a.exponent <= 0:
            raise UsageError("--time-at-n and --exponent must be positive")
        def time_at(n):
            try:
                return a.time_at_n * (n / a.n) ** a.exponent
            except OverflowError:
                return math.inf
        ops_at = lambda n: time_at(n) * budget  # noqa: E731
        model = "time = %g s x (n/%g)^%g" % (a.time_at_n, a.n, a.exponent)
    else:
        raise UsageError("give --ops EXPR, or --time-at-n SECONDS with --exponent E")
    n1, n10 = a.n, 10 * a.n
    t1, t10 = time_at(n1), time_at(n10)
    if t10 < t1:
        raise UsageError("time falls between n and 10n; the model must grow with n")
    flip = flip_size(time_at, threshold)
    decision = "proceed" if t10 >= threshold else "stop"
    why = ("brute force at 10x n reaches the %s threshold (%s): optimise" % (a.path, fmt_time(threshold))
           if decision == "proceed" else
           "brute force at 10x n is under the %s threshold (%s): keep the simple code" % (
               a.path, fmt_time(threshold)))
    measured = a.ops is None
    out = {"schema": "devkit-bound/1", "kind": "ops", "model": model, "runtime": rate_name,
           "estimate": not measured,
           "budget_ops_per_s": budget, "path": a.path, "threshold_s": threshold,
           "n": n1, "ops_at_n": ops_at(n1), "time_at_n": t1, "ops_at_10n": ops_at(n10),
           "time_at_10n": t10, "flip_size": flip, "decision": decision, "reason": why}
    if a.json:
        print_json(out)
        return 0
    if measured:
        print("Gate 2 (%s path, %s): %s at n=%g; %s at 10n=%g (from the measured time)" % (
            a.path, model, fmt_time(t1), n1, fmt_time(t10), n10))
    else:
        print("Gate 2 (%s path, %s rate %.0e ops/s, %s): ops %.3g ~ %s at n=%g; ops %.3g ~ %s "
              "at 10n=%g" % (a.path, rate_name, budget, model, out["ops_at_n"], fmt_time(t1),
                             n1, out["ops_at_10n"], fmt_time(t10), n10))
        print("estimate from an operation count; one timed run (--time-at-n, --exponent) "
              "replaces it; an element scanned inside a C builtin (x in list, sum, sorted) "
              "costs about 20x less than an interpreted step: --runtime c-builtin")
    if flip is None:
        print("threshold %s is not crossed below n = 1e15" % fmt_time(threshold))
    else:
        print("threshold %s crossed at flip size n = %d" % (fmt_time(threshold), flip))
    print("%s: %s" % (decision, why))
    return 0
