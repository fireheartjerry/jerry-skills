"""Help text, the command table, the option registry and main()."""

import os
import sys

from agent_sessions.common import DEFAULT_BUDGET, OPTIONS, VERSION, die, out
from agent_sessions import commands, plans, review
from agent_sessions.runner import cmd_hook, cmd_run

__all__ = ["HELP", "COMMANDS", "FLAGS", "main", "entry"]

HELP = """agent-session %s: run claude, codex or gemini as child agent sessions

usage:
  agent-session spawn   <engine> <name> <dir> [<prompt>] [--prompt-file F | -]
                        [--tier cheap|standard|deep] [--model M] [--role implementation|review|research]
                        [--readonly | --write] [--worktree] [--base REF] [--copy P1,P2] [--steerable]
                        [--timeout S|Nm|Nh] [--stall S] [--deadline S|Nm|Nh] [--retries N]
                        [--budget-usd X] [--max-tokens N] [--max-turns N]
                        [--tree-budget-usd X] [--tree-max-tokens N]
                        [--schema FILE] [--verify CMD] [--verify-timeout S] [--verify-fixups N]
                        [--after a,b] [--queue] [--n N] [--engines e1,e2] [--on-finish CMD]
                        [--backend tmux|process] [--replace] [--json]
  agent-session list    [--json] [--mine] [--running] [--group G]
  agent-session status  <name> [--json]
  agent-session wait    [--timeout S] [--any] [--json] [--max-chars N] <name|group>... | --mine
  agent-session result  <name> [--json] [--max-chars N]
  agent-session events  <name> [--follow] [--timeout S] [--since N] [--json]
  agent-session send    <name> [<text>] [--file F | -] [--no-wait] [--interrupt] [--timeout S] [--json]
  agent-session turn    <name> [N | last] [--message ID] [--timeout S] [--json]
  agent-session log     <name> [N] [--turn K]
  agent-session peek    <name> [N]
  agent-session stop    <name>
  agent-session kill    <name> [--grace S]
  agent-session resume  <name> [--message TEXT] [--json]
  agent-session recover [--all] [--dry-run] [--json]
  agent-session judge   <name> [--engine E] [--tier T] [--brief F] [--timeout S] [--json]
  agent-session select  <group> [--judge] [--json]
  agent-session diff    <name> [--stat] [--base REF]
  agent-session collect <name|group> --merge | --squash | --cherry-pick SHA | --branch [--json]
  agent-session conflicts [<name|group>...] [--json]
  agent-session reap    [<name|group>...] [--all] [--force] [--discard SHA] [--dry-run] [--json]
  agent-session snapshots [--prune --older-than Nd] [--json]
  agent-session budget  [--json]
  agent-session unpause <engine>
  agent-session plan    run <file.json> [--replace-failed] | status <name> [--json]
  agent-session engines [--json]
  agent-session schema  [result|judge|event]
  agent-session doctor  [--json] [--engine claude|codex|gemini] [--backend tmux|process]
  agent-session --help | -h | help | --version

commands:
  spawn     start a detached child (or queue it); prints how to collect it
  list      every known child with state, model, last activity and owner (filters: --mine, --running, --group)
  status    one child: phase, turn, activity, tokens and cost so far, stall deadline, queue position
  wait      block (up to --timeout, default %d s) until children finish; exit 3 while pending
            --any returns when the first named child finishes; --mine waits for all of your unfinished children
            (a steerable child finishes only after stop or idle; its opening reply is `turn <name> 1`)
  result    a finished child's status, cost, usage, verify and final answer (structured_output with --schema)
  events    the child's event stream (events.jsonl); --follow until it ends, exit 3 on --timeout
  send      next message to a --steerable child; waits for its own reply; --interrupt stops the running turn
  turn      one turn record of a steerable child (by number, 'last' or --message id)
  log       transcript tail; --turn K renders one turn's raw events
  peek      the live pane (tmux) or the tail of pane.log (process backend)
  stop      ask a steerable child to exit after its current turn and any queued messages
  kill      stop the engine now (TERM, then KILL), then any children it spawned; record killed
  resume    continue a lost, killed, timed-out, stalled or crashed child in the same engine conversation
  recover   resume every lost child of yours that has a session id (first command after a reboot)
  judge     independent read-only review of a finished child by the other engine; fixed JSON verdict
  select    rank the members of a best-of-N group (ok, verify, judge, diff size, cost) and pick a winner
  diff      what a child changed (worktree: against its base commit, uncommitted included)
  collect   bring a worktree child's commits into the parent branch (merge, squash, cherry-pick)
  conflicts files changed by more than one worktree child, or by a child and the parent tree; exit 1 on overlap
  reap      delete finished state and worktrees (yours by default); snapshots before discarding anything
  snapshots list or prune the refs/agent-sessions/* snapshots reap wrote
  budget    tree budgets (ledgers) you own or run under, and what each child spent
  unpause   clear an engine pause written after a rate or usage limit, and start queued children
  plan      run a JSON plan of dependent spawns (replayed by content hash), or show its status
  engines   what is installed, versions, tier mapping, capabilities and codex auth; spends no tokens
  schema    print the JSON Schema of result.json, judge.json verdicts or an events.jsonl line
  doctor    check python, tmux or the process backend, engines, git, state dir; spends no tokens

exit codes: 0 ok, 1 child finished non-ok (conflicts: overlaps found), 2 usage/missing dependency/
            unknown name/refused, 3 still pending when the budget ran out

environment (the full table is in references/commands.md):
  AGENT_SESSIONS_STATE        state root (default ~/.local/state/agent-sessions)
  AGENT_SESSIONS_MAX          max children running at once (default 3); _MAX_CLAUDE/_CODEX/_GEMINI per engine
  AGENT_SESSIONS_BACKEND      tmux or process (default: tmux when installed)
  AGENT_SESSIONS_STALL        default --stall in seconds (default 900; 0 disables the watchdog)
  AGENT_SESSIONS_RETRIES      default --retries (default 0, max 5)
  AGENT_SESSIONS_{CODEX,GEMINI}_PRICE  input=..,cached=..,output=.. USD per 1M tokens; enables cost estimates
  AGENT_SESSIONS_<ENGINE>_MODEL_{CHEAP,STANDARD,DEEP}  tier mapping
  AGENT_SESSIONS_LEDGER       set for children of a tree budget; spawns under it join that budget
  AGENT_SESSIONS_ON_FINISH    default --on-finish command (run by /bin/sh after each result)

gemini support is experimental: tested against the documented event format with a fake engine only.
see references/commands.md for every option, the state layout and the engine command lines.
""" % (VERSION, DEFAULT_BUDGET)

