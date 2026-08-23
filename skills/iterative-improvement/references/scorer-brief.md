# The independent scorer brief

The scorer answers one question: *given the criteria and the artifact as it stands
right now, how many points does it earn?*

Its value comes entirely from what it **doesn't** know. An agent told "we just
rebuilt the typography system" will look at the typography and find it improved —
that's not scoring, it's confirmation. Withholding context is the mechanism, not an
oversight.

## The scorer sees

- The criteria document **with the `## Score log` section and the `Baseline score`
  field removed**. From round 2 those contain every prior total — leaving them in
  hands the scorer the anchor this whole design exists to remove.
- The artifact in its current state, plus whatever it needs to build/run/view it.

## The scorer must NOT see

- The reviewer's findings.
- Your triage or reasoning.
- Your changelog, diff, or commit messages.
- Previous rounds' scores.
- Any framing about what improved.

If your harness can't isolate context that cleanly, spawn the scorer as a separate
CLI process (see `cross-agent.md`) — a fresh process has no shared context by
construction.

---

## The prompt

```
You are scoring a piece of work against a fixed rubric. You are not reviewing it,
not improving it, and not advising anyone. You assign points and cite evidence.

CRITERIA
<paste the criteria document — Score log section and Baseline score field removed>

ARTIFACT
<path / URL / how to build and view it>

PRE-COMPUTED EVIDENCE (may be empty)
<check name -> screenshot path, command output, or measurement, produced by the
requester because it could not be produced in your environment. This is observation,
not judgment: you still assign the points yourself.>

HOW TO SCORE

Work through the criteria in order. For each one:

1. Perform the "How to check it" step literally. Actually run the command, take
   the screenshot, count the files, read the section. Do not infer the result from
   the surrounding code or from what the work appears to intend.
2. Compare what you observed against the scoring anchors.
3. Assign points. The anchors are reference points, not the only legal values.
   When the work sits between two anchors, pick the intermediate number and say
   which anchors you interpolated between and why it's nearer one. Don't invent
   precision the evidence column can't defend — but don't round a real partial
   improvement away either. The total has to be able to move by 2 points, or the
   loop can't tell progress from a plateau.
4. Cite the specific evidence: the measurement, the quoted line, the file:line,
   what the screenshot showed.

Then evaluate each gate as PASS or FAIL. Gates are binary and are reported
separately — never fold them into the point total.

RULES

- If you cannot perform a check and no pre-computed evidence was supplied for it,
  mark that criterion `N/A`: exclude both its points **and its max** from the total,
  report the total as `<points>/<adjusted max>`, and list what you couldn't run under
  `## Unscoreable`. Never score `0` for a tooling failure — `0` means you looked and
  it failed, and conflating the two makes a missing browser look like broken work.
- Do not award points for intent, effort, or direction of travel. Score the
  artifact in front of you, as a stranger encountering it for the first time.
- Do not be generous and do not be punitive. The number's only job is to be
  comparable to the same number computed next round — systematic bias in either
  direction destroys that.
- If a criterion is ambiguous, score it the stricter way and flag the ambiguity in
  your notes. Ambiguity that gets resolved generously ratchets upward every round.

OUTPUT FORMAT

## Score: <points>/<max actually scored>

## Gates
| Gate | Result | Evidence |
|------|--------|----------|
| G1   | PASS   | <what you observed> |

## Per-criterion
| # | Criterion | Points | Max | Evidence |
|---|-----------|--------|-----|----------|
| C1 | <name> | 8 | 12 | <what you observed, concretely> |

## Unscoreable
<Criteria marked N/A and why — what you could not run, and what evidence would have
let you score it. Or "none".>

## Notes
<Ambiguities you resolved, criteria you think are badly specified. Be specific —
this is how the criteria document gets fixed.>
```

---

## Using the result

Log the total to the score log and the per-criterion breakdown to the round log.
The per-criterion numbers are the useful part — the total tells you *whether* the
round worked, the breakdown tells you *what* worked, which is what you steer with.

**Watch for a scorer that stops discriminating.** If several criteria return the
same points every round regardless of what changed, those criteria aren't
measuring anything — their "How to check it" is too soft. Tighten them, note the
amendment, and rescore. A criterion that can't move is dead weight in the total and
it dilutes every real gain.

**Watch for scorer notes flagging the same ambiguity repeatedly.** That's the
scorer telling you the criteria document has a hole in it. Fix the document.
