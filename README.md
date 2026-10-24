# jerry-skills

Five agent skills for the parts of agentic coding a single pass does badly: knowing
what the user meant, owning a task to a verified finish, making open-ended quality
measurable, running more than one agent, and proving a speedup is real.

They are plain `SKILL.md` skills with supporting references, scripts and tests. They
work in Claude Code (as a plugin or installed skills), Codex, and any agent that reads
a `SKILL.md`.

| Skill | What it does |
|---|---|
| [`mind-reading`](skills/mind-reading) | Infers the goal behind a short request, retrieves relevant personal history and actual human choices, anticipates missing requirements, and follows through. |
| [`high-agency`](skills/high-agency) | Carries a task from intent to a verified result: infers what done means, proceeds on reversible assumptions, keeps consent boundaries, and closes the loop on long-running work. |
| [`iterative-improvement`](skills/iterative-improvement) | Turns "make this better" into weighted criteria, then runs hostile review, triage, fixes and isolated scoring until the score stops moving. |
| [`agent-sessions`](skills/agent-sessions) | Runs Claude, Codex or Gemini child sessions with results on disk: parallel fan-out, verify gates, budgets, cross-engine judging, best-of-N, worktrees, resume after reboot. |
| [`competitive-systems`](skills/competitive-systems) | Evidence-driven performance work: profile, bound the gain, apply an algorithmic technique whose precondition holds, prove it with an oracle and statistically sound timing. Ships a portable `dev.py`. |

---

## Install

### Claude Code plugin

```
/plugin marketplace add fireheartjerry/jerry-skills
```

```
/plugin install jerry-skills@jerry-skills
```

### Any agent (Claude Code, Codex, others)

```bash
git clone https://github.com/fireheartjerry/jerry-skills.git ~/jerry-skills
```

```bash
~/jerry-skills/install.sh
```

`install.sh` symlinks every skill into `~/.claude/skills`, `~/.codex/skills` and
`~/.agents/skills`. It backs up any existing directory of the same name instead of
overwriting it. Useful flags: `--target claude|codex|agents|all`, `--skill <name>`,
`--copy`, `--dry-run`, `--uninstall`.

Requirements by skill: `python3` (3.8 or newer) for the scripts; `tmux` plus the
`claude`, `codex` or `gemini` CLI for `agent-sessions`; nothing beyond the agent for
`high-agency`. Each skill's `SKILL.md` lists its own.

---

## mind-reading

You type **"add the migration"**. Your agent remembers that you care about a rollback
path, existing rows and offline use, because you have actually said so before.
`mind-reading` connects the request to the current goal, anticipates what you
would otherwise have to add, and acts within the authorization it already has.

The useful part is what you did not type again: your constraints, scope, quality bar
and expected follow-through. Current context makes a terse "ready" meaningful;
chosen-versus-rejected briefs teach the agent how you actually chose. Explicit
invocations show a reviewable brief. With the personal always-on block installed,
underspecified ordinary requests can use the same reasoning without a magic phrase.

The runtime asks what should be true when you return and what you would object
to first. Optional timestamped forecasts let you compare those predictions with
later human feedback, and replay keeps later messages separate from generation
input so every comparison is fair.

A private **working notebook** carries the mission across sessions: open loops,
settled answers, decisions, referent bindings and unresolved assumptions. Preferences
can depend on the project or phase and carry explicit exceptions and counterexamples.
One stdin packet combines that state with current context; no extra model call is
needed just to interpret the request. Portable handoffs and an offline explorer make
the memory inspectable. [Working-memory guide](skills/mind-reading/references/working-memory.md).

An **experience layer** learns from how you actually reacted to past agent replies.
If silent endings made you type "keep going", or needless questions made you type
"yes, do it", the packet tells the agent so with your own numbers, and a repeat
alert fires the moment you restate a request. A weekly self-refresh relearns from
your newest sessions with no ratings. [Experience layer](skills/mind-reading/references/experience.md).

**Try it without importing anything:** follow the
[quickstart](skills/mind-reading/references/quickstart.md) for an offline fictional
engineer demo. Its three seeds use the real selector and make no model calls.

For your own model, the [build guide](skills/mind-reading/references/build.md) walks
through extraction, review, capped mining and evaluation in a separate candidate
directory. Local scripts filter transcripts and scrub common secrets. Engine stages
send the selected, scrubbed text to your configured model provider; the files remain
on your machine, and a review step lets you inspect the scrubbed text before any
engine call.

