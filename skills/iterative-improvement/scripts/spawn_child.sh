#!/usr/bin/env bash
# Spawn an isolated, read-only reviewer or scorer on a staged copy of a snapshot and
# verify afterwards that nothing was touched.
#
# usage: spawn_child.sh ENGINE ROLE PROMPT_FILE SNAPSHOT_DIR OUT_FILE
#                       [--evidence DIR] [--model M] [--loop-dir DIR]
#                       [--timeout SECONDS] [--no-bash] [--same-engine] [--dry-run]
#        spawn_child.sh --preflight ENGINE [--model M] [--same-engine] [--timeout SECONDS]
#
#   ENGINE  codex | claude | kimi      (kimi has no sandbox: reviewer only)
#   ROLE    reviewer | scorer          (a scorer requires --model: the pin)
#
# Every run is staged in a fresh temporary directory outside any git work tree and
# outside the loop dir:
#   <stage>/artifact/   copy of SNAPSHOT_DIR (verified against its manifest), with
#                       agent instruction files renamed *.as-data; the child's cwd
#   <stage>/prompt.md   copy of PROMPT_FILE plus a STAGING section naming these paths
#   <stage>/evidence/   copy of --evidence DIR, when given
#   <stage>/out.md      the only file the child may write; moved to OUT_FILE after
#   <stage>/home/       isolated CODEX_HOME (auth.json symlink only) or kimi skills dir
# The child gets no path under the loop dir or the repo, so round numbers, prior
# scores, the changelog and commit history are not reachable. The child loads no
# user config, memory, skills, MCP servers or project instruction files. After the
# run the staged artifact is re-hashed and the artifact root's git status is
# compared with the pre-run fingerprint; any change discards the output (exit 4).
#
# --no-bash      claude only: drop the Bash tool entirely (the stricter rerun variant)
# --same-engine  allow spawning the CLI you are running (disclosed in the .cmd record
#                and the report; it is more isolated than a harness subagent)
# --timeout      default 900 when a timeout binary exists; 0 disables
# --dry-run      print the staging plan and the exact argv; creates nothing
# --preflight    run a one-file smoke test through the full path and report ok/failure
#
# Records: OUT_FILE minus .md plus .cmd (argv with <stage> placeholders, engine
# version, model) and .log (child stdout and stderr).
#
# Exit codes: 0 ok, 2 bad argument or missing file, 3 staging failed, 4 child modified
# the staged artifact or the artifact root changed during the run (output discarded),
# 5 refused to spawn the engine you are running, 6 child produced no output, 7 child
# exited non-zero or failed the preflight.

set -eu

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  printf 'spawn_child: %s\n' "$1" >&2
  exit "${2:-2}"
}

abs_dir() {
  (cd "$1" 2>/dev/null && pwd -P) || die "directory not found: $1"
}

abs_file() {
  local dir base
  dir="$(abs_dir "$(dirname "$1")")"
  base="$(basename "$1")"
  printf '%s/%s' "$dir" "$base"
}

shquote() {
  local s="$1"
  local q="'\\''"
  s="${s//\'/$q}"
  printf "'%s'" "$s"
}

