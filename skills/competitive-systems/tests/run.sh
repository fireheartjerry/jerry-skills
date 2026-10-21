#!/usr/bin/env bash
# Verify the competitive-systems skill: structural lint, then the unit tests.
# bash 3.2 compatible; standard library Python 3.8+; no network; no tokens spent.
# -I isolates Python from the environment (no PYTHON* variables, no user site, no cwd).
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
python3 -I "$here/lint_skill.py"
python3 -I -m unittest discover -s "$here" -p 'test_*.py' -v