At runtime, corrections take precedence, only relevant evidence is loaded, and each
inferred sentence is tagged stated, recurring or guessed. A preference never grants
permission. Lessons from your own later corrections can improve an approach while
your current instructions remain decisive.

**Intent echoes** add a concrete memory: a short request you actually typed beside
what you later had to add. The selector retrieves relevant, source-linked precedents
from training history. They show how this kind of request unfolded for you; old
project details stay historical rather than becoming facts about today's task.
`atlas --out <private-file.html>` turns those episodes and the confidence ledger
into a searchable offline view: what you asked, what you later added, and the exact
source references behind a model claim.

It is measured the way that matters: **blind human preference and fewer
follow-up corrections**, with automated judges calibrated against those human
choices. A weekly refresh builds an improved candidate for you to activate.

## high-agency

Coding agents stop after one step, re-ask for approval they already have, retry
failures blindly, or call a passing exit code done. This skill sets an operating
contract for substantial work: infer the outcome, proceed on the most likely reading,
sweep the whole class of a reported bug, investigate before retrying, verify the result
itself, and hand off honestly when a job outlives the turn. It distinguishes execute,
discuss and status requests, scopes approvals to what was actually approved, and grants
no new permission. A delegated-child mode covers agents working under a parent's brief.

## iterative-improvement

When "better" has no test suite, an agent grading its own work reports progress it did
not make. This skill interviews you into a weighted, scoreable criteria document, scores
a baseline, then loops: an isolated hostile reviewer finds problems, the agent triages
and fixes them, and a separate scorer that never sees the changelog rescores. Stopping is
noise-aware (target, budget, regression or plateau). A lite mode covers small one-file
requests. Reviewer and scorer children run read-only and without your global instructions.

Use it when two competent people could disagree about whether the work is finished.
Do not use it for a single bug, a task with a definite done state, or anything a test
suite or benchmark already measures.

## agent-sessions

A CLI (`scripts/agent-session`) that runs coding agents as isolated child sessions in
tmux, so one agent can delegate to others and collect structured results. Each child
gets its own log and a JSON result with the final answer, session id and cost.
Highlights:

- `wait --any`, `status`, `list --json` and a per-child event stream for a parent agent
- a watchdog that kills the whole process group, classifies failures and retries a bounded number of times
- verify gates: a command that must pass before a child counts as done
- token and cost budgets across the whole child tree
- an independent judge from the other engine, and best-of-N with automatic selection
- git worktree isolation, cleanup that refuses to discard uncommitted work, resume after reboot
- read-only children for review tasks

Run `agent-session doctor` first to check dependencies.

## competitive-systems

Most agent "optimizations" optimize the wrong place, break correctness, or report noise
as a speedup. This skill runs one evidence loop: state the goal and correctness check,
profile, bound the possible gain, pick a technique only when its precondition holds,
implement it with a brute-force oracle, and accept a speedup only when its confidence
interval clears the noise floor. Technique cards cover scheduling, test selection,
dependency impact, multi-pattern matching, dedup, caching, LLM context packing and more,
each with preconditions, complexity, a tested sketch and known traps.

`scripts/dev.py` is a portable task runner for any repository: language-aware check and
test commands, affected-test selection, test timing history, a repo map, an LLM context
packet, the technique catalog, and a benchmarking engine (`bench`, `compare`, `aa` noise
floor, `scale`, `bound`, `report`, `changepoint`). `dev.py init` adds a `tools/dev.py`
shim to a repository so it is always one command away.

---

## Evidence

Each skill ships its scenarios, rubric and results under `evals/` (mind-reading's
evaluation is part of its pipeline). Blind A/B results against each skill's previous
version, judged by a separate model:

| Skill | New | Previous | Wins / ties / losses |
|---|---|---|---|
| agent-sessions | 9.0 | 6.5 | 11 / 1 / 0 |
| iterative-improvement | 9.2 | 5.7 | 10 / 2 / 0 |
| high-agency | 8.8 | 6.2 (no skill: 7.0) | 8 / 1 / 3 vs no skill |
| competitive-systems | 8.9 | 6.8 (no skill: 7.9) | 6 / 4 / 2 vs no skill |

Every skill ships its offline test suite; run them all with `scripts/run-all-tests.sh`. They run in CI on macOS and Linux together with `scripts/validate.py`.

## Contributing

Run `scripts/run-all-tests.sh` and `python3 scripts/validate.py` before opening a pull
request. `validate.py` checks frontmatter, referenced paths, executable bits and the repo
for secrets or personal data.

## License

MIT. See [LICENSE](LICENSE).
