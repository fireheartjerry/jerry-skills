#!/usr/bin/env bash
# Offline test runner for the high-agency skill. Stdlib Python only; spends no
# tokens. Runs every tests/test_*.py, then validates the shipped routing set.
# Exits non-zero if either step fails.
set -u
cd "$(dirname "$0")/.." || exit 2
command -v python3 >/dev/null 2>&1 || { echo "run.sh: python3 not found" >&2; exit 2; }
status=0
python3 -I -m unittest discover -s tests -p 'test_*.py' -v || status=1
python3 -I evals/route_check.py --offline || status=1
exit "$status"
