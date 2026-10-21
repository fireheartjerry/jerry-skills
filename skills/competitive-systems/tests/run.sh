#!/usr/bin/env bash
# Verify the competitive-systems skill: structural lint, then the bench.py unit tests.
# bash 3.2 compatible; standard library Python 3.8+; no network.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
python3 "$here/lint_skill.py"
python3 -m unittest discover -s "$here" -p 'test_*.py' -v
