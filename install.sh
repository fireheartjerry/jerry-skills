#!/usr/bin/env bash
# install.sh: install the skills in this repository for Claude Code, Codex and
# generic agents by symlinking (or copying) each skills/<name> directory.
#
# Works on macOS bash 3.2 and Linux. Idempotent: re-running changes nothing that
# is already correct. Never overwrites: an existing directory or file in the way
# is moved to <skills-dir>.bak/<name>-<timestamp> first, outside the directory
# agents scan, so a backup never loads as a duplicate skill (a copy this script made earlier is
# recognised by its .install-source marker and refreshed in place).
#
#   ./install.sh                      symlink every skill into all three targets
#   ./install.sh --target codex       one target: claude | codex | agents | all
#   ./install.sh --skill mind-reading --skill high-agency
#   ./install.sh --copy               copy instead of symlink (for machines that
#                                     do not keep this clone around)
#   ./install.sh --dry-run            print what would happen, change nothing
#   ./install.sh --uninstall          remove what this script installed; leaves
#                                     anything it did not create untouched
#
# Targets:  claude -> ~/.claude/skills   codex -> ~/.codex/skills   agents -> ~/.agents/skills

set -eu

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 2
}

repo_dir="$(cd "$(dirname "$0")" && pwd -P)"
skills_src="$repo_dir/skills"
marker=".install-source"

targets=""
mode="link"
dry_run=0
uninstall=0
selected=""

while [ $# -gt 0 ]; do
  case "$1" in
    --target)
      [ $# -ge 2 ] || die "--target needs a value (claude|codex|agents|all)"
      targets="$targets $(printf '%s' "$2" | tr ',' ' ')"
      shift 2 ;;
    --target=*)
      targets="$targets $(printf '%s' "${1#--target=}" | tr ',' ' ')"
      shift ;;
    --skill)
      [ $# -ge 2 ] || die "--skill needs a skill name"
      selected="$selected $2"
      shift 2 ;;
    --skill=*)
      selected="$selected ${1#--skill=}"
      shift ;;
    --copy) mode="copy"; shift ;;
    --dry-run|-n) dry_run=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[ -n "${HOME:-}" ] || die "HOME is not set"
[ -d "$skills_src" ] || die "no skills/ directory next to $0"

# Expand and validate targets.
expanded=""
for t in ${targets:-all}; do
  case "$t" in
    all) expanded="$expanded claude codex agents" ;;
    claude|codex|agents) expanded="$expanded $t" ;;
    *) die "unknown target: $t (use claude, codex, agents or all)" ;;
  esac
done
targets=""
for t in $expanded; do
  case " $targets " in *" $t "*) ;; *) targets="$targets $t" ;; esac
done

target_dir() {
  case "$1" in
    claude) printf '%s\n' "$HOME/.claude/skills" ;;
    codex) printf '%s\n' "$HOME/.codex/skills" ;;
    agents) printf '%s\n' "$HOME/.agents/skills" ;;
  esac
}

