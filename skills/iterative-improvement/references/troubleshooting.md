# Troubleshooting

Short answers for the situations that derail the loop. Each heading names the symptom.

## No user to interview

Phase 1 assumes a responsive human. Running headless (`-p` or `exec` mode), in an
auto-permission mode that forbids questions, or after the user has gone offline, you
cannot skip the phase and you must not pretend it happened. A loop run against criteria
the agent invented, reported as if the user approved them, optimizes a guess and lends
it false authority. Do this instead:

1. **Derive the criteria from real evidence, not invention.** Mine the existing spec,
   design doc, README, prior audit, issue thread, commit messages and earlier
   conversation. Every criterion gets its own `**Source:**` line naming the file,
   section, issue or commit it came from; a criterion with no source is a guess.
2. **Mark the document's status at the top:**
   `**Status:** DERIVED, not user-approved. Sources: <list>.`
3. **Tag every guess** `[ASSUMED: <what you assumed>]`. Weights, thresholds, the
   off-limits list and the budget are guesses too; the Phase 2 defaults (5 rounds, stop
   at 90, no commits, off-limits = Constraints) stand in for the user's answers.
4. **Lead the final report with it.** The first line names the sources, says nobody
   approved the criteria, and lists each assumption by criterion.

One derived criterion, as written in the doc:

```markdown
### C4. Install path reachable from the home page in one click (12 pts) [ASSUMED: weight; the README puts install first, so it gets the second-highest weight]
**Source:** README.md "Installation" (first section after the title); issue #41 "can't find the install docs"
**How to check it:** From `/`, count the clicks to a page showing the install command. Quote the link text used.
```

The report's first line, as the user reads it: `Criteria were DERIVED from README.md,
docs/, issues #41 and #57 and the last 30 commits; nobody approved them. Assumed: every
weight; C3's 45-word paragraph limit; C6 (dark mode parity) entirely, from one commit
message; the budget (5 rounds, stop at 90); nothing marked off limits.`

A derived criteria doc is still far better than none: it makes the target inspectable,
so the user can correct it in one pass instead of relitigating every round. It must
never masquerade as an approved one.

## Noise: what n and T mean

`n` is the gap between two scorer runs on the identical baseline snapshot and evidence;
it measures the instrument, not the work. `T = max(3, 2n)` is the smallest movement the
loop treats as real. Two identical runs are not proof of a stable instrument, and two
runs more than 8 apart may be one aberrant run; in both cases `score_math.py baseline A B`
prints `third-run=needed`. Run the scorer a third time on the same snapshot and evidence
and pass all three (`baseline A B C`). The script keeps the two runs that agree within 8
and names the outlier; log the outlier in the score log with its per-criterion diff
against the kept pair, and tighten the criteria where it disagreed at the next
amendment. If no two runs agree within 8 the script prints `soft=true`: the check lines
do not discriminate. Find the criteria with the largest per-criterion disagreement (the
tables make this a subtraction), rewrite their "How to check it" into a procedure with a
count, a measurement or a quoted line, and take two fresh baseline runs. After any
rubric change in full mode, rescore the previous snapshot twice and recompute `n` and
`T` with `score_math.py baseline` from that pair, adding a third run when it says
`third-run=needed`; the rubric is a new instrument (lite: once, `T` stays 3). Never
compare numbers from different scorer pins; a changed engine, model or CLI version is a
new instrument, and the previous snapshot must be rescored under it first (`.cmd` files
make the pin diff mechanical: they must be byte-identical).

## Gate stuck at FAIL

An ACHIEVE gate still failing when the budget ends: halt, report it as the first line,
and ask. `score_math.py decide --gates FAIL --rounds-used N` returns `HALT
gate-fail-rounds-exhausted`; never add rounds on your own to get it green. A PRESERVE
gate that flips to FAIL after a change: revert that round from the previous snapshot
(`snapshot.py restore --dry-run`, then `restore`) or fix it before scoring; a round
that raises three criteria and breaks a gate is a net loss. A gate whose check cannot
run anywhere in this environment is not a gate: move it to a criterion with evidence
the orchestrator produces, or drop it with the user's agreement and record the decision
in Amendments. A scorer reporting a gate UNKNOWN means you did not supply its evidence
file; run the check line and rescore.

## Same criterion N/A twice

The decision rule returns `BLOCKED na-repeated`. Run the exact command in that
criterion's "How to check it" line yourself, once, save the output under
`evidence/round-N/` and write `manifest.md` (check, command, timestamp, attempts,
reason for any retake). Pass the directory with `spawn_child.sh --evidence` on the
next run. If the check line is prose that cannot be run as a command, that is the
defect: amend the criterion so its check line names a command, bump the rubric
version and rescore (`references/full-protocol.md` section 5). Do not read a `/80` number against a `/100` one, and do not
declare target hit while any criterion is N/A.

## Budget in money or time

The user said "about 90 minutes" or "around 15 dollars" and you need a round ceiling.
Measure, do not guess: every child run's `.log` ends with `# started`, `# finished`,
`# duration: Ns` and `# usage:` (codex's token count, or `not reported`: claude and kimi
print none). Spent time is the sum of those durations plus your own work between them.
Money is not measured by either supported engine (codex reports tokens, not dollars), so
agree a time or round equivalent with the user and call any dollar figure "estimated".
After each round convert with `score_math.py budget --total <budget> --spent <measured
so far> --per-round <measured cost of the last round> --rounds <round ceiling>
--rounds-used <rounds run>`. Before round 1, `--total 90 --spent 35 --per-round 12
--rounds 5` prints `remaining=55 rounds-used=0 rounds-affordable=4 max-rounds=4`; after
round 3, `--total 90 --spent 66 --per-round 20 --rounds 5 --rounds-used 3` prints
`max-rounds=4` (3 run plus 1 affordable). That `max-rounds` is a total, the same scale
as `decide --rounds-used`, so pass it to `decide --max-rounds` unchanged. Never start a round the
remaining budget cannot finish: `rounds-affordable=0` means stop now and report, even
with rounds left on the ceiling. Before round 1 has run there is no measured per-round
cost; use the round 0 scorer durations as the estimate and say "estimated" until round 1
replaces it. The report quotes measured numbers
only, taken from the footers; never an invented duration or spend.

