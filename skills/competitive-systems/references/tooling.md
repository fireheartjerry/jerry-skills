# dev.py command reference

`scripts/dev.py` is the skill's one tool: standard-library Python 3.8+, no dependencies, and
no network access of its own. The project tools it runs never install anything: npm steps
run as `npm exec --no -- <bin>`, and a binary missing from `node_modules/.bin` is reported
as not installed. Run it as `python3 <skill-dir>/scripts/dev.py <command> [options]`, where
`<skill-dir>` is the directory holding SKILL.md, from the project directory so relative
paths in measured commands resolve. `dev.py help` lists the commands;
`dev.py <command> --help` shows every option.

Conventions shared by every command:

- Exit codes: 0 success or verdict produced; 1 a gate failed (or no gate ran), a measured
  command failed, an oracle failed, a verdict was INVALID or a drift check found drift; 2
  usage error or no catalog match; 3 an external tool or the measured command could not be
  started; 70 an internal error (a bug in dev.py; the traceback is printed); 130, 143 and
  129 for SIGINT, SIGTERM and SIGHUP, after the measured process group is killed. CI can
  tell a failing gate (1) from a crash (70).
- Errors are one line starting `dev.py <command>:` with the fix; no tracebacks for expected
  errors (bad arguments, missing files, failing measured commands).
- `--json` prints exactly one JSON object on stdout with `"schema": "devkit-<command>/1"`;
  warnings and notes go to stderr.
- Every random draw (bootstrap, run order, generated inputs) takes `--seed` (default 0) and
  records it in the JSON.
- Repository root: `--repo PATH`, else `$DEVKIT_REPO` (the `tools/dev.py` shim sets it),
  else the git top level, else the current directory with a note. State lives under
  `<root>/.devkit/` (see the end of the `tools/dev.json` section).

## Evidence commands

### bench

`dev.py bench --arm NAME=CMD [--arm NAME=CMD ...] [--runs 10] [--warmup 1] [--order interleave|random|blocked] [--seed 0] [--shell] [--check-output] [--oracle-cmd CMD] [--timeout S] [--max-total 300] [--input FILE]... [--min-effect X] [--out FILE] [--json]`

`--oracle-cmd` runs the correctness check (through the shell) before the first timed run; a
non-zero exit stops the bench with exit 1 and no timings, so an unverified candidate is
never timed, and the passing result is stored in the bench JSON and carried by
`compare --json` into `report`'s Oracle line.

Times each arm as an argv list (`--shell` runs `/bin/sh -c` and says so, because shell
startup then sits inside every timing). Order `interleave` runs A B A B; `random` shuffles
each round with the seed; `blocked` runs one arm then the next. Per run: wall
(`time.perf_counter`), the child's user+sys CPU and max RSS, exit code, and with
`--check-output` the sha256 of stdout. Arms whose outputs never agree get the verdict
INVALID: no paste block, exit 1, `outputs_differ` in the JSON, repeated by `compare` and
`report`; outputs that are all empty warn that the check proved nothing. Metadata: input
hashes, git revision and dirty flag, Python, platform, CPU count, load average, timestamp,
seed. One arm prints the run table and the `Gate 1:` CPU-versus-wall line; two or more print
the compare output for the first two. Each run gets `/dev/null` as stdin and its own process
group; `--timeout` kills the whole group, so `--shell` pipelines and background children die
with it; a process an arm leaves running after it exits is killed and warned about, since it
would load later runs. `--input`, `--repo`, `--out` and `--min-effect` are checked before
the first timed run, so a typo costs nothing; if `--out` cannot be written afterwards the
timings are still printed. An arm whose median is under 3x its interpreter's startup time (
`python3 -c pass`, `node -e 0`, timed once) gets a warning with the startup-corrected
median, because the ratio then understates the change. `--input` takes one file per flag. A
failing or timed-out command exits 1 with its last 20 stderr lines; one that cannot start
exits 3. The default 10 runs is the protocol minimum. Before the first run it prints how
many times each arm will run and that every run repeats the command's side effects
(`measurement.md` section 9). `--max-total S` (default 300; 0 disables) projects the whole
bench from the first run of every arm and exits 1 before the rest when the projection is
over S, naming the value that would allow it. Schema `devkit-bench/1`. Example:
`dev.py bench --arm before="python3 old.py 40000" --arm after="python3 new.py 40000" --runs 10 --check-output --out bench.json`

The `devkit-bench/1` file: `schema`; `arms`, an object of arm name to
`{"command": argv list or shell string, "shell": bool, "wall": [seconds per run], "cpu": [user+sys seconds or null], "rss": [bytes or null], "exit": [codes], "stdout_sha256": [hex or null]}`
(all lists in run order, one entry per timed run); `order` (the arm name of each run in
sequence); `runs`, `warmup`, `order_mode`; `meta` (input hashes, git revision and dirty
flag, Python, platform, CPU count, load average, timestamp, seed); `warnings`; and, when
present, `outputs_differ` (pairs of arm names) and `oracle` (`{command, exit, last_line}`).
`compare` reads `arms.<name>.<metric>` and the commands; a file written by another tool in
this shape is compared the same way, though `compare --samples` is the simpler route for
external timings.

### compare

`dev.py compare BENCH.json | A.json B.json | --samples NAME=FILE --samples NAME=FILE [--unit s|ms|us|ns] [--before NAME --after NAME] [--metric wall|cpu|rss] [--min-effect X] [--alpha 0.05] [--resamples 5000] [--seed 0] [--fail-on slower,inconclusive] [--json]`

