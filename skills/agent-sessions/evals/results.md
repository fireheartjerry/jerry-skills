# Results

What has been measured for agent-sessions v3.1.0, how, with what n, and what
has not been measured. Offline suite re-run on 2026-10-22 after the follow-up
pass (`tests/test_longrun.py`); routing eval re-run on 2026-10-22, after the
hardening pass (atomic best-of-N, the collect gate, MCP servers off
by default, the runner crash guard and the rest in `tests/test_hardening.py`,
`tests/test_followups.py` and `tests/test_gates.py`); the live smoke is from
2026-10-21 and predates that pass.

## Offline suite

Method: `tests/run.sh` against the fake engines in `tests/fake-engine/`, on a
stripped PATH (fake engines, a private bin with only tmux, python3, git and ps,
then `/usr/bin:/bin`). It spends no tokens. It also runs
`evals/check_scenarios.py`.

| OS | Python | tmux | Command | Tests | Result | Wall time |
|---|---|---|---|---|---|---|
| macOS 26.5 (arm64) | 3.9.6 | 3.8 | `tests/run.sh` | 272 | OK | 7 min (437 s) |
| macOS 26.5 (arm64) | 3.9.6 | 3.8 | `tests/run.sh --fast` | 47 | OK | 18 s |

Linux: the repository's CI runs the whole suite on `ubuntu-latest` and
`macos-latest` (GitHub Actions); both passed at commit 3185e47 (run
39732188423), before the hardening pass. No local Linux run. The suite avoids
GNU-only tools and the portability test enforces it. Python 3.8, the
documented floor, is checked only by parsing every module with
`ast.parse(feature_version=(3, 8))`; no 3.8 interpreter ran the suite.

## Live smoke

Method: `tests/live-smoke.sh`, real CLIs (claude 2.1.306, codex 0.163.0, which
the codex adapter, built against 0.162, also runs on;
gemini not installed, so its steps were skipped), private state root and tmux
socket, cheapest tiers. Steps: doctor, engines, one-shot and two-turn
steerable children on both engines, a read-only steerable codex child, a
read-only claude child that must read a file without creating one, a kill of
a working child, a claude child that starts a 15 s background task, is killed,
resumed, and must answer with the task's output line (claude reports a result
per segment, so this fails if the first result ends the turn), `--schema` on
claude and on codex (`--output-schema`) returning `{"n": 7}`, a
`--verify "test -f pong.txt"` child in a worktree, a codex child with
`--max-tokens 1` (expected `failure_class budget` after its turn), a codex
judge of the verify child, a `reap --force` that must write a snapshot ref,
and `reap --all`.

Final line of the passing run (n=1, 2 min 36 s):

```
live smoke passed; claude cost: $0.062685
```