COMMANDS = {
    "spawn": commands.cmd_spawn, "list": commands.cmd_list, "status": commands.cmd_status,
    "wait": commands.cmd_wait, "result": commands.cmd_result, "events": commands.cmd_events,
    "send": commands.cmd_send, "turn": commands.cmd_turn, "log": commands.cmd_log, "peek": commands.cmd_peek,
    "stop": commands.cmd_stop, "kill": commands.cmd_kill, "resume": commands.cmd_resume,
    "recover": commands.cmd_recover, "judge": review.cmd_judge, "select": review.cmd_select,
    "diff": review.cmd_diff, "collect": review.cmd_collect, "conflicts": review.cmd_conflicts,
    "reap": commands.cmd_reap, "snapshots": review.cmd_snapshots, "budget": commands.cmd_budget,
    "unpause": commands.cmd_unpause, "plan": plans.cmd_plan,
    "engines": commands.cmd_engines, "schema": commands.cmd_schema, "doctor": commands.cmd_doctor,
    "_run": cmd_run, "_hook": cmd_hook,
}

# Every --option each command accepts (the parser reads the same registry).
FLAGS = dict((cmd, tuple(valued) + tuple(flags)) for cmd, (valued, flags) in OPTIONS.items())


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(HELP)
        return 0
    if argv[0] in ("--version", "version"):
        out(VERSION)
        return 0
    cmd = argv[0]
    if cmd not in COMMANDS:
        die("unknown command '%s'" % cmd, "agent-session --help")
    code = COMMANDS[cmd](argv[1:])
    return code or 0


def entry():
    """Process entry: exit code from main, 130 on Ctrl-C, quiet on a closed pipe."""
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        sys.exit(1)
