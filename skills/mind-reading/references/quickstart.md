# First read

Start with a fictional model. It demonstrates the useful behavior before you give
the pipeline access to private transcripts.

## Install

```bash
git clone https://github.com/fireheartjerry/jerry-skills.git ~/jerry-skills
~/jerry-skills/install.sh --skill mind-reading
```

Python 3.9 or newer is enough for the offline demo. No API key, CLI login or Python
package installation is required. The installer supports Claude Code, Codex and
agents that read `SKILL.md`; see the repository README for plugin installation.

Define this function once in your shell so the commands below work in zsh and bash:

```bash
MR() { python3 -I "$HOME/jerry-skills/skills/mind-reading/scripts/mind_reading.py" "$@"; }
```

## Try three short requests

```bash
MR --home ~/.local/share/mind-reading-demo demo
```

The demo writes only to that separate data home and uses an entirely fictional
engineer. It reads no transcript directories and makes no model calls. It runs the
real runtime selector for these seeds:

| Seed | Evidence it retrieves that the seed omitted |
|---|---|
| `add the migration` | Preserve existing rows and include a rollback path |
| `add the setting` | Make it reachable from the command palette |
| `fix the crash` | Reproduce first and keep a deterministic regression test |

Read the printed context and the authored example prompts. The examples show what
an agent can write from that evidence; they are labeled illustrations rather than
generated benchmark results. The compact demo uses the supported legacy format,
which the selector reports explicitly. It is not your personal model.

Try a different seed against the same fictional model:

```bash
MR --home ~/.local/share/mind-reading-demo select --seed-file - --kind "settings keyboard" <<'SEED'
add a shortcut for this setting
SEED
```

The quoted heredoc passes shell punctuation literally. Re-run `demo --force` in
the same demo home to reset its fictional files. The command refuses a directory
containing a real model or files outside the demo.

## Build your own candidate

Ask your agent:

> Build a private mind-reading candidate from my Claude Code and Codex transcripts.
> Follow references/build.md, inspect the scrub report before model calls, show the
> estimate and use an explicit call budget. Keep my current model active while I
> review the candidate. Do not write global instructions automatically.

The [build guide](build.md) gives the commands. Extraction and selection run locally;
mining and evaluation send scrubbed text to your configured model provider. Review
the source roots and scrubbed records first.

Once you choose the reviewed model and install its personal always-on block,
underspecified ordinary requests can use the skill automatically. Clear requests
stay clear. To explicitly inspect the full reading, use:

> read my mind: add the migration

The agent shows `Reading:` and a brief with stated `[S]`, recurring `[L]` and guessed
`[G]` inferences, then acts within the current session's authorization. Unsupported
details should stay out. Your latest words override the model, and a correction
takes effect on the next read without waiting for a rebuild.

The agent can supply a small private context frame to the selector: the active
goal, recent user turns and verified facts. New models retain the context before
historical short requests, so "ready" can recall a relevant situation rather than
match unrelated people who also said "ready". Completed blind choices can be learned
into a separate candidate with `preferences learn`; the original reason for a
choice remains unknown unless you gave one.

Your own candidate can also produce a private **intent atlas** with
`atlas --out '<private atlas.html>'`: a searchable view of what you asked, what you later added,
and the exact sources behind the model's confidence. It runs offline and keeps an
empty evidence store empty.

## Decide whether it helps

Use blind human choices between a model-assisted expansion and the same seed
without a model. Record wins, ties, losses and the corrections you subsequently
make, with the total number of reads beside each rate. Inspect wrong assumptions
and scope expansion directly. Automated judge scores are diagnostic until they
agree with enough human decisions to establish useful calibration.

For next-step predictions, `foresight record` freezes the prediction before the
later turn. `foresight observe` records explicit human outcomes; `foresight report`
keeps unresolved and pending predictions outside its precision denominator.
Historical replay is available through `foresight replay`, with separate input and
reveal files. It is useful for development, and is labelled retrospective.

A weekly refresh should build a separate candidate and show the change, checks
and evidence for review. It never makes a candidate live automatically. A scheduled
plan is a plan; your scheduler must actually be configured to run it.
