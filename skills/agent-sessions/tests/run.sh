#!/usr/bin/env bash
# Deterministic suite for agent-session. Uses stub engines and a private tmux
# server per test; spends no tokens. Pass unittest arguments through, e.g.
#   tests/run.sh -k steerable
# `tests/run.sh --fast` runs only the quick classes (Cli, Contract, Parser,
# Schema; under 30 s) and sets AGENT_SESSIONS_TEST_FAST=1 for the tests. Both
# modes first run evals/check_scenarios.py.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
tmux_bin="$(command -v tmux || true)"
[ -n "$tmux_bin" ] || { echo "tests need tmux on PATH" >&2; exit 2; }
py="$(command -v python3 || true)"
[ -n "$py" ] || { echo "tests need python3 on PATH" >&2; exit 2; }
fast=0
if [ "${1:-}" = "--fast" ]; then
  fast=1
  shift
fi
root="$(mktemp -d "${TMPDIR:-/tmp}/agent-sessions-test.XXXXXX")"
trap 'rm -rf "$root"' EXIT
# Private bin dir with only the programs the script may call. It stands in for
# wherever tmux, python3 and git really live (often Homebrew's bin, which also
# holds GNU coreutils), so PATH never reaches a directory with `timeout`,
# `gtimeout` or GNU `date`; any GNU-only dependency fails loudly here.
bin="$root/bin"
mkdir -p "$bin"
ln -s "$tmux_bin" "$bin/tmux"
ln -s "$py" "$bin/python3"
ln -s "$(command -v ps)" "$bin/ps"
git_bin="$(command -v git || true)"
[ -z "$git_bin" ] || ln -s "$git_bin" "$bin/git"
export PATH="$here/fake-engine:$bin:/usr/bin:/bin"
export AGENT_SESSIONS_TEST_ROOT="$root"
# Keep __pycache__ out of the source tree: the tests import the package
# directly. test_cli checks that the entry itself never writes bytecode, with
# `python3 -I`, which ignores this variable.
export PYTHONDONTWRITEBYTECODE=1
export AGENT_SESSIONS_TEST_BIN="$bin"
unset AGENT_SESSION_DEPTH AGENT_SESSION_NAME AGENT_SESSION_STARTED AGENT_SESSIONS_OWNER \
      AGENT_SESSIONS_STATE AGENT_SESSIONS_TMUX_SOCKET AGENT_SESSIONS_TMUX_CONF AGENT_SESSIONS_MAX \
      AGENT_SESSIONS_LINGER AGENT_SESSIONS_IDLE AGENT_SESSIONS_PASS_ENV AGENT_SESSIONS_STALL \
      AGENT_SESSIONS_CODEX_PRICE AGENT_SESSIONS_GEMINI_PRICE AGENT_SESSIONS_TEST_FAST \
      AGENT_SESSIONS_RETRIES AGENT_SESSIONS_POST_RESULT_WAIT AGENT_SESSIONS_LADDER_S AGENT_SESSIONS_LEDGER \
      AGENT_SESSIONS_MAX_CLAUDE AGENT_SESSIONS_MAX_CODEX AGENT_SESSIONS_MAX_GEMINI \
      AGENT_SESSIONS_BACKEND AGENT_SESSIONS_ON_FINISH
cd "$here"
rc=0
# Offline consistency check of evals/ against --help and references/commands.md.
"$py" "$here/../evals/check_scenarios.py" || rc=$?
if [ "$fast" = 1 ]; then
  export AGENT_SESSIONS_TEST_FAST=1
  "$py" -m unittest discover -s "$here" -p 'test_*.py' -v -k Cli -k Contract -k Parser -k Schema "$@" || rc=$?
else
  "$py" -m unittest discover -s "$here" -p 'test_*.py' -v "$@" || rc=$?
fi
exit "$rc"