Per arm: runs, median, IQR, MAD, CV, min, max, Tukey outliers (kept), Spearman rho of time
against run index; `--metric rss` prints bytes and skips the wall-time checks (startup
floor, waiting), and a zero median in either arm is a usage error. Effect: ratio of medians
with a seeded bootstrap 95% CI, Hodges-Lehmann shift, Mann-Whitney U (exact when the split
count allows, else normal with tie correction). Minimum effect: the saved `.devkit/aa.json`
when it is under 7 days old and was measured on one of the arms' commands (numeric arguments
such as sizes ignored), and never below it, so `--min-effect` can raise the threshold but
not lower it under the noise floor; a floor from another command is ignored with a note
naming it; else `--min-effect`, else 5%. The source line names the A/A command, its run
count and date. Verdict rule: FASTER when the CI lower bound exceeds 1 + min effect and the
one-sided p < alpha; SLOWER mirrored; NO DETECTABLE CHANGE when the whole CI lies inside
[1/(1 + min effect), 1 + min effect]; INCONCLUSIVE otherwise, with a suggested run count.
Prints the two paste-ready lines (Before/After, Verdict). An INVALID verdict (outputs
differ, from a bench file written with `--check-output`) exits 1 and prints no paste block.
`--fail-on` turns the named verdicts into exit 1 for CI. External timings (JMH, `go test
-bench`, criterion, hyperfine, CI history, a driver that loops in-process): `--samples
before=FILE --samples after=FILE`, the before arm first, each file one positive number per
line (`#` comments and blank lines skipped, `label,value` lines and a JSON list also read)
in `--unit` (default seconds). The same CI, p value and verdict rules apply; the startup
floor and waiting checks are skipped, since such samples are often per-iteration times, and
a warning asks the report to name the source and say the outputs were not compared run by
run. Only `--metric wall` applies. Schema `devkit-compare/1`. Example:
`dev.py compare bench.json --json > compare.json`

### aa

`dev.py aa [--runs 20] [--warmup 1] [--seed 0] [--resamples 5000] [--shell] [--max-total 300] [--save] [--force] [--json] -- CMD [ARGS...]`

Runs one command as two interleaved arms and reports the ratio CI that pure noise produces,
and the suggested minimum effect `max(2%, 2 x CI half-width)`. It runs the command
2 x (runs + warmup) times, 42 by default, and says so first; for a command over about a
minute use a proxy size or skip it (`measurement.md`, section 3); `--max-total` (default
300 s, 0 disables) stops it after the first run of each arm when the projection is over the
cap. Under 10 runs warns.
`--save` writes `.devkit/aa.json` (command, runs, platform, CPU count) for `compare` and
`bench`, and refuses to replace a floor measured with more runs unless `--force`. With
`--shell` the arguments are quoted before they are joined. Schema `devkit-aa/1`.
Example: `dev.py aa --runs 20 --save -- python3 step.py 40000`

### scale

`dev.py scale --sizes n1,n2[,n3...] [--baseline 'CMD' | --no-baseline] [--runs 7] [--warmup 1] [--seed 0] [--timeout S] [--max-total 300] [--quiet] [--json] -- CMD [ARGS...]`

Doubling test: every `{n}` in the command is replaced by each size; a startup baseline
(`python3 -c pass` for Python commands, or `--baseline`) is timed and subtracted before the
log-log fit; the exponent carries a bootstrap 95% CI and the class is read from the
unrounded value (`exponent_raw` in the JSON). Four sizes recommended; fewer than three warn;
a smallest median under 5x the baseline (about 0.1 s for Python), or under 0.5 s with no
baseline, warns to raise the sizes. A progress line per size goes to stderr, with an
estimate of the rest: quadratic after the first size, then at the slope fitted so far
(`--quiet` hides them); `--timeout` kills a run that takes longer. Before each size after
the first, the cost is projected from the last size and the slope so far; a size that would
pass `--max-total` (default 300 s, 0 disables) is not started, and the command exits 1 with
the medians measured so far and the largest size that fits. `Pairwise slopes:` prints the
log-log slope between consecutive sizes (`pair_slopes` and `slopes_rising` in the JSON);
slopes that each rise at least 1.3x over three pairs, ending above 2.5, warn that the growth
is exponential and a fitted exponent means nothing, and an exponent of 1.3 to 1.7 with
rising slopes warns that two terms are mixed (`measurement.md`, section 2). Cost and size choice: `measurement.md`, section
2. Quote the placeholder as `'{n}'`: zsh drops an unquoted `{n}` that comes before a
   redirect, and the error says so. Classes: under 1.3 about linear, 1.3 to 1.7
   inconclusive, 1.7 to 2.5 about quadratic, 2.5 to 3.5 about cubic, over 3.5 worse.
   `--baseline` with `--no-baseline` is a usage error. Schema `devkit-scale/1`. Example:
   `dev.py scale --sizes 10000,20000,40000,80000 --json -- python3 step.py '{n}' > scale-before.json`

### bound

`dev.py bound amdahl --p FRACTION --s LOCAL_SPEEDUP [--json]`: end-to-end speedup
`1/((1-p) + p/s)` and the cap `1/(1-p)`. Example: `dev.py bound amdahl --p 0.12 --s 100`
prints 1.13x overall and a 1.14x cap.

`dev.py bound makespan --durations FILE|LIST [--workers M] [--pinned NAME ...] [--current alphabetical|roundrobin|FILE] [--brief] [--json]`:
loads `{name: seconds}`, `{"jobs": {...}, "runners": M}`, a list of `[name, seconds]` or a
comma list of seconds (`--durations 120,45,300`), replaces missing or non-positive durations
by the p50 of the valid ones, renames a duplicate name `name#2` (keeping its duration), and
lists both, replays the current plan (`alphabetical` = sorted names in m contiguous slices;
`roundrobin` = dealt in file order; or a plan file), prints LB = max(sum/m, longest, pinned
total) with the binding term, the LPT plan and makespan/LB, the (4/3 - 1/(3m)) bound as a
number, and the m from which the longest job binds. `--brief` cuts each worker's list to its
first two names and the job count. Durations of an hour or more also print in hours.

