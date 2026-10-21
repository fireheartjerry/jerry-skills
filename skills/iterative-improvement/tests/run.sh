#!/usr/bin/env bash
# Token-free checks for the iterative-improvement skill: shell syntax, lint of the
# markdown files, and behaviour of every script in scripts/.
set -eu
here="$(cd "$(dirname "$0")" && pwd -P)"
bash -n "$here/../scripts/spawn_child.sh"
python3 -I -m unittest discover -s "$here" -p 'test_*.py' -v
