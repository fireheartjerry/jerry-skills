#!/usr/bin/env bash
# run-all-tests.sh: run every offline check in this repository and print a
# per-skill pass/fail table. Exit 0 only when everything that ran passed.
#
# For each skills/<name>/ the entry point is, in order of preference:
#   1. tests/run.sh                     the skill's own offline runner
#   2. tests/test*.py                   python3 -m unittest discover (or pytest,
#                                       only when the tests import it and it is
#                                       installed; otherwise SKIP)
#   3. nothing                          reported as NONE (a failure with --strict)
# Then: scripts/validate.py, scripts/test_*.py and tests/test_*.py (repo tests).
#
# Anything with "live" in its name (tests/test_live_*.py, tests/*live*/,
# tests/run-live.sh) is never run unless --live is passed: those may need
# network access, tokens or real agent CLIs. A skill's tests/run.sh is trusted
# to be offline; keep live checks out of it.
#
#   scripts/run-all-tests.sh [--live] [--strict] [--verbose] [--list] [--skill NAME]...
#
# bash 3.2 compatible; needs python3 on PATH.

set -u

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

root="$(cd "$(dirname "$0")/.." && pwd -P)"
live=0; strict=0; verbose=0; list_only=0; only=""

while [ $# -gt 0 ]; do
  case "$1" in
    --live) live=1 ;;
    --strict) strict=1 ;;
    --verbose|-v) verbose=1 ;;
    --list) list_only=1 ;;
    --skill) [ $# -ge 2 ] || { echo "run-all-tests.sh: --skill needs a name" >&2; exit 2; }; only="$only $2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "run-all-tests.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "run-all-tests.sh: python3 not found" >&2; exit 2; }

logdir="$(mktemp -d "${TMPDIR:-/tmp}/run-all-tests.XXXXXX")"
trap 'rm -rf "$logdir"' EXIT

have_pytest=0
python3 -c 'import pytest' >/dev/null 2>&1 && have_pytest=1

results=""
n_fail=0; n_pass=0; n_skip=0; n_none=0

record() { # name entry status time
  results="$results$1|$2|$3|$4
"
  case "$3" in
    PASS) n_pass=$((n_pass + 1)) ;;
    FAIL) n_fail=$((n_fail + 1)) ;;
    SKIP) n_skip=$((n_skip + 1)) ;;
    NONE) n_none=$((n_none + 1)); [ "$strict" = 1 ] && n_fail=$((n_fail + 1)) ;;
  esac
}

run_step() { # name entry command...
  local name="$1" entry="$2" log start end status
  shift 2
  if [ "$list_only" = 1 ]; then
    record "$name" "$entry" "LIST" "-"
    return 0
  fi
  log="$logdir/$(printf '%s' "$name" | tr -c 'A-Za-z0-9_.-' '_').log"
  printf '>>> %s: %s\n' "$name" "$*"
  start=$(date +%s)
  if (cd "$root" && "$@") >"$log" 2>&1; then status=PASS; else status=FAIL; fi
  end=$(date +%s)
  if [ "$status" = FAIL ] || [ "$verbose" = 1 ]; then
    sed 's/^/    /' "$log"
  fi
  printf '<<< %s: %s (%ds)\n' "$name" "$status" "$((end - start))"
  record "$name" "$entry" "$status" "$((end - start))s"
}

wants() { # skill name -> 0 if selected
  [ -z "$only" ] && return 0
  case " $only " in *" $1 "*) return 0 ;; esac
  return 1
}