sha256_stdin() {
  python3 -I -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

engine_version() {
  local v
  v="$("$1" --version 2>/dev/null | head -n 1 | tr -d '\r')" || true
  printf '%s' "${v:-version unavailable}"
}

# Fingerprint of the artifact root: git status plus the diff against HEAD. "none" when
# the manifest has no root, "not-a-repo" when the root is not inside a git work tree.
root_fingerprint() {
  if [ -z "$1" ] || [ ! -d "$1" ]; then
    printf 'none'
    return 0
  fi
  if git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    { git -C "$1" status --porcelain -z 2>/dev/null; git -C "$1" diff HEAD 2>/dev/null; } | sha256_stdin
  else
    printf 'not-a-repo'
  fi
}

log_incident() {
  if [ -n "${LOOP_DIR:-}" ] && [ -d "$LOOP_DIR" ]; then
    printf -- '- %s %s %s: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$ENGINE" "$ROLE" "$1" >> "$LOOP_DIR/rounds.md"
  fi
}

if [ "$#" -ge 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
  usage
  exit 0
fi

# ---------------------------------------------------------------------------- preflight
if [ "$#" -ge 1 ] && [ "$1" = "--preflight" ]; then
  [ "$#" -ge 2 ] || die "--preflight needs an engine"
  P_ENGINE="$2"; shift 2
  P_ARGS=()
  P_MODEL=""
  P_TIMEOUT=300
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --model) [ "$#" -ge 2 ] || die "--model needs a value"; P_MODEL="$2"; P_ARGS+=(--model "$2"); shift 2 ;;
      --same-engine) P_ARGS+=(--same-engine); shift ;;
      --timeout) [ "$#" -ge 2 ] || die "--timeout needs seconds"; P_TIMEOUT="$2"; shift 2 ;;
      *) die "unknown preflight option: $1" ;;
    esac
  done
  P_ARGS+=(--timeout "$P_TIMEOUT")
  case "$P_ENGINE" in codex|claude|kimi) ;; *) die "unknown engine: $P_ENGINE (use codex, claude or kimi)" ;; esac
  P_ROLE="reviewer"
  if [ -n "$P_MODEL" ] && [ "$P_ENGINE" != "kimi" ]; then
    P_ROLE="scorer"
  fi
  SCRIPT_DIR="$(abs_dir "$(dirname "$0")")"
  P_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ii-preflight.XXXXXX")"
  trap 'rm -rf "$P_TMP"' EXIT
  mkdir -p "$P_TMP/root" "$P_TMP/loop/scores"
  printf 'marigold\n' > "$P_TMP/root/hello.txt"
  python3 -I "$SCRIPT_DIR/snapshot.py" create --root "$P_TMP/root" --out "$P_TMP/loop/snapshots/round-0" >/dev/null
  printf 'Read hello.txt in your working directory. Your complete output is exactly one line: PREFLIGHT OK <the word in hello.txt>\n' > "$P_TMP/prompt.md"
  P_STATUS=0
  "$0" "$P_ENGINE" "$P_ROLE" "$P_TMP/prompt.md" "$P_TMP/loop/snapshots/round-0" "$P_TMP/loop/scores/preflight.md" \
    --loop-dir "$P_TMP/loop" ${P_ARGS[@]+"${P_ARGS[@]}"} >"$P_TMP/spawn.out" 2>&1 || P_STATUS=$?
  if [ "$P_STATUS" -ne 0 ]; then
    cat "$P_TMP/spawn.out" >&2
    [ -f "$P_TMP/loop/scores/preflight.log" ] && tail -n 20 "$P_TMP/loop/scores/preflight.log" >&2
    die "preflight failed for $P_ENGINE (spawn exit $P_STATUS); fix access before round 0" 7
  fi
  if grep -q 'PREFLIGHT OK' "$P_TMP/loop/scores/preflight.md" && grep -q 'marigold' "$P_TMP/loop/scores/preflight.md"; then
    printf 'preflight ok: %s (%s) as %s%s\n' "$P_ENGINE" "$(engine_version "$P_ENGINE")" "$P_ROLE" "${P_MODEL:+, model $P_MODEL}"
    exit 0
  fi
  printf '%s\n' "--- child output ---" >&2
  cat "$P_TMP/loop/scores/preflight.md" >&2
  die "preflight failed for $P_ENGINE: the child did not read the staged file" 7
fi

# ---------------------------------------------------------------------------- arguments
[ "$#" -ge 5 ] || { usage >&2; exit 2; }

ENGINE="$1"; ROLE="$2"; PROMPT_ARG="$3"; SNAPSHOT_ARG="$4"; OUT_ARG="$5"
shift 5

DRY_RUN=0
TIMEOUT_S=""
LOOP_DIR=""
MODEL=""
EVIDENCE_ARG=""
NO_BASH=0
SAME_ENGINE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --evidence) [ "$#" -ge 2 ] || die "--evidence needs a directory"; EVIDENCE_ARG="$2"; shift 2 ;;
    --model) [ "$#" -ge 2 ] || die "--model needs a value"; MODEL="$2"; shift 2 ;;
    --loop-dir) [ "$#" -ge 2 ] || die "--loop-dir needs a directory"; LOOP_DIR="$2"; shift 2 ;;
    --timeout) [ "$#" -ge 2 ] || die "--timeout needs seconds"; TIMEOUT_S="$2"; shift 2 ;;
    --no-bash) NO_BASH=1; shift ;;
    --same-engine) SAME_ENGINE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --allow|--allow-scorer-commands)
      die "$1 was removed: children never run write-dependent commands; run the check line yourself and pass the output with --evidence" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$ENGINE" in
  codex|claude|kimi) ;;
  *) die "unknown engine: $ENGINE (use codex, claude or kimi)" ;;