## Decision rule questions

- **Rounds used looks wrong.** After an amendment the series restarts from the
  rescored reference, so `len(series) - 1` undercounts. Pass `--rounds-used N` with the
  real round number every time; the detail line prints `N of R rounds used` so you can
  check it.
- **`STOP regression` did not fire on two small drops.** Correct: a single drop inside
  `T` is noise, up or down. Regression needs one drop of `T` or two drops summing to
  `T`. Two small moves in either direction trip the plateau rule instead.
- **`STOP plateau` fired after two decent rounds.** Plateau means two consecutive
  deltas each inside `T` whose total does not exceed `T`. If `+5, +3` against `T=8`
  stops you, check `n`: a noise floor of 4 means a move of 5 or of 3 is
  indistinguishable from nothing, and the two together have not shown more than one
  `T`. Budget left is not a counter-argument; the report tells the user what is left and
  they can ask for more.
- **`CONTINUE slow real trend` after two moves inside `T`.** Two sub-T deltas that add
  up past `T` (`+6, +6` against `T=8`) are a slow real trend, not a plateau; the script
  says `CONTINUE` and names the trend in its detail line. Keep going.

## Failure modes

- **The agent wrote the criteria alone.** The most common failure. The score then
  measures agreement with the agent's opening guess. Interview, or mark DERIVED.
- **Score climbs while the work visibly does not improve.** The scorer is inflating.
  The "How to check it" lines are too soft to discriminate; tighten them, bump the
  rubric version, rescore the previous snapshot.
- **The reviewer repeats last round's findings verbatim.** Either nothing changed, or
  the reviewer saw a stale snapshot. Check `snapshots/round-N` holds the new state.
- **Churn.** The same area rewritten every round with no score movement above T. Stop
  touching it; the problem is elsewhere, or the criterion is unachievable as stated.
- **Scope creep via the reviewer.** Hostile reviewers love proposing rewrites.
  Constraints outrank any finding: `REJECT` with the constraint named.
- **Death by round count.** Five rounds is a ceiling. If round 3 hits the target or
  round 4 plateaus, stop and say so.
- **The child changed the staged copy (exit 4).** `spawn_child.sh` discards the output
  and logs the incident. Claude and Codex children cannot write to the stage, so their
  exit 4 almost always means ROOT changed during the run: find out who edited it (you, a
  watcher, a formatter), then rerun once; a second discard means switch engine. A kimi
  exit 4 means kimi wrote: replace it.
- **"no files to snapshot" (snapshot.py, exit 2) or "snapshot manifest lists no files"
  (spawn_child.sh, exit 3).** ROOT sits under a gitignored path of an enclosing repo,
  every file is ignored or secret-shaped, or `--path` is wrong. Check with `git -C ROOT ls-files --cached --others
  --exclude-standard`, move the artifact or pass the right `--path`, and snapshot again.
- **The child hangs.** Both `codex exec` and `claude -p` wait on an open stdin when
  launched from an agent's shell tool; the script redirects stdin from `/dev/null` and
  applies a 900 second default timeout. A hang despite that is an auth prompt: run the
  preflight.
- **Exit 6 (no output) from Claude.** Usually the output path is not allowlisted. The
  script uses `Edit(//<stage>/out.md)`; if you typed the flags yourself, the rule
  needs the double slash and the `Edit(` form, not `Write(`.
- **The reviewer keeps re-raising rejected findings.** First check the PRIOR FINDINGS
  slot in `prompts/reviewer-round-N.md` was actually filled. If it was, the rejection
  reason is probably not convincing: write a better one, or reconsider the finding.
- **The scorer's Notes keep naming the same defect.** Nobody triaged it because no
  reviewer raised it. Notes are findings; give them a row.
- **Budget overrun.** A round was started that the time or money budget could not
  finish. Before every round run `score_math.py budget --rounds-used <rounds run>` and
  pass its `max-rounds` to `decide`.

## Lite mode escalation

Escalate to full when lite triage verifies 3 or more DO NOW or DO rows that need a file
outside the one file or component lite was scoped to, or the user asks for more rounds, a
target or a budget. The reviewer's severity labels never escalate by themselves: the
brief tells the reviewer to be hostile, so an ordinary README routinely draws several
`major` findings that lite fixes in place (three of three live runs did before this rule
changed). Read `references/full-protocol.md` first; the lite
card does not contain the full rules. Move the criteria doc to its full-mode
location, add criteria up to 6 to 12 and rebalance to 100, run the two-run baseline on
the current state (this becomes the new row 0 with `n` and `T`), classify gates, and
continue under the full rules. Say in the report that the loop started lite and why it
escalated.
