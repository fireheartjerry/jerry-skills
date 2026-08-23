# Spawning an independent reviewer or scorer

The reviewer and the scorer are only worth what their independence is worth. Three
levels, best first:

| Level | Independence | How |
|---|---|---|
| Separate agent CLI | Different model, separate process, zero shared context | commands below |
| Subagent | Fresh context, same model, same session | your harness's subagent tool |
| Inline | None — you are grading your own work | last resort; disclose it in the report |

A different CLI is usually worth the extra minute. It is the only option where the
reviewer's blind spots differ from yours, and cross-model disagreement is itself
signal: when two different models independently flag the same thing, that finding
is real.

**Independence is a property of what the reviewer could see, not of which tool you
used.** A subagent sharing your workspace can read your changed files and your
notes. Verify the review cites real evidence before acting on it — see the
independence check in SKILL.md §3a.

---

## Invocations

**Preflight — do this before the user goes offline.** Check what exists *and works*:

```bash
command -v codex kimi claude                                  # Bash
```
```powershell
Get-Command codex,kimi,claude -ErrorAction SilentlyContinue   # PowerShell
```

Then confirm one actually responds — `codex exec --skip-git-repo-check "reply with OK"`.
A login prompt, an auth error, or an empty response means that CLI is unavailable:
drop a tier and record which tier you actually used for the final report. An
installed-but-unauthenticated binary fails silently and reads exactly like a clean
review.

**Never spawn the CLI you are running.** If you are Claude Code use `codex` or
`kimi`; if you are Codex use `claude` or `kimi`; if you are Kimi use `claude` or
`codex`. A self-spawn is the same model with the same blind spots — it is the
Subagent tier wearing the Separate-CLI label. If the only CLI available is the one
you're running, log it honestly as Subagent.

**Pass the brief by reference, never as an argument.** Write the full prompt to a
file at an absolute path, then hand the CLI a short ASCII one-liner pointing at it.

This is not a style preference. Reviewer briefs are long and full of double quotes,
and passing one as an argument corrupts it in ways that do not announce themselves:

- **Windows PowerShell 5.1 silently strips embedded double quotes.** Measured: a
  brief containing `It says "LOCATION" and "CONSEQUENCE".` arrives at the child
  process as `It says LOCATION and CONSEQUENCE.` — one argv entry, no error, no
  exit code. Every quoted section name, output-format example, and error string in
  the brief loses its quoting, and the review proceeds against a mangled prompt.
  (PowerShell 7 preserves them; you often won't know which shell the loop runs in.)
- Backticks in the brief are PowerShell escape characters.
- Very long argv entries hit platform limits and truncate.

A file path has none of these problems and is byte-identical in every shell:

### Codex
```
codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -c model_reasoning_effort="high" -C "<project-dir>" "Read and follow the instructions in <abs-path>/reviewer-prompt.md"
```

### Kimi
```
kimi -p "Read and follow the instructions in <abs-path>/reviewer-prompt.md" --add-dir "<project-dir>"
```

### Claude
```
claude -p "Read and follow the instructions in <abs-path>/reviewer-prompt.md" --dangerously-skip-permissions --add-dir "<project-dir>"
```

### Per-CLI notes

**Codex** — `-C` (alias `--cd`) sets the working root; `--skip-git-repo-check`
allows non-repo directories. `-c model_reasoning_effort="high"` is **usually
necessary**: the config default is often `low`, which is far too shallow for an
audit and yields a thin review that looks like the work is fine.

**Kimi** — `-p` is already fully autonomous; it executes tools without approval
because there is no channel to ask on. **Do not pass `-y`/`--yolo` or `--auto`
with `-p`** — Kimi rejects both outright (`error: Cannot combine --prompt with
--yolo.`) and the run dies before it starts. This is the most common way a Kimi
headless call fails. `--add-dir` grants access outside the cwd and is repeatable.

**Claude** — `--add-dir` likewise; pass the artifact directory explicitly or the
reviewer cannot read it.

---

## Practical notes

**Give the CLI the path, not the contents.** Do not read the brief into a variable
and pass it as an argument (`$(cat …)`, `Get-Content -Raw`) — that is the silent
corruption described above. Let the child process read the file itself.

**Ask for the output on disk.** Append to the prompt:

> Write your complete review to `<abs-path>/review-round-N.md`. Print only a
> one-line confirmation to stdout.

Then read the file. This survives terminal truncation and gives you an artifact to
diff against the next round's review.

**Set a generous timeout and run it in the background.** A real audit takes minutes.
Blocking on it wastes the time you could spend preparing the triage.

**Give it a way in.** The reviewer starts with nothing. Include in the prompt:
absolute paths, the command to build or run the thing, the URL if it's a web page,
whether a dev server is already running, and any credentials or ports it needs. A
reviewer that spends its whole budget fighting the environment returns an empty
review — and an empty review reads deceptively like good news.

**Verify it actually looked.** A review with no file:line citations and no quoted
evidence means the agent never reached the artifact. Check for citations before you
act on any of it; a hallucinated review sends you into a full round of work against
problems that don't exist.

**Environments don't inherit.** A separate process has its own working directory,
its own PATH, and no access to your browser session or running servers unless you
arrange it. This matters most for the scorer: a visual check that you can run and it
cannot will score 0 and read as a regression.

---

## When only one CLI is available

Alternate roles rather than agents: use the other CLI for the **reviewer**, where
independence matters most because it's finding what you're blind to, and a fresh
subagent for the **scorer**. If you have neither a second CLI nor subagents, run
both inline and say so in the final report — the numbers remain useful as a relative
trend, just not as an absolute.