esac
case "$ROLE" in
  reviewer|scorer) ;;
  *) die "unknown role: $ROLE (use reviewer or scorer)" ;;
esac
if [ "$ENGINE" = "kimi" ] && [ "$ROLE" = "scorer" ]; then
  die "kimi has no sandbox and no tool allowlist; use it as reviewer only"
fi
if [ "$ROLE" = "scorer" ] && [ -z "$MODEL" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'spawn_child: warning: a scorer needs --model M on a real run (the model is part of the pin)\n' >&2
  else
    die "a scorer needs --model M: the model is part of the pin and the CLI default can change mid-loop"
  fi
fi
if [ "$NO_BASH" -eq 1 ] && [ "$ENGINE" != "claude" ]; then
  die "--no-bash applies to claude only (codex is sandboxed read-only; kimi has no allowlist)"
fi
case "$TIMEOUT_S" in
  "") ;;
  *[!0-9]*) die "--timeout takes whole seconds, got: $TIMEOUT_S" ;;
esac

# Spawning the CLI you are running is the same model with the same blind spots. It is
# still a fresh, isolated process; allow it only when disclosed with --same-engine.
if [ "$SAME_ENGINE" -eq 0 ]; then
  if [ "$ENGINE" = "claude" ] && [ -n "${CLAUDECODE:-}" ]; then
    die "refusing to spawn the engine you are running; pass --same-engine to run it anyway as an isolated process and disclose it" 5
  fi
  if [ "$ENGINE" = "codex" ] && { [ -n "${CODEX_SANDBOX:-}" ] || [ -n "${CODEX_THREAD_ID:-}" ]; }; then
    die "refusing to spawn the engine you are running; pass --same-engine to run it anyway as an isolated process and disclose it" 5
  fi
fi

[ -r "$PROMPT_ARG" ] || die "prompt file not found: $PROMPT_ARG"
PROMPT_FILE="$(abs_file "$PROMPT_ARG")"
[ -d "$SNAPSHOT_ARG" ] || die "snapshot dir not found: $SNAPSHOT_ARG"
SNAPSHOT_DIR="$(abs_dir "$SNAPSHOT_ARG")"
MANIFEST="${SNAPSHOT_DIR}.manifest.json"
[ -r "$MANIFEST" ] || die "snapshot manifest not found: $MANIFEST"
OUT_PARENT="$(dirname "$OUT_ARG")"
[ -d "$OUT_PARENT" ] || die "output directory not found: $OUT_PARENT"
OUT_FILE="$(abs_file "$OUT_ARG")"
EVIDENCE_DIR=""
if [ -n "$EVIDENCE_ARG" ]; then
  [ -d "$EVIDENCE_ARG" ] || die "evidence dir not found: $EVIDENCE_ARG"
  EVIDENCE_DIR="$(abs_dir "$EVIDENCE_ARG")"
fi
if [ -n "$LOOP_DIR" ]; then
  LOOP_DIR="$(abs_dir "$LOOP_DIR")"
elif [ "$(basename "$(dirname "$SNAPSHOT_DIR")")" = "snapshots" ]; then
  LOOP_DIR="$(abs_dir "$(dirname "$SNAPSHOT_DIR")/..")"
elif [ "$DRY_RUN" -eq 1 ]; then
  printf 'spawn_child: warning: cannot infer the loop dir (snapshot is not under <LOOP>/snapshots/); a real run needs --loop-dir\n' >&2
  LOOP_DIR="/<loop>"
else
  die "cannot infer the loop dir: the snapshot is not under <LOOP>/snapshots/; pass --loop-dir"
fi
SCRIPT_DIR="$(abs_dir "$(dirname "$0")")"
RECORD_BASE="${OUT_FILE%.md}"
CMD_FILE="${RECORD_BASE}.cmd"
LOG_FILE="${RECORD_BASE}.log"

