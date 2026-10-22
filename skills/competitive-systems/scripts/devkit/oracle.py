"""`dev.py oracle`: write a differential unittest of a new implementation against the old one.

The generated file imports both callables by `module:function` (resolved from the directory
dev.py ran in), feeds both the same seeded random inputs plus edge cases, compares results
(exceptions compare by type; NaN equals NaN; floats within --tol relative tolerance), and
adds an anti-cache test: call the new function, mutate the same input object (or a mutable
member of an argument tuple) in place, call again, compare with the reference on the mutated
input. A result cached by object identity fails it.

Calling convention (--call): `one` calls fn(case); `args` calls fn(*case), so a generator
for a function of several arguments returns a tuple of them. `auto` (default) picks `args`
when the reference has more than one required positional parameter. The generated file also
guards against a vacuous pass: it fails when the reference raises on more than half of the
random cases, when every reference output is identical, or when the comparison cannot tell a
deliberately perturbed output from the real one. Existing files are never overwritten
without --force.
"""
import importlib
import inspect
import random
import os
import re
import sys

from devkit.common import CommandFailed, DevError, Parser, StartError, UsageError, print_json

BUILTIN_GENS = ("ints", "floats", "strings", "lists", "dicts")

TEMPLATE = '''"""Differential test written by dev.py oracle: {new} against the reference {ref}.

Inputs: {cases} seeded random cases (generator {gen}, seed {seed}) plus the edge cases below.
Call: {call_doc}.
Comparison: {comparison}; exceptions compare by type; NaN equals NaN.
Vacuity guards: the test fails when the reference raises on more than MIN_OK_SHARE of the
random cases, or returns one identical value on every case (the inputs do not exercise it),
or when a deliberately perturbed copy of the reference output still compares equal.
The anti-cache test calls the new function, mutates the same input object (or the first
mutable member of an argument tuple) in place, calls again and compares with the reference
on the mutated input, so a result cached by object identity (id(), weakref keys, "same list
as last time") fails.
Regenerate: dev.py oracle --ref {ref} --new {new} --gen {gen} --call {call} --cases {cases} --seed {seed} --tol {tol} --force
"""
import copy
import importlib
import math
import os
import random
import sys
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), {rel!r}))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def load(spec):
    module, _, name = spec.partition(":")
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


REF = load({ref!r})
NEW = load({new!r})
CASES = {cases}
SEED = {seed}
TOL = {tol!r}
CALL = {call!r}          # "one": fn(case); "args": fn(*case)
MIN_OK_SHARE = 0.5      # lower only if the reference legitimately rejects most random inputs

{generator}


def call(fn, x):
    if CALL == "args":
        if not isinstance(x, (tuple, list)):
            raise AssertionError("--call args needs each case to be a tuple of arguments, got "
                                 "%s; make the generator return a tuple" % type(x).__name__)
        return fn(*x)
    return fn(x)


def outcome(fn, x):
    try:
        return ("ok", call(fn, x))
    except AssertionError:
        raise
    except Exception as e:  # the reference's exceptions are part of its behaviour
        return ("raise", type(e).__name__)


def same(a, b):
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        if a == b:                      # also equal infinities, whose difference is NaN
            return True
        if TOL:
            return abs(a - b) <= TOL * max(1.0, abs(b))
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return type(a) is type(b) and len(a) == len(b) and all(
            same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def perturb(v, rng):
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 1 + rng.randint(0, 100)
    if isinstance(v, float):
        return v + 1.0 if v == v else 1.0
    if isinstance(v, str):
        return v + "z"
    if isinstance(v, tuple):
        items = [perturb(x, rng) for x in v]
        return type(v)(*items) if hasattr(v, "_fields") else tuple(items)
    return copy.deepcopy(v)


def broken(v):
    """A value that differs from v, standing in for a wrong candidate's output."""
    if v is None:
        return "broken"
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v + 1 if v == v else 0.0
    if isinstance(v, str):
        return v + "z"
    if isinstance(v, list):
        return v[:-1] if v else [0]
    if isinstance(v, tuple):
        return v[:-1] if v else (0,)
    if isinstance(v, dict):
        out = dict(v)
        out["__broken__"] = 0
        return out
    if isinstance(v, (set, frozenset)):
        return type(v)(list(v)[:-1]) if v else type(v)([0])
    return ("broken", v)


def mutable_target(x):
    """The object the anti-cache test mutates: x itself, or a list/dict inside a tuple."""
    if isinstance(x, (list, dict)):
        if CALL == "args" and isinstance(x, list):
            for m in x:
                if isinstance(m, (list, dict)):
                    return m
            return None
        return x
    if isinstance(x, tuple):
        for m in x:
            if isinstance(m, (list, dict)):
                return m
    return None


def mutate(x, rng):
    """Change x in place; False when x is not a mutable container."""
    if isinstance(x, list):
        if len(x) >= 2:
            x.reverse()
        x.append(perturb(x[0], rng) if x else 1)
        return True
    if isinstance(x, dict):
        x["__mutated_%d" % rng.randint(0, 10 ** 6)] = perturb(next(iter(x.values()), 1), rng)
        return True
    return False


def short(x, limit=300):
    text = repr(x)
    return text if len(text) <= limit else text[:limit] + "..."


def inputs():
    rng = random.Random(SEED)
    return [copy.deepcopy(e) for e in EDGES] + [generate(rng) for _ in range(CASES)]


SUMMARY = []


def tearDownModule():
    for line in SUMMARY:
        sys.stderr.write("%s\\n" % line)


class OracleTest(unittest.TestCase):
    def test_matches_reference(self):
        keep = []   # every input stays alive, so object ids are never reused between calls
        edges = len(EDGES)
        ok = total = 0
        wants = []
        for i, x in enumerate(inputs()):
            a, b = copy.deepcopy(x), copy.deepcopy(x)
            keep.extend((a, b))
            want, got = outcome(REF, a), outcome(NEW, b)
            if i >= edges:
                total += 1
                if want[0] == "ok":
                    ok += 1
                    wants.append(want[1])
            if not same(got, want):
                self.fail("case %d: input %s\\n new: %s\\n ref: %s" % (
                    i, short(x), short(got), short(want)))
        SUMMARY.append("reference ok on %d of %d random cases" % (ok, total))
        self.assertGreaterEqual(
            ok, max(1, MIN_OK_SHARE * total),
            "vacuous oracle: the reference raised on %d of %d random cases, so both sides "
            "raising compares equal. Check the call convention (CALL=%r) and the generator."
            % (total - ok, total, CALL))
        if len(wants) > 1:
            self.assertFalse(
                all(same(w, wants[0]) for w in wants[1:]),
                "vacuous oracle: the reference returned %s on every random case, so a stub "
                "returning that value passes; widen the generator" % short(wants[0]))
        self.assertTrue(
            any(not same(("ok", broken(w)), ("ok", w)) for w in wants),
            "insensitive oracle: a perturbed copy of the reference output still compares equal")

    def test_anti_cache_mutated_input(self):
        rng = random.Random(SEED + 1)
        checked = 0
        for x in inputs():
            target = mutable_target(x)
            if target is None:
                continue
            want0 = outcome(REF, copy.deepcopy(x))
            got0 = outcome(NEW, x)                # first call on this very object
            if not same(got0, want0):
                self.fail("wrong output on the unmutated input (not a cache issue): input %s"
                          "\\n new: %s\\n ref: %s" % (short(x), short(got0), short(want0)))
            if not mutate(target, rng):
                continue
            want = outcome(REF, copy.deepcopy(x))
            got = outcome(NEW, x)                 # same object, new contents
            self.assertTrue(same(got, want), "stale result after mutating the input in "
                            "place (identity-keyed cache?): input %s\\n new: %s\\n ref: %s" % (
                                short(x), short(got), short(want)))
            checked += 1
            if checked >= 50:
                break
        if not checked:
            self.skipTest("no mutable input (list or dict, or one inside the argument tuple): "
                          "the anti-cache check did not run; use fresh inputs per timed "
                          "iteration instead")

    # Metamorphic stubs: uncomment the ones that hold for this function.
    # def test_permutation_invariance(self):        # output ignores input order
    #     rng = random.Random(SEED + 2)
    #     for x in inputs():
    #         y = list(x)
    #         rng.shuffle(y)
    #         self.assertTrue(same(outcome(NEW, y), outcome(NEW, list(x))))
    #
    # def test_idempotence(self):                   # f(f(x)) == f(x)
    #     for x in inputs():
    #         once = outcome(NEW, copy.deepcopy(x))
    #         if once[0] == "ok":
    #             self.assertTrue(same(outcome(NEW, copy.deepcopy(once[1])), once))


if __name__ == "__main__":
    unittest.main()
'''

