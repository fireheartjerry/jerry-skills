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

echo ">>> unittest (python3 -I -W error::ResourceWarning)"
if ! python3 -I -W error::ResourceWarning -m unittest discover -s "$here" -p 'test*.py' 2>"$here/.unittest.err"; then
  status=1
  # show what failed (CI has no other way to see it)
  grep -E -A25 "^(FAIL|ERROR):" "$here/.unittest.err" | head -120
fi
# unittest cannot turn a warning raised during garbage collection into a failure, so the log is grepped as well
if grep -q "ResourceWarning" "$here/.unittest.err"; then
  echo "ResourceWarning found in the test log:"
  grep -B1 -A3 "ResourceWarning" "$here/.unittest.err" | head -40
  status=1
fi
tail -3 "$here/.unittest.err"
rm -f "$here/.unittest.err"

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
