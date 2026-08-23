# Skills available in this repository

Two skills live under `skills/`. Each is a directory containing a `SKILL.md`
with YAML frontmatter (`name`, `description`) and supporting `references/` and
`scripts/`. Read the `SKILL.md` in full before acting on the skill; read a
`references/` file only when the `SKILL.md` tells you to.

## skills/iterative-improvement

Use when the user wants something made **better** rather than made, and "better"
has no objective oracle — designs, landing pages, UI, codebases, architecture,
docs, copy. The test is whether two competent people could disagree about
whether the work is finished; if yes, this applies, even when the request is
short or scoped to one file.

Typical triggers: "improve this", "polish this", "make this world class",
"audit and fix it", "iterate on this", "this is good but…", "run N rounds on
this", "keep going until it's great".

Do **not** use it for a specific wrong behavior (that's debugging), for tasks
with a definite done state ("add a login page"), or when a test suite or
benchmark already measures success.

Entry point: `skills/iterative-improvement/SKILL.md`

## skills/agent-sessions

Use when work would benefit from a second agent: running independent jobs in
parallel, getting an adversarial second opinion, delegating a long-running task
so the parent stays responsive, or having Codex check Claude's work and vice
versa. Also use it to inspect, steer or clean up children already running.

Typical triggers: "spawn an agent", "run these in parallel", "get codex to look
at this", "second opinion", "have another agent review", "delegate this", "run
it in the background", "subagent".

Requires `tmux` and the `claude` / `codex` CLIs on `PATH`. The driver script is
`skills/agent-sessions/scripts/agent-session`.

Entry point: `skills/agent-sessions/SKILL.md`
