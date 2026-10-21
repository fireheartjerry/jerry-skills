# Evals

What this skill claims, how each claim was measured, and how to reproduce it. Nothing here
needs network access or tokens to be checked for shape; running the scenarios live is a
manual procedure, described below.

## Files

- `triggers.json`: messages that should and should not invoke the skill. Each
  should-trigger entry names its artifact domain; each should-not entry names the
  exclusion in the description that rules it out (`debugging`, `definite-done`, `oracle`,
  `out-of-domain`).
- `scenarios.json`: behavioural scenarios. S1 to S12 are the phase-1 set, P1 to P13 probe
  the phase-2 changes. Each has a situation, the user message, the `mode` the agent should
  start in (`full`, `lite`, or `none` when the skill must not be used), ideal behaviour and
  failure modes.
- `rubric.md`: how a blind judge grades a transcript and how trigger runs are scored.
- `results.md`: what was run, by what method, with what n, the scores, and the limits.

## Offline checks

`tests/run.sh` runs every test in `tests/`, including `test_evals.py`, which checks that
the JSON files parse, meet their minimum counts, contain no duplicate messages, tie every
exclusion and domain to wording in the SKILL.md description, and that `results.md` lists
every scenario id and keeps its Limits section. These checks prove the files are
well-formed and consistent with the description; they prove nothing about behaviour.

## Live trigger check

1. Install the skill where your agent discovers skills (for Claude Code, a
   `.claude/skills/iterative-improvement` folder or symlink in a scratch project).
2. Send each `message` from `triggers.json` as the first message of a fresh conversation,
   in a project that contains a plausible artifact for it.
3. Record whether the skill was invoked before the agent made any edit.
4. Score with `rubric.md`: precision is triggered should-trigger messages divided by all
   triggered messages; recall is triggered should-trigger messages divided by all
   should-trigger messages. List every miss by message.

## Live behavioural A/B

Arms are skill versions: `original` (commit `414c914`), `phase-1` (commit `64b1e16`) and
`phase-2` (the current files).

1. For each scenario and each arm, start one fresh agent session with only that arm's
   skill installed. Recreate the described repo state, then send the `user_message`
   (scenarios that begin mid-loop give the agent the described loop state instead).
2. Save the full transcript under a neutral name that does not reveal the arm.
3. Give a judge the scenario, the transcript and that arm's skill files, never the arm
   label, and grade with `rubric.md`: a scenario passes only when no listed failure mode
   occurred, and gets a 0 to 10 score.
4. Compare arms per scenario (win, tie, loss) and report the means, with n.

A headless run (`claude -p`, `codex exec`) stops at the first message that needs the user,
so for most full-mode scenarios it can only test routing, mode choice and the proposal.
Grade only the ideal items the run could reach, as `rubric.md` describes.

## Limits

See the Limits section of `results.md`. In short: one judge, synthetic scenarios written
by the skill's authors, and small n. Treat the scores as ordinal evidence, not
measurements.