`dev.py bound ops --n N (--time-at-n S --exponent E | --ops EXPR [--runtime cpython|c-builtin|compiled | --rate OPS_PER_S]) [--path request|batch] [--json]`:
Gate 2 arithmetic. Prefer one timing at a small n with the measured (or expected) exponent.
EXPR uses `n`, numbers, `+ - * / // % ** ()`, `log`, `log2`, `sqrt`, evaluated in floats (a
value too large is infinity, so `2**n` never hangs); `^` is rejected with a pointer to `**`.
An operation is one interpreted step at 1e7/s (`cpython`), one element scanned inside a C
builtin such as `x in list`, `sum` or `sorted` at 2e8/s (`c-builtin`; measured 1.5e8 to
3.3e8 on CPython 3.9), or a compiled step at 1e9/s; `--rate` sets it from a measurement.
Prints the rate used, time at n and 10n, an `estimate` label for operation counts, the flip
size where the threshold (100 ms request path, 1 s batch) is crossed, and `proceed` or
`stop`. Example: `dev.py bound ops --n 2000 --ops "40*n" --path batch` (stop; flip size
250,000). `dev.py bound` with no subcommand prints the usage and exits 2. Schema for all
three: `devkit-bound/1`.

### changepoint

`dev.py changepoint (SERIES | --perf NAME) [--min-size 5] [--alpha 0.01] [--repo PATH] [--json]`

SERIES is one number per line, `label,value` lines, or a JSON list of numbers or of
`{"label", "value"}`. `--perf NAME` reads the medians recorded by `perf run` in
`.devkit/perf/NAME.jsonl`, labelled by short commit (`-dirty` when the tree had changes).
Deterministic binary segmentation with Welch's t and a Bonferroni correction over candidate
splits; prints each change point with medians before and after, percent change and p. A
non-finite value (`nan`, `inf`) is a usage error naming its position, since one would hide
every step. Change points need triage: a single outlier is not a change. The commit inside
the range is found with `bisect`. Schema `devkit-changepoint/1`.

### bisect

`dev.py bisect --good REF --bad REF [--runs 5] [--warmup 1] [--build CMD] [--min-effect X] [--timeout S] [--max-total 300] [--keep] [--repo PATH] [--json] -- CMD [ARGS...]`

Finds the first commit on the first-parent history from `--good` (exclusive) to `--bad`
where CMD got slower. Adds a throwaway `git worktree` under the system temp directory, so
the user's tree, index and branch are never touched; each probed commit is checked out
there, cleaned (`git clean -fdx` inside the worktree only), built with `--build` (run
through `/bin/sh` in the worktree; a failure skips the commit) and timed `--runs` times
(at least 3) with CMD run from the worktree root. Both ends are timed first: the bad median
must exceed the good one by more than twice the minimum effect (the saved A/A floor for
CMD, else `--min-effect`, else 5%), or the command exits 1 with nothing to bisect. A probe
is bad when its median is above the midpoint of the two ends; one within a minimum effect
of the midpoint is rerun with twice the runs first. A skipped commit is replaced by its
nearest untested neighbour; when skips hide the step the range left is printed and the
exit is 1. `--max-total` stops before the search when the two ends project it past the cap.
The worktree is removed (and `git worktree prune` run) unless `--keep`. Prints each probe
(commit, median, runs, verdict, subject), the threshold and the first slower commit with
both medians; `--json` adds `first_bad`, `range`, `skipped` and `probes`. Schema
`devkit-bisect/1`. Example:
`dev.py bisect --good v2.3.0 --bad HEAD --runs 5 --build "make" -- python3 bench.py 40000`

### catalog

`dev.py catalog [--symptom TEXT] [--id ID] [--class CLASS] [--list] [--json]`

Searches `references/techniques.json` (one entry per card heading) by token overlap with
each card's symptom, aliases, title and class; prints, best first, the title, file and
heading, tier, precondition, reject-when list, complexity, oracle and the facts to quote,
each followed by `card says: <the card sentence that states it>` (code blocks excluded) so
the answer can carry the claim, not a bare number; `--json` adds that list as
`fact_sentences`. Cards that a command implements also print `repo tool:` (for example
`dev.py affected` for impact-based test selection). The best match is flagged
`weak match` when its score is under 2, or when none of the words it shares with the symptom
come from its title or aliases (two generic words such as "loop" and "inside" are not
evidence): read the precondition and reject-when before opening the card, and treat a failed
check as no card. Questions that need no card print a route first: a claimed speedup
(`no card: this is a measurement question`), a benchmark that crept up (`dev.py changepoint`
then `dev.py bisect`; "regression" alone does not trigger it, it needs a co-term such as
benchmark, median, commits, history or perf, so "regression tests are slow" still reaches
the CI cards), catastrophic regex backtracking (rewrite the pattern or use a linear-time
engine; this route replaces the card list), or a stock structure (deque, heapq, str.join, a
compiled pattern hoisted out of a loop); JSON has `weak` and `routes`. No match and no route exits 2 with `no card matches; try --list`. Schema
`devkit-catalog/1`. Example: `dev.py catalog --symptom "CI takes 40 minutes on 8 runners"`

### report

`dev.py report --compare COMPARE.json [--scale-before S1.json --scale-after S2.json] [--aa AA.json] [--oracle-cmd CMD] [--set FIELD=TEXT]... [--out report.md] [--json]`