# Skills: every skills/<name> directory that has a SKILL.md, or the --skill selection.
available=""
for d in "$skills_src"/*/; do
  [ -f "$d/SKILL.md" ] || continue
  available="$available $(basename "$d")"
done
[ -n "$available" ] || die "no skills found under $skills_src"

if [ -n "$selected" ]; then
  for s in $selected; do
    case " $available " in
      *" $s "*) ;;
      *) die "unknown skill: $s (available:$available)" ;;
    esac
  done
  skills="$selected"
else
  skills="$available"
fi

# Helpers.
# Physical path of a directory (follows symlinks); empty when it cannot be entered.
physical() { (cd "$1" 2>/dev/null && pwd -P) || true; }

run() {
  [ "$dry_run" = 1 ] && return 0
  "$@"
}

prefix=""
[ "$dry_run" = 1 ] && prefix="(dry-run) "

say() { # target verb detail
  printf '%s[%s] %-8s %s\n' "$prefix" "$1" "$2" "$3"
}

n_linked=0; n_copied=0; n_unchanged=0; n_backed_up=0; n_removed=0; n_skipped=0

backup_path() {
  local base cand n
  base="$(dirname "$1").bak/$(basename "$1")-$(date +%Y%m%d-%H%M%S)"
  cand="$base"; n=1
  while [ -e "$cand" ] || [ -L "$cand" ]; do
    cand="$base-$n"; n=$((n + 1))
  done
  printf '%s\n' "$cand"
}

our_copy() { # dest src -> 0 if dest is a copy this script made from src
  [ -f "$1/$marker" ] && [ "$(cat "$1/$marker")" = "$2" ]
}

move_aside() { # target name dest what
  local bak
  bak="$(backup_path "$3")"
  say "$1" "backup" "$2: existing $4 moved to $bak"
  run mkdir -p "$(dirname "$bak")"
  run mv "$3" "$bak"
  n_backed_up=$((n_backed_up + 1))
}

install_one() { # target name
  local target="$1" name="$2" src dir dest cur
  src="$skills_src/$name"
  dir="$(target_dir "$target")"
  dest="$dir/$name"

  if [ -L "$dest" ]; then
    cur="$(physical "$dest")"
    if [ "$mode" = link ] && [ -n "$cur" ] && [ "$cur" = "$(physical "$src")" ]; then
      say "$target" "ok" "$name already linked -> $src"
      n_unchanged=$((n_unchanged + 1))
      return 0
    fi
    if [ -z "$cur" ]; then
      say "$target" "unlink" "$name: broken symlink -> $(readlink "$dest") removed"
      run rm -f "$dest"
    else
      move_aside "$target" "$name" "$dest" "symlink -> $cur"
    fi
  elif [ -d "$dest" ]; then
    if our_copy "$dest" "$src"; then
      if [ "$mode" = copy ] && diff -rq -x "$marker" "$src" "$dest" >/dev/null 2>&1; then
        say "$target" "ok" "$name already copied and unchanged"
        n_unchanged=$((n_unchanged + 1))
        return 0
      fi
      say "$target" "replace" "$name: copy installed earlier by this script is replaced"
      run rm -rf "$dest"
    else
      move_aside "$target" "$name" "$dest" "directory"
    fi
  elif [ -e "$dest" ]; then
    move_aside "$target" "$name" "$dest" "file"
  fi

  run mkdir -p "$dir"
  if [ "$mode" = link ]; then
    say "$target" "link" "$name -> $src"
    run ln -s "$src" "$dest"
    n_linked=$((n_linked + 1))
  else
    say "$target" "copy" "$name <- $src"
    run cp -Rp "$src" "$dest"
    if [ "$dry_run" = 0 ]; then
      printf '%s\n' "$src" > "$dest/$marker"
    fi
    n_copied=$((n_copied + 1))
  fi
}

uninstall_one() { # target name
  local target="$1" name="$2" src dir dest cur backups
  src="$skills_src/$name"
  dir="$(target_dir "$target")"
  dest="$dir/$name"

  if [ -L "$dest" ]; then
    cur="$(physical "$dest")"
    if { [ -n "$cur" ] && [ "$cur" = "$(physical "$src")" ]; } || { [ -z "$cur" ] && [ "$(readlink "$dest")" = "$src" ]; }; then
      say "$target" "unlink" "$name (-> $src)"
      run rm -f "$dest"
      n_removed=$((n_removed + 1))
    else
      say "$target" "skip" "$name: symlink points elsewhere ($(readlink "$dest")), left alone"
      n_skipped=$((n_skipped + 1))
    fi
  elif [ -d "$dest" ]; then
    if our_copy "$dest" "$src"; then
      say "$target" "remove" "$name (copy installed by this script)"
      run rm -rf "$dest"
      n_removed=$((n_removed + 1))
    else
      say "$target" "skip" "$name: directory was not installed by this script, left alone"
      n_skipped=$((n_skipped + 1))
    fi
  elif [ -e "$dest" ]; then
    say "$target" "skip" "$name: not a symlink or directory, left alone"
    n_skipped=$((n_skipped + 1))
  else
    say "$target" "absent" "$name: not installed"
  fi

  backups="$(ls -d "$(dirname "$dest").bak/$name"-* 2>/dev/null || true)"
  if [ -n "$backups" ]; then
    say "$target" "note" "$name: backups present, restore or delete by hand: $(printf '%s' "$backups" | tr '\n' ' ')"
  fi
}

action="install"
[ "$uninstall" = 1 ] && action="uninstall"
printf 'install.sh: %s %s (mode: %s, targets:%s, skills:%s)\n' "$action" "$repo_dir" "$mode" "$targets" "$skills"

for target in $targets; do
  for name in $skills; do
    if [ "$uninstall" = 1 ]; then
      uninstall_one "$target" "$name"
    else
      install_one "$target" "$name"
    fi
  done
done

if [ "$uninstall" = 1 ]; then
  printf '%sdone: %d removed, %d skipped\n' "$prefix" "$n_removed" "$n_skipped"
else
  printf '%sdone: %d linked, %d copied, %d unchanged, %d backed up\n' "$prefix" "$n_linked" "$n_copied" "$n_unchanged" "$n_backed_up"
fi
