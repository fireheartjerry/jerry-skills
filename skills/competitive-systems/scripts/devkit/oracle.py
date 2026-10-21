"""`dev.py oracle`: write a differential unittest of a new implementation against the old one.

The generated file imports both callables by `module:function` (resolved from the directory
dev.py ran in), feeds both the same seeded random inputs plus edge cases, compares results
(exceptions compare by type; NaN equals NaN; floats within --tol relative tolerance), and
adds an anti-cache test: call the new function, mutate the same input object in place, call
again, compare with the reference on the mutated input. A result cached by object identity
fails it. Metamorphic stubs are included as comments. Existing files are never overwritten
without --force.
"""
import importlib
import os
import re
import sys

from devkit.common import DevError, Parser, UsageError, print_json

BUILTIN_GENS = ("ints", "floats", "strings", "lists", "dicts")

TEMPLATE = '''"""Differential test written by dev.py oracle: {new} against the reference {ref}.

Inputs: {cases} seeded random cases (generator {gen}, seed {seed}) plus the edge cases below.
Comparison: {comparison}; exceptions compare by type; NaN equals NaN.
The anti-cache test calls the new function, mutates the same input object in place, calls
again and compares with the reference on the mutated input, so a result cached by object
identity (id(), weakref keys, "same list as last time") fails.
Regenerate: dev.py oracle --ref {ref} --new {new} --gen {gen} --cases {cases} --seed {seed} --tol {tol} --force
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

{generator}


def outcome(fn, x):
    try:
        return ("ok", fn(x))
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


class OracleTest(unittest.TestCase):
    def test_matches_reference(self):
        keep = []   # every input stays alive, so object ids are never reused between calls
        for i, x in enumerate(inputs()):
            a, b = copy.deepcopy(x), copy.deepcopy(x)
            keep.extend((a, b))
            want, got = outcome(REF, a), outcome(NEW, b)
            if not same(got, want):
                self.fail("case %d: input %s\\n new: %s\\n ref: %s" % (
                    i, short(x), short(got), short(want)))

    def test_anti_cache_mutated_input(self):
        rng = random.Random(SEED + 1)
        checked = 0
        for x in inputs():
            if not isinstance(x, (list, dict)):
                continue
            outcome(NEW, x)                       # first call on this very object
            if not mutate(x, rng):
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
            self.skipTest("inputs are immutable; use fresh inputs per timed iteration instead")

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


def render(ref, new, gen, cases, seed, tol, out_path, root):
    if gen in GENERATORS:
        generator = GENERATORS[gen]
    else:
        generator = CUSTOM.format(spec=gen[len("custom:"):])
    rel = os.path.relpath(root, os.path.dirname(os.path.abspath(out_path))).replace(os.sep, "/")
    comparison = ("relative tolerance %g (|a - b| <= tol x max(1, |b|))" % tol if tol
                  else "exact equality")
    return TEMPLATE.format(ref=ref, new=new, gen=gen, cases=cases, seed=seed, tol=tol, rel=rel,
                           generator=generator, comparison=comparison)


def cmd_oracle(args):
    p = Parser(prog="dev.py oracle",
               description="Write a unittest comparing a new implementation with the reference "
                           "on seeded random and edge inputs, with an anti-cache check.")
    p.add_argument("--ref", required=True, help="reference MODULE:FUNC (the old code, kept callable)")
    p.add_argument("--new", required=True, help="candidate MODULE:FUNC")
    p.add_argument("--gen", default="lists",
                   help="ints | floats | strings | lists | dicts | custom:MODULE:FUNC (default lists)")
    p.add_argument("--cases", type=int, default=300,
                   help="seeded random cases, on top of the edge cases (default 300)")
    p.add_argument("--seed", type=int, default=0, help="seed for the generated inputs (default 0)")
    p.add_argument("--tol", type=float, default=0.0, help="relative tolerance for floats")
    p.add_argument("--out", default=None, help="default tests/test_oracle_<new func>.py")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    a = p.parse_args(args)
    if a.cases < 1:
        raise UsageError("--cases must be at least 1")
    if a.tol < 0:
        raise UsageError("--tol must be >= 0")
    if a.gen not in BUILTIN_GENS and not a.gen.startswith("custom:"):
        raise UsageError("--gen must be one of %s or custom:MODULE:FUNC" % ", ".join(BUILTIN_GENS))
    root = os.getcwd()
    resolve(a.ref, "--ref", root)
    resolve(a.new, "--new", root)
    if a.gen.startswith("custom:"):
        resolve(a.gen[len("custom:"):], "--gen", root)
    func = a.new.split(":", 1)[1].replace(".", "_")
    out = a.out or os.path.join("tests", "test_oracle_%s.py" % func)
    if os.path.exists(out) and not a.force:
        raise DevError("%s exists; pass --force to overwrite it or --out to choose another path"
                       % out)
    text = render(a.ref, a.new, a.gen, a.cases, a.seed, a.tol, out, root)
    parent = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    rel = out.replace(os.sep, "/")
    if a.json:
        print_json({"schema": "devkit-oracle/1", "out": rel, "ref": a.ref, "new": a.new,
                    "gen": a.gen, "cases": a.cases, "seed": a.seed, "tol": a.tol,
                    "run": "python3 -m unittest -v %s" % rel})
        return 0
    print("wrote %s (%d cases + edge cases, seed %d, %s)" % (
        rel, a.cases, a.seed, "tol %g" % a.tol if a.tol else "exact"))
    print("run it before any timing: python3 -m unittest -v %s" % rel)
    return 0
