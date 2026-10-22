# Scope and pairing

agent-sessions runs coding agents as child sessions on the machine where it is called and returns their results on disk, so one agent can delegate, fan out, verify and collect. These notes show where it fits and what to pair it with.

| You want | agent-sessions gives you | Pair it with |
|---|---|---|
| A person watching and steering many agents | Children visible through the calling agent, `list`, `peek` and `tmux attach` | a desktop or kanban orchestrator when a GUI is preferred |
| Work on remote or cloud machines | Children run where `spawn` runs | a hosted coding agent or the engine's cloud mode |
| A pull request | `collect --branch` prepares the branch | `gh pr create` |
| Running untrusted code | Implementation children run with approvals skipped; read-only roles narrow the engine's tools | a container or VM around the engine CLI |
| Gemini | An adapter built on the documented flags and event format; `doctor` checks the installed CLI | the Gemini CLI directly for anything beyond it |
| Windows | POSIX hosts (tmux, process groups) | WSL |
| Per-child ports, databases or setup scripts | `--copy` of ignored files into a worktree | a worktree manager with setup hooks |
| Recurring agents (nightly, weekly) | Queued children start whenever an agent-session command runs | cron, a CI schedule or the host's scheduled-agent feature |
| Other agent CLIs | claude, codex and gemini engines | that CLI directly |
| Many plain commands in parallel | Full agent sessions per child | the test runner's parallel mode or `xargs -P` |
| Merge confidence | `conflicts` forecasts from changed paths and, with git 2.38+, simulates merges | the test command after `collect` |

For a quick same-engine subtask that finishes in one turn, the host's native subagent tool is the lighter choice; see the Decide table in SKILL.md.
