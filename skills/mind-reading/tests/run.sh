#!/usr/bin/env bash
# Offline test entry point for the mind-reading skill. Runs every unit test with ResourceWarnings surfaced, checks that
# the committed fixtures match their generator, and lints the public skill. No engine calls, no tokens, no network.
#
#   bash skills/mind-reading/tests/run.sh            from the repository root
#   MR_DENY_FILE=path/to/deny.txt bash .../run.sh    also run the private-name check with your own deny list
set -u
here="$(cd "$(dirname "$0")" && pwd -P)"
skill="$(dirname "$here")"
status=0
unit_log="$(mktemp "${TMPDIR:-/tmp}/mind-reading-tests.XXXXXX")"
trap 'rm -f "$unit_log"' EXIT

echo ">>> unittest (python3 -I -W error::ResourceWarning)"
if ! python3 -I -W error::ResourceWarning -m unittest discover -s "$here" -p 'test*.py' 2>"$unit_log"; then
  cat "$unit_log"
  status=1
else
  tail -3 "$unit_log"
fi
# unittest cannot turn a warning raised during garbage collection into a failure, so the log is grepped as well
if grep -q "ResourceWarning" "$unit_log"; then
  echo "ResourceWarning found in the test log:"
  grep -B1 -A3 "ResourceWarning" "$unit_log" | head -40
  status=1
fi

echo ">>> fixtures match their generator"
python3 -I "$here/make_fixtures.py" --check || status=1

echo ">>> lint --skill"
if [ -n "${MR_DENY_FILE:-}" ]; then
  python3 -I "$skill/scripts/mind_reading.py" --home "${TMPDIR:-/tmp}/mr-lint-home" lint --skill "$skill" --deny-file "$MR_DENY_FILE" || status=1
else
  python3 -I "$skill/scripts/mind_reading.py" --home "${TMPDIR:-/tmp}/mr-lint-home" lint --skill "$skill" || status=1
fi

if [ "$status" -ne 0 ]; then
  echo "FAILED"
  exit 1
fi
echo "OK"