Fills `references/report-template.md`: Before and Verdict (always with both absolute
medians) from the compare JSON (or a bench JSON holding a `compare` object), Class from the
two scale files, Noise floor from the A/A file, Oracle by running the command (exit code and
last output line; a failing oracle makes the report exit 1), Environment and Protocol from
the metadata, Card and Card facts from `--set card=<id>`. `--set precondition=TEXT` fills
the Card line's precondition; `--set "not verified=TEXT"` adds to that line. Every field
still in `<...>` is named under `Not verified:`. `--json` prints `report` (the text),
`unfilled`, `oracle_ok` and `out`; schema `devkit-report/1`. Example:
`dev.py report --compare compare.json --scale-before sb.json --scale-after sa.json --aa .devkit/aa.json --oracle-cmd "python3 -m unittest test_oracle_dedup.py" --set card=hash-key-dedup`

### oracle

`dev.py oracle --ref MODULE:FUNC --new MODULE:FUNC [--gen ints|floats|strings|lists|dicts|custom:MODULE:FUNC] [--call auto|one|args] [--cases 300] [--seed 0] [--tol 0] [--out PATH] [--force] [--json]`

`dev.py oracle --ref-cmd CMD --new-cmd CMD --gen-cmd 'CMD {seed}' [--cases 200] [--seed 0] [--tol 0] [--timeout 60] [--json]`

Writes a unittest file (default `tests/test_oracle_<new module>_<new func>.py`, relative to
the current directory, which is also where the modules are imported from) that runs both
callables on seeded random inputs plus edge cases (empty, single, duplicates, sorted,
reversed, large, negative, Unicode and whitespace strings, NaN and infinities), compares
results (exceptions by type, NaN equal to NaN, `--tol` as relative tolerance), and adds the
anti-cache test (call, mutate the same input in place, call again, compare with the
reference). A custom generator is called as `generate(rng)`.

Calling convention: `--call one` calls `fn(case)`; `--call args` calls `fn(*case)`, so the
custom generator returns a tuple of arguments, for example `(bank, ledger)` for
`reconcile(bank, ledger)`. `auto` (the default) picks `args` when the reference has more
than one required positional parameter; the built-in generators then refuse (exit 2), since
they produce one value. The `wrote` line names the convention and how many of 10 sample
cases the reference ran on without raising; a mismatch or a reference that raises on most
samples is warned about on stderr.

Vacuity guards in the generated file: `test_matches_reference` fails when the reference
raised on more than `MIN_OK_SHARE` (0.5) of the random cases, when it returned the same
value on every case (a stub returning that value would pass), or when a perturbed copy of
its output still compares equal; it prints `reference ok on K of N random cases`. The
anti-cache test first checks the unmutated call (a wrong answer is reported as wrong, not as
a cache), then mutates the input, or the first list or dict inside an argument tuple; with
no mutable input it is skipped with a message saying the check did not run. Refuses to
overwrite an existing file without `--force` (exit 1). Run the file with
`python3 -m unittest -v <file>` or `python3 <file>`. `--json` prints the path written, the
options, `call`, `reference_ok` `[ok, sampled]`, `warnings` and the command to run it;
schema `devkit-oracle/1`. Example:
`dev.py oracle --ref dedup_old:dedup --new dedup_new:dedup --gen custom:gen_events:case --seed 7`

Command mode, for code in any language: `--gen-cmd` (through `/bin/sh`, `{seed}` replaced
by seed, seed + 1, ...) prints one case; its stdout goes to `--ref-cmd` and `--new-cmd` on
stdin, and the two must exit with the same code and print the same stdout, byte for byte,
or with `--tol` token by token (numeric tokens within that relative tolerance, NaN equal to
NaN, every other token exact). Each command runs in its own process group and is killed
after `--timeout` seconds. Exit 0 prints `oracle: PASS on N cases`, the line to rerun in CI
and the sensitivity reminder; the first difference exits 1 with the seed, the input and
both outputs. A vacuous run exits 1 too: the reference failing on more than half the cases,
or printing one identical output on every case. A failing generator exits 1. `--ref` and
`--ref-cmd` together are a usage error. `--json` prints `mode: "commands"`, `ok`, `cases`,
`reference_ok`, `distinct_outputs`, `problems` or `first_mismatch`, and `rerun`. Example:
`dev.py oracle --ref-cmd 'node old.js' --new-cmd 'node new.js' --gen-cmd 'python3 gen.py {seed}' --cases 200`

## Repository commands

These read `tools/dev.json` (next section) when it exists and detect the rest. They cover
Python, TypeScript/JavaScript, Go and Rust; for any other stack `check` and `lint` fail with
`no language dev.py supports was detected` rather than pass having run nothing, and the
repository's own build graph (Bazel `rdeps`, the Gradle or Maven module graph) is the tool.
Files come from `git ls-files --cached --others --exclude-standard` in a git work tree, so
gitignored directories (virtual environments, generated code, outputs) are never read;
outside git the tree is walked. Either way `exclude_dirs` prunes, and a directory holding
`pyvenv.cfg` is skipped. Changed files default to `git diff --name-status HEAD` plus
untracked files; `--base REF` diffs `REF...HEAD` plus the working tree, and falls back to
`origin/REF` with a note when REF does not resolve (fresh clones and CI checkouts often have
only the remote branch); FILES (relative to the current directory or absolute) replace both.
Statuses in `--json`: `A`, `M`, `D`, `R`, `U` (unmerged) and `?` (untracked). A rename
counts as the new path plus a deletion of the old one. Outside a git work tree with no
FILES, the command exits 2 with `no git: pass files explicitly` (or, when git refuses the
directory as owned by another user, the `safe.directory` command to run); a FILE that does
not exist counts as deleted. An empty default diff warns on stderr that CI, where the change
is already committed, must pass `--base`. A `--base` that cannot be diffed says why: the ref
does not resolve (fetch it), or the clone is shallow and has no merge base (`fetch-depth: 0`
or `git fetch --unshallow`), or git's own message.

