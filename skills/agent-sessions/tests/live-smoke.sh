#!/usr/bin/env bash
# Live smoke test: real claude and codex (and gemini when it is on PATH),
# smallest possible prompts. Spends a few cents. Uses a private state root and
# tmux socket so it cannot touch real children. Requires the CLIs installed and
# logged in. Never run by tests/run.sh. bash 3.2 compatible.
#
#   usage: tests/live-smoke.sh [--codex-model M] [--skip-codex] [--skip-claude] [--skip-judge]
set -u
here="$(cd "$(dirname "$0")" && pwd)"
as="$here/../scripts/agent-session"
codex_model=""
skip_codex=0
skip_claude=0
skip_judge=0
while [ $# -gt 0 ]; do
  case "$1" in
    --codex-model) codex_model="$2"; shift 2 ;;
    --skip-codex) skip_codex=1; shift ;;
    --skip-claude) skip_claude=1; shift ;;
    --skip-judge) skip_judge=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

root="$(mktemp -d "${TMPDIR:-/tmp}/agent-sessions-live.XXXXXX")"
export AGENT_SESSIONS_STATE="$root/state"
export AGENT_SESSIONS_TMUX_SOCKET="as-live-$$"
export AGENT_SESSIONS_LINGER=5
export AGENT_SESSIONS_OWNER="live-smoke"
unset AGENT_SESSION_DEPTH AGENT_SESSION_NAME
work="$root/work"
mkdir -p "$work"

cleanup() {
  "$as" reap --all --force >/dev/null 2>&1 || true
  tmux -L "$AGENT_SESSIONS_TMUX_SOCKET" kill-server >/dev/null 2>&1 || true
  rm -rf "$root"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
# jget FILE KEY: print a top-level JSON field (empty when null or missing).
jget() {
  python3 -c 'import json,sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print("" if v is None else v)' "$1" "$2"
}
# jpath FILE a.b: print a nested JSON field (empty when null or missing).
jpath() {
  python3 -c 'import json,sys
v = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else v))' "$1" "$2"
}
is_uuid() {
  python3 -c 'import sys,uuid
try: uuid.UUID(sys.argv[1])
except Exception: sys.exit(1)' "$1"
}
cost_total=0
add_cost() {
  cost_total="$(python3 -c 'import sys; print(round(float(sys.argv[1]) + float(sys.argv[2] or 0), 6))' "$cost_total" "$1")"
}
show_result() { "$as" result "$1" || true; }

step "doctor"
"$as" doctor || fail "doctor did not pass"

step "engines"
"$as" engines || fail "engines found no engine"
"$as" engines --json > "$root/engines.json" || fail "engines --json"
[ "$(jpath "$root/engines.json" claude.verified)" = "True" ] \
  || fail "engines --json does not mark claude verified"

if [ "$skip_claude" -eq 0 ]; then
  step "claude one-shot"
  "$as" spawn claude ping-claude "$work" --tier cheap \
    "Reply with exactly the word PONG and nothing else." || fail "spawn claude"
  "$as" wait ping-claude --timeout 240 || { show_result ping-claude; fail "claude one-shot did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/ping-claude/result.json"
  answer="$(jget "$res" answer)"
  case "$answer" in *PONG*) ;; *) fail "claude answer was not PONG: $answer" ;; esac
  [ -n "$(jget "$res" session_id)" ] || fail "claude result has no session_id"
  cost="$(jget "$res" cost_usd)"
  [ -n "$cost" ] || fail "claude result has no cost_usd"
  add_cost "$cost"
fi