GENERATORS = {
    "ints": '''EDGES = [0, 1, -1, 2 ** 63, -(2 ** 63), 10 ** 18]


def generate(rng):
    return rng.choice([rng.randint(-10, 10), rng.randint(-10 ** 6, 10 ** 6)])''',
    "floats": '''EDGES = [0.0, -0.0, 1.0, -1.5, 1e308, 5e-324, float("inf"), float("-inf"), float("nan")]


def generate(rng):
    return rng.choice([rng.uniform(-1, 1), rng.uniform(-1e6, 1e6), float(rng.randint(-5, 5))])''',
    "strings": '''EDGES = ["", "a", "aaaa", "abc", "cba", "  padded  ", " \\t\\n ", "na\\u00efve caf\\u00e9",
         "\\u65e5\\u672c\\u8a9e", "e\\u0301", "x" * 1000]


def generate(rng):
    alphabet = "abcAB \\t\\n.,-\\u00e9\\u65e5"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))''',
    "lists": '''EDGES = [[], [7], [5, 5, 5, 5], list(range(10)), list(range(10, 0, -1)),
         [10 ** 18, -(10 ** 18), 0], [-3, -1, -2], [1, 2] * 25]


def generate(rng):
    hi = rng.choice([3, 10, 1000])
    return [rng.randint(-hi, hi) for _ in range(rng.randint(0, 50))]''',
    "dicts": '''EDGES = [{}, {"a": 1}, dict(("k%d" % i, i) for i in range(30)),
         {"\\u00e9": -1, "\\u65e5": 2}, {"neg": -(10 ** 9)}]


def generate(rng):
    return dict(("k%d" % rng.randint(0, 30), rng.randint(-100, 100))
                for _ in range(rng.randint(0, 20)))''',
}

