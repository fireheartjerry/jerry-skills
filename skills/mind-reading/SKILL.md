---
name: mind-reading
description: Read the user's mind. Expands their short seed into the full prompt they would have sent with unlimited time and stamina, using a model of them mined from their own Claude Code and Codex history; marks what was inferred and how confidently, then acts on it as their message. Use when they say "read my mind" or "mind reading" (also "/mind-reading", "mind-read this"), usually followed by a few words. Also use when they ask to build, rebuild or refresh their mind-reading model.
metadata:
  author: Jerry Yuze Li (https://github.com/fireheartjerry)
  version: 1.0.0
  license: MIT
---

# Mind Reading

People send a few words when they mean a full brief. This skill writes the brief the user would have written with perfect stamina, as the user at their best, shows it to them, and then does it.

The model lives in `$MIND_READING_HOME/model/` (default `~/.local/share/mind-reading/model/`):
- `light.md`: how to read them; always applies.
- `profile.md`: the full model: traits, quality bar, standing rules, unsaid defaults grouped by kind of request, vocabulary.
- `examples.md`: seed to full-prompt pairs.

Never open `$MIND_READING_HOME/eval/`: it is held out for testing the model.

**No model yet?** If `light.md` is missing, say so in one line, expand the seed from project memory and the conversation alone (tag more as `[G]`), and offer to build the model. To build or refresh it, follow [references/build.md](references/build.md).

## 1. Gather (quick, no subagents)
- Read `light.md` and `examples.md`. In `profile.md`, read "Standing rules" and the "Unsaid defaults" groups that match the seed; read the whole file once per session.
- Read the current project's memory and instructions: CLAUDE.md, AGENTS.md, a MEMORY.md with its linked files if the project has one, and any notes the seed touches.
- Read the live state from the conversation: the active goal, the task list, the last report, what is running, open questions, anything they pasted or attached.

## 2. Decode the seed
- Resolve every referent ("it", "the PR", a first name, "round 2", "the queue") from memory and the conversation.
- Name the kind of request (the profile's groups) and the mode: execute, discuss, status or hold. "discuss", "don't start", "thoughts?" and "what do you think" mean discuss.
- Find the end goal behind it: the mission, the person who will read the result, the deadline.
- List what they would obviously want but left out: the matching unsaid defaults, their standing rules, the follow-through.

## 3. Write the full prompt
Write as the user at their best: first person, direct, clean (no typos, no filler), keeping their priorities and their words where they carry meaning. Size it to the seed: a status ping gets a few sentences; a new initiative gets the full shape. Cover what applies, in this order:
1. The goal, and why (the end mission).
2. Scope: what is in, and what not to touch (other agents' and teammates' work, frozen baselines, held-out data, stakeholders' channels).
3. The quality bar: their numbers, the named rivals, verified on the real path, honest evidence.
4. Deliverables, and where they land.
5. Follow-through: docs, commit or push, cleanup, the next queue.
6. How to report: the result first, in their timezone, verified kept apart from unproven.

Use the nearest pairs in `examples.md` as patterns. Add only what they would want; don't pad with generic best practice.

## 4. Mark what you inferred
Tag each inferred sentence at its end:
- `[S]` strong: a standing rule, a strong trait, or a fact from memory. Applied.
- `[L]` likely: a moderate trait, or clear from context. Applied.
- `[G]` guess: weak evidence or ambiguous. Shown; applied only if cheap and reversible, otherwise held.

What they literally said stays untagged.

## 5. Show it, then act
Reply in this shape:

```
Reading: <one line: what they want and why>

> <the full prompt, with tags>

Held: <only if a guess is held: numbered, each with your recommended default>
```

Then treat the full prompt as their message and do it in the same turn. Don't wait for them to approve the prompt.

Stop and ask first, with one short numbered question and a recommended default, while you carry on with everything else, before:
- anything outward-facing: messages or posts to people, emails, public deploys, merges to main on shared repos, new PRs;
- anything destructive: deleting what isn't regenerable, force-pushes, teardowns they didn't name;
- new spend beyond the stated budget;
- acting on a held guess that would change the scope.

If the mode is discuss, the action is the discussion: opinionated, with numbered options and a recommendation, and nothing changed.

## 6. Learn
If they correct the expansion ("no, I meant ..."), follow the correction, then append one line to `$MIND_READING_HOME/model/corrections.md`: the date, the seed, what was wrong, what they meant. Don't edit the other model files.

## Rules
- Stay light: the model files and the project memory are enough. No subagents, workflows or web research just to expand a seed.
- Never invent project facts. A referent you can't resolve becomes a `[G]` with the assumption stated.
- Keep secrets out of the prompt.
- Read through their typos; never ask what one meant.
- Their newest words beat the model. If the seed conflicts with a standing rule, follow the seed and note the conflict in one line.
- Keep the shown prompt readable in under a minute: about 250 words at most, unless the seed opens a big initiative.

## Example
Seed: `read my mind: keep going` (a standing goal to beat a benchmark is live; the profile says the user hates recaps, wants work committed as it lands, and reports with clock times)

```
Reading: resume the standing benchmark goal from where it stopped.

> Keep going on the standing goal from exactly where you stopped, with no recap and no questions. [S] Take the next unfinished item in the plan file; if the plan is done, take the biggest known gap against the baseline we're chasing. [L] Finish each item with tests and docs, commit it to the working branch, then move to the next. [S] Don't touch the shared GPU box another teammate owns. [S] Report in three lines with clock times: finished, running, next. [S]
```

Then it carries on with the next plan item in the same turn.