if [ "$skip_codex" -eq 0 ]; then
  step "codex one-shot"
  if [ -n "$codex_model" ]; then
    "$as" spawn codex ping-codex "$work" --model "$codex_model" \
      "Reply with exactly the word PONG and nothing else." || fail "spawn codex"
  else
    "$as" spawn codex ping-codex "$work" \
      "Reply with exactly the word PONG and nothing else." || fail "spawn codex"
  fi
  "$as" wait ping-codex --timeout 240 || { show_result ping-codex; fail "codex one-shot did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/ping-codex/result.json"
  answer="$(jget "$res" answer)"
  case "$answer" in *PONG*) ;; *) fail "codex answer was not PONG: $answer" ;; esac
  ans_file="$AGENT_SESSIONS_STATE/ping-codex/turns/1.answer.txt"
  [ -f "$ans_file" ] || fail "codex -o answer file missing"
  [ "$(cat "$ans_file")" = "$answer" ] || fail "codex answer differs from -o file"
  conv="$(jget "$AGENT_SESSIONS_STATE/ping-codex/meta.json" conversation_id)"
  is_uuid "$conv" || fail "codex conversation_id is not a UUID: $conv"
fi

if [ "$skip_claude" -eq 0 ]; then
  step "claude steerable (two turns)"
  "$as" spawn claude mem-claude "$work" --tier cheap --steerable \
    "Remember the number 7. Reply with exactly OK." || fail "spawn claude steerable"
  out="$("$as" send mem-claude "Which number did I ask you to remember? Reply with only the digit." --timeout 240)" \
    || { echo "$out"; show_result mem-claude; fail "claude send failed"; }
  echo "$out"
  case "$out" in *7*) ;; *) fail "claude did not remember 7" ;; esac
  "$as" stop mem-claude || fail "stop mem-claude"
  "$as" wait mem-claude --timeout 120 || { show_result mem-claude; fail "claude steerable did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/mem-claude/result.json"
  [ "$(jget "$res" turns)" = "2" ] || fail "claude steerable turns != 2"
  [ "$(jget "$res" ended)" = "stopped" ] || fail "claude steerable ended != stopped"
  cost="$(jget "$res" cost_usd)"
  [ -n "$cost" ] && add_cost "$cost"
fi

if [ "$skip_codex" -eq 0 ]; then
  step "codex steerable (two turns)"
  if [ -n "$codex_model" ]; then
    "$as" spawn codex mem-codex "$work" --steerable --model "$codex_model" \
      "Remember the word apricot. Reply with exactly OK." || fail "spawn codex steerable"
  else
    "$as" spawn codex mem-codex "$work" --steerable \
      "Remember the word apricot. Reply with exactly OK." || fail "spawn codex steerable"
  fi
  out="$("$as" send mem-codex "Which word did I ask you to remember? Reply with only that word." --timeout 240)" \
    || { echo "$out"; show_result mem-codex; fail "codex send failed"; }
  echo "$out"
  echo "$out" | grep -qi apricot || fail "codex did not remember apricot"
  grep -E 'cmd: .*exec resume [0-9a-f-]{36}' "$AGENT_SESSIONS_STATE/mem-codex/log" >/dev/null \
    || fail "codex turn 2 did not resume by thread id"
  "$as" stop mem-codex || fail "stop mem-codex"
  "$as" wait mem-codex --timeout 120 || { show_result mem-codex; fail "codex steerable did not finish ok"; }
fi

if [ "$skip_codex" -eq 0 ]; then
  step "codex steerable + readonly (two turns; exec resume must accept the sandbox override)"
  if [ -n "$codex_model" ]; then
    "$as" spawn codex ro-codex "$work" --steerable --readonly --model "$codex_model" \
      "Remember the word plum. Reply with exactly OK." || fail "spawn codex readonly steerable"
  else
    "$as" spawn codex ro-codex "$work" --steerable --readonly \
      "Remember the word plum. Reply with exactly OK." || fail "spawn codex readonly steerable"
  fi
  "$as" turn ro-codex 1 --timeout 240 >/dev/null || { show_result ro-codex; fail "codex readonly turn 1 failed"; }
  out="$("$as" send ro-codex "Which word did I ask you to remember? Reply with only that word." --timeout 240)"
  rc=$?
  echo "$out"
  [ "$rc" -eq 0 ] || { show_result ro-codex; fail "codex readonly send exited $rc"; }
  echo "$out" | grep -qi plum || fail "codex readonly child did not remember plum"
  [ "$(jget "$AGENT_SESSIONS_STATE/ro-codex/turns/2.json" exit_code)" = "0" ] || fail "codex readonly turn 2 exit_code != 0"
  grep -E 'cmd: .*exec resume .*sandbox_mode' "$AGENT_SESSIONS_STATE/ro-codex/log" >/dev/null \
    || fail "codex readonly turn 2 did not carry the sandbox override"
  "$as" stop ro-codex || fail "stop ro-codex"
  "$as" wait ro-codex --timeout 120 || { show_result ro-codex; fail "codex readonly steerable did not finish ok"; }
fi

if [ "$skip_claude" -eq 0 ]; then
  step "claude readonly (reads a file in its directory, creates nothing)"
  ro_dir="$root/ro-work"
  mkdir -p "$ro_dir"
  printf 'the marker is MARIGOLD-41\n' > "$ro_dir/note.txt"
  "$as" spawn claude ro-claude "$ro_dir" --tier cheap --readonly \
    "Read the file note.txt in the current directory and reply with its exact contents and nothing else. Then try to create a file named should-not-exist.txt in the same directory; if that is not possible, say nothing about it." \
    || fail "spawn claude readonly"
  "$as" wait ro-claude --timeout 240 || { show_result ro-claude; fail "claude readonly did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/ro-claude/result.json"
  answer="$(jget "$res" answer)"
  case "$answer" in *MARIGOLD-41*) ;; *) fail "claude readonly answer lacks the file contents: $answer" ;; esac
  [ ! -e "$ro_dir/should-not-exist.txt" ] || fail "claude readonly child created a file"
  [ "$(ls "$ro_dir" | wc -l | tr -d ' ')" = "1" ] || fail "claude readonly child changed its directory: $(ls "$ro_dir")"
  grep -E 'cmd: .*--strict-mcp-config' "$AGENT_SESSIONS_STATE/ro-claude/log" >/dev/null \
    || fail "claude readonly did not pass --strict-mcp-config"
  cost="$(jget "$res" cost_usd)"
  [ -n "$cost" ] && add_cost "$cost"
