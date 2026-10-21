# Skills available in this repository

Five skills live under `skills/`. Each is a directory with a `SKILL.md` (YAML
frontmatter `name` and `description`) and supporting `references/`, `scripts/`, `tests/`
and `evals/`. Read the `SKILL.md` in full before acting on a skill; read a `references/`
file only when the `SKILL.md` points to it. Install with `./install.sh`.

## skills/mind-reading

Expands a short seed into the full prompt the user would have written, from a model mined from their own Claude Code and Codex transcripts; shows it with confidence tags, then acts on it. Not for talk about the skill or its model, not for prompts that are already complete, and not for building or refreshing the model unless asked by name (then follow references/build.md). Fires when an invocation phrase appears as an instruction: "read my mind", "mind-read this", "/mind-reading" (Claude Code) or "$mind-reading" (Codex); the rest is the seed. A quoted or reported phrase does not fire it.

Entry point: `skills/mind-reading/SKILL.md`

## skills/high-agency

Own a task through to a verified result: multi-step builds, repairs, investigations, plans or briefs to execute, jobs that outlive a turn. Use when the user says "high agency", "handle it", "keep going", "take ownership", or asks whether something works or is done ("does X work?", "is it fixed?"). Not for pure explain, brainstorm or discuss requests. Grants no new permission to send, publish, delete or spend.

Entry point: `skills/high-agency/SKILL.md`

## skills/iterative-improvement

Improvement loop for designs, pages, UI, code, architecture, docs or copy with no test oracle. Not for debugging one defect, a definite done state, explaining, planning or deploying, or work a test suite or benchmark measures, even if asked to iterate until it passes. Use when the user says improve this, polish this, world class, next level, this is good but, audit and fix it, iterate on this, run N rounds or keep going until it is great, and two competent people could disagree whether it is finished. Weighted criteria, hostile review, isolated scoring.

Entry point: `skills/iterative-improvement/SKILL.md`

## skills/agent-sessions

Delegate work to Claude, Codex or Gemini child sessions with results on disk: wait --any, status, verify gates, budgets, judge, best-of-N, worktrees, resume after reboot. Not for a quick same-engine subtask the host's native subagent tool finishes in one turn. Use it when work must outlive one tool call, fan out in parallel, cross engines (Codex judging Claude's diff), be verified by a command before it counts, stay under a cost cap, or run in its own worktree. Triggers: "spawn an agent", "run these in parallel", "get codex to review", "second opinion", "best of 3", "child session".

Entry point: `skills/agent-sessions/SKILL.md`

## skills/competitive-systems

Make real software faster, cheaper or more scalable: profile, bound the gain, apply an algorithmic technique whose precondition holds, prove it with an oracle and A/B timing. Not for I/O or network waits, config, micro-optimisation or inputs small enough for brute force: it says when to stop. Covers quadratic loops, dedup, slow CI and test selection, dependency impact, LLM context packing, multi-pattern scans, repeated work, perf regressions, and whether a claimed speedup is real. Use for 'this is O(n^2)', 'CI takes 40 minutes', 'which tests must run for this PR', 'is this speedup real'.

Entry point: `skills/competitive-systems/SKILL.md`
