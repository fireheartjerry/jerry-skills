# dev.py command reference

`scripts/dev.py` is the skill's one tool: standard-library Python 3.8+, no dependencies,
and no network access of its own. The project tools it runs never install anything: npm steps
run as `npm exec --no -- <bin>`, and a binary missing from `node_modules/.bin` is reported as
not installed. Run it as `python3 <skill-dir>/scripts/dev.py <command> [options]`, where
`<skill-dir>` is the directory holding SKILL.md, from the project directory so relative paths
in measured commands resolve. `dev.py help` lists the commands; `dev.py <command> --help`
shows every option.

Conventions shared by every command:

- Exit codes: 0 success or verdict produced; 1 a gate failed, a measured command failed, an
  oracle failed or a drift check found drift; 2 usage error or no catalog match; 3 an
  external tool or the measured command could not be started.
- Errors are one line starting `dev.py <command>:` with the fix; no tracebacks for expected
  errors (bad arguments, missing files, failing measured commands).
- `--json` prints exactly one JSON object on stdout with `"schema": "devkit-<command>/1"`;
  warnings and notes go to stderr.
- Every random draw (bootstrap, run order, generated inputs) takes `--seed` (default 0) and
  records it in the JSON.
- Repository root: `--repo PATH`, else `$DEVKIT_REPO` (the `tools/dev.py` shim sets it),
  else the git top level, else the current directory with a note. State lives under `<root>/.devkit/` (see the end of the `tools/dev.json` section).

## Evidence commands

### bench

`dev.py bench --arm NAME=CMD [--arm NAME=CMD ...] [--runs 7] [--warmup 1] [--order interleave|random|blocked] [--seed 0] [--shell] [--check-output] [--timeout S] [--input FILE]... [--min-effect X] [--out FILE] [--json]`

Times each arm as an argv list (`--shell` runs `/bin/sh -c` and says so, because shell
startup then sits inside every timing). Order `interleave` runs A B A B; `random` shuffles
each round with the seed; `blocked` runs one arm then the next. Per run: wall
(`time.perf_counter`), the child's user+sys CPU and max RSS, exit code, and with
`--check-output` the sha256 of stdout (differing outputs warn). Metadata: input hashes, git
revision and dirty flag, Python, platform, CPU count, load average, timestamp, seed. One arm
prints the run table and the `Gate 1:` CPU-versus-wall line; two or more print the compare
output for the first two. Each run gets `/dev/null` as stdin and its own process group;
`--timeout` kills the whole group, so `--shell` pipelines and background children die with
it. `--input` takes one file per flag. A failing or timed-out command exits 1 with its last
20 stderr lines; one that cannot start exits 3. Schema `devkit-bench/1`.
Example: `dev.py bench --arm before="python3 old.py 40000" --arm after="python3 new.py 40000" --runs 10 --check-output --out bench.json`

### compare

`dev.py compare BENCH.json | A.json B.json [--before NAME --after NAME] [--metric wall|cpu|rss] [--min-effect X] [--alpha 0.05] [--resamples 5000] [--seed 0] [--fail-on slower,inconclusive] [--json]`

Per arm: n, median, IQR, MAD, CV, min, max, Tukey outliers (kept), Spearman rho of time
against run index. Effect: ratio of medians with a seeded bootstrap 95% CI, Hodges-Lehmann
shift, Mann-Whitney U (exact when the split count allows, else normal with tie correction).
Minimum effect: `--min-effect`, else a `.devkit/aa.json` under 7 days old, else 5% with a
note. Verdict rule: FASTER when the CI lower bound exceeds 1 + min effect and the one-sided
p < alpha; SLOWER mirrored; NO DETECTABLE CHANGE when the whole CI lies inside
[1/(1 + min effect), 1 + min effect]; INCONCLUSIVE otherwise, with a suggested run count.
Prints the two paste-ready lines (Before/After, Verdict). `--fail-on` turns the named
verdicts into exit 1 for CI. Schema `devkit-compare/1`.
Example: `dev.py compare bench.json --json > compare.json`

### aa

`dev.py aa [--runs 20] [--warmup 1] [--seed 0] [--resamples 5000] [--shell] [--save] [--json] -- CMD [ARGS...]`

Runs one command as two interleaved arms and reports the ratio CI that pure noise produces,
and the suggested minimum effect `max(2%, 2 x CI half-width)`. `--save` writes
`.devkit/aa.json` for `compare`. Schema `devkit-aa/1`.
Example: `dev.py aa --runs 20 --save -- python3 step.py 40000`

### scale

`dev.py scale --sizes n1,n2[,n3...] [--baseline 'CMD' | --no-baseline] [--runs 7] [--warmup 1] [--seed 0] [--json] -- CMD [ARGS...]`

Doubling test: every `{n}` in the command is replaced by each size; a startup baseline
(`python3 -c pass` for Python commands, or `--baseline`) is timed and subtracted before the
log-log fit; the exponent carries a bootstrap 95% CI. Four sizes recommended; fewer than
three warn; a smallest median under 10x the baseline warns to raise sizes until it is at
least 0.5 s. Classes: under 1.3 about linear, 1.3 to 1.7 inconclusive, 1.7 to 2.5 about
quadratic, 2.5 to 3.5 about cubic, over 3.5 worse. `--baseline` with `--no-baseline` is a
usage error. Schema `devkit-scale/1`.
Example: `dev.py scale --sizes 10000,20000,40000,80000 --json -- python3 step.py {n} > scale-before.json`

