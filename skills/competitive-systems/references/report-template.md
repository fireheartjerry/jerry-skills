# Report template

Fill it whenever the work reaches a card (procedure step 4 onward). A stop at Gate 1, 2 or 3
is answered in plain words instead (`worked-example.md`, Examples C and D). Every line is
either filled from something that ran or left as `<...>` and named under `Not verified:`.

`python3 <skill-dir>/scripts/dev.py report --compare compare.json` fills the measured lines
(Before, Verdict, Class, Noise floor, Oracle, Card facts, Environment, Protocol) from the JSON
that `dev.py compare --json`, `dev.py scale --json` and `dev.py aa --save` write, and lists
every field it could not fill under `Not verified:`. `--set FIELD=TEXT` fills the rest
(`tooling.md`, report).

```
Problem: <one line>. Input: n=<...> now, <...> at 10x. Target: <...>.
Gate 1: <exponent x.x at n=a..b | makespan m vs LB l | selection ratio r | must-keep t + candidates c vs B | calls k for d distinct>; CPU <user+sys> of <wall> wall -> <algorithmic | I/O | constants | repeated work>.
Gate 2: brute force at 10x n ~ <ops> ops ~ <time> -> <proceed | stop>.
Gate 3: Amdahl cap <x>x for share <p> -> <proceed | stop>.
Noise floor: A/A ratio CI <lo>..<hi> on <runs> runs; min effect <x>%.
Card: <name> (<file>). Precondition: <test and result>.
Card facts: <the numeric facts quoted from the card>.
Invariant: <what must be equal>.
Oracle: <reference>, <N> random cases, sizes <lo>..<hi>, seed <s>: <pass | fail>.
Before: median <t> (IQR <q>, runs=<k>) at n=<...>; After: median <t> (IQR <q>, runs=<k>).
Class: exponent <x.x> -> <y.y> (scaling cards) | <makespan/LB, selection ratio, hit rate or tokens> <before> -> <after> (other cards).
Verdict: <k>x faster (95% CI <lo>x..<hi>x, p=<p>; medians <before> -> <after>) | no detectable change | inconclusive.
Guards: oracle before timing <pass>; fresh inputs <yes>; harness untouched <yes>; implausibility <within Amdahl cap>.
Kept: <interface, ordering, fallbacks>. Changed: <...>. New dependency: <none | name, why>.
Environment: <Python version, platform, CPU count, load, git revision>.
Protocol: <metric, runs per arm, minimum effect and its source, alpha, bootstrap resamples, seed>.
Not verified: <every field above still in <...>, and anything stated but not executed>.
```

## One filled line per field (from `worked-example.md`, Example A)

- Problem: `Problem: ETL dedup step takes 19 min/day. Input: n=1.2M events now, 12M at 10x. Target: under 1 min.`
- Gate 1: `Gate 1: exponent 1.91 at n=10k..80k (95% CI 1.83..1.97); CPU 5.02 s of 5.08 s wall -> algorithmic.`
- Gate 2: `Gate 2: brute force at 10x n ~ 1.2e12 ops ~ 34 h by budget (1.2M: 1,224 s, matching the reported 19 min) -> proceed.`
- Gate 3: `Gate 3: Amdahl cap 50x for share 0.98 (profile: dedup 2.21 s of 2.26 s) -> proceed.`
- Noise floor: `Noise floor: A/A ratio CI 0.966..1.044 on 20 runs; min effect 7.8%.`
- Card: `Card: Hash-key dedup (exact duplicates) (cards-lookup-dedup.md). Precondition: key(e) reproduces same() on 10,000 random pairs of distinct rows incl. NaN: pass.`
- Card facts: `Card facts: none listed for this card; Gate 2 numbers quoted instead.`
- Invariant: `Invariant: keep-first output identical, same order; NaN rows never merged.`
- Oracle: `` Oracle: `python3 -m unittest test_oracle_dedup.py`: exit 0 (pass); last line: OK. ``
- Before: `Before: median 1.37 s (IQR 42.6 ms, runs=10); After: median 47.6 ms (IQR 4.6 ms, runs=10).`
- Class: `Class: exponent 1.91 (95% CI 1.83..1.97 at n=10000..80000) -> 0.96 (95% CI 0.91..1.12 at n=100000..800000).`
- Verdict: `Verdict: 28.78x faster (95% CI 25.87x..30.03x, p=5.41e-06; medians 1.37 s -> 47.6 ms).`
- Guards: `Guards: oracle before timing pass (incl. anti-cache); fresh inputs yes (new process and regenerated events per run); harness untouched yes; implausibility within Amdahl cap (28.8x < 50x).`
- Kept: `Kept: signature, return order, NaN rows never merged. Changed: set-based keep-first. New dependency: none.`
- Environment: `Environment: Python 3.9.6 on macOS-26.5-arm64-arm-64bit; 15 CPUs; load 10.4; git 64b1e16 (dirty).`
- Protocol: `Protocol: metric wall; runs 10/10; min effect 7.8% (from .devkit/aa.json measured 2026-10-21T00:59:22-04:00); alpha 0.05; 5000 bootstrap resamples, seed 0; Mann-Whitney exact.`
- Not verified: `Not verified: the 12M-row run (projected from the exponent); the 1.2M before time is the user's figure, not rerun; measured with load average 10 on the machine (warnings above).`

These are the lines `dev.py report` printed for that example, with the hand-written fields
passed as `--set`; `worked-example.md` shows the commands that produced the measured ones.
