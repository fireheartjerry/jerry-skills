"""agent_sessions: the package behind the `agent-session` entry script.

Modules, by concern (a stranger auditing the part that bypasses engine
permissions needs engines.py and runner.py; the user-supplied shell commands
of --verify and --on-finish run in runner.py only):

  common    constants, output helpers, JSON/text file io, option parsing and registry
  procs     ps-based liveness, process-group kill, owner tokens, binary lookup
  backend   tmux and process backends: start, liveness, pane capture, kill
  state     state root layout, meta/turn/result io, result v3 builder, lock, ledgers, pauses, groups
  engines   Engine adapters (claude, codex, gemini), stream parsers, classification, fixed prompts
  schema    minimal JSON Schema validator and the built-in schemas
  gitops    git helpers: worktree summary, worktrees from a base, --copy, snapshots
  launch    spawn spec parsing, child creation and start, templates, the queue (kick)
  runner    the process behind every child (`agent-session _run`): turns, watchdog, retries,
            budgets, verify, hooks
  commands  spawn, list, status, wait, result, events, send, turn, log, peek, stop, kill, reap,
            resume, recover, budget, unpause, engines, schema, doctor
  review    judge, select, diff, collect, conflicts, snapshots
  plans     plan run and plan status
  cli       help text, command table, option registry, main

Python 3.8+, standard library only.
"""

VERSION = "3.1.0"

__all__ = ["VERSION"]
