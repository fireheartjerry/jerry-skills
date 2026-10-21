# What agent-sessions does not do

agent-sessions is a CLI an agent calls to run other agents on the same machine
and read their results from disk. Use this list to decide when to reach for
something else; it makes no claims about other products beyond the category
they belong to, and those products change faster than this file.

| Need | agent-sessions | Look instead at |
|---|---|---|
| A GUI or board where a person watches and steers many agents | No GUI: a person sees children through the calling agent, `list`, `peek` or `tmux attach` | desktop or kanban orchestrators for coding agents |
| Children on remote or cloud machines | Children run where `spawn` ran | hosted coding agents, or the engine vendor's cloud mode |
| Pull requests opened for you | `collect --branch` prints the branch; opening a PR is the user's step | the forge's own coding agent, or `gh pr create` after `collect --branch` |
| A containment boundary for untrusted code | None. Implementation children bypass permissions; read-only roles narrow the engine's tools but do not contain it | containers or VMs around the engine CLI |
| Gemini as a first-class engine | Experimental: built from the documented flags and event format, tested with a fake engine only; `doctor` checks that `gemini --help` lists the flags the adapter passes | the Gemini CLI directly |
| Windows | POSIX only (tmux, process groups, `ps`) | WSL, or a Windows-native orchestrator |
| Per-child ports, databases or setup scripts | Only `--copy` of ignored files into a worktree | a worktree manager with setup hooks |
| A scheduler that runs while nothing is calling it | No daemon: queued children start when some agent-session command runs | a job queue or CI system |
| Merge certainty | `conflicts` forecasts from changed paths and, with git 2.38+, simulates merges of committed work; a clean result does not mean the tests pass | run the test command after `collect` |

When the task is a quick same-engine subtask that finishes in one turn, the
host's native subagent tool is cheaper than a child session; see the Decide
table in SKILL.md.