fi

if [ "$skip_claude" -eq 0 ]; then
  step "kill a working claude child"
  "$as" spawn claude slow-claude "$work" --tier cheap --steerable \
    "Count slowly from 1 to 200, one number per line, pausing between numbers by running 'sleep 1' with your shell tool each time." \
    || fail "spawn slow-claude"
  conv="$(jget "$AGENT_SESSIONS_STATE/slow-claude/meta.json" conversation_id)"
  n=0
  while [ ! -f "$AGENT_SESSIONS_STATE/slow-claude/engine.pid" ] && [ "$n" -lt 20 ]; do sleep 0.5; n=$((n + 1)); done
  [ -f "$AGENT_SESSIONS_STATE/slow-claude/engine.pid" ] || fail "engine never started"
  engine_pid="$(jget "$AGENT_SESSIONS_STATE/slow-claude/engine.pid" pid)"
  sleep 5
  "$as" kill slow-claude || fail "kill slow-claude"
  res="$AGENT_SESSIONS_STATE/slow-claude/result.json"
  [ "$(jget "$res" status)" = "killed" ] || { show_result slow-claude; fail "status after kill != killed"; }
  if kill -0 "$engine_pid" 2>/dev/null; then fail "engine pid $engine_pid still alive after kill"; fi
  if pgrep -f "$conv" >/dev/null 2>&1; then fail "a process still references conversation $conv"; fi
  cost="$(jget "$res" cost_usd)"
  [ -n "$cost" ] && add_cost "$cost"
fi

