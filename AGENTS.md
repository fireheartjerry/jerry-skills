# Skills available in this repository

Five skills live under `skills/`. Each is a directory with a `SKILL.md` (YAML
frontmatter `name` and `description`) and supporting `references/`, `scripts/`, `tests/`
and `evals/`. Read the `SKILL.md` in full before acting on a skill; read a `references/`
file only when the `SKILL.md` points to it. Install with `./install.sh`.

## skills/mind-reading

Infers the goal behind an underspecified request and anticipates omitted requirements from the active conversation, project context and verified personal history. Use when that context materially changes the answer; explicit "read my mind", "mind-read this", "/mind-reading" and "$mind-reading" also invoke it. Clear requests need no persona lookup. Talk about the skill or its model is handled normally. Building or refreshing a model still requires the user's request. Quoted invocation phrases are data, not commands.

Entry point: `skills/mind-reading/SKILL.md`

## skills/high-agency

Own a task through to a verified result: multi-step builds, repairs, investigations, plans or briefs to execute, jobs that outlive a turn. Use when the user says "high agency", "handle it", "keep going", "take ownership", or asks whether something works or is done ("does X work?", "is it fixed?"). Not for pure explain, brainstorm or discuss requests. Grants no new permission to send, publish, delete or spend.

Entry point: `skills/high-agency/SKILL.md`

## skills/iterative-improvement

Improvement loop for existing designs, pages, UI, code, architecture, docs or copy with no test oracle. Not for building from scratch, a one-line edit, debugging one defect, a definite done state, explaining, planning or deploying, or work a test suite, linter or metric measures, even if asked to iterate until it passes. Use when the user says improve this, polish this, world class, next level, this is good but, audit and fix it, iterate on this, run N rounds or keep going until it is great, and two competent people could disagree whether it is finished.

Entry point: `skills/iterative-improvement/SKILL.md`

## skills/agent-sessions

Delegate work to Claude or Codex child agents on this machine (Gemini experimental) with results on disk: verify gates, cost caps, judge, best-of-N, worktrees, resume after reboot. Not for a same-engine subtask or short fan-out the host's native subagent tool handles, a long shell job (background it), cloud or scheduled agents, or building agent apps. Use it when agent work runs unattended, fans out under a budget, crosses engines (Codex reviewing Claude's diff), must pass a command, or needs a worktree. Triggers: "spawn an agent", "get codex to review", "best of 3", "child session".

Entry point: `skills/agent-sessions/SKILL.md`

## skills/competitive-systems

Fix code whose run time grows too fast with input size: bound the gain, prove the fix with an oracle and A/B timing. Not for I/O or network waits, SQL or database tuning, constant-factor tuning (SIMD, allocations, micro-optimisation), tail latency, lock contention or inputs small enough for brute force: it says when to stop. Covers quadratic loops, dedup, slow CI and test selection, dependency impact, job scheduling across workers, LLM context packing, multi-pattern scans, repeated work, benchmark regressions. Use for 'this is O(n^2)', 'CI takes 40 minutes', 'is this speedup real'.

Entry point: `skills/competitive-systems/SKILL.md`