### bound

`dev.py bound amdahl --p FRACTION --s LOCAL_SPEEDUP [--json]`: end-to-end speedup
`1/((1-p) + p/s)` and the cap `1/(1-p)`. Example: `dev.py bound amdahl --p 0.12 --s 100`
prints 1.13x overall and a 1.14x cap.

`dev.py bound makespan --durations FILE [--workers M] [--pinned NAME ...] [--current alphabetical|roundrobin|FILE] [--json]`:
loads `{name: seconds}` or `{"jobs": {...}, "runners": M}`, replaces missing, non-positive
or duplicate durations by the p50 of the valid ones and lists them, replays the current plan
(`alphabetical` = sorted names in m contiguous slices; `roundrobin` = dealt in file order; or
a plan file), prints LB = max(sum/m, longest, pinned total) with the binding term, the LPT
plan and makespan/LB, the (4/3 - 1/(3m)) bound as a number, and the m from which the longest
job binds.

`dev.py bound ops --n N (--ops EXPR | --time-at-n S --exponent E) [--runtime cpython|compiled] [--path request|batch] [--json]`:
Gate 2 arithmetic. EXPR uses `n`, numbers, `+ - * / ** ()`, `log`, `log2`, `sqrt`; budget
1e7 ops/s for CPython, 1e9 compiled; prints time at n and 10n, the flip size where the
threshold (100 ms request path, 1 s batch) is crossed, and `proceed` or `stop`.
Example: `dev.py bound ops --n 2000 --ops "40*n" --path batch` (stop; flip size 250,000).
Schema for all three: `devkit-bound/1`.

### changepoint

`dev.py changepoint (SERIES | --perf NAME) [--min-size 5] [--alpha 0.01] [--repo PATH] [--json]`

SERIES is one number per line, `label,value` lines, or a JSON list of numbers or of
`{"label", "value"}`. `--perf NAME` reads the medians recorded by `perf run` in
`.devkit/perf/NAME.jsonl`, labelled by short commit (`-dirty` when the tree had changes). Deterministic binary segmentation with Welch's t and a Bonferroni
correction over candidate splits; prints each change point with medians before and after,
percent change and p. Change points need triage: a single outlier is not a change.
Schema `devkit-changepoint/1`.

### catalog

`dev.py catalog [--symptom TEXT] [--id ID] [--class CLASS] [--list] [--json]`

Searches `references/techniques.json` (one entry per card heading) by token overlap with each
card's symptom, aliases, title and class; prints, best first, the title, file and heading,
tier, precondition, reject-when list, complexity, oracle and the facts to quote, each followed
by `card says: <the card sentence that states it>` (code blocks excluded) so the answer can
carry the claim, not a bare number; `--json` adds that list as `fact_sentences`. No match exits
2 with `no card matches; try --list`. Schema `devkit-catalog/1`.
Example: `dev.py catalog --symptom "CI takes 40 minutes on 8 runners"`

### report

`dev.py report --compare COMPARE.json [--scale-before S1.json --scale-after S2.json] [--aa AA.json] [--oracle-cmd CMD] [--set FIELD=TEXT]... [--out report.md] [--json]`

Fills `references/report-template.md`: Before and Verdict (always with both absolute
medians) from the compare JSON (or a bench JSON holding a `compare` object), Class from the
two scale files, Noise floor from the A/A file, Oracle by running the command (exit code and
last output line; a failing oracle makes the report exit 1), Environment and Protocol from
the metadata, Card and Card facts from `--set card=<id>`. `--set precondition=TEXT` fills the
Card line's precondition; `--set "not verified=TEXT"` adds to that line. Every field still in
`<...>` is named under `Not verified:`. `--json` prints `report` (the text), `unfilled`,
`oracle_ok` and `out`; schema `devkit-report/1`.
Example: `dev.py report --compare compare.json --scale-before sb.json --scale-after sa.json --aa .devkit/aa.json --oracle-cmd "python3 -m unittest test_oracle_dedup.py" --set card=hash-key-dedup`

### oracle

`dev.py oracle --ref MODULE:FUNC --new MODULE:FUNC [--gen ints|floats|strings|lists|dicts|custom:MODULE:FUNC] [--cases 300] [--seed 0] [--tol 0] [--out PATH] [--force] [--json]`

Writes a unittest file (default `tests/test_oracle_<new func>.py`, relative to the current
directory, which is also where the modules are imported from) that runs both callables on
seeded random inputs plus edge cases (empty, single, duplicates, sorted, reversed, large,
negative, Unicode and whitespace strings, NaN and infinities), compares results (exceptions
by type, NaN equal to NaN, `--tol` as relative tolerance), and adds the anti-cache test (call,
mutate the same input in place, call again, compare with the reference). A custom generator is
called as `generate(rng)`. Refuses to overwrite an existing file without `--force` (exit 1).
Run the file with `python3 -m unittest -v <file>` or `python3 <file>`. `--json` prints the
path written, the options and the command to run it; schema `devkit-oracle/1`.
Example: `dev.py oracle --ref dedup_old:dedup --new dedup_new:dedup --gen custom:gen_events:case --seed 7`

## Repository commands