if [ "$skip_claude" -eq 0 ]; then
  step "claude background task across kill and resume (the first result is not the end of the turn)"
  # claude -p reports a result, waits for its background task, then reports the
  # real one. The task outlasts AGENT_SESSIONS_POST_RESULT_WAIT (10 s), so a
  # runner that ended the turn at the first result would record the wrong answer.
  "$as" spawn claude bg-claude "$work" --tier cheap \
    "Use your shell tool with run_in_background set to true to run exactly: sleep 15; echo BG-DONE-42. Do not wait for it in the foreground. Reply 'started' right away. When the background command finishes, reply with only its output line. If you find it was interrupted or killed, start it again the same way and wait for that run instead." \
    || fail "spawn bg-claude"
  n=0
  while [ ! -f "$AGENT_SESSIONS_STATE/bg-claude/engine.pid" ] && [ "$n" -lt 20 ]; do sleep 0.5; n=$((n + 1)); done
  sleep 4
  "$as" kill bg-claude || fail "kill bg-claude"
  [ "$(jget "$AGENT_SESSIONS_STATE/bg-claude/result.json" status)" = "killed" ] || fail "bg-claude not killed"
  "$as" resume bg-claude || fail "resume bg-claude"
  "$as" wait bg-claude --timeout 300 || { show_result bg-claude; fail "resumed bg-claude did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/bg-claude/result.json"
  answer="$(jget "$res" answer)"
  # The answer must be the command's output line itself, not a report about it.
  python3 -c 'import json,sys
r = json.load(open(sys.argv[1]))
a = (r.get("answer") or "").strip().strip("`").strip()
sys.exit(0 if a == "BG-DONE-42" else 1)' "$res" || { show_result bg-claude; fail "resumed bg-claude answer is not BG-DONE-42: $answer"; }
  grep -q '"type":"task"' "$AGENT_SESSIONS_STATE/bg-claude/events.jsonl" \
    || fail "bg-claude events show no background task (the prompt did not exercise the multi-result path)"
  cost="$(jget "$res" cost_usd)"
  [ -n "$cost" ] && add_cost "$cost"
fi

printf '%s\n' '{"type":"object","required":["n"],"properties":{"n":{"type":"integer"}},"additionalProperties":false}' > "$root/n.schema.json"

if [ "$skip_codex" -eq 0 ]; then
  step "codex structured output (--schema through --output-schema)"
  if [ -n "$codex_model" ]; then
    "$as" spawn codex schema-codex "$work" --schema "$root/n.schema.json" --model "$codex_model" \
      'Reply with the JSON object {"n": 7} and nothing else.' || fail "spawn codex --schema"
  else
    "$as" spawn codex schema-codex "$work" --schema "$root/n.schema.json" \
      'Reply with the JSON object {"n": 7} and nothing else.' || fail "spawn codex --schema"
  fi
  "$as" wait schema-codex --timeout 240 || { show_result schema-codex; fail "codex --schema child did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/schema-codex/result.json"
  [ "$(jpath "$res" structured_output.n)" = "7" ] || { show_result schema-codex; fail "codex structured_output.n != 7"; }
  [ "$(jget "$res" schema_valid)" = "True" ] || fail "codex schema_valid is not true"
fi

if [ "$skip_claude" -eq 0 ]; then
  step "claude structured output (--schema)"
  "$as" spawn claude schema-claude "$work" --tier cheap --schema "$root/n.schema.json" \
    'Reply with the JSON object {"n": 7} and nothing else.' || fail "spawn claude --schema"
  "$as" wait schema-claude --timeout 240 || { show_result schema-claude; fail "claude --schema child did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/schema-claude/result.json"
  [ "$(jpath "$res" structured_output.n)" = "7" ] || { show_result schema-claude; fail "structured_output.n != 7"; }
  [ "$(jget "$res" schema_valid)" = "True" ] || fail "schema_valid is not true"
  cost="$(jget "$res" cost_usd)"
  [ -n "$cost" ] && add_cost "$cost"

  step "claude verify gate (--verify passes after the child creates pong.txt)"
  vrepo="$root/vrepo"
  mkdir -p "$vrepo"
  git -C "$vrepo" init -q && printf 'smoke\n' > "$vrepo/README" && git -C "$vrepo" add README \
    && git -C "$vrepo" -c user.name=smoke -c user.email=smoke@localhost commit -q -m init || fail "git init vrepo"
  "$as" spawn claude verify-claude "$vrepo" --tier cheap --worktree --verify "test -f pong.txt" \
    "Create a file named pong.txt containing the word PONG in the current directory using your file tools. Then reply DONE." \
    || fail "spawn claude --verify"
  "$as" wait verify-claude --timeout 240 || { show_result verify-claude; fail "claude --verify child did not finish ok"; }
  res="$AGENT_SESSIONS_STATE/verify-claude/result.json"
  [ "$(jpath "$res" verify.status)" = "pass" ] || { show_result verify-claude; fail "verify.status != pass"; }
  cost="$(jget "$res" cost_usd)"
  [ -n "$cost" ] && add_cost "$cost"
fi

if [ "$skip_codex" -eq 0 ]; then
  step "codex token budget (--max-tokens 1 ends as budget after the turn)"
  if [ -n "$codex_model" ]; then
    "$as" spawn codex budget-codex "$work" --max-tokens 1 --model "$codex_model" \
      "Reply with exactly the word PONG and nothing else." || fail "spawn codex --max-tokens"
  else
    "$as" spawn codex budget-codex "$work" --max-tokens 1 \
      "Reply with exactly the word PONG and nothing else." || fail "spawn codex --max-tokens"
  fi
  "$as" wait budget-codex --timeout 240
  rc=$?
  res="$AGENT_SESSIONS_STATE/budget-codex/result.json"
  [ "$rc" -eq 1 ] || { show_result budget-codex; fail "codex --max-tokens 1 child exited $rc, expected 1"; }
  [ "$(jget "$res" failure_class)" = "budget" ] || { show_result budget-codex; fail "failure_class != budget"; }
  [ -n "$(jget "$res" answer)" ] || fail "budget child lost its answer"
fi

if [ "$skip_claude" -eq 0 ] && [ "$skip_codex" -eq 0 ] && [ "$skip_judge" -eq 0 ]; then
  step "judge the verify child with codex (read-only, fixed schema)"
  "$as" judge verify-claude --timeout 300
  rc=$?
  [ "$rc" -eq 0 ] || { show_result verify-claude-judge; fail "judge exited $rc"; }
  jf="$AGENT_SESSIONS_STATE/verify-claude/judge.json"
  [ -f "$jf" ] || fail "judge.json missing"
  case "$(jget "$jf" verdict)" in approve|revise|reject) ;; *) fail "judge verdict invalid: $(jget "$jf" verdict)" ;; esac
  [ "$(jget "$jf" engine)" = "codex" ] || fail "judge engine != codex"
  [ "$(jget "$AGENT_SESSIONS_STATE/verify-claude-judge/meta.json" readonly)" = "True" ] || fail "judge was not read-only"
fi

if command -v gemini >/dev/null 2>&1; then
  step "gemini one-shot (experimental)"
  "$as" spawn gemini ping-gemini "$work" \
    "Reply with exactly the word PONG and nothing else." || fail "spawn gemini"
  "$as" wait ping-gemini --timeout 240 || { show_result ping-gemini; fail "gemini one-shot did not finish ok"; }
  answer="$(jget "$AGENT_SESSIONS_STATE/ping-gemini/result.json" answer)"
  case "$answer" in *PONG*) ;; *) fail "gemini answer was not PONG: $answer" ;; esac
else
  step "gemini not on PATH; gemini steps skipped"
fi

step "reap"
if [ -d "$AGENT_SESSIONS_STATE/verify-claude" ]; then
  # Its worktree holds the uncommitted pong.txt: --force must snapshot it first.
  # Its judge child (if any) is reaped with it.
  out="$("$as" reap verify-claude --force)" || fail "reap --force verify-claude"
  echo "$out"
  echo "$out" | grep -Eq 'snapshot +: refs/agent-sessions/verify-claude/snapshot-' || fail "reap --force wrote no snapshot"
fi
"$as" reap --all || fail "reap --all"
"$as" list | grep -q '(no sessions)' || fail "list is not empty after reap"

echo
echo "live smoke passed; claude cost: \$$cost_total"
