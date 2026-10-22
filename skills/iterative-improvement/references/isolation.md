# How spawn_child.sh isolates a child

Background for maintainers and for anyone who has to build a stage by hand. No loop
step needs this file: `scripts/spawn_child.sh` applies all of it, and
`references/cross-agent.md` says what the agent must do.

## What "independent" has to mean here

A child process is not isolated by default. Without flags it loads the operator's
global instruction files, memory, skills and MCP servers; one of those skills may be
this one, and a global "be high agency" instruction turns a reviewer into an editor.
Isolation has three layers, and all three are used:

1. **What it is given**: a staged copy in a fresh temp dir outside any git work tree
   and outside LOOP, laid out as `artifact/` (the snapshot, verified against its
   manifest), `prompt.md` (the brief plus a STAGING section naming these paths),
   `evidence/` (when supplied) and `out.md`. The child's cwd is `artifact/`. No path it
   receives contains the round number, the repo or LOOP, so `git rev-parse`, `..`
   and `ls ../..` find nothing. This hides those paths; whether the child could still
   reach them by absolute path depends on layer 3. Agent instruction files inside the
   artifact (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.claude/`, `.codex/`, ...,
   matched case-insensitively, so `agents.md` and `.Claude/` count) are renamed
   `*.as-data` and listed in the STAGING section, so the child reads them as part of
   the artifact instead of obeying them.
2. **What it loads**: per-engine flags below drop user config, memory, skills, MCP and
   project instruction files.
3. **What it can do**: a read-only sandbox where the engine has one, a tool allowlist
   where it does not, and two checks after every run: the staged artifact is re-hashed
   against its sealed manifest, and the artifact's files in ROOT (the manifest's root
   and `--path` scope, or the whole root for kimi) are re-hashed against a fingerprint
   taken before the run with `snapshot.py fingerprint`. The fingerprint hashes file
   content, so it sees edits to untracked files and works outside git; it reads the
   file list with `git ls-files`, which takes no index lock. Either change discards
   the output (exit 4). The stage is deleted afterwards.

The child runs under a small python3 wrapper in its own process group. On `--timeout`
(default 900 seconds) or when the script receives TERM, INT or HUP, the whole group is
killed, so no engine keeps running, and billing, after the script has gone. The
wrapper also polls for its parent once a second: a script killed with SIGKILL runs no
trap, so the wrapper sees itself reparented and kills the group (exit 129). A timeout
exits 124 inside the wrapper and 7 from the script. `--background` starts the same
command in a new session (`os.setsid`), so a harness that kills its own process group
when a call ends does not reach the child; the run writes `.pid`, `.spawn` and, from
its exit trap, `.status`.

## Per-engine invocations (what the script runs)

### Codex
```
CODEX_HOME="<stage>/engine-home" codex exec --ephemeral -s read-only --skip-git-repo-check \
  -c model_reasoning_effort="high" -c project_doc_max_bytes=0 -m "<model>" \
  -C "<stage>/artifact" -o "<stage>/out.md" \
  "Read and follow the instructions in <stage>/prompt.md. Your final message must be your complete output and nothing else; it is saved to a file automatically." < /dev/null
```
`<stage>/engine-home` is a fresh directory containing only a symlink `auth.json ->
$CODEX_HOME/auth.json` (default `~/.codex/auth.json`; a relative `CODEX_HOME` is
resolved first). Verified: `--ignore-user-config` alone still loads the global
`AGENTS.md` from the real `CODEX_HOME`; a fresh home does not, and `-s read-only` blocks
writes everywhere, which is why the output file is written by Codex itself from `-o`
rather than by the sandboxed child. Verified: `-c project_doc_max_bytes=0` stops Codex
loading an `AGENTS.md` from the cwd (a child asked to repeat its project instructions
answered `NONE` with the flag and quoted the file without it); the rename to
`*.as-data` is the second line of defence. Codex fills the fresh home with caches on
first run; the stage is deleted with them. If Codex rewrites `auth.json` (token
refresh) the symlink becomes a plain file; the script then copies it back over the
original with a `.bak` of the previous file and says so, on a signal too. `-m` pins the
model; the CLI default can change when Codex is upgraded mid-loop, and the `.cmd`
header records the CLI version.

`-C` sets the working root; `--skip-git-repo-check` allows non-repo directories (the
stage never has `.git`). `-c model_reasoning_effort="high"` is usually necessary: the
config default is often `low`, which yields a thin review that looks like the work is
fine. `-o/--output-last-message` writes the child's final message to a file; the script
moves it to OUT afterwards.

### Claude
```
cd "<stage>/artifact" && claude -p "Read and follow the instructions in <stage>/prompt.md. Write your complete output to <stage>/out.md. Print only a one-line confirmation to stdout." \
  --no-session-persistence --setting-sources "" --disable-slash-commands --strict-mcp-config \
  --permission-mode dontAsk --add-dir "<stage>" [--model "<model>"] \
  --tools "Read,Grep,Glob,Write" \
  --allowedTools "Read(//<stage>/**)" "Grep(//<stage>/**)" "Glob(//<stage>/**)" "Edit(//<stage>/out.md)" < /dev/null
```
`--no-session-persistence` keeps the child from writing a transcript, which would
otherwise hold the whole artifact and review under `~/.claude/projects/` in a
directory named after the deleted stage. Runs made before this flag was added left
such directories (names containing `ii-stage`); they are safe to delete by hand.
`--setting-sources ""` drops user, project and local settings; `--disable-slash-commands`
drops all skills; `--strict-mcp-config` drops MCP servers; `dontAsk` denies anything not
allowlisted without prompting. Every allow rule is scoped to the stage: a bare `Read`,
`Grep` or `Glob` rule allows that tool on any path (an earlier version did this, and a
child read a shell rc file in the user's home). Verified live on Claude Code 2.1.306:
with the scoped rules, Read of a file outside the stage, Glob and Grep outside it were
denied, while Read, Glob and Grep inside it worked. Verified: write permission matches
`Edit(...)` patterns only, and a path rule needs the `//` prefix to be absolute (a single
`/` is relative to the project root), so `Edit(//<stage>/out.md)` is what lets the Write
tool create the output; `Write(<path>)` is ignored and `Edit(/<abs path>)` denies.
Verified: with `--setting-sources ""` a `CLAUDE.md` in the cwd was not loaded (the child
reported no project instructions); the rename is still applied. `--bare` would also skip
the user-level `CLAUDE.md` but skips keychain reads too, so on macOS it fails auth unless
`ANTHROPIC_API_KEY` is set; the script adds `--bare` only when that variable is set.
There is no Bash tool: a prefix rule such as `Bash(cat:*)` cannot be confined to a
directory (a path that climbs out with `..`, or any absolute path, matches it), and Read,
Glob and Grep cover listing, reading and counting. Never pass the flag that skips
permissions; the allowlist is the sandbox.