ROOT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("root") or "")' "$MANIFEST" 2>/dev/null || true)"

if [ -z "$TIMEOUT_S" ]; then
  TIMEOUT_S=900
fi
TIMEOUT_BIN=""
if [ "$TIMEOUT_S" != "0" ]; then
  TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
  if [ -z "$TIMEOUT_BIN" ]; then
    printf 'spawn_child: no timeout binary found; running without a timeout\n' >&2
  fi
fi

if [ "$ENGINE" = "codex" ]; then
  SRC_AUTH="${CODEX_HOME:-$HOME/.codex}/auth.json"
  if [ ! -f "$SRC_AUTH" ]; then
    die "codex auth.json not found at $SRC_AUTH. Run \"codex login\". If you are logged in but your config stores credentials in the OS keyring (cli_auth_credentials_store), the isolated home cannot see them; switch that setting to file for the loop or use another engine"
  fi
fi

# Names that agent CLIs load as instructions when present in a directory they run in.
INSTRUCTION_NAMES="CLAUDE.md CLAUDE.local.md AGENTS.md AGENTS.override.md GEMINI.md .cursorrules .claude .codex .agents .kimi"

# ---------------------------------------------------------------------------- command
# STAGE is the real temp dir on a run, or the placeholder /<stage> on a dry run and in
# the .cmd record, so the record is byte-identical from round to round.
PLACEHOLDER='/<stage>'

build_cmd() {  # $1 = stage dir
  local stage="$1"
  ENV_PREFIX=()
  cmd=()
  case "$ENGINE" in
    codex)
      PROMPT_LINE="Read and follow the instructions in $stage/prompt.md. Your final message must be your complete output and nothing else; it is saved to a file automatically."
      ENV_PREFIX=("CODEX_HOME=$stage/home")
      cmd=(codex exec --ephemeral -s read-only --skip-git-repo-check
           -c 'model_reasoning_effort="high"' -c project_doc_max_bytes=0)
      [ -n "$MODEL" ] && cmd+=(-m "$MODEL")
      cmd+=(-C "$stage/artifact" -o "$stage/out.md" "$PROMPT_LINE")
      ;;
    claude)
      PROMPT_LINE="Read and follow the instructions in $stage/prompt.md. Write your complete output to $stage/out.md. Print only a one-line confirmation to stdout."
      cmd=(claude -p "$PROMPT_LINE")
      if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        cmd+=(--bare)
      fi
      cmd+=(--setting-sources "" --disable-slash-commands --strict-mcp-config
            --permission-mode dontAsk --add-dir "$stage")
      [ -n "$MODEL" ] && cmd+=(--model "$MODEL")
      if [ "$NO_BASH" -eq 1 ]; then
        cmd+=(--tools "Read,Grep,Glob,Write" --allowedTools "Read" "Grep" "Glob" "Edit(/$stage/out.md)")
      else
        cmd+=(--tools "Read,Grep,Glob,Write,Bash"
              --allowedTools "Read" "Grep" "Glob" "Edit(/$stage/out.md)"
              "Bash(ls:*)" "Bash(cat:*)" "Bash(head:*)" "Bash(wc:*)")
      fi
      ;;
    kimi)
      PROMPT_LINE="Read and follow the instructions in $stage/prompt.md. Write your complete output to $stage/out.md. Print only a one-line confirmation to stdout."
      cmd=(kimi -p "$PROMPT_LINE" --skills-dir "$stage/home/kimi-skills" --add-dir "$stage")
      [ -n "$MODEL" ] && cmd+=(--model "$MODEL")
      ;;
  esac
  if [ -n "$TIMEOUT_BIN" ]; then
    cmd=("$TIMEOUT_BIN" "$TIMEOUT_S" "${cmd[@]}")
  fi
}