These read `tools/dev.json` (next section) when it exists and detect the rest. Changed files
default to `git diff --name-status HEAD` plus untracked files; `--base REF` diffs
`REF...HEAD` plus the working tree, and falls back to `origin/REF` with a note when REF does
not resolve (fresh clones and CI checkouts often have only the remote branch); FILES
(relative to the current directory or absolute) replace both. Statuses in `--json`: `A`, `M`,
`D`, `R`, `U` (unmerged) and `?` (untracked). A rename counts as the new path plus a deletion
of the old one. Outside a git
work tree with no FILES, the command exits 2 with `no git: pass files explicitly`; a FILE
that does not exist counts as deleted.

### detect

`dev.py detect [--repo PATH] [--json]`

Languages by marker files at the root and one level down, each with its root directory:
Python (`pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `Pipfile`,
`tox.ini`, or any `.py` file outside the excluded directories), TypeScript or JavaScript
(`package.json`; TypeScript when a `tsconfig*.json` exists or `typescript` is a dependency),
Go (`go.mod`), Rust (`Cargo.toml`). The JS package manager comes from the lockfile:
`pnpm-lock.yaml` pnpm, `yarn.lock` yarn, `bun.lockb` bun, else npm. Then, per language and
step, the command `check` will run, or why the step is skipped or not runnable. Schema
`devkit-detect/1`.

Resolution when `commands.<language>.<step>` is null takes the first available tool. A tool is
available when it is on PATH, importable by the interpreter running dev.py (Python tools), or
declared by the project (`pyproject.toml`, requirements files, `package.json`); declared but
not installed shows as not runnable, which `check` reports as skipped with an install hint.
JS package binaries run from `node_modules/.bin` (the package's, then the repository root's)
through `npm exec --no --` (other managers: `<pm> exec`, which runs only local binaries),
else from PATH; a declared binary in neither place is not runnable, and `detect` notes when
`package.json` declares dependencies but `node_modules` is absent (Yarn Plug'n'Play counts as
installed).

| Step | Python | TypeScript / JavaScript | Go | Rust |
|---|---|---|---|---|
| lint | `ruff check`, else `flake8`, else `python3 -m pyflakes`, else `python3 -m compileall -q` | script `lint`, else eslint, else skipped | `gofmt -l` (fails on any output) then `go vet` on the changed packages | `cargo fmt --check` then `cargo clippy -q` when installed, else `cargo check -q` |
| typecheck | `mypy` when configured (`[tool.mypy]`, `mypy.ini`, setup.cfg `[mypy]`), else `pyright` when available, else skipped | script `typecheck` or `type-check`, else `tsc --noEmit -p <tsconfig>`, else skipped | covered by `go vet` | covered by `cargo check` or clippy |
| test (full) | `python3 -m pytest -q` when pytest is configured, declared or installed, else `python3 -m unittest discover -s <tests dir> -p 'test*.py'` when that reaches every test file, else `python3 -m unittest <files>` once per directory over every file `test_globs` matches | script `test`, else `vitest run`, else `jest`, else skipped | `go test ./...` | `cargo test -q` |
| test (targets) | the runner plus the test files (unittest: one run per directory) | `vitest run` or `jest` plus the files, else the `test` script plus the files | `go test` plus the package directories | `cargo test -q -p <crate> ...` |
| record format | JUnit XML (`--junitxml`); unittest through a bundled reporter | vitest `--reporter=json`, jest `--json` | `go test -json` | one record per run with the wall time |

Lint and typecheck tools receive the changed files of their language (package.json scripts
and cargo run on the whole project); a language with no changed files runs nothing. `python3` in a command runs as the interpreter that
runs dev.py, so a virtual environment's tools are found by running dev.py with its python.
`compileall` writes its bytecode to a temporary directory, never into the repository. A
changed path that starts with `-` is passed as `./<path>` so no tool reads it as an option.
`--timeout` kills the gate's whole process group.

The unittest full run must reach every test file, or a change whose tests live outside the
discovered tree would pass untested: `discover` only finds files in one directory and in
subdirectories that hold `__init__.py`. When any file matched by `test_globs` lies outside
that, the full run lists every matched file, one `python3 -m unittest FILE...` process per
top-level directory (the file's directory, or above it while directories hold
`__init__.py`) with that directory on `PYTHONPATH`, as `discover -s` would put it on
`sys.path`. Sibling imports such as `import helpers` then resolve, and same-named helpers in
different directories never share a process. Targeted runs are split the same way.

### check

`dev.py check [FILES...] [--base REF] [--strict] [--no-tests] [--timeout S] [--repo PATH] [--json]`

Gates in stages across the detected languages, stopping at the first failure: lint on the
changed files, typecheck on the changed files, the affected tests (`affected` below: selected
targets, the full suite, or nothing), then map drift when `.devkit/repo-map.json` exists.
Each gate records name, language, command, status (`pass`, `fail` or `skipped`), seconds, the
first 25 output lines, the last 15 when the output is longer, and a fix hint. A skipped gate
(missing tool, nothing configured) does not fail the check unless `--strict`. Exit 0 when no
gate failed, 1 otherwise. Schema `devkit-check/1`. On the bundled Python fixture
(`tests/fixtures/repos/py`, no linters installed, `--no-tests` so the output stays short):

```
lint (python)          pass     111.0 ms  python3 -m compileall -q pkg/core.py
typecheck (python)     skipped  no type checker configured (mypy config or pyright); set commands.python.typecheck in tools/dev.json
note: tests not run (--no-tests)
check: PASS (1 file(s), 1 gate(s) skipped)
```

A failure prints the gate's output and fix, then `check: FAIL at <gate> (<language>)`.

### lint and typecheck

`dev.py lint [FILES...] [--base REF] [--all] [--strict] [--timeout S] [--repo PATH] [--json]`
and the same for `dev.py typecheck`: one stage of `check` on the changed files, or on every
file of each language with `--all`. Go and Rust typechecking is part of lint; `typecheck`
says so. Schemas `devkit-lint/1`, `devkit-typecheck/1`.

### test

`dev.py test [--lang LANG] [--record | --no-record] [--strict] [--timeout S] [--repo PATH] [--json] [-- RUNNER_ARGS...]`

Runs the full suite of each detected language, streaming the runner's output (not with
`--json`). Runner arguments after `--` need a single language; for unittest and Go they
replace the default discovery (`dev.py test -- <test file>`). Recording is on by default when
the runner has a report format (table above) and off otherwise; `--record` on a runner
without one writes a single record for the whole run. When the suite is a `test` script whose
runner is vitest or jest, recording runs that runner directly so it can write its report, and
says so. Each run writes `.devkit/runs/<utc stamp>-<language>.jsonl`, one JSON
object per test:

`{"run_id", "nodeid", "target", "outcome": "passed|failed|error|skipped", "duration_seconds", "timestamp"}`

`target` uses the same form as `affected` (test file path, `./<dir>`, crate), so durations
join the selection; subtests fold into their test; a module that fails to import or build is
one `error` record. The summary line names the file and the outcome counts. A configured test
command records per test only when it contains `{report}` and `commands.<language>.test_report`
names the format (`junit`, `vitest` or `jest`). Schema `devkit-test/1`.

### affected

`dev.py affected [FILES...] [--base REF] [--explain] [--repo PATH] [--json]`

Prints the test targets, space-separated (one `<language>: ...` line per language when
several), or `full: <reason>` or `none: <reason>`. Rules, in order, each failing closed:

1. A changed path matching `sensitive` gives `full: sensitive file: <path>`. One exception:
   an untracked `tools/dev.json` (just written by `init`) is listed as ignored with a note to
   commit it, since it changes no code and selection already reads it from disk; once
   tracked, a change to it is sensitive again.
2. A deleted or renamed-away path gives `full: deleted or renamed: <path>` (the graph comes
   from the working tree, so the old dependents are unknown). A deleted path that matches
   `ignore` is dropped instead: by definition it affects no test.
3. Paths matching `ignore` are dropped; when nothing remains, `none: only ignored paths changed`.
4. A changed test file (`test_globs`) selects itself.
5. A source file maps through `test_map` (exact key, else every matching glob key; the listed
   targets replace the graph result), else every test file that imports it transitively
   (reverse breadth-first search over the import graph), else a name match (`test_<stem>.py`,
   `<stem>_test.py`, `<stem>.test.*`, `<stem>.spec.*`). A file with none of these gives
   `full: unmapped source file: <path>`.
6. More than `max_changed_files` (default 25) remaining paths give `full`.

Import graphs: Python by `ast` (absolute and relative imports, `importlib.import_module` with
a literal, first-party roots from top-level packages, top-level modules and `src/` layouts,
sibling helper modules); JS/TS by import, export-from, `require` and dynamic `import()`, with
extension and `index` resolution and `tsconfig` `paths` aliases of the `"@/*": ["src/*"]`
form; Go by package directory within the `go.mod` module; Rust by crate, from workspace
members and path dependencies. Go selects every impacted package directory (`go test` compiles
packages without tests too); Rust selects every impacted crate (`-p name`).

`--explain` prints, per target, the chain from the target to the changed file. On the bundled
TypeScript and Rust fixtures:

```
$ dev.py affected src/paginate.ts --explain
src/paginate.test.ts
  src/paginate.test.ts <- src/index.ts <- src/paginate.ts (changed)
$ dev.py affected crates/core/src/lib.rs --explain
-p api -p core
  api <- core <- crates/core/src/lib.rs (changed)
  core <- crates/core/src/lib.rs (changed)
```

`--json` gives `mode` (`selected`, `full` or `none`), `reason`, `targets` and `args` per
language (`args` holds the argv form, `-p` included for Rust), `chains`, `changed`, `ignored`
and `notes`. Schema `devkit-affected/1`. Dynamic imports with computed names, plugins loaded by
name, and files read by path are invisible to the graph: map them in `test_map` or mark them
`sensitive`.

## Test history commands

`slowest`, `flakes`, `smart` and `shards` read the records `dev.py test` writes to
`.devkit/runs/*.jsonl`. Records are deduplicated by `(run_id, nodeid)`, the last one winning.
Per run, a target failed when any of its records failed or errored, passed when one passed and
none failed, and was skipped otherwise; its duration in that run is the sum of its records'
durations. Per target: runs, passes, failures, skips, failure rate (failures over passes plus
failures), total and maximum duration, p50 duration (median over runs, skip-only runs left
out when any run executed), last seen and last failed. A target with no recorded duration is
estimated at the p50 of the known targets, or 5 s when nothing is known, and is marked
`estimated`. Unreadable lines are skipped with a warning. All four take `--repo PATH` and
`--json`; all but `smart` take `--days N` to keep only recent records. With no history,
`slowest` and `flakes` print a hint to run `dev.py test`, `smart` estimates every target at
5 s, and `shards` needs TARGETS (else exit 2).

### slowest

`dev.py slowest [--limit 20] [--days N] [--json]`

Targets by p50 duration, descending, ties by name, with the maximum and the run count. The
median is used so one slow outlier run does not reorder the list; `--limit 0` prints every
target. Schema `devkit-slowest/1`.

### flakes

`dev.py flakes [--min-runs 3] [--max-rate 0.5] [--write-quarantine] [--days N] [--json]`

Targets with both passes and failures over at least `--min-runs` executed runs and a failure
rate at most `--max-rate`, highest rate first. A test that failed and was then fixed also has
mixed outcomes, so the output says to rerun a candidate at one commit before calling it
flaky. `--write-quarantine` writes `.devkit/quarantine.json`:
`{"schema": "devkit-quarantine/1", "generated_at", "policy": {"min_runs", "max_rate"}, "targets": [{"target", "runs", "failures", "failure_rate"}]}`.
`smart` never picks a quarantined target as optional, still runs it when it is required, and
lists the quarantine in its output. Schema `devkit-flakes/1`.

### smart

`dev.py smart [FILES...] [--base REF] [--budget S] [--repo PATH] [--json]`

Budgeted test selection for one change. Changed files are gathered as for `affected`.

- Required: the `affected` targets (reason `affected`) plus `always_run` from tools/dev.json
  (reason `always_run`). When `affected` selects the full suite, the required set is the
  marker `<full suite>`, the mode is `full`, and the output says to run `dev.py test`, with
  the recorded full-suite estimate against the budget when there is history.
- Optional candidates: targets that failed in the last 14 days, are not required, are not
  quarantined and still exist on disk. Value = 1 + failure rate + 1 (the recent failure);
  cost = p50 seconds rounded up to an integer, at least 1.
- Budget: `--budget`, else `budget_seconds` in tools/dev.json, else 480; the output names the
  source. Budget left = budget minus the required cost. When it is negative, the mode is
  `over_budget`, the message is `required set exceeds budget by N s`, every required target is
  still listed and nothing is dropped. Otherwise the candidates are chosen by exact 0/1
  knapsack (`devkit.graph.knapsack`: the value is additive and costs are integers, so the
  dynamic program is optimal), ties broken by name.

Every target is printed with its reason (`affected`, `always_run`, `knapsack`, or `not
selected: over the remaining budget`) and its estimated seconds, then the estimate against
the budget and one `run:` line per language in the runner's argument form (`-p` for Rust
crates). On the selftest's Python fixture:

```
$ dev.py smart pkg/core.py --budget 20
smart: selected; estimated 9 s against a budget of 20 s (--budget)
required (5 s; affected: selected, 2 target(s) from 1 changed file(s)):
  3 s      tests/test_api.py                        affected
  2 s      tests/test_core.py                       affected
optional (knapsack over 1 recently failed candidate(s), 15 s left):
  4 s      tests/test_broken.py                     knapsack: value 3.00 (failure rate 100%, last failed ...)
quarantined (never optional; still run when required): tests/test_flaky.py
run:
  python: tests/test_api.py tests/test_broken.py tests/test_core.py
note: seconds are recorded p50 durations rounded up per target
```

JSON keys: `mode` (`selected`, `over_budget` or `full`), `budget_seconds`, `budget_source`,
`required`, `optional`, `not_selected`, `quarantined`, `required_seconds`,
`optional_seconds`, `estimated_seconds`, `budget_left`, `message`, `affected`, `run`, `args`,
`history`. Schema `devkit-smart/1`.

### shards

`dev.py shards N [TARGETS...] [--language LANG] [--matrix] [--days N] [--json]`

Splits the targets (default: every recorded target that still exists, optionally of one
language) into N shards by LPT on p50 durations, ties by name (`devkit.graph.lpt`). Prints
LB = max(total / N, longest) with the binding term, each shard's estimated seconds and
targets, the makespan, makespan/LB and the LPT guarantee 4/3 - 1/(3N). When the longest
target binds, it says more shards cannot help until that target is split. Targets without a
recorded duration use the estimate above and are listed in a note; targets of several
languages produce a warning, since one runner takes one language.

```
$ dev.py shards 3
LB = max(sum/N, longest) = max(47.00 s / 3, 30.00 s) = 30.00 s (longest target binds)
shard 1: 30.00 s    tests/test_slow.py
shard 2: 8.00 s     tests/test_flaky.py
shard 3: 9.00 s     tests/test_broken.py tests/test_api.py tests/test_core.py
makespan 30.00 s; makespan/LB 1.000 (LPT guarantees at most 1.222 x optimum)
the longest target binds: more shards cannot help until tests/test_slow.py is split
```

`--matrix` prints one line for a GitHub Actions `strategy.matrix`:
`{"include": [{"shard", "estimated_seconds", "targets": "space-separated", "has_targets"}]}`;
use `has_targets` to skip an empty shard. Schema `devkit-shards/1`.

## Navigation commands

These read the source files of the detected languages (`.py`; `.ts .tsx .mts .cts .js .jsx
.mjs .cjs`; `.go`; `.rs`) outside `exclude_dirs`, and never modify one. A file over 1 MB or
holding a NUL byte is listed without exports.

### map

`dev.py map [--check] [--repo PATH] [--json]`

Writes `.devkit/repo-map.json`, one entry per source file (here, the Go fixture's first file):

```
{"schema": "devkit-map/1", "root_id": "<sha1 of the sorted mapped paths>",
 "files": {"pkg/a/a.go": {"lang": "go",
   "exports": [{"name": "Double", "kind": "func", "line": 5,
                "signature": "func Double(x int) int", "summary": "Double returns 2x."}],
   "imports": [], "importers": ["pkg/b/b.go"], "tests": ["pkg/b/b_test.go"]}}}
```

Exports, with the definition line:

- Python, by `ast`: top-level functions and classes without a leading underscore; when the
  module assigns `__all__` a literal list, exactly those names (variables and re-exported
  imports included, as kinds `variable` and `reexport`). The signature comes from
  `ast.unparse` on Python 3.9+ and is argument names only on 3.8, so compare maps written by
  the same interpreter. The summary is the docstring's first line, else the comment above.
- JS/TS: `export [default] [async] function|class|interface|type|enum|const|let|var|namespace
  Name`; `export default` of a function, class or expression (named `default`);
  `export { a, b as c } [from ...]`; `export * from`; CommonJS `exports.name =`.
- Go: `func Name`, methods as `Recv.Name`, `type Name`, `var` and `const`; exported when
  capitalised.
- Rust: `pub fn|struct|enum|trait|type|mod|union|const|static Name` at column 0
  (`pub(crate)` does not count) and `pub fn` inside an `impl` block as `Type::name`.

Go, Rust and JS/TS summaries come from the comment block above the definition.

`imports` and `importers` are file-level edges between mapped files. Python and JS/TS
resolve as in `affected`. A Go file imports every non-test file of each package directory
it imports. A Rust file imports the files of its `mod name;` declarations and the root
file (`src/lib.rs` or `src/main.rs`) of each path-dependency crate it names (`dep::`,
`use dep`); an integration test naming its own crate imports that crate's root. `tests` is
what `affected` selects for a change to the file, as test file paths (Go: the `_test.go`
files of the selected packages; Rust: the test files of the selected crates), and is empty
when that selection is `full` or `none`.

`--check` rebuilds the map in memory, writes nothing, prints `changed: <path>` for every
added, removed or changed entry, and exits 1 when there is any or no map is stored. `check`
runs the same comparison as its last gate whenever the map exists, so refresh it with
`dev.py map` after edits. Schema `devkit-map/1`; `--check --json` prints
`{"mode": "check", "stored", "changed"}`.

### outline

`dev.py outline FILE [--repo PATH] [--json]`

Every definition of one file (top level, plus Python class methods and Rust `impl` methods,
indented) with its line, signature, `(private)` for an unexported top-level name, and the
docstring or leading comment's first line; section markers (a comment of four or more `=`,
`-`, `~`, `*` or `#`, with its title) print as `---- title`. The language comes from the
extension; any other extension exits 2. Schema `devkit-outline/1`.

```
$ dev.py outline pkg/core.py      # on tests/fixtures/repos/py
pkg/core.py (python, 6 lines): Core logic.
4  def frobnicate(values)  # Sorted unique values, doubled.
```

### where

`dev.py where SYMBOL [--repo PATH] [--json]`

SYMBOL is a name, `Class.method`, `Type::method` or a module-qualified `pkg.mod.name`.
Searches the map's exports, then every definition of the mapped files (private names and
methods), and prints each match as `path:line  signature  (kind)` with its summary, the
files that import the defining file, and its mapped tests; definitions come before
re-exports. It reads `.devkit/repo-map.json` when present; when the map is absent, or stale
(the file set changed, or the recorded line no longer holds the name), it builds the map in
memory and says so on stderr. Exit 1 when nothing matches. Schema `devkit-where/1`.

```
$ dev.py where paginate          # on tests/fixtures/repos/js
src/paginate.ts:1  export function paginate<T>(items: T[], page: number, size = 2): T[]  (function)
  importers: src/index.ts
  tests: src/paginate.test.ts
(the re-export at src/index.ts:1 follows, labelled reexport)
```

### context

`dev.py context QUERY [--budget 12000] [--must-keep PATH[:START-END]]... [--emit] [--repo PATH] [--json]`

The prompt context for a task, built with the algorithms of `cards-llm-context.md`:

1. Names: the code-like tokens of the query (containing `_`, `.`, `/` or camelCase), as in
   the card, plus plain words of four or more characters. A code-like name matches a path
   by substring, a plain word only a directory name or file stem. Whole-word line matches
   rank definitions (a definition keyword followed by the name, or a mapped definition
   line; `Class.method` finds the method) before uses.
2. Direct targets: the files with a path match or a definition, best rank first. The chunk
   holding each definition joins the must-keep set, as does each `--must-keep` file or
   range. A path-matched file joins whole when it fits in 40% of the limit; otherwise its
   header chunk joins and its other chunks gain score. A name defined in, or matching the
   paths of, more than 5 files directs nothing: only its uses are scored, with a note.
3. Chunks: the module header and one chunk per top-level definition, leading comments and
   decorators included; a definition over 120 lines splits at its methods, then into
   100-line windows.
4. Scores: BM25 over each chunk's path and text (an inverted index above 200 chunks, the
   same formula scored directly at or below), divided by the best chunk's score; plus 0.5
   per query name the chunk uses (at most 1); plus 0.6 or 0.3 for the call-site chunks of
   files one or two hops from the direct targets over importers and imports (at most 40
   files; files with more than 50 edges are skipped as hubs). Chunks scoring 0 are not
   candidates.
5. Pack, the card's fixed algorithm: the budget minus 10% headroom is the limit;
   must-keep ranges are merged and subtracted from the candidates, so no line appears
   twice; ratio greedy, replaced by the best single chunk when that scores higher; an
   over-budget must-keep set is shortened to its signature lines (definitions, imports,
   lines naming a query name) and the note says so, with the overrun when still over.
   Candidates already partition each file, so the card's adjacency merge is applied to the
   must-keep set only (merging adjacent candidates would keep only the larger score).

The output lists every chunk in path and line order with its line range (`sig` when
shortened to signature lines), estimated tokens and the reason it was added, then the
tokens used against the limit. Tokens are characters / 4, an estimate that can be about 20%
off a real tokenizer. `--emit` appends each chunk's text under `==> path:start-end <==`.
For a fixed repository state the output is byte-identical across runs. Schema
`devkit-context/1`: `names`, `direct_targets`, `retrieval`, `expanded`, `used`, `limit`,
`note`, `notes`, and `chunks` with `path`, `start`, `end`, `lines` (kept ranges or null),
`tokens`, `score`, `must_keep`, `reason` (and `text` with `--emit`).

```
$ dev.py context "paginate returns the wrong page" --budget 2000
context: "paginate returns the wrong page"; budget 2000 tokens, limit 1800 after 10% headroom; tokens are chars/4 estimates
direct targets: src/paginate.ts (path matches paginate, defines paginate)
retrieval: direct BM25 scoring over 3 chunks (index above 200); expansion: 2 file(s) within 2 hops
  path                  lines        tokens  reason
  src/index.ts          1-1              10  uses paginate; importer of src/paginate.ts; bm25 0.10
  src/paginate.test.ts  1-8              56  uses paginate, returns, page; importer of src/index.ts; bm25 1.00
  src/paginate.ts       1-3              32  must-keep: defines paginate; path matches paginate
used 98 of 1800 tokens (budget 2000; estimate)
```

## Setup commands

### init

`dev.py init [--repo PATH] [--force] [--check] [--dry-run] [--shim-path tools/dev.py] [--json]`

Installs dev.py into a repository so its agents and CI run `python3 tools/dev.py <command>`
without knowing where the skill lives. It writes:

- `tools/dev.py`, a shim of under 60 lines marked `# devkit-shim v1`. It looks for the
  bundled dev.py in this order: `$DEVKIT_DEV_PY`; the path recorded at init
  (repository-relative when the skill is inside the repository, `~/...` under the home
  directory, else absolute); `$CLAUDE_PLUGIN_ROOT/skills/competitive-systems/scripts/dev.py`;
  `<repo>/.claude/skills/competitive-systems/scripts/dev.py` and the same under
  `<repo>/.codex/skills/`; `~/.claude/skills/`, `~/.codex/skills/` and `~/.agents/skills/`;
  then the most recently modified Claude Code plugin install,
  `~/.claude/plugins/cache/*/*/*/skills/competitive-systems/scripts/dev.py`. Init run from a
  plugin install records nothing, because the version directory in that path disappears on
  the next plugin update; the shim finds the newest install instead. It runs the first one found in the same process with
  `DEVKIT_REPO` set to the repository root, so it works from any subdirectory, with or
  without git, and exit codes pass through. When none exists it prints every location it
  tried and the two fixes (`export DEVKIT_DEV_PY=<skill-dir>/scripts/dev.py`, or rerun
  `init` from the installed skill) and exits 3.
- `tools/dev.json`, only when absent: the detected languages and package manager, null
  commands, the defaults of every other key, and `_doc` comments; the shim path is listed
  under `ignore`.
- `.devkit/.gitignore` containing `*`.

An existing `tools/dev.py` without the marker is never overwritten: init exits 1 with `a
different tools/dev.py exists; pass --force to replace it or --shim-path to choose another
name` and writes nothing. A marked shim is rewritten in place, refreshing the recorded
path. `--check` exits 0 when the shim carries the current marker and runs `--version`, else
1 with the reason. `--dry-run` prints each file it would write, with its contents, and
writes nothing. Commit `tools/dev.py` and `tools/dev.json`. Schema `devkit-init/1`.

```
$ dev.py init
init: wrote tools/dev.py (shim; records ~/.claude/skills/competitive-systems/scripts/dev.py)
init: wrote tools/dev.json (languages: python)
init: wrote .devkit/.gitignore
next: python3 tools/dev.py detect; commit tools/dev.py and tools/dev.json (.devkit/ ignores itself)
$ dev.py init --check
dev.py init: tools/dev.py resolves (dev.py 3.0.0)
```

### size-audit

`dev.py size-audit [--include-tests] [--repo PATH] [--json]`

Line counts of every source file of the detected languages and of every Python function
(from the `def` line to the end of its body, methods and nested functions included, by
`ast`), against `size_audit.file_lines` (default 450) and `size_audit.function_lines`
(default 70) in tools/dev.json. Test files (`test_globs`) are skipped unless
`--include-tests`: suites grow by cases, and the limits target code an agent edits. Prints
each file and function over its limit, largest first, and exits 1 when there is any;
otherwise names the largest file and function. The defaults are a starting point, not a
measured optimum (this skill's own engine modules exceed the file limit); set them per
repository. Schema `devkit-size-audit/1`.

### perf

`dev.py perf run [--name NAME] [--runs N] [--repo PATH] [--json]`
`dev.py perf history NAME [--repo PATH] [--json]`

`perf run` times each `benchmarks` entry of tools/dev.json (or only `--name`) with the same
timing core as `bench`, from the repository root, and appends one line per benchmark to
`.devkit/perf/<name>.jsonl`:
`{"schema", "name", "command", "ts", "commit", "dirty", "median", "iqr", "cpu_median", "runs", "wall"}`.
An entry is `{"name": "report-step", "command": "python3 -m app.report"}` with optional
`runs` (default 7; `--runs` overrides), `warmup` (default 1) and `shell` (`/bin/sh -c`
instead of an argv list). The name must match `[A-Za-z0-9][A-Za-z0-9._-]*` because it names
the file. Run it once per commit (for example in CI on the main branch) to build a series;
`perf history NAME` prints it, and `changepoint --perf NAME` tests its medians for step
changes once there are at least 10 points. A median from a noisy shared runner is a weak
point: compare series from one machine class only. Schemas `devkit-perf-run/1` and
`devkit-perf-history/1`.

### selftest

`dev.py selftest [--keep] [--json]`

The command to run first on a new machine. It writes four fixture repositories embedded in
`devkit/selftest.py` (Python with a passing unittest suite, TypeScript with echo scripts and
a pnpm lockfile, a Go module with two packages, a Rust workspace of two crates) to a
temporary directory and drives the real CLI on each, asserting known answers: `init`, `init
--check`, `detect`, `affected` on a synthetic change, `check --no-tests`, `map`, `outline`,
`where` and `context`; then on the Python fixture `test` (recording history), `aa`, `bench`
and `compare` on identical arms (never FASTER), `scale --no-baseline`, all three `bound`
forms, `changepoint` on a 60-point series with a step at index 30, an `oracle` scaffold run
under unittest, `catalog --symptom dedup`, `flakes`, `smart` (selected and over budget),
`shards`, `perf run`, `perf history` and `changepoint --perf`. Each child runs with a PATH
holding only `python3`, so git, Node, Go, Cargo and linters are absent by construction and
the result does not depend on what is installed. It prints one line per step and
`selftest: PASS (N steps, T s)`, or the failing step and its output; exit 0 or 1. It needs no
network and no tests/ directory and takes under 20 s on a laptop. `--keep` leaves the
temporary directory for inspection. Schema `devkit-selftest/1`.

## tools/dev.json

Optional; `dev.py` uses the defaults below for any key it lacks, and a key present replaces
its default (lists are not extended; `test_globs` is merged per language). Unknown keys
produce a warning; keys starting with `_doc` are comments. A wrong type is a usage error
naming the key.

| Key | Default | Meaning |
|---|---|---|
| `schema` | `devkit-config/1` | format version |
| `languages` | detected | restrict or force languages |
| `package_manager` | from the lockfile | `pnpm`, `yarn`, `bun` or `npm` |
| `commands` | all null | `{language: {lint, typecheck, test, test_report}}`; null resolves at run time, a string runs through the shell. `{files}` in lint or typecheck and `{targets}` in test are replaced by the quoted list; without the placeholder the command runs as written on the whole project |
| `sensitive` | build and dependency manifests, lockfiles (`*.lock`), `conftest.py`, `tsconfig*.json`, runner configs, `.github/workflows/**`, `tools/dev.json` | a change forces a full test run |
| `ignore` | `*.md`, `*.rst`, `docs/**`, `LICENSE*`, `COPYING*`, common image types | a change affects no test |
| `exclude_dirs` | `.git`, `node_modules`, virtual environments, `dist`, `build`, `target`, `.devkit`, tool caches | never scanned |
| `test_globs` | Python `**/test_*.py`, `**/*_test.py`; JS/TS `**/*.test.*`, `**/*.spec.*`, `**/__tests__/**`; Go `**/*_test.go` | which files are tests |
| `test_map` | `{}` | `{source path or glob: [targets]}`, overriding the graph |
| `max_changed_files` | 25 | above this, `affected` selects the full suite |
| `always_run`, `budget_seconds`, `benchmarks`, `size_audit` | `[]`, 480, `[]`, 450/70 lines | read by the test-history, perf and size commands |

Globs are gitignore-style: `*` and `?` stay inside one path segment, `**/` matches zero or
more directories, a trailing `/**` everything below, and a pattern without `/` also matches
the basename anywhere (`conftest.py` matches `a/b/conftest.py`).

`.devkit/` holds local state and ignores itself (a `.gitignore` of `*` is written when the
directory is created): `aa.json` from `aa --save`, `runs/*.jsonl` from `test`,
`repo-map.json` from `map`, `quarantine.json` from `flakes --write-quarantine`, and
`perf/<name>.jsonl` from `perf run`.

## Compatibility entry: scripts/bench.py

`python3 <skill-dir>/scripts/bench.py run|compare|scale` keeps the phase-1 command line,
output lines, JSON keys and exit codes (0 success, 2 usage, 3 the measured command failed or
could not run) for existing scripts and shell history. It uses the same timing engine; its
`compare` still applies the phase-1 IQR noise rule (the hand rule in `measurement.md`). New
work uses `dev.py`.