The run before it also passed, but the background step accepted an answer
that only mentioned the expected output ("was killed before it printed
BG-DONE-42"); the check now requires the answer to be that line, and the
prompt tells the child to restart a killed task. The cost covers claude
children only; codex reports tokens, not dollars, and no codex price was
configured.

## Behavioural A/B (phase 1)

An earlier blind A/B compared the original skill documentation with the
phase-1 (v2.1.0) documentation on 12 scenarios of an earlier set, graded with
an earlier rubric: means 6.5 (original) and 9.0 (v2.1.0). The per-scenario
scores were not kept, the scenario set is not the current
`evals/scenarios.json` (now 24 scenarios), and there was no no-skill arm, so
the numbers cannot be checked or reproduced from this repository and say
nothing about v3. They show only that the grading method can tell two
documentation versions apart. No behavioural eval of v3, against a no-skill
baseline or otherwise, has been run.

## Behavioural eval of v3 (runner ready, not run)

`evals/run_scenarios.py` runs the scenarios with and without the skill: an
executor model (haiku by default) plays the parent with Bash only, in a
temporary git repository with the fake engines first on PATH and a private
state root, so its children spend nothing; a grader call scores each saved
transcript against `rubric.md` without knowing the arm. A hard cap (default
$0.80) stops the run before it is exceeded, and the output lists every
scenario's score per arm, not only the means. This pass added nine
scenarios (slow child, verify failure, judge revise loop, auth and budget
failures, missing worktree dependencies, a child asking for a push, audit
findings to check, a parent that lost track of its children); the runner's
default set is eight hard ones, six of them new. Its offline pieces are tested
(`tests/test_longrun.py`) and `--dry-run` spends nothing.

Not run: a run spends real tokens, and this pass had no go-ahead for that
spend. Until it runs, there is no evidence that SKILL.md v3 changes what an
agent does. One limit to keep in mind when it does: a situation's described
state (a child that already failed) is not on disk, so the executor is told to
take the situation as true.

## Routing eval

Method: `evals/run_routing.py`, a classifier approximation of host routing:
claude haiku is shown the name and description of the five skills in this
repository (not the host's own skills) and asked which to load for each of the
60 phrases in `evals/triggers.json`, one call per phrase, no tools, no MCP
servers. n=1 vote per phrase, 2026-10-22, $0.0123 spent.

| List | Result |
|---|---|
| should_fire (recall) | 28 / 30 |
| should_not_fire (specificity) | 20 / 30 |

Misses. Recall: both "research, implement, test" pipeline phrases went to
high-agency (defensible: they read as one owned multi-step task). Specificity:
the near-misses that share this description's vocabulary still load it under
haiku: 200 unit tests in parallel, a worktree for the user, a weekly audit, a
cloud agent, Grok, "use a subagent" (twice), a short research fan-out,
resuming the user's own session, and a host cost question. The description
names most of these exclusions; haiku does not apply them. An earlier probe
during review found a stronger router (sonnet) leaves the short fan-out alone
and that rewording the exclusions alone does not move haiku, so treat these as
an upper bound on misrouting, not a measurement of a real host. A misroute
costs one load of SKILL.md, whose Decide table sends these cases elsewhere.

## Not measured

Live coverage is the list above. Everything else is covered only by the fake
engines in `tests/fake-engine/`, which encode the event formats as observed or
documented. They missed the multi-result claude turn until a live run exposed
it, so treat fake-only paths as less proven:

- `--retries` and rate-limit pauses against a real 429 or usage limit; the
  anchored error patterns are tested on sample strings only.
- `recover` after a real reboot or tmux server loss, and `resume` of a codex
  child.
- best-of-N (`--n`), `select --judge`, `plan run`, `--after` templates and
  `--on-finish` with real engines.
- the process backend (`--backend process`) with real engines in this
  release (it was checked live during review, not in the smoke).
- codex `--verify` fixup turns; claude live token counts in `status` during a
  turn (the fake emits per-message usage the way claude does).
- `conflicts` merge simulation on git older than 2.38 (the fallback path).
- Real gemini: the adapter is tested only against the documented event format
  with a fake engine; `doctor` now checks that `gemini --help` lists the flags
  the adapter passes.
- Behaviour: whether a parent escalates tiers on a shallow answer, stops a
  cross-engine dialogue at 3 or 4 exchanges, gains from `judge` over verify
  alone, or picks well in best-of-N on real tasks.
- The v3 SKILL.md against `evals/scenarios.json`: no behavioural eval of this
  version has been run (see the A/B section), and none against a no-skill
  baseline.
- The follow-up pass (spawn's `limits :` line and uncapped warning, `status`
  elapsed and `ends by`, `list` AGE, the repository feature warnings, the
  dirty-worktree and parent-tree warnings, `AGENT_SESSION_BASE` and
  `AGENT_SESSION_WORKTREE` for `--verify` and `--on-finish`, the judge cap
  warning and revise-round hints): fake engines only. `doctor`'s adapter flag
  check was run against the real CLIs (claude 2.1.308 `--help`, codex-cli
  0.163.0 `exec --help`): every flag the adapters pass is listed, except
  claude's `--max-turns`, which its help hides and the check skips.
- `tests/live-smoke.sh` now also checks that a default write child loads no
  MCP servers (the init event lists no `mcp__` tools), a `--diff` review, and a
  worktree `--verify` that fails on a missing ignored dependency. Those steps
  have not run.
- The hardening pass with real engines: `--mcp none` (claude
  `--strict-mcp-config` on write children, codex's `mcp_servers.*.enabled=false`
  overrides, verified against `codex mcp list --json` on codex-cli 0.163.0 but
  not in a live child), `claude auth status` in `doctor`, the tmux option
  handling (verified against tmux 3.8 by hand), the collect gate, `--diff`,
  `--ledger`, owner adoption after a real reboot.