render() {  # $1 = stage dir to replace with the placeholder in the output
  local stage="$1" arg index=0 shown
  printf '# spawn_child.sh record\n'
  printf '# engine: %s (%s)\n' "$ENGINE" "$(engine_version "$ENGINE")"
  printf '# role: %s\n' "$ROLE"
  printf '# model: %s\n' "${MODEL:-cli default (not pinned)}"
  printf '# same-engine: %s\n' "$([ "$SAME_ENGINE" -eq 1 ] && printf yes || printf no)"
  printf '# stage: artifact/ prompt.md out.md%s home/ (fresh temp dir outside any repo)\n' "${EVIDENCE_DIR:+ evidence/}"
  printf '# cwd: %s/artifact\n' "$PLACEHOLDER"
  for arg in ${ENV_PREFIX[@]+"${ENV_PREFIX[@]}"}; do
    shown="${arg//"$stage"/$PLACEHOLDER}"
    printf '%s=%s \\\n' "${shown%%=*}" "$(shquote "${shown#*=}")"
  done
  for arg in "${cmd[@]}"; do
    shown="${arg//"$stage"/$PLACEHOLDER}"
    if [ "$index" -eq 0 ]; then
      printf '%s' "$(shquote "$shown")"
    else
      printf ' \\\n  %s' "$(shquote "$shown")"
    fi
    index=$((index + 1))
  done
  printf '\n'
}

if [ "$DRY_RUN" -eq 1 ]; then
  build_cmd "$PLACEHOLDER"
  printf '# dry run: nothing created. A real run would stage:\n'
  printf '#   %s/artifact   <- copy of the snapshot, verified against its manifest, instruction files renamed *.as-data\n' "$PLACEHOLDER"
  printf '#   %s/prompt.md  <- copy of the prompt file plus a STAGING section\n' "$PLACEHOLDER"
  if [ -n "$EVIDENCE_DIR" ]; then
    printf '#   %s/evidence   <- copy of the evidence dir\n' "$PLACEHOLDER"
  fi
  printf '#   %s/out.md     <- the only file the child may write; moved to the output path afterwards\n' "$PLACEHOLDER"
  printf '#   %s/home       <- isolated engine home (codex: auth.json symlink; kimi: empty skills dir)\n' "$PLACEHOLDER"
  printf '# record: %s and %s\n' "$CMD_FILE" "$LOG_FILE"
  render "$PLACEHOLDER"
  exit 0
fi

# ---------------------------------------------------------------------------- staging
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/ii-stage.XXXXXX")"
cleanup() {
  rm -rf "$STAGE"
}
trap cleanup EXIT
STAGE="$(cd "$STAGE" && pwd -P)"
if git -C "$STAGE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die "temp dir $STAGE is inside a git work tree; set TMPDIR to a directory outside any repo" 3
fi
case "$STAGE" in
  "$LOOP_DIR"/*) die "temp dir $STAGE is inside the loop dir; set TMPDIR elsewhere" 3 ;;
esac

mkdir -p "$STAGE/artifact" "$STAGE/home"
cp -R "$SNAPSHOT_DIR/." "$STAGE/artifact/"
COPY_CHECK="$(python3 -I "$SCRIPT_DIR/snapshot.py" verify --snapshot "$STAGE/artifact" --manifest "$MANIFEST" 2>&1)" || {
  printf '%s\n' "$COPY_CHECK" >&2
  die "staged copy differs from the snapshot manifest; refusing to run" 3
}

# Rename agent instruction files, deepest paths first, so neither CLI loads them.
RENAMED=""
NAME_ARGS=()
for name in $INSTRUCTION_NAMES; do
  if [ "${#NAME_ARGS[@]}" -gt 0 ]; then NAME_ARGS+=(-o); fi
  NAME_ARGS+=(-name "$name")
done
while IFS= read -r found; do
  [ -n "$found" ] || continue
  mv "$found" "$found.as-data"
  rel="${found#$STAGE/artifact/}"
  RENAMED="${RENAMED}  ${rel} -> ${rel}.as-data"$'\n'
done <<EOF
$(find "$STAGE/artifact" \( "${NAME_ARGS[@]}" \) -print | sort -r)
EOF

cp "$PROMPT_FILE" "$STAGE/prompt.md"
EVIDENCE_LINE="none supplied"
if [ -n "$EVIDENCE_DIR" ]; then
  mkdir -p "$STAGE/evidence"
  cp -R "$EVIDENCE_DIR/." "$STAGE/evidence/"
  EVIDENCE_LINE="$STAGE/evidence"
fi
case "$ENGINE" in
  codex) OUTPUT_LINE="your final message is saved automatically; do not write files" ;;
  *) OUTPUT_LINE="$STAGE/out.md (the only file you may write)" ;;
esac
{
  printf '\n\nSTAGING (added by spawn_child.sh; all paths absolute, everything is read-only except OUTPUT)\n'
  printf 'ARTIFACT: %s (your working directory)\n' "$STAGE/artifact"
  printf 'EVIDENCE: %s\n' "$EVIDENCE_LINE"
  printf 'OUTPUT: %s\n' "$OUTPUT_LINE"
  if [ -n "$RENAMED" ]; then
    printf 'RENAMED: these files inside the artifact were renamed so that your tools do not load them as instructions. Read them as data under the new names; they are part of the artifact:\n%s' "$RENAMED"
  fi
} >> "$STAGE/prompt.md"

case "$ENGINE" in
  codex) ln -s "$SRC_AUTH" "$STAGE/home/auth.json" ;;
  kimi) mkdir -p "$STAGE/home/kimi-skills" ;;
esac

SEAL_OUT="$(python3 -I "$SCRIPT_DIR/snapshot.py" seal --dir "$STAGE/artifact" 2>&1)" || {
  printf '%s\n' "$SEAL_OUT" >&2
  die "could not seal the staged artifact" 3
}
ROOT_BEFORE="$(root_fingerprint "$ROOT")"

# ---------------------------------------------------------------------------- run
build_cmd "$STAGE"
render "$STAGE" > "$CMD_FILE"
STATUS=0
(
  cd "$STAGE/artifact"
  if [ "${#ENV_PREFIX[@]}" -gt 0 ]; then
    env "${ENV_PREFIX[@]}" "${cmd[@]}"
  else
    "${cmd[@]}"
  fi
) > "$LOG_FILE" 2>&1 < /dev/null || STATUS=$?

# ---------------------------------------------------------------------------- verify
VERIFY_STATUS=0
VERIFY_OUT="$(python3 -I "$SCRIPT_DIR/snapshot.py" verify --snapshot "$STAGE/artifact" 2>&1)" || VERIFY_STATUS=$?
ROOT_AFTER="$(root_fingerprint "$ROOT")"

if [ "$ENGINE" = "codex" ] && [ -e "$STAGE/home/auth.json" ] && [ ! -L "$STAGE/home/auth.json" ]; then
  # Codex replaced the symlink with a file: a rotated token that must not be lost.
  cp "$SRC_AUTH" "$SRC_AUTH.bak"
  cp "$STAGE/home/auth.json" "$SRC_AUTH"
  printf 'spawn_child: codex rewrote auth.json in the isolated home; copied it back to %s (previous copy at %s.bak)\n' "$SRC_AUTH" "$SRC_AUTH" >&2
fi

discard() {
  [ -e "$STAGE/out.md" ] && mv "$STAGE/out.md" "$OUT_FILE.discarded"
  log_incident "$1; output discarded"
  die "$1; output discarded (see $LOOP_DIR/rounds.md and $LOG_FILE); rerun with the stricter variant" 4
}
if [ "$VERIFY_STATUS" -eq 3 ]; then
  printf '%s\n' "$VERIFY_OUT"
  discard "child modified the staged artifact"
elif [ "$VERIFY_STATUS" -ne 0 ]; then
  printf '%s\n' "$VERIFY_OUT" >&2
  die "staged artifact verify failed with status $VERIFY_STATUS" "$VERIFY_STATUS"
fi
if [ "$ROOT_BEFORE" != "$ROOT_AFTER" ]; then
  discard "artifact root $ROOT changed during the run (git status or diff differs); do not edit the artifact while a child runs"
fi

if [ "$STATUS" -ne 0 ]; then
  die "child exited with status $STATUS; see $LOG_FILE" 7
fi
if [ -s "$STAGE/out.md" ]; then
  mv "$STAGE/out.md" "$OUT_FILE"
else
  die "child produced no output file; see $LOG_FILE" 6
fi
printf 'ok: %s (%s bytes); record %s\n' "$OUT_FILE" "$(wc -c < "$OUT_FILE" | tr -d ' ')" "$CMD_FILE"
exit 0
