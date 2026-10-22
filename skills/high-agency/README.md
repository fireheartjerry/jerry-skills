# high-agency

An instruction-only skill for Claude Code, Codex and any agent that reads
SKILL.md. It carries a task from request to verified result and keeps consent
rules intact: it grants no new permission to send, publish, delete or spend.

Status: the shipped text is unmeasured. The last measured text (phase 2,
2026-10-21) passed 37/42 scenarios injected against 25/42 with no skill, and
failed its own ship rule on routing and hard violations (`evals/results.md`).
Codex has run two smoke cells and no consent scenario.

## Install

From the repository root (`../../README.md#install` has the details):

    ./install.sh --skill high-agency            # symlinks into ~/.claude, ~/.codex and ~/.agents skills
    ./install.sh --skill high-agency --copy     # copies instead of symlinking

Or copy this directory to `~/.claude/skills/high-agency` or
`~/.codex/skills/high-agency` by hand, or install the repository as a Claude
Code plugin (which installs every skill in it).

## Make it load

Installed this way the skill loads on its own only for some requests. In the
phase-2 routing run (Claude Code, a project holding this skill alone) it
loaded for the quoted cue phrases ("handle it", "take ownership", "high
agency": 9/9), for about two thirds of check questions ("does X work?", "is
it fixed?": 14/21), for 3/9 resume prompts, and for no plain multi-step
imperative, plan or brief, or long job (0/39). Investigation requests and
"don't wait for my approval" were not in that run's prompt set and are
unmeasured. In the 42 behavior cells of the installed arm it loaded 4 times
and scored like no skill (25/42).

Four ways to have it present for the tasks it is written for:

- Always on (the delivery form that was measured): append `SKILL.md` to your
  project's `CLAUDE.md` or `AGENTS.md`, or pass it to Claude Code with
  `claude --append-system-prompt-file skills/high-agency/SKILL.md`, and add a
  line saying where its `references/` directory is. The 37/42 above is the
  phase-2 body injected this way; the shipped body has not run.
- Always on, short form: `core.md` (under 350 words) carries the mode
  table, the consent guards, the verify floor and the report shape. Pass it
  with `claude --append-system-prompt-file skills/high-agency/core.md`, or
  paste it into `CLAUDE.md` or `AGENTS.md`. Unmeasured; the `core` arm in
  `evals/run.py` measures it.
- By name, per task: `/high-agency` in Claude Code (`/<plugin>:high-agency`
  when installed as a plugin), `$high-agency` in Codex. Unmeasured.
- A pointer in project instructions. Unmeasured (the `pointer` arm in
  `evals/run.py` measures it); paste it into `CLAUDE.md` or `AGENTS.md`:

      For multi-step builds, repairs, investigations, plans or briefs, and jobs
      that outlive a turn, read the high-agency skill's SKILL.md first and
      follow it.

Writing a brief for a child agent also needs one of these: the description
claims briefs to execute, not briefs to write.

## Tests and evals

Offline tests (python3, bash, git and make; no tokens): `bash tests/run.sh`.
Evals spend tokens and run agents unsandboxed: read `evals/README.md` first.