#### Use in CI

The repository commands need the skill on the runner and the history of the target branch:

```yaml
- uses: actions/checkout@v4
  with: {fetch-depth: 0}                      # merge base for --base
- run: git clone --depth 1 --branch <tag> https://github.com/<owner>/<skills-repo> "$RUNNER_TEMP/skills"
- run: echo "DEVKIT_DEV_PY=$RUNNER_TEMP/skills/skills/competitive-systems/scripts/dev.py" >> "$GITHUB_ENV"
- run: python3 tools/dev.py check --base origin/main --strict
```

Pin the clone to a tag or commit. Copy the whole skill directory when vendoring
(`.claude/skills/competitive-systems/` in the repository is found by the shim): `catalog`
and `report` read `references/`, which must sit next to `scripts/`; `detect`, `check` and
`affected` work from `scripts/` alone. `shards --matrix` and `compare --fail-on` are the
other CI entry points.

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

Resolution when `commands.<language>.<step>` is null takes the first available tool. A tool
is available when it is on PATH, importable by the interpreter running dev.py (Python
tools), or declared by the project (`pyproject.toml`, requirements files, `package.json`);
declared but not installed shows as not runnable, which `check` reports as skipped with an
install hint. JS package binaries run from `node_modules/.bin` (the package's, then the
repository root's) through `npm exec --no --` (other managers: `<pm> exec`, which runs only
local binaries), else from PATH; a declared binary in neither place is not runnable, and
`detect` notes when `package.json` declares dependencies but `node_modules` is absent (Yarn
Plug'n'Play counts as installed).

| Step | Python | TypeScript / JavaScript | Go | Rust |
|---|---|---|---|---|
| lint | `ruff check`, else `flake8`, else `python3 -m pyflakes`, else `python3 -m compileall -q` | script `lint`, else eslint, else skipped | `gofmt -l` (fails on any output) then `go vet` on the changed packages | `cargo fmt --check` then `cargo clippy -q` when installed, else `cargo check -q` |
| typecheck | `mypy` when configured (`[tool.mypy]`, `mypy.ini`, setup.cfg `[mypy]`), else `pyright` when available, else skipped | script `typecheck` or `type-check`, else `tsc --noEmit -p <tsconfig>`, else skipped | covered by `go vet` | covered by `cargo check` or clippy |
| test (full) | `python3 -m pytest -q` when pytest is configured, declared or installed, else `python3 -m unittest discover -s <tests dir> -p 'test*.py'` when that reaches every test file, else `python3 -m unittest <files>` once per directory over every file `test_globs` matches | script `test`, else `vitest run`, else `jest`, else skipped | `go test ./...` | `cargo test -q` |
| test (targets) | the runner plus the test files (unittest: one run per directory) | `vitest run` or `jest` plus the files, else the `test` script plus the files | `go test` plus the package directories | `cargo test -q -p <crate> ...` |
| record format | JUnit XML (`--junitxml`); unittest through a bundled reporter | vitest `--reporter=json`, jest `--json` | `go test -json` | one record per run with the wall time |

Lint and typecheck tools receive the changed files of their language (package.json scripts
and cargo run on the whole project); a language with no changed files runs nothing.
`python3` in a command runs as the interpreter that runs dev.py, so a virtual environment's
tools are found by running dev.py with its python. `compileall` writes its bytecode to a
temporary directory, never into the repository. A changed path that starts with `-` is
passed as `./<path>` so no tool reads it as an option. `--timeout` kills the gate's whole
process group.

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
changed files, typecheck on the changed files, the affected tests (`affected` below:
selected targets, the full suite, or nothing), then map drift when `.devkit/repo-map.json`
exists. Each gate records name, language, command, status (`pass`, `fail` or `skipped`),
seconds, the first 25 output lines, the last 15 when the output is longer, and a fix hint. A
skipped gate (missing tool, nothing configured) does not fail the check unless `--strict`,
but a check in which relevant files changed and no gate ran fails with
`nothing was checked`, and a repository with no supported language says so. `--strict` also
fails on an empty diff. Exit 0 when no gate failed, 1 otherwise. The summary counts the
gates that ran and were skipped and says when no tests ran. Schema `devkit-check/1`
(`executed` counts the gates that ran). On the bundled Python fixture
(`tests/fixtures/repos/py`, no linters installed, `--no-tests` so the output stays short):

