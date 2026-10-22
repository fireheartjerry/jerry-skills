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
   off-limits list and the budget are guesses too; the mode's own defaults stand in for
   the user's answers (lite: 2 rounds, `T = 3`; full: 5 rounds, stop at 90; both: no
   commits, off-limits = Constraints). Unattended, the child runs stay within the
   mode's count (lite 2 preflights plus at most 7 runs, full at most 18) and use only
   engines already configured on the machine; the report states both.
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
message; the budget (the mode's default rounds and target); nothing marked off limits.`

A derived criteria doc is still far better than none: it makes the target inspectable,
so the user can correct it in one pass instead of relitigating every round. It must
never masquerade as an approved one.

## Noise: what n and T mean

`n` is the gap between two scorer runs on the identical baseline snapshot and evidence;
it measures the instrument, not the work. `T = max(3, 2n)` is the smallest movement the
loop treats as real. `score_math.py baseline` says when a third run is needed
(`third-run=needed`: two identical runs say nothing about spread, two runs more than 8
apart may hold one aberrant run), names an outlier, or prints `soft=true` when the
check lines do not discriminate; its detail line says what to do, and
`references/full-protocol.md` round 0 step 4 caps the rebaseline at one. To find soft
criteria, subtract the per-criterion tables of the disagreeing runs and rewrite the
largest gaps' "How to check it" into a count, a measurement or a quoted line. After a
rubric change in full mode, rescore the previous snapshot twice and take `n` and `T`
from that pair (lite: once, `T` stays 3); the rubric is a new instrument. So is a
changed engine, model or CLI major.minor version: rescore under it before comparing
(`loop.py status` lists every scorer `.cmd` that differs from the pin).

In lite, `T = 3` is a floor, not a measurement: one scorer run cannot show the spread.
Say so in the report, and before acting on a lite `STOP regression`, score the same
snapshot once more; if the second run puts the drop inside 3, it was noise.

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
`evidence/round-N/` with `evidence.py run`, which writes the `manifest.md` row. Pass
the directory with `spawn_child.sh --evidence` on the next run. If the check line is
prose that cannot be run as a command, that is the defect: amend the criterion so its
check line names a command, bump the rubric version and rescore
(`references/full-protocol.md` section 5). Do not read a `/80` number against a `/100`
one, and do not declare target hit while any criterion is N/A.

If the evidence was supplied and the child still marked the criterion N/A, the engine
cannot read that format, usually an image. Producing the evidence again changes
nothing. Run `spawn_child.sh --preflight ENGINE --vision`; on failure convert the
capture to text (axe JSON, a DOM text dump, a printed measurement) or pin another
engine, which is a new instrument: rescore the previous snapshot under it.

## Budget in money or time

The procedure is `references/full-protocol.md` section 6; the usual questions:

- **What counts as spent?** Now minus the `Kickoff (UTC)` line `loop.py init` wrote in
  `rounds.md`: child runs and your own work between them. Money is not measured for
  codex children (they report tokens, not dollars) or kimi: agree a time or round
  equivalent with the user and call any dollar figure "estimated". Claude children run
  with `--max-usd N` are capped per run and report their cost: sum the `cost USD`
  footers (`grep -h '^# usage: cost USD' LOOP/*/*.log`).
- **What do I pass mid-loop?** After round 3 of a 90-minute budget with 66 spent and
  20 per round: `budget --total 90 --spent 66 --per-round 20 --rounds 5 --rounds-used 3`
  prints `max-rounds=4` (3 run plus 1 affordable). That is a total on the same scale as
  `decide --rounds-used`, so pass it to `decide --max-rounds` unchanged.
- **The numbers say no round fits.** `rounds-affordable=0` means stop now and report,
  even with rounds left on the ceiling; `decide --max-rounds 0` prints the same stop.

## Decision rule questions

- **Rounds used looks wrong.** After an amendment the series restarts from the
  rescored reference, so `len(series) - 1` undercounts. Pass `--rounds-used N` with the
  real round number every time; the detail line prints `N of R rounds used` so you can
  check it.
- **`STOP regression` did not fire on two small drops.** Correct: a single drop inside
  `T` is noise, up or down. Regression needs one drop of `T` or two drops summing to
  `T`. Two small moves in either direction trip the plateau rule instead.
- **`STOP plateau` fired after two decent rounds.** Plateau means two consecutive
  deltas each inside `T` whose total is at most `T`. If `+5, +3` against `T=8`
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
- **Score climbs while the work visibly does not improve.** The scorer is inflating,
  or the loop is fitting its own rubric. The blind final judge
  (`references/judge-brief.md`) is the instrument: a baseline win says so. Tighten
  the "How to check it" lines, bump the rubric version, rescore the previous snapshot.
- **The reviewer repeats last round's findings verbatim.** Either nothing changed, or
  the reviewer saw a stale snapshot. Check `snapshots/round-N` holds the new state.
- **Churn.** The same area rewritten every round with no score movement above T. Stop
  touching it; the problem is elsewhere, or the criterion is unachievable as stated.
- **Scope creep via the reviewer.** Hostile reviewers love proposing rewrites.
  Constraints outrank any finding: `REJECT` with the constraint named.
- **Death by round count.** Five rounds is a ceiling. If round 3 hits the target or
  round 4 plateaus, stop and say so.
- **Exit 4, output discarded.** The message names the cause. "Child modified the
  staged artifact": rerun once, then switch engine; a kimi discard means kimi wrote.
  "The artifact's files in ROOT changed": the paths are listed, and the child cannot
  write there, so you, a teammate, a watcher or a formatter edited the snapshot's
  files. An edit elsewhere in the repo never discards. Let the edits settle,
  re-snapshot if the artifact itself changed, and rerun; switching engine cannot help.
  In a busy shared tree, run the loop in its own git worktree.
- **Exit 7, "timed out".** The child ran past `--timeout` (default 900 s) and its
  whole process group was killed, so nothing keeps billing. Raise `--timeout` once (for
  example 1800), or narrow the snapshot with `--path`, then switch engine. Exit 7
  without "timed out" is the CLI's own error: read the `.log`.
- **"no files to snapshot" (snapshot.py, exit 2) or "snapshot manifest lists no files"
  (spawn_child.sh, exit 3).** ROOT sits under a gitignored path of an enclosing repo,
  every file is ignored or secret-shaped, or `--path` is wrong. `snapshot.py create`
  prints every file it skipped and why; check with `git -C ROOT ls-files --cached
  --others --exclude-standard`, pass the right `--path` (or `--include` for a file a
  name rule dropped wrongly), and snapshot again.
- **A file the artifact needs is missing from the snapshot.** A credential-shaped name
  (`.npmrc`, `secrets.yaml`, `id_rsa`) or a top-level `*-criteria.md` was skipped, and
  the stderr line says which rule fired. If it is not a secret, keep it with
  `--include <glob>`; if it is, the reviewer gets a note in the brief instead.
- **"refusing a whole-root snapshot".** Over 500 files or 50 MB with no `--path`. Pass
  `--path` for the artifact and the files it is checked against; `--whole-root` only
  when the artifact really is the whole tree (then see "Large artifacts").
- **`strip failed` (strip_criteria.py, exit 4).** Either loop residue reached the
  scorer-visible text (a `[PRESERVE]` tag, a `**Target:**` label, an unterminated
  `<!-- loop-only -->` block: fix the source doc), or a criterion uses a loop word in
  its ordinary sense ("plateau", "round 2", "noise floor"): reword it, or pass
  `--allow-term "<term>"`. Never hand-edit `criteria-for-scorer.md`; it is regenerated every
  round.
- **The child hangs.** The script gives every child `/dev/null` as stdin and kills its
  process group at `--timeout`. A hang that ends in a timeout on the first run is an
  auth prompt: run `spawn_child.sh --check`, then the preflight.
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

Escalate to full when lite triage verifies 3 or more DO NOW or DO rows that need a
file outside the one file or component lite was scoped to, or the user asks for more
rounds, a target or a budget. The reviewer's severity labels never escalate by
themselves: the brief tells the reviewer to be hostile, so an ordinary README routinely
draws several `major` findings that lite fixes in place. Read
`references/full-protocol.md` first; the lite card does not contain the full rules.
Move the criteria doc to its full-mode location, add criteria up to 6 to 12 and
rebalance to 100, run the two-run baseline on the current state (this becomes the new
row 0 with `n` and `T`), classify gates, and continue under the full rules. Escalation
grows the plan, so restate the engines and child runs to the user first. Say in the
report that the loop started lite and why it escalated.

## Large artifacts

A whole codebase, a site with many pages, a long doc set. One reviewer on everything
reads a sample and reports it as the whole, or times out (exit 7). Slice instead: one
`--path` scope per slice (a package, a page, a chapter), one reviewer per slice per
round, each told which subtree to read first, each listing "Files read". The scorer
still grades the whole snapshot, so its check lines must name commands that cover it
(a count, a grep, a test run), not "read every file". A reviewer that times out on a
slice means the slice is still too big.

## Scripts cannot run

Bash denied, repo writes denied, no python3: the loop cannot stage, pin or measure.
Say so on the report's first line, drop to the inline tier explicitly, and claim none
of isolation, a scorer pin, `n` or `T`. You can still propose criteria, review against
them by hand, triage and fix; mark every score "not instrument-measured" and list the
steps that did not run.
