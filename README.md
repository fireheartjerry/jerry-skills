# jerry-skills

Two agent skills for the work a single pass does badly: making open-ended quality
measurable, and getting more than one agent on the problem.

Both are plain markdown skills. They work in Claude Code as a plugin, and in
Codex, Cursor, opencode or anything else that reads a `SKILL.md`.

| Skill | What it does |
|---|---|
| [`iterative-improvement`](skills/iterative-improvement) | Turns "make this better" into a weighted criteria document, then runs hostile review → triage → execute → independent scoring until the score stops moving. |
| [`agent-sessions`](skills/agent-sessions) | Runs Claude and Codex as isolated child sessions in tmux, so one agent can fan work out, hold a cross-engine debate, or delegate a long job and stay responsive. |

---

## Install

### Claude Code (plugin)

```
/plugin marketplace add fireheartjerry/jerry-skills
```

```
/plugin install jerry-skills@jerry-skills
```

### Any agent (manual)

Clone once and symlink the skills into wherever your agent looks:

```bash
git clone https://github.com/fireheartjerry/jerry-skills.git ~/jerry-skills
ln -s ~/jerry-skills/skills/iterative-improvement ~/.claude/skills/iterative-improvement
ln -s ~/jerry-skills/skills/agent-sessions        ~/.claude/skills/agent-sessions
```

Codex reads `AGENTS.md`, not a skills directory — point it at this repo by adding
the contents of [`AGENTS.md`](AGENTS.md) to your own, or symlinking the skills
somewhere your `AGENTS.md` references. Copying the directories instead of
symlinking works fine too; nothing here phones home or expects a fixed path.

---

## iterative-improvement

The problem with asking an agent to improve something is that both the target
and the progress are invisible. It will make *changes*, feel productive, and
have no idea whether it moved toward or away from what you wanted. Ten rounds of
that produces churn.

This skill makes the target explicit and numerical first — it interviews you
into a weighted, scoreable criteria document — and then runs a loop where the
critic and the scorer are both independent of the agent doing the work:

```
INTERVIEW ──► CRITERIA DOC (weighted, scoreable, user-approved)
                    │
                    ▼
       ┌──────► HOSTILE REVIEW  (independent, brutal, specific)
       │            │
       │            ▼
       │        TRIAGE          (accept, reject, defer — with reasons)
       │            │
       │            ▼
       │        EXECUTE         (make the changes, verify each)
       │            │
       │            ▼
       │        SCORE           (independent scorer, fresh context)
       │            │
       └────────────┴──► target hit / rounds spent / plateau ──► REPORT
```

The separation is the point. An agent grading its own homework reports progress
it did not make; a reviewer that has never seen the reasoning finds what the
author is blind to.

**Use it when** two competent people could disagree about whether the work is
finished — designs, landing pages, architecture, prose, a codebase that should
be cleaner.

**Don't use it when** there is one specific wrong behavior (that's debugging),
when the task has a definite done state ("add a login page" — just build it), or
when a test suite or benchmark already gives you an objective signal.

Requires nothing beyond the agent itself. It pairs with `agent-sessions` when you
want the reviewer and scorer to be genuinely separate processes — see
[`references/cross-agent.md`](skills/iterative-improvement/references/cross-agent.md).

## agent-sessions

The naive way to delegate is `claude --dangerously-skip-permissions "..."` as a
bare subprocess. That works right up until you want two at once, or you need to
know whether one finished, or one corrupts a working tree the other is editing.

`agent-sessions` is the same idea with those failure modes engineered out. Each
child gets its own tmux session, its own log, and a result file the parent reads
instead of scraping scrollback.

```bash
agent-session spawn claude audit-auth ~/code/app \
  "Read src/auth/. List every place a token is validated. Output file:line and
   what each checks. Do not change files." \
  --tier cheap

agent-session list                 # what is running
agent-session wait audit-auth 600  # block until done
agent-session result audit-auth    # status, exit code, transcript tail
```

Also does steerable children (`--steerable` plus `send`, for a real turn-by-turn
conversation), git worktree isolation (`--worktree`, for children that write),
and cross-engine debate — Codex has not seen your reasoning and will not politely
agree with it.

Model tier is explicit and defaults to cheap, because the largest waste in
multi-agent work is running a frontier model on mechanical tasks:

| Tier | Claude | For |
|---|---|---|
| `cheap` (default) | Haiku | Search sweeps, format conversions, running tests, applying a specified edit |
| `standard` | Sonnet | Ordinary implementation and review |
| `deep` | Opus | Subtle debugging, architecture, adversarial review where being wrong is expensive |

**Requires:** `tmux`, the `claude` CLI, the `codex` CLI (only for Codex children),
and `git` (only for `--worktree`). The `agent-session` script is at
[`skills/agent-sessions/scripts/agent-session`](skills/agent-sessions/scripts/agent-session) —
put it on your `PATH` or call it by absolute path.

Concurrency, session lifetime and idle timeouts are set by `AGENT_SESSIONS_MAX`,
`AGENT_SESSIONS_LINGER` and `AGENT_SESSIONS_IDLE`.

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it commercially. Attribution
appreciated, not required.
