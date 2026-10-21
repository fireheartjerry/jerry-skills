#!/usr/bin/env bash
# Deterministic suite for agent-session. Uses stub engines and a private tmux
# server per test; spends no tokens. Pass unittest arguments through, e.g.
#   tests/run.sh -k steerable
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
tmux_bin="$(command -v tmux || true)"
[ -n "$tmux_bin" ] || { echo "tests need tmux on PATH" >&2; exit 2; }
py="$(command -v python3 || true)"
[ -n "$py" ] || { echo "tests need python3 on PATH" >&2; exit 2; }
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
export AGENT_SESSIONS_TEST_BIN="$bin"
unset AGENT_SESSION_DEPTH AGENT_SESSION_NAME AGENT_SESSION_STARTED AGENT_SESSIONS_OWNER \
      AGENT_SESSIONS_STATE AGENT_SESSIONS_TMUX_SOCKET AGENT_SESSIONS_TMUX_CONF AGENT_SESSIONS_MAX \
      AGENT_SESSIONS_LINGER AGENT_SESSIONS_IDLE AGENT_SESSIONS_PASS_ENV
cd "$here"
rc=0
"$py" -m unittest discover -s "$here" -p 'test_*.py' -v "$@" || rc=$?
exit "$rc"
