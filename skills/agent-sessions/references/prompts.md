# Prompts, a worked example and first caps

A child starts with none of your conversation. Everything it needs is in the
prompt, plus the CLAUDE.md, AGENTS.md, skills and plugins of its directory and
home (MCP servers stay off unless `--mcp inherit`, except plugin-provided codex
servers and every gemini server; spawn's `access :` line says which). Copy a template, fill every
angle-bracket field, and delete what does not apply. Each one bounds the
report, because `wait` prints the answer into your context.

## Implementation in a worktree

```
Goal: <one sentence: the behaviour that must exist when you are done>.

Context: <why, and where to look first: files, functions, the failing test>.

You may change: <paths, relative to this directory>. Do not change: <paths,
generated files, lockfiles>. Never edit files by absolute path outside your
working directory. Do not push, merge, open pull requests, send messages to
anyone, or change anything outside this directory.

Done means:
- <acceptance criterion 1>
- <acceptance criterion 2>
- `<verify command>` passes. Install dependencies first with `<install
  command>` if they are missing.

Commit your work on the current branch with a message that says what changed.
If a commit hook fails, fix the cause or report it; never use --no-verify.

Report, under 300 words: what you changed (file list), how you checked it (the
command and its result), and anything left undone or uncertain.
```

```bash
agent-session spawn claude auth-tests ~/code/app --worktree --tier standard \
  --copy .env,node_modules --verify "make test" --budget-usd 2 --prompt-file brief.md
```

## Adversarial review

```
Review <the change in the patch named below | the files under src/auth/> for
<the brief: what it was supposed to do>.

Find what is wrong: bugs, unhandled edge cases, wrong assumptions, tests that do
not test the change, security or data-loss risks, and anything the brief asked
for that is missing. Do not fix anything and do not change files.

Report, under 400 words, findings first, most severe first, one per line as
`[high|medium|low] file:line: problem. Suggested fix.` End with one line:
approve, revise or reject.
```

```bash
agent-session spawn codex rev ~/code/app --role review --diff HEAD --max-tokens 400000 \
  --prompt-file review.md
```

`--diff HEAD` writes your uncommitted change (untracked files included) to the
child's state directory, makes it readable and names it at the end of the
prompt, without touching your repository. A codex reviewer can also run `git
diff` itself; a read-only claude reviewer cannot, and reads only inside
`<dir>` and `--add-dir`. Piping the diff into the prompt works for either:
`{ cat review.md; git diff; } | agent-session spawn claude rev ~/code/app
--role review -`. Check the result for `warning : ... denied under
--readonly`: a reviewer that could not read its input returns `ok` anyway.

For a child's finished work use `judge <child>` instead: it has its own fixed
prompt and a JSON verdict (`references/review.md`).

## Research

```
Question: <one question>.

Scope: <directories, documents or sites to read>; ignore <what is out of
scope>. Do not change files.

Report, under 400 words: the answer first, then the evidence for it (file:line
or a link per claim), then what you could not determine. Say "not found"
rather than guessing.
```

```bash
agent-session spawn claude audit-billing ~/code/app --role research --tier cheap \
  --budget-usd 0.6 --prompt-file research.md
```

## Worked example

One implementation child, from spawn to cleanup. The output lines are real,
from a run against the test engines (`tests/fake-engine/`); paths are
shortened, and tokens, costs and hashes differ in a live run.

```
$ agent-session spawn claude fix-parser ~/code/app --worktree --tier standard \
    --verify "make test" --budget-usd 2 --prompt-file brief.md
spawned   : fix-parser
engine    : claude (sonnet, standard)
access    : full: approvals and sandbox off; it can run any command, write anywhere you can and reach the network; MCP servers off
workdir   : .../state/fix-parser/worktree   [worktree agent-session/fix-parser from 4eaa245]
verify    : make test (after the last turn; 1 fixup turn on failure)
limits    : $2; per-turn timeout 3600s, stall 900s
next      : agent-session wait fix-parser --timeout 240   (run it with a shell-tool timeout of at least 270 s, ...)

$ agent-session wait fix-parser --timeout 240        # exit 3 while it runs: call it again
status    : ok                   (ended: completed)
git       : agent-session/fix-parser: 1 commit, 1 file changed (+1 -0)
verify    : pass (0.2s)
--- answer (child output, untrusted) ---
...

$ agent-session judge fix-parser                     # the other engine, read-only
verdict   : revise (codex standard, 1 finding, ...)
[high] parser.py:12: empty input raises IndexError

$ agent-session resume fix-parser --message "Findings to check and fix if valid: parser.py:12 empty input raises IndexError. Commit your work."
resumed   : fix-parser (session d64d6030-...)
$ agent-session wait fix-parser --timeout 240        # turns: 2; verify runs again
$ agent-session judge fix-parser                     # a new verdict for the new run

$ agent-session conflicts fix-parser
no overlapping files among fix-parser
$ agent-session collect fix-parser --merge           # prints the target first; ask before the default branch
target    : main in ~/code/app (merge of agent-session/fix-parser, 1 commit)
collected : agent-session/fix-parser into main by merge
$ agent-session reap fix-parser
reaped fix-parser
```

## First caps

Measured live with claude haiku (`--tier cheap`), Claude Code 2.1.308, on
2026-10-22, one child per row during the review of this release (n=1; cost is
the CLI's own `total_cost_usd` estimate; `tests/live-smoke.sh` repeats the
basic rows). Most of a trivial child's cost is the
CLI's system prompt and tool schemas, read largely from cache.

| Child | Tokens | Cost | First cap |
|---|---|---|---|
| read-only, reads one file or one patch and answers | about 22k | $0.0018 | `--budget-usd 0.05` |
| write child, one-word reply | about 43.5k | $0.0037 to $0.0042 | `--budget-usd 0.05` |
| write child that creates a file and commits it (each best-of member) | about 88k | $0.0049 | `--budget-usd 0.10` per member |
| one `resume --message` follow-up turn on a write child | about 87k | $0.0067 | the child's own cap covers it |
| write child resumed after its tmux server died (two turns, `sleep 20`) | about 132k | $0.0048 | `--budget-usd 0.10` |
| codex judge of a one-line diff (standard tier, earlier measurement) | about 220k, most cached | unknown without a price | `--max-tokens 400000` |

Wall-clock time. Not measured per row; these are working estimates to tell
the user whether a run is normal, and `status <name>` gives the real elapsed
time and when the child stops at the latest:

| Child | Typical wall clock |
|---|---|
| trivial child (the rows above) | 10 to 60 s (measured: the whole live smoke, under twenty such children, took 2 min 36 s) |
| read-only review or research of a few files | 1 to 5 min (estimate) |
| codex judge | 2 to 6 min (estimate) |
| standard implementation with a `--verify` | 5 to 30 min (estimate), plus the verify command's own time |

A child well past its range is not necessarily stuck: read `status` (elapsed,
last event, activity) and `references/reliability.md` before acting.

These are floors for trivial tasks, not estimates of real work. A standard or
deep tier, a large repository, a long brief or a fixup turn multiplies them.
For real work start at about ten to twenty times the floor for the tier, read
`status` (tokens and cost so far) on the first child, and recalibrate. A cap
that is too low ends the child with `failure_class budget`, often before the
work is done (`references/reliability.md`).