```
lint (python)          pass     111.0 ms  python3 -m compileall -q pkg/core.py
typecheck (python)     skipped  no type checker configured (mypy config or pyright); set commands.python.typecheck in tools/dev.json
note: tests not run (--no-tests)
check: PASS (1 file(s), 1 gate(s) ran, 1 skipped; --strict fails on a skipped gate)
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
replace the default discovery (`dev.py test -- <test file>`). Recording is on by default
when the runner has a report format (table above) and off otherwise; `--record` on a runner
without one writes a single record for the whole run. When the suite is a `test` script
whose runner is vitest or jest, recording runs that runner directly to get its report only
when the script is the bare runner (`jest`, `vitest`, `vitest run`); a script with flags, a
config or environment variables runs as written, recorded as one target with its wall time,
and a note before the run says why. A package script never runs without `node_modules` (pnpm
10 and later would install first and rewrite the lockfile): the step is reported missing
with the install command. Each run writes `.devkit/runs/<utc stamp>-<language>.jsonl`, one
JSON object per test:

`{"run_id", "nodeid", "target", "outcome": "passed|failed|error|skipped", "duration_seconds", "timestamp"}`

`target` uses the same form as `affected` (test file path, `./<dir>`, crate), so durations
join the selection; subtests fold into their test; a module that fails to import or build is
one `error` record. The summary line names the file and the outcome counts. A configured
test command records per test only when it contains `{report}` and
`commands.<language>.test_report` names the format (`junit`, `vitest` or `jest`). Schema
`devkit-test/1`.

### affected

`dev.py affected [FILES...] [--base REF] [--explain] [--history N] [--repo PATH] [--json]`

Prints the test targets, space-separated (one `<language>: ...` line per language when
several), or `full: <reason>` or `none: <reason>`. Rules, in order, each failing closed:

1. A changed path matching `sensitive` gives `full: sensitive file: <path>`. One exception:
   an untracked `tools/dev.json` (just written by `init`) is listed as ignored with a note
   to commit it, since it changes no code and selection already reads it from disk; once
   tracked, a change to it is sensitive again.
2. A deleted or renamed-away path gives `full: deleted or renamed: <path>` (the graph comes
   from the working tree, so the old dependents are unknown). A deleted path that matches
   `ignore` is dropped instead: by definition it affects no test.
3. Paths matching `ignore` are dropped; when nothing remains,
   `none: only ignored paths changed`. A path under a test or fixture directory (`tests`,
   `test`, `__tests__`, `__snapshots__`, `snapshots`, `fixtures`, `__fixtures__`,
   `testdata`, `test_data`, `spec`, `golden`, `goldens`) is never ignored: a changed golden
   `.md` or snapshot `.svg` gives `full: test data changed`, unless `test_map` maps it.
4. A changed test file (`test_globs`) selects itself.
5. A source file maps through `test_map` (exact key, else every matching glob key; the
   listed targets replace the graph result), else every test file that imports it
   transitively (reverse breadth-first search over the import graph), else a name match
   (`test_<stem>.py`, `<stem>_test.py`, `<stem>.test.*`, `<stem>.spec.*`). A file with none
   of these gives `full: unmapped source file: <path>`.
6. An import the graph could not resolve but that names first-party code (a Python name
   equal to a repository directory or module, a JS/TS alias or workspace package name) fails
   closed: `full: unresolved import X in F may reach P` when it could point at the changed
   file (its stem, its directory, or a directory the alias or package maps to), and the name
   match of rule 5 is used only when the language has no such import at all.
7. More than `max_changed_files` (default 25) remaining paths give `full`.

Import graphs: Python by `ast` (absolute and relative imports, `importlib.import_module`
with a literal, sibling helper modules), with first-party roots in every project directory
(the repository root, each directory holding `pyproject.toml`, `setup.py` or `setup.cfg`,
and their `src/` layouts: uv and poetry workspaces), packages with or without `__init__.py`
(PEP 420), and an import resolved against the project that holds the importing file first,
so two projects with the same package name stay apart; JS/TS by import, export-from,
`require` and dynamic `import()`, with extension and `index` resolution, the nearest
`tsconfig.json` or `jsconfig.json` above each file (following relative `extends`; `paths`
aliases and `baseUrl` bare imports), and workspace packages: every `package.json` `name` in
the repository maps to its directory, resolved through `exports`, `source`, `module`, `main`
(a `dist/` or `lib/` entry falls back to `src/`), then `src/index` and `index`; Go by
package directory within the `go.mod` module; Rust by crate, from workspace members and path
dependencies. Go selects every impacted package directory (`go test` compiles packages
without tests too); Rust selects every impacted crate (`-p name`).

`--explain` prints, per target, the chain from the target to the changed file. On the
bundled TypeScript and Rust fixtures:

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
language (`args` holds the argv form, `-p` included for Rust), `chains`, `changed`,
`ignored` and `notes`. Schema `devkit-affected/1`.

`--history N` replays the selection over the last N first-parent commits of HEAD (each
against its first parent) and prints, per commit, the mode and selected/total test targets,
then the median and 90th-percentile selection ratio and the share that fell back to `full`:
the Gate 1 evidence for test selection. The graph comes from the current checkout, so older
commits are approximate. Compare each commit's targets with the failures in your CI history
to count escapes. Schema `devkit-affected-history/1`. Dynamic imports with computed names,
plugins loaded by name, and files read by path are invisible to the graph: map them in
`test_map` or mark them `sensitive`.

## Test history commands

`slowest`, `flakes`, `smart` and `shards` read the records `dev.py test` writes to
`.devkit/runs/*.jsonl`. Records are deduplicated by `(run_id, nodeid)`, the last one
winning. Per run, a target failed when any of its records failed or errored, passed when one
passed and none failed, and was skipped otherwise; its duration in that run is the sum of
its records' durations. Per target: runs, passes, failures, skips, failure rate (failures
over passes plus failures), total and maximum duration, p50 duration (median over runs,
skip-only runs left out when any run executed), last seen and last failed. A target with no
recorded duration is estimated at the p50 of the known targets, or 5 s when nothing is
known, and is marked `estimated`. Unreadable lines are skipped with a warning. All four take
`--repo PATH` and `--json`; all but `smart` take `--days N` to keep only recent records.
With no history, `slowest` and `flakes` print a hint to run `dev.py test`, `smart` estimates
every target at 5 s, and `shards` needs TARGETS (else exit 2).

### slowest

`dev.py slowest [--limit 20] [--days N] [--json]`

Targets by p50 duration, descending, ties by name, with the maximum and the run count. The
median is used so one slow outlier run does not reorder the list; `--limit 0` prints every
target. Schema `devkit-slowest/1`.

### flakes

`dev.py flakes [--min-runs 3] [--max-rate 0.5] [--write-quarantine] [--days N] [--json]`

Targets with both passes and failures over at least `--min-runs` executed runs and a failure
rate at most `--max-rate`, highest rate first. A test that failed and was then fixed also
has mixed outcomes, so the output says to rerun a candidate at one commit before calling it
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
- Budget: `--budget`, else `budget_seconds` in tools/dev.json, else 480; the output names
  the source. Budget left = budget minus the required cost. When it is negative, the mode is
  `over_budget`, the message is `required set exceeds budget by N s`, every required target
  is still listed and nothing is dropped. Otherwise the candidates are chosen by exact 0/1
  knapsack (`devkit.graph.knapsack`: the value is additive and costs are integers, so the
  dynamic program is optimal), ties broken by name.

Every target is printed with its reason (`affected`, `always_run`, `knapsack`, or
`not selected: over the remaining budget`) and its estimated seconds, then the estimate
against the budget and one `run:` line per language in the runner's argument form (`-p` for
Rust crates). On the selftest's Python fixture (its paths, such as its own test files, are
relative to that fixture, not to this skill):

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
`optional_seconds`, `estimated_seconds`, `budget_left`, `message`, `affected`, `run`,
`args`, `history`. Schema `devkit-smart/1`.

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

These read the source files of the detected languages (`.py`;
`.ts .tsx .mts .cts .js .jsx .mjs .cjs`; `.go`; `.rs`) outside `exclude_dirs`, and never
modify one. A file over 1 MB or holding a NUL byte is listed without exports.

### map

`dev.py map [--check] [--repo PATH] [--json]`

Writes `.devkit/repo-map.json`, one entry per source file (here, the Go fixture's first
file):

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
- JS/TS:
  `export [default] [async] function|class|interface|type|enum|const|let|var|namespace Name`;
  `export default` of a function, class or expression (named `default`);
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

`dev.py context QUERY [--budget 12000] [--must-keep PATH[:START-END]]... [--must-keep-text TEXT]... [--min-score 0.25] [--emit] [--repo PATH] [--json]`

QUERY is the identifiers, paths and error text of the task, not the whole task: words such
as the test command or "do not change the public API" pull in unrelated chunks. Pass those
with `--must-keep-text`, which puts them in the packet as text and counts them against the
budget.

The prompt context for a task, built with the algorithms of `cards-llm-context.md`:

1. Names: the code-like tokens of the query (containing `_`, `.`, `/` or camelCase), as in
   the card, plus plain words of four or more characters that are not common instruction
   words (after, empty, status, error, test, run and the like; they still count in BM25). A
   code-like name matches a path by substring, a plain word only a directory name or file
   stem. Whole-word line matches rank definitions (a definition keyword followed by the
   name, or a mapped definition line; `Class.method` finds the method) before uses.
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
   files; files with more than 50 edges are skipped as hubs). Chunks scoring under
   `--min-score` (default 0.25; 0 keeps every chunk that scores at all) are not candidates,
   so a large budget is not filled with noise; the note counts them and the tokens left
   unused.
5. Pack, the card's fixed algorithm: the budget minus 10% headroom, minus the
   `--must-keep-text` items, is the limit; each chunk costs its text plus the `==>` header
   and blank line `--emit` prints, so the emitted packet fits the limit;
   must-keep ranges are merged and subtracted from the candidates, so no line appears
   twice; ratio greedy, replaced by the best single chunk when that scores higher; an
   over-budget must-keep set is shortened to its signature lines (definitions, imports,
   lines naming a query name) and the note says so, with the overrun when still over.
   Candidates already partition each file, so the card's adjacency merge is applied to the
   must-keep set only (merging adjacent candidates would keep only the larger score).

The output lists every chunk in path and line order with its line range (`sig` when
shortened to signature lines), estimated tokens and the reason it was added, then the tokens
used against the limit. Tokens are characters / 4, an estimate that can be about 20% off a
real tokenizer. `--emit` appends each chunk's text under `==> path:start-end <==` (adjacent
chunks of one file as one block, `--must-keep-text` items under `==> <text> <==`) and prints
the emitted size. For a fixed repository state the output is byte-identical across runs.
Schema `devkit-context/1`: `names`, `direct_targets`, `retrieval`, `expanded`, `used`,
`limit`, `min_score`, `dropped_below_min_score`, `note`, `notes`, and `chunks` with `path`,
`start`, `end`, `lines` (kept ranges or null), `tokens`, `score`, `must_keep`, `reason` (and
`text` with `--emit`).

```
$ dev.py context "paginate returns the wrong page" --budget 2000
context: "paginate returns the wrong page"; budget 2000 tokens, limit 1800 after 10% headroom; tokens are chars/4 estimates
direct targets: src/paginate.ts (path matches paginate, defines paginate)
retrieval: direct BM25 scoring over 3 chunks (index above 200); expansion: 2 file(s) within 2 hops
  path                  lines        tokens  reason
  src/index.ts          1-1              17  uses paginate; importer of src/paginate.ts; bm25 0.10
  src/paginate.test.ts  1-8              64  uses paginate, page; importer of src/index.ts; bm25 1.00
  src/paginate.ts       1-3              39  must-keep: defines paginate; path matches paginate
used 120 of 1800 tokens (budget 2000; estimate)
```

## Setup commands

### init

`dev.py init [--repo PATH] [--force] [--check] [--dry-run] [--shim-path tools/dev.py] [--json]`

Installs dev.py into a repository so its agents and CI run `python3 tools/dev.py <command>`
without knowing where the skill lives. It writes:

- `tools/dev.py`, a shim of under 60 lines marked `# devkit-shim v1`. It looks for the
  bundled dev.py in this order: `$DEVKIT_DEV_PY`; the path recorded at init
  (repository-relative when the skill is inside the repository, `~/...` under the home
  directory, else absolute);
  `$CLAUDE_PLUGIN_ROOT/skills/competitive-systems/scripts/dev.py`;
  `<repo>/.claude/skills/competitive-systems/scripts/dev.py` and the same under
  `<repo>/.codex/skills/`; `~/.claude/skills/`, `~/.codex/skills/` and `~/.agents/skills/`;
  then the most recently modified Claude Code plugin install,
  `~/.claude/plugins/cache/*/*/*/skills/competitive-systems/scripts/dev.py`. Init run from a
  plugin install records nothing, because the version directory in that path disappears on
  the next plugin update; the shim finds the newest install instead. It runs the first one
  found in the same process with `DEVKIT_REPO` set to the repository root, so it works from
  any subdirectory, with or without git, and exit codes pass through. When none exists it
  prints every location it tried and the two fixes
  (`export DEVKIT_DEV_PY=<skill-dir>/scripts/dev.py`, or rerun `init` from the installed
  skill) and exits 3.
- `tools/dev.json`, only when absent: the detected languages and package manager, null
  commands, the defaults of every other key, and `_doc` comments; the shim path is listed
  under `ignore`.
- `.devkit/.gitignore` containing `*`.

An existing `tools/dev.py` without the marker is never overwritten: init exits 1 with

`a different tools/dev.py exists; pass --force to replace it or --shim-path to choose another name`
and writes nothing. A marked shim is rewritten in place, refreshing the recorded path.
`--check` exits 0 when the shim carries the current marker and runs `--version`, else 1 with
the reason. `--dry-run` prints each file it would write, with its contents, and writes
nothing. Commit `tools/dev.py` and `tools/dev.json`. On a CI runner the skill itself must be
present too: see "Use in CI" above. Schema `devkit-init/1`.

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
temporary directory and drives the real CLI on each, asserting known answers: `init`,
`init --check`, `detect`, `affected` on a synthetic change, `check --no-tests`, `map`,
`outline`, `where` and `context`; then on the Python fixture `test` (recording history),
`aa`, `bench` and `compare` on identical arms (never FASTER), `scale --no-baseline`, all
three `bound` forms, `changepoint` on a 60-point series with a step at index 30, an `oracle`
scaffold run under unittest, `catalog --symptom dedup`, `flakes`, `smart` (selected and over
budget), `shards`, `perf run`, `perf history` and `changepoint --perf`. Each child runs with
a PATH holding only `python3`, so git, Node, Go, Cargo and linters are absent by
construction and the result does not depend on what is installed. It prints one line per
step and `selftest: PASS (N steps, T s)`, or the failing step and its output; exit 0 or 1.
It needs no network and no tests/ directory and takes under 20 s on a laptop. `--keep`
leaves the temporary directory for inspection. Schema `devkit-selftest/1`.

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
| `ignore` | `*.md`, `*.rst`, `docs/**`, `LICENSE*`, `COPYING*`, common image types | a change affects no test; never applied under a test or fixture directory (`affected`, rule 3) |
| `exclude_dirs` | `.git`, `node_modules`, `.venv`, `venv`, `env`, `.conda`, `.nox`, `dist`, `build`, `target`, `out`, `coverage`, `.next`, `.nuxt`, `.turbo`, `.devkit`, tool caches | never scanned; gitignored directories and any directory holding `pyvenv.cfg` are skipped as well |
| `test_globs` | Python `**/test_*.py`, `**/*_test.py`; JS/TS `**/*.test.*`, `**/*.spec.*`, `**/__tests__/**`; Go `**/*_test.go` | which files are tests |
| `test_map` | `{}` | `{source path or glob: [targets]}`, overriding the graph |
| `max_changed_files` | 25 | above this, `affected` selects the full suite |
| `always_run`, `budget_seconds`, `benchmarks`, `size_audit` | `[]`, 480, `[]`, 450/70 lines | read by the test-history, perf and size commands |

Globs are gitignore-style: `*` and `?` stay inside one path segment, `**/` matches zero or
more directories, a trailing `/**` everything below, `[...]` is a class (`[!...]` negates, a
leading `]` is a member), `{a,b}` is alternation (`config/*.{yml,yaml}`), and a pattern
without `/` also matches the basename anywhere (`conftest.py` matches `a/b/conftest.py`).
A glob that cannot compile, such as the reversed range `[z-a]`, is a usage error naming its
key when the file is loaded.

`.devkit/` holds local state and ignores itself (a `.gitignore` of `*` is written when the
directory is created): `aa.json` from `aa --save`, `runs/*.jsonl` from `test`,
`repo-map.json` from `map`, `quarantine.json` from `flakes --write-quarantine`, and
`perf/<name>.jsonl` from `perf run`.

## Deprecated entry: scripts/bench.py

`python3 <skill-dir>/scripts/bench.py run|compare|scale` is deprecated and kept only so
existing scripts keep working: the same command line, output lines, JSON keys and exit codes
(0 success, 2 usage, 3 the measured command failed or could not run) as when it was the
skill's only tool. It uses the same timing engine, but its `compare` applies the older IQR
noise rule (the hand rule in `measurement.md`), not the bootstrap CI against a noise floor,
so it can disagree with `dev.py compare`. New work uses `dev.py bench`, `compare` and
`scale`; SKILL.md never routes to bench.py.
