# Results

What has been measured for agent-sessions v3.1.0, how, with what n, and what
has not been measured. Dated 2026-10-21.

## Offline suite

Method: `tests/run.sh` against the fake engines in `tests/fake-engine/`, on a
stripped PATH (fake engines, a private bin with only tmux, python3, git and ps,
then `/usr/bin:/bin`). It spends no tokens. It also runs
`evals/check_scenarios.py`.

| OS | Python | tmux | Command | Tests | Result | Wall time |
|---|---|---|---|---|---|---|
| macOS 26.5 (arm64) | 3.9.6 | 3.8 | `tests/run.sh` | 194 | OK | 5 min 52 s |
| macOS 26.5 (arm64) | 3.9.6 | 3.8 | `tests/run.sh --fast` | 35 | OK | 14 s |

Linux: not run for this release (no Linux host was used). The suite avoids
GNU-only tools and the portability test enforces it, but that is not the same
as a run.

## Live smoke

Method: `tests/live-smoke.sh`, real CLIs (claude 2.1.306, codex 0.163.0;
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

## Behavioural A/B (phase 1, not re-run)

Method: blind A/B on 12 scenarios like those in `evals/scenarios.json`,
graded with a rubric like `evals/rubric.md`, comparing the original skill
documentation with the phase-1 (v2.1.0) documentation. n=12, mean 6.5 to 9.0,
11 wins for v2.1.0, 1 tie, 0 losses.

Caveat: this measured the phase-1 SKILL.md, not this version. The v3
SKILL.md is a rewrite with a decision table and new commands, and has not been
graded against the v3 scenarios. Treat the A/B as evidence that the grading
method discriminates, not as a score for v3.

## Routing eval

Not run. To run it, give a host the skill descriptions of every skill
installed alongside this one, present each phrase in `evals/triggers.json` as a
fresh user message, record whether this skill is loaded, and report recall on
`should_fire` and specificity on `should_not_fire` separately, as
`evals/rubric.md` describes. With Claude Code, one way is a scratch project
with the skills installed and `claude -p "<phrase>" --output-format stream-json
--verbose`, checking the stream for a Skill tool call naming agent-sessions.

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
  version has been run (see the A/B caveat).