CUSTOM = '''EDGES = []   # add the card's edge cases here
generate = load({spec!r})   # called as generate(rng) with a random.Random'''


def resolve(spec, what, root):
    """Import MODULE:NAME with root on sys.path; UsageError with a fix when it fails."""
    if not re.match(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$", spec):
        raise UsageError("%s must be MODULE:FUNCTION (e.g. mymod:dedup), got %r" % (what, spec))
    module, _, name = spec.partition(":")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        obj = importlib.import_module(module)
        for part in name.split("."):
            obj = getattr(obj, part)
    except ImportError as e:
        raise UsageError("cannot import %s for %s: %s; run dev.py oracle from the directory "
                         "where `python3 -c 'import %s'` works" % (module, what, e, module))
    except AttributeError:
        raise UsageError("%s has no attribute %s (%s)" % (module, name, what))
    if not callable(obj):
        raise UsageError("%s (%s) is not callable" % (spec, what))
    return obj


def required_positional(fn):
    """Number of required positional parameters, or None when the signature is unknown."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    n = 0
    for prm in sig.parameters.values():
        if prm.kind in (prm.POSITIONAL_ONLY, prm.POSITIONAL_OR_KEYWORD) and \
                prm.default is prm.empty:
            n += 1
        elif prm.kind is prm.VAR_POSITIONAL:
            return None
    return n


def choose_call(mode, ref_fn):
    """Resolve --call auto from the reference's signature; (mode, note or None)."""
    need = required_positional(ref_fn)
    if mode == "auto":
        if need is not None and need > 1:
            return "args", None
        return "one", None
    if mode == "one" and need is not None and need > 1:
        return mode, ("the reference takes %d required arguments but --call one passes one; "
                      "every case will raise TypeError" % need)
    return mode, None


def smoke(ref_fn, gen_fn, call_mode, seed, n=10):
    """Run the reference on a few generated cases; (ok, total, first error type or None)."""
    rng = random.Random(seed)
    ok, err = 0, None
    for _ in range(n):
        try:
            x = gen_fn(rng)
        except Exception as e:
            return 0, n, "generator raised %s: %s" % (type(e).__name__, e)
        try:
            if call_mode == "args":
                if not isinstance(x, (tuple, list)):
                    return 0, n, ("--call args needs the generator to return a tuple of "
                                  "arguments, got %s" % type(x).__name__)
                ref_fn(*x)
            else:
                ref_fn(x)
            ok += 1
        except Exception as e:
            err = err or "%s: %s" % (type(e).__name__, e)
    return ok, n, err


def builtin_generator(gen):
    """The generate() function of a built-in generator, for the smoke check."""
    space = {}
    exec(GENERATORS[gen], space)
    return space["generate"]


def render(ref, new, gen, cases, seed, tol, out_path, root, call="one"):
    if gen in GENERATORS:
        generator = GENERATORS[gen]
    else:
        generator = CUSTOM.format(spec=gen[len("custom:"):])
    rel = os.path.relpath(root, os.path.dirname(os.path.abspath(out_path))).replace(os.sep, "/")
    comparison = ("relative tolerance %g (|a - b| <= tol x max(1, |b|))" % tol if tol
                  else "exact equality")
    call_doc = ("fn(*case): each case is a tuple of arguments" if call == "args"
                else "fn(case): each case is the single argument")
    return TEMPLATE.format(ref=ref, new=new, gen=gen, cases=cases, seed=seed, tol=tol, rel=rel,
                           generator=generator, comparison=comparison, call=call,
                           call_doc=call_doc)


# ------------------------------------------------------------------ command mode (any language)

NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$|^[-+]?(?:inf|nan)$", re.I)


def run_shell(cmd, stdin_bytes, timeout):
    """(exit code, stdout bytes, stderr tail) of `sh -c cmd` fed stdin_bytes; its whole
    process group is killed on timeout (exit None)."""
    import subprocess
    from devkit.common import kill_group, start_group
    try:
        proc = start_group(["/bin/sh", "-c", cmd], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise StartError("cannot run /bin/sh for %r: %s" % (cmd, e.strerror or e))
    try:
        out, err = proc.communicate(stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        proc.communicate()
        return None, b"", ["timed out after %g s" % timeout]
    except BaseException:
        kill_group(proc)
        raise
    tail = err.decode("utf-8", "replace").splitlines()[-5:]
    return proc.returncode, out, tail


def same_output(a, b, tol):
    """Byte-for-byte, or with tol > 0 token by token: numeric tokens within relative tol
    (NaN equals NaN), every other token exact, the same token count."""
    if a == b:
        return True
    if not tol:
        return False
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        return False
    for x, y in zip(ta, tb):
        if x == y:
            continue
        xs, ys = x.decode("utf-8", "replace"), y.decode("utf-8", "replace")
        if not (NUMBER.match(xs) and NUMBER.match(ys)):
            return False
        fx, fy = float(xs), float(ys)
        if fx != fx or fy != fy:
            if not (fx != fx and fy != fy):
                return False
            continue
        if fx != fy and abs(fx - fy) > tol * max(1.0, abs(fy)):
            return False
    return True


def head(data, limit=200):
    text = data.decode("utf-8", "replace")
    return repr(text if len(text) <= limit else text[:limit] + "...")


def cmd_mode(a):
    """Run --gen-cmd for each seed, feed its stdout to --ref-cmd and --new-cmd, and compare
    their exit codes and stdout. Exit 0 on agreement, 1 on the first mismatch or a vacuous
    run (the reference fails on most cases, or prints one identical output every time)."""
    if not a.gen_cmd or "{seed}" not in a.gen_cmd:
        raise UsageError("--gen-cmd is required with --ref-cmd and must contain {seed}, for "
                         "example --gen-cmd 'python3 gen.py {seed}'")
    if not a.new_cmd:
        raise UsageError("--ref-cmd needs --new-cmd")
    ok = 0
    outputs = set()
    checked = 0
    for k in range(a.cases):
        seed = a.seed + k
        code, case, tail = run_shell(a.gen_cmd.replace("{seed}", str(seed)), b"", a.timeout)
        if code != 0:
            raise CommandFailed("--gen-cmd failed on seed %d (exit %s); fix the generator "
                                "first" % (seed, code), details=tail)
        rc, rout, rtail = run_shell(a.ref_cmd, case, a.timeout)
        nc, nout, ntail = run_shell(a.new_cmd, case, a.timeout)
        checked += 1
        if rc == 0:
            ok += 1
            outputs.add(rout)
        if rc != nc or (rc == 0 and not same_output(nout, rout, a.tol)):
            msg = ("seed %d: outputs differ (exit ref %s, new %s)\n input: %s\n ref: %s\n new: %s"
                   % (seed, rc, nc, head(case), head(rout), head(nout)))
            if a.json:
                print_json({"schema": "devkit-oracle/1", "mode": "commands", "ok": False,
                            "cases": checked, "first_mismatch": seed, "message": msg})
                return 1
            sys.stdout.write("oracle: FAIL on case %d of %d\n%s\n" % (checked, a.cases, msg))
            for line in ntail:
                sys.stdout.write("  new stderr: %s\n" % line)
            return 1
    problems = []
    if ok * 2 < checked:
        problems.append("vacuous: the reference failed on %d of %d cases, so both sides "
                        "failing compares equal; fix the generator or the commands"
                        % (checked - ok, checked))
    if ok > 1 and len(outputs) == 1:
        problems.append("vacuous: the reference printed the same output on every case, so a "
                        "stub printing it passes; widen the generator")
    regen = ("dev.py oracle --ref-cmd %s --new-cmd %s --gen-cmd %s --cases %d --seed %d%s"
             % (shlex_quote(a.ref_cmd), shlex_quote(a.new_cmd), shlex_quote(a.gen_cmd),
                a.cases, a.seed, " --tol %g" % a.tol if a.tol else ""))
    if a.json:
        print_json({"schema": "devkit-oracle/1", "mode": "commands", "ok": not problems,
                    "cases": checked, "reference_ok": [ok, checked],
                    "distinct_outputs": len(outputs), "problems": problems,
                    "tol": a.tol, "seed": a.seed, "rerun": regen})
        return 1 if problems else 0
    for msg in problems:
        sys.stdout.write("oracle: FAIL: %s\n" % msg)
    if problems:
        return 1
    print("oracle: PASS on %d cases (seeds %d..%d; %s; reference ok on %d, %d distinct "
          "outputs)" % (checked, a.seed, a.seed + checked - 1,
                        "tolerance %g on numbers" % a.tol if a.tol else "byte for byte", ok,
                        len(outputs)))
    print("rerun it in CI or the project's test script: %s" % regen)
    print("sensitivity: point --new-cmd at a deliberately broken build (drop one output line) "
          "once and confirm this fails before trusting a pass")
    return 0


def shlex_quote(text):
    import shlex
    return shlex.quote(text)


def cmd_oracle(args):
    p = Parser(prog="dev.py oracle",
               description="Write a unittest comparing a new implementation with the reference "
                           "on seeded random and edge inputs, with an anti-cache check; or, "
                           "with --ref-cmd, compare two commands' stdout over generated cases.")
    p.add_argument("--ref", default=None, help="reference MODULE:FUNC (the old code, kept callable)")
    p.add_argument("--new", default=None, help="candidate MODULE:FUNC")
    p.add_argument("--ref-cmd", default=None, metavar="CMD",
                   help="any language: the reference command; reads a case on stdin, prints "
                        "the result (run through /bin/sh)")
    p.add_argument("--new-cmd", default=None, metavar="CMD", help="the candidate command")
    p.add_argument("--gen-cmd", default=None, metavar="CMD",
                   help="prints one case for {seed}, e.g. 'python3 gen.py {seed}'")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="seconds per command run in --ref-cmd mode (default 60)")
    p.add_argument("--gen", default="lists",
                   help="ints | floats | strings | lists | dicts | custom:MODULE:FUNC (default lists)")
    p.add_argument("--call", default="auto", choices=("auto", "one", "args"),
                   help="one: fn(case); args: fn(*case), the generator returns a tuple of "
                        "arguments; auto (default): args when the reference has more than one "
                        "required positional parameter")
    p.add_argument("--cases", type=int, default=None,
                   help="seeded random cases, on top of the edge cases (default 300; 200 in "
                        "--ref-cmd mode)")
    p.add_argument("--seed", type=int, default=0, help="seed for the generated inputs (default 0)")
    p.add_argument("--tol", type=float, default=0.0, help="relative tolerance for floats")
    p.add_argument("--out", default=None,
                   help="default tests/test_oracle_<new module>_<new func>.py")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if a.cases is None:
        a.cases = 200 if a.ref_cmd else 300
    if a.cases < 1:
        raise UsageError("--cases must be at least 1")
    if a.tol < 0:
        raise UsageError("--tol must be >= 0")
    if a.ref_cmd or a.new_cmd or a.gen_cmd:
        if a.ref or a.new:
            raise UsageError("give --ref/--new (Python callables) or --ref-cmd/--new-cmd/"
                             "--gen-cmd (commands), not both")
        if not a.ref_cmd:
            raise UsageError("--new-cmd and --gen-cmd need --ref-cmd")
        if a.timeout <= 0:
            raise UsageError("--timeout must be positive")
        return cmd_mode(a)
    if not (a.ref and a.new):
        raise UsageError("give --ref MODULE:FUNC and --new MODULE:FUNC, or --ref-cmd, "
                         "--new-cmd and --gen-cmd for commands in any language")
    if a.gen not in BUILTIN_GENS and not a.gen.startswith("custom:"):
        raise UsageError("--gen must be one of %s or custom:MODULE:FUNC" % ", ".join(BUILTIN_GENS))
    root = os.getcwd()
    ref_fn = resolve(a.ref, "--ref", root)
    resolve(a.new, "--new", root)
    if a.gen.startswith("custom:"):
        gen_fn = resolve(a.gen[len("custom:"):], "--gen", root)
    else:
        gen_fn = builtin_generator(a.gen)
    call_mode, call_note = choose_call(a.call, ref_fn)
    if call_mode == "args" and not a.gen.startswith("custom:"):
        raise UsageError("the reference takes several arguments (or --call args was given), so "
                         "--gen must be custom:MODULE:FUNC returning a tuple of arguments")
    module, func = a.new.split(":", 1)
    stem = "%s_%s" % (module.replace(".", "_"), func.replace(".", "_"))
    out = a.out or os.path.join("tests", "test_oracle_%s.py" % stem)
    if os.path.exists(out) and not a.force:
        raise DevError("%s exists; pass --force to overwrite it or --out to choose another path"
                       % out)
    ok, total, err = smoke(ref_fn, gen_fn, call_mode, a.seed)
    warnings = [call_note] if call_note else []
    if ok * 2 < total:
        warnings.append("the reference raised on %d of %d sample cases (first: %s); the "
                        "generated test will fail as vacuous until the generator or --call "
                        "is fixed" % (total - ok, total, err))
    text = render(a.ref, a.new, a.gen, a.cases, a.seed, a.tol, out, root, call_mode)
    parent = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    rel = out.replace(os.sep, "/")
    call_text = "fn(*case)" if call_mode == "args" else "fn(case)"
    if a.json:
        print_json({"schema": "devkit-oracle/1", "out": rel, "ref": a.ref, "new": a.new,
                    "gen": a.gen, "call": call_mode, "cases": a.cases, "seed": a.seed,
                    "tol": a.tol, "reference_ok": [ok, total], "warnings": warnings,
                    "run": "python3 -m unittest -v %s" % rel})
        return 0
    for w in warnings:
        sys.stderr.write("warning: %s\n" % w)
    print("wrote %s (%d cases + edge cases, seed %d, %s, calls %s; reference ok on %d of %d "
          "sample cases)" % (rel, a.cases, a.seed, "tol %g" % a.tol if a.tol else "exact",
                             call_text, ok, total))
    print("run it before any timing: python3 -m unittest -v %s" % rel)
    print("sensitivity: point --new at a deliberately broken copy (return [] or drop one "
          "element) once and confirm the test fails before trusting a pass")
    return 0