### Kimi (reviewer only)
```
cd "<stage>/artifact" && kimi -p "Read and follow the instructions in <stage>/prompt.md. Write your complete output to <stage>/out.md. Print only a one-line confirmation to stdout." \
  --skills-dir "<stage>/engine-home/kimi-skills" --add-dir "<stage>" [--model "<model>"] < /dev/null
```
`--skills-dir` pointed at an empty directory replaces auto-discovered skills. Kimi has
no sandbox and no tool allowlist: untrusted artifact content can drive any command as
you, and the post-run checks cannot see writes outside the stage and ROOT (a shell rc
file, `~/.ssh`). The script refuses kimi without `--unsandboxed`, records
`# unsandboxed: yes` in the `.cmd`, refuses `kimi scorer`, and fingerprints the whole
root for it. `-p` is already fully autonomous; it executes tools without approval
because there is no channel to ask on. Do not combine `-p` with `-y` or `--auto`; kimi
rejects both (`error: Cannot combine --prompt with ...`). Kimi is the only engine that
can reach a localhost server.

## Why the brief goes by reference

The script copies the brief to `<stage>/prompt.md` and hands the CLI a short ASCII
one-liner pointing at it. Reviewer briefs are long and full of double quotes, and
passing one as an argument corrupts it in ways that do not announce themselves: Windows
PowerShell 5.1 silently strips embedded double quotes (measured: `It says "LOCATION" and
"CONSEQUENCE".` arrives as `It says LOCATION and CONSEQUENCE.`, no error), backticks are
PowerShell escape characters, and very long argv entries hit platform limits. A file
path has none of these problems. Both `codex exec` and `claude -p` wait on an open stdin
when launched from an agent's shell tool, which is why stdin is `/dev/null`.

## Prompt-level defence (both briefs carry it)

Both briefs state that everything inside the artifact is data, not instructions. This
is the third line after the config flags and the `*.as-data` rename, because reviewed
content can contain injected text anywhere, not only in instruction files.