run_python_tests() { # name tests_dir(relative) label-suffix file...
  local name="$1" rel="$2" suffix="$3" offline="" livefiles="" needs_pytest=0 f base
  shift 3
  for f in "$@"; do
    base="$(basename "$f" .py)"
    case "$base" in
      *live*) livefiles="$livefiles $base" ;;
      *) offline="$offline $base"
         grep -Eq '^(import pytest|from pytest)' "$f" 2>/dev/null && needs_pytest=1 ;;
    esac
  done
  if [ -n "$offline" ]; then
    if [ "$needs_pytest" = 1 ]; then
      if [ "$have_pytest" = 1 ]; then
        # shellcheck disable=SC2086
        run_step "$name$suffix" "pytest $rel" python3 -m pytest -q "$rel" --ignore-glob='*live*'
      else
        record "$name$suffix" "pytest $rel" "SKIP" "pytest not installed"
      fi
    elif [ -z "$livefiles" ]; then
      run_step "$name$suffix" "unittest discover $rel" python3 -m unittest discover -s "$rel" -p 'test*.py' -v
    else
      # shellcheck disable=SC2086
      run_step "$name$suffix" "unittest $rel (offline modules)" \
        sh -c 'cd "$1" && shift && python3 -m unittest -v "$@"' _ "$rel" $offline
    fi
  elif [ -n "$livefiles" ]; then
    record "$name$suffix" "$rel" "SKIP" "live-only tests (pass --live)"
  fi
  if [ "$live" = 1 ] && [ -n "$livefiles" ]; then
    # shellcheck disable=SC2086
    run_step "$name (live)" "unittest $rel (live modules)" \
      sh -c 'cd "$1" && shift && python3 -m unittest -v "$@"' _ "$rel" $livefiles
  fi
}

# ---------------------------------------------------------------- skills

for dir in "$root"/skills/*/; do
  [ -f "$dir/SKILL.md" ] || continue
  name="$(basename "$dir")"
  wants "$name" || continue
  rel="skills/$name/tests"
  tests="$dir/tests"
  if [ -f "$tests/run.sh" ]; then
    run_step "$name" "$rel/run.sh" bash "$rel/run.sh"
  elif [ -d "$tests" ] && ls "$tests"/test*.py >/dev/null 2>&1; then
    run_python_tests "$name" "$rel" "" "$tests"/test*.py
  else
    record "$name" "-" "NONE" "no tests/"
  fi
  if [ "$live" = 1 ] && [ -f "$tests/run-live.sh" ]; then
    run_step "$name (live)" "$rel/run-live.sh" bash "$rel/run-live.sh"
  fi
  if [ "$live" = 1 ] && [ -f "$tests/live/run.sh" ]; then
    run_step "$name (live)" "$rel/live/run.sh" bash "$rel/live/run.sh"
  fi
done

# ------------------------------------------------------------- repo level

if [ -z "$only" ]; then
  run_step "repo: validate" "scripts/validate.py" python3 scripts/validate.py
  for d in scripts tests; do
    if ls "$root/$d"/test_*.py >/dev/null 2>&1; then
      run_python_tests "repo: $d" "$d" "" "$root/$d"/test_*.py
    fi
  done
  for f in "$root"/tests/test_*.sh; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in *live*) [ "$live" = 1 ] || continue ;; esac
    run_step "repo: $(basename "$f" .sh)" "tests/$(basename "$f")" bash "tests/$(basename "$f")"
  done
fi

# ------------------------------------------------------------------ table

printf '\n%-28s %-44s %-6s %s\n' "SKILL / CHECK" "ENTRY" "RESULT" "TIME"
printf '%-28s %-44s %-6s %s\n' "----------------------------" "--------------------------------------------" "------" "----"
printf '%s' "$results" | while IFS='|' read -r n e s t; do
  [ -n "$n" ] && printf '%-28s %-44s %-6s %s\n' "$n" "$e" "$s" "$t"
done
echo

if [ "$list_only" = 1 ]; then
  exit 0
fi
summary="$n_pass passed, $n_fail failed, $n_skip skipped, $n_none without tests"
if [ "$live" = 0 ]; then
  summary="$summary (offline only; pass --live to include live checks)"
fi
if [ "$n_fail" -gt 0 ]; then
  echo "FAILED: $summary"
  exit 1
fi
echo "OK: $summary"
exit 0
