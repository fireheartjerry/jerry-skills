# Example loop: the tidylog landing page

One complete full-mode loop on a fictional artifact, so every filled artifact appears
once. `tidylog` is an invented open-source CLI; the page is `site/index.html` plus
`site/styles.css` in a git repo. The user asked for "up to 5 rounds, stop at 85".
Reviewer: Codex. Scorer: Codex, pinned to one model. `loop.py init --root . --slug
landing-page --mode full --scope site --scope README.md` made LOOP inside the repo's git
dir, so nothing below shows in `git status`. Every number below is checked:
`tests/test_lint.py` checks that points add up to totals, `tests/test_scripts.py` that
the totals produce the decisions shown. The final criteria doc with every round logged
is `tests/fixtures/criteria-full.md`; the round-1 scorer output is
`tests/fixtures/scorer-output.md`.

## 1. The criteria doc after sign-off (rubric v1)

```markdown
# Criteria: tidylog landing page

**Artifact:** `site/index.html` and `site/styles.css`, served by `npm run dev` at http://localhost:4321
**Status:** APPROVED by user on 2026-10-20
**Rubric version:** v1

<!-- loop-only -->
**Mode:** full
**Target:** 85/100   **Budget:** 5 rounds
**Baseline score:** 43   **Noise floor:** n=4   **Move threshold:** T=8
**Baseline SHA:** a1b2c3d
**Scorer pin:** codex, model gpt-5, flags as recorded in LOOP/scores/round-0a.cmd
**Loop dir:** .git/iterative-improvement/landing-page/
<!-- /loop-only -->

## Context

tidylog is an open-source command-line tool that folds noisy log files into a short,
grouped summary. The landing page is the first thing a developer sees after a link from
a README or a package registry. Success means a visitor understands what the tool does
within a few seconds, sees proof that it works on real output, and can copy the install
command without hunting for it.

## Gates (binary, reported separately from the score)

A failed gate means the work is not deliverable regardless of points. Gate checks are
run by the orchestrator before scoring; their output is supplied as evidence.
<!-- loop-only -->
Class is set at round 0: PRESERVE passes at baseline and must stay passing; ACHIEVE
fails at baseline and must pass by the end.
<!-- /loop-only -->

- **G1.** [PRESERVE] `npm run build` exits 0 | *check:* run `npm run build` in the repo root and read the exit code
- **G2.** [ACHIEVE] No horizontal overflow at 390px wide | *check:* with the dev server running, `node tools/check-overflow.mjs http://localhost:4321 390` prints the document scrollWidth; pass when it is 390 or less
- **G3.** [PRESERVE] `site/tokens.css` unchanged | *check:* `git diff --quiet a1b2c3d -- site/tokens.css` exits 0

## Criteria (100 points)

### C1. Hero states what the tool does in one sentence (14 pts)
**What it means:** The first sentence a visitor reads says what tidylog does to what input, in plain words.
**How to check it:** Read the first heading and the first paragraph in `site/index.html`. Quote them. Count the words in the first sentence.
**Anchors:**
- `0`: the heading is a slogan ("Logs, tamed") and no sentence above the fold names input or output
- `7`: one sentence names the tool and the output but takes more than 25 words or two sentences
- `14`: one sentence of at most 25 words names the input (log files), the action (groups) and the output (a summary)

### C2. Install path visible without scrolling at 1440px (12 pts)
**What it means:** A laptop visitor sees the install command and a copy affordance without scrolling.
**How to check it:** `node tools/shot.mjs http://localhost:4321 1440 900 c2-1440.png`. Note whether an install command is fully inside the capture and whether a copy button or selectable code block accompanies it.
**Anchors:**
- `0`: none in the first screen
- `6`: present but cut off or no copy affordance
- `12`: fully visible, code block, working copy button

### C3. Typographic hierarchy (blur test) (12 pts)
**How to check it:** `node tools/shot.mjs http://localhost:4321 1440 900 c3-shot.png && convert c3-shot.png -blur 0x8 c3-blur.png`. In `c3-blur.png` the primary message should still be the first thing the eye lands on, by size or weight alone.
**Anchors:**
- `0`: nothing dominates
- `6`: one element dominates but a second competes
- `12`: one focal point, deliberate second and third tier

### C4. Mobile scroll integrity at 390x844 (10 pts)
**How to check it:** With the dev server running, `node tools/frames.mjs http://localhost:4321 390 844 300` writes a screenshot every 300px to `frames/` and prints the document scrollWidth and the indices of any identical consecutive frames.
**Anchors:**
- `0`: overflow or more than 2 dead screens
- `5`: no overflow, one dead stretch
- `10`: no overflow, no dead stretch

### C5. Proof: a real terminal example or output (12 pts)
**How to check it:** Find any `<pre>` or `<code>` block over three lines in `site/index.html`. Quote it. Check it shows both a command and its output.
**Anchors:**
- `0`: none, or install command only
- `6`: output without the command, or obviously invented output
- `12`: command plus five or more plausible lines consistent with the README

### C6. Copy density (14 pts)
**How to check it:** Count words per paragraph. List every paragraph over 45 words. Search for three or more adjectives in a row.
**Anchors:**
- `0`: two long paragraphs or an adjective list in the hero
- `7`: one long paragraph
- `14`: no long paragraph and no adjective list

### C7. Performance (10 pts)
**How to check it:** `npm run build`, `npx serve dist`, then `npx lighthouse http://localhost:3000 --only-categories=performance --output=json --quiet`; read `categories.performance.score`.
**Anchors:**
- `0`: below 0.70
- `5`: 0.70 to 0.89
- `10`: 0.90 or above

### C8. Accessibility basics (16 pts)
**How to check it:** `npx @axe-core/cli http://localhost:4321 --exit`; count issues by impact.
**Anchors:**
- `0`: any critical
- `8`: no critical, one or two serious
- `16`: zero serious or critical

## Out of scope
Dark mode, the blog, the docs pages, the GitHub README.

## Constraints
- `site/tokens.css` must not change.  - No new dependencies.  - English copy only.
- No testimonials, logos or user counts may be invented; the project has none yet.
```

The user approved it after one correction (C5 originally said "a screenshot of the
tool"; they wanted real terminal text). Budget answers: 5 rounds as a ceiling, stop at
85, no commits by the loop (the criteria doc stays uncommitted), `site/tokens.css` off
limits. With the questions came the plan: Codex for both roles, the 9 files under
`site/` plus `README.md` sent to OpenAI on every child run, about 15 child runs. C2, C3,
C4, C7 and C8 need a browser or Lighthouse, which the scorer will not have, so each
check line is an exact command the orchestrator runs once per round and hands over as
evidence. `tools/shot.mjs`, `tools/frames.mjs` and `tools/check-overflow.mjs` are small
Playwright scripts in the fictional tidylog repo, described here and
not shipped with this skill.

## 2. Round 0: baseline and noise

`snapshot.py create --root . --path site --path README.md --out LOOP/snapshots/round-0`
copied 10 files. `strip_criteria.py` produced `criteria-for-scorer.md`. The
orchestrator ran the three gate check lines and the C2, C3, C4, C7 and C8 check lines
once each with `evidence.py run`, which wrote `evidence/round-0/manifest.md`. Two
scorer runs on the same snapshot and evidence (`spawn_child.sh codex scorer ... --model
gpt-5 --evidence LOOP/evidence/round-0`):

| # | Max | Run 0a | Run 0b | Note |
|---|-----|--------|--------|------|
| C1 | 14 | 4 | 4 | h1 "Logs, tamed."; first sentence naming the input is in paragraph 3 |
| C2 | 12 | 6 | 6 | install block at y=1140, below the 1440x900 capture (evidence supplied) |
| C3 | 12 | 4 | 8 | largest disagreement: the blurred capture judged differently (evidence supplied) |
| C4 | 10 | 0 | 0 | scrollWidth 428 at 390 (evidence supplied) |
| C5 | 12 | 3 | 3 | 4-line `<pre>` of output, no command |
| C6 | 14 | 10 | 10 | one 52-word paragraph |
| C7 | 10 | 5 | 5 | lighthouse 0.81 (evidence supplied) |
| C8 | 16 | 9 | 9 | axe: 0 critical, 2 serious (evidence supplied) |
| **Total** | 100 | **41** | **45** | |

Gates from the orchestrator's runs, confirmed by both scorer runs: G1 PASS, G2 FAIL,
G3 PASS. Classes written into the doc: G1 PRESERVE, G2 ACHIEVE, G3 PRESERVE.

`score_math.py baseline 41 45` prints `baseline=43 noise=4 threshold=8 soft=false third-run=no`.
Both `.log` footers put a scorer run at about six minutes; the user set no time or money
budget, so the round ceiling of 5 stayed the only one. Score log rows `0a | v1 | 41`,
`0b | v1 | 45`, `0 | v1 | 43 | ... | baseline = mean, n=4, T=8`.
C3 is noted as the criterion to tighten if noise recurs. `scores/round-0a.cmd` is the
pin; `round-0b.cmd` is byte-identical to it.

## 3. Round 1: blind review, triage, score, amendment

Reviewer brief: round 1 template with REVIEW TARGET pointing at the staged artifact,
the entry file and the captures in its evidence view (`evidence.py view` of round 0:
no manifest, no check ids), CONSTRAINTS and OUT OF SCOPE pasted. `spawn_child.sh codex
reviewer LOOP/prompts/reviewer-round-1.md LOOP/snapshots/round-0
LOOP/reviews/round-1.md --evidence LOOP/evidence/view-round-0` returned 9 findings.
Excerpt of the output:

```
## Verdict
A page that describes a mood instead of a tool. Nothing above the fold tells me
what tidylog takes in or gives back, and the one code block is output with no command.

### F1. Hero headline is a slogan  [severity: critical]  [basis: fact]
**Where:** site/index.html:18, `<h1>Logs, tamed.</h1>`
**Consequence:** a developer arriving from a registry link cannot tell what the tool does.
### F2. Install command below the fold at 1440px  [major]  [fact]
### F3. Testimonial section is placeholder text  [major]  [fact]
**Where:** index.html:88-104, three cards reading "Quote goes here"
**Cheapest real fix:** replace with two real quotes from users.
### F4. 52-word paragraph in the hero  [minor]  [fact]  (index.html:36)
### F5. Hero illustration has no alt text  [major]  [fact]  (index.html:22)
### F6. Footer links fail contrast 3.9:1  [major]  [fact]  (styles.css:212)
### F7. Feature grid wraps to one column at 390px and overflows  [critical]  [fact]  (styles.css:140, fixed 420px min-width)
### F8. No working example anywhere  [critical]  [taste]  (whole page; the `<pre>` at :71 shows output with no command)
### F9. Font loads from a third-party CDN  [minor]  [fact]  (index.html:9)
```

Triage table written to `rounds.md`:

| F# | Sev | Bucket | Verified | Criterion | Action or reason |
|----|-----|--------|----------|-----------|------------------|
| F1 | critical | DO NOW | yes: h1 text at index.html:18 | C1 | rewrite h1 to one 11-word sentence naming input, action and output |
| F2 | major | DO NOW | yes: install block y=1140 in 1440 capture | C2 | move install block into the hero, add copy button |
| F3 | major | DO NOW | yes: cards are placeholders at :88-104 | C6 | remove the section; the reviewer's fix (real quotes) is rejected: Constraints forbid invented testimonials |
| F4 | minor | DO NOW | yes: 52 words at :36 | C6 | cut to one paragraph of 28 words |
| F5 | major | DO NOW | yes: `<img>` at :22 has no alt | C8 | add alt text |
| F6 | major | DEFER | yes: measured 3.9:1 | C8 | the footer color sits beside the off-limits tokens and may be a brand color: asked the user (one line); change it in round 2 if it is not |
| F7 | critical | DO NOW | yes: min-width 420px at styles.css:140 | G2, C4 | min-width 0, grid auto-fit (cheaper than the reviewer's proposed rewrite of the grid) |
| F8 | critical | DEFER | yes: the `<pre>` at :71 is output with no command | C5 | a real command-plus-output example belongs in the hero, which F1 and F2 already move this round; do it in round 2 once the hero settles |
| F9 | minor | DEFER | yes: Google Fonts link at :9 | C7 | low weight; revisit if C7 under 8 after round 2 |

F3's proposed fix conflicts with a constraint and F6 might, and those are the two
places the table says so explicitly. Six changes made (F1, F2, F3 removal, F4, F5, F7),
one changelog line each; F5's sweep found no other `<img>` without alt. PRESERVE gate
checks re-run after each: G1 PASS, G3 PASS. The reviewer's "What is working" (the
install command is copy-pasteable, the page has no tracking scripts) went into the KEEP
list. G2 (ACHIEVE) still fails: the grid no
longer overflows but the `<pre>` at :71 is 412px wide at a 390 viewport. Snapshot
`round-1` created, criteria re-stripped, the gate checks and the C2, C3 and C8 check
lines run into `evidence/round-1/`, pinned scorer run. The orchestrator did **not** re-run the C4 and C7 check lines for
this round (evidence is regenerated every round; this was a lapse, kept here because
it shows how the N/A path reads), and F8's gap showed only at the scoring step: the
scorer's C5 row credits a block anywhere on the page, so the example above the fold
that F8 asks for has no criterion. Found after scoring, the amendment below follows the
"after round N was scored" rule. The scorer
marked both N/A:

```
## Score: 49/80
## Scaled: 61
## Unscoreable
- C4: needs the output of `node tools/frames.mjs http://localhost:4321 390 844 300` under evidence/.
- C7: needs the Lighthouse JSON on the served build.
```

(`tests/fixtures/scorer-output.md` is this output in full.) Score log:
`1 | v1 | 49 | 80 | 61 | n/a | C4,C7 | G1 P, G2 F, G3 P | two N/A: produce evidence`.
`score_math.py decide --threshold 8 --target 85 --max-rounds 5 --scores 43,49/80 --rounds-used 1 --gates FAIL`
prints `CONTINUE` with detail `incomparable: adjusted max changed, no delta; 1 of 5
rounds used`. The 61 is not compared with 43; the evidence is produced before round 2.

**Amendment** (steps: `references/full-protocol.md` section 5). F8 matters and no
criterion covers it. Added C9 "Working example above the fold" (8 pts): `0` no example
in the first 900px, `4` command visible but output below the fold, `8` command and
three or more output lines visible; its check line is a `tools/shot.mjs` capture.
Funded by C6 14 -> 10 and C8 16 -> 12; C6's anchors now say which adjective lists count
(the hero's), the ambiguity the round-1 scorer's Notes raised. Rubric v2, user told in
one line. The orchestrator ran the C4, C7 and C9 check lines once each with
`evidence.py run`, and rescored `snapshots/round-1` **twice** under rubric v2 with that
evidence (full mode re-measures the noise after a rubric change): 56 and 60
(`third-run=no`), so the new reference is 58, n=4, T stays 8.
Run A's breakdown:

```
## Score: 56/100
## Scaled: 56
C1 14, C2 12, C3 6, C4 0 (scrollWidth 412 at 390), C5 5, C6 10, C7 5, C8 4, C9 0
```

Run B differed on C8 (8: it weighed the two serious issues, footer contrast and the
unlabelled newsletter input, as peripheral) and on no other criterion. Score log:
`1a | v2 | 56`, `1b | v2 | 60`,
`1 | v2 | 58 | 100 | 58 | n/a | - | G1 P, G2 F, G3 P | new reference = mean; n=4, T=8`.
Amendments: `v2: added C9 (8) from F8; C6 14->10, C8 16->12; round-1 snapshot 49/80 (v1)
-> 56 and 60 (v2, rescored), reference 58`.

## 4. Round 2: criteria-aware review

Reviewer brief: round 2 template with the stripped v2 criteria, the reference
per-criterion scores, the KEEP list as PROTECTED, and this PRIOR FINDINGS block:

```
PRIOR FINDINGS (from earlier rounds; do not re-list these)
F1 | hero is a slogan | index.html:18 | addressed
F2 | install below fold | index.html:40 | addressed
F3 | placeholder testimonials | index.html:88 | addressed (section removed); proposed quotes rejected (Constraints forbid invented testimonials)
F4 | 52-word paragraph | index.html:36 | addressed
F5 | missing alt | index.html:22 | addressed
F6 | footer contrast | styles.css:212 | deferred (user confirmed it is not a brand color; scheduled this round)
F7 | grid overflow at 390 | styles.css:140 | addressed
F8 | no working example | whole page | addressed by new criterion C9; work pending
F9 | third-party font | index.html:9 | deferred (low weight; revisit if C7 < 8)
```

The reviewer returned 7 findings. F3' asked again for "two or three short testimonials
to build trust" with no new evidence and no tag. Triage table:

| F# | Sev | Bucket | Verified | Criterion | Action or reason |
|----|-----|--------|----------|-----------|------------------|
| F3' | major | REJECT (repeat) | n/a | - | re-raised without new evidence; see R1 F3 |
| F10 | critical | DO NOW | yes: `<pre>` at :71 is 412px wide at 390 | G2, C4 | `overflow-x: auto; max-width: 100%` on `pre` |
| F11 | critical | DO NOW | yes: no `<pre>` in first 900px | C9 | add command + 6-line output block to the hero |
| F12 | major | DO NOW | yes: `<pre>` at :71 lacks the command | C5 | prepend `$ tidylog app.log` and use README sample output |
| F6 | major | DO NOW | yes: user confirmed not a brand color | C8 | footer link color to #3b4a5a (contrast 7.1:1) |
| F13 | minor | DEFER | yes: h2 28px vs h1 56px, no mid step | C3 | revisit if C3 stays at 6 |
| F14 | minor | REJECT | no: README documents `--group` | C5 | claim false |

Evidence manifest `evidence/round-2/manifest.md`, written by `evidence.py run` (every
command is the check line verbatim; the dev server on 4321 was already running, and C7
started `npx serve dist` itself with `--serve`):

```
| Check | Command | Timestamp (UTC) | Attempts | Note |
|-------|---------|-----------------|----------|------|
| G1 | npm run build | 2026-10-20T19:01:30Z | 1 | exit 0; output G1-1.log |
| G2 | node tools/check-overflow.mjs http://localhost:4321 390 | 2026-10-20T19:01:32Z | 1 | exit 0; output G2-1.log |
| G3 | git diff --quiet a1b2c3d -- site/tokens.css | 2026-10-20T19:01:33Z | 1 | exit 0; output G3-1.log |
| C2 | node tools/shot.mjs http://localhost:4321 1440 900 c2-1440.png | 2026-10-20T19:01:58Z | 1 | exit 0; output C2-1.log; files: c2-1440.png |
| C3 | bash -c 'node tools/shot.mjs http://localhost:4321 1440 900 c3-shot.png && convert c3-shot.png -blur 0x8 c3-blur.png' | 2026-10-20T19:02:05Z | 1 | exit 0; output C3-1.log; files: c3-blur.png, c3-shot.png |
| C4 | node tools/frames.mjs http://localhost:4321 390 844 300 | 2026-10-20T19:02:11Z | 1 | exit 0; output C4-1.log; files: frames/ (7 PNGs) |
| C7 | npx lighthouse http://localhost:3000 --only-categories=performance --output=json --quiet | 2026-10-20T19:04:40Z | 1 | exit 0; output C7-1.log; server log C7-1.serve.log |
| C8 | npx @axe-core/cli http://localhost:4321 --exit | 2026-10-20T19:05:02Z | 1 | exit 1; output C8-1.log |
| C9 | node tools/shot.mjs http://localhost:4321 1440 900 c9-1440.png | 2026-10-20T19:05:09Z | 1 | exit 0; output C9-1.log; files: c9-1440.png |
```

Scorer: `73/100` (C4 0 -> 10, C9 0 -> 4, C5 5 -> 6; C8 stays 6 because one serious
axe issue remains, the unlabelled newsletter input, which the scorer's Notes point out). Gates all
PASS from the orchestrator's runs. `decide --scores 58,73 --rounds-used 2 --gates PASS`
prints `CONTINUE` with detail `+15; 2 of 5 rounds used`. Score log:
`2 | v2 | 73 | 100 | 73 | +15 | - | G1 P, G2 P, G3 P | G2 now passes`. Per-criterion
Before/After table written to `rounds.md`.

## 5. Round 3

Four findings, two DO NOW: F9 (deferred, now blocking C7 under 8: self-host the font)
and a type-scale step for C3. Scorer `78/100`: C7 5 -> 10 (Lighthouse 0.93); C3
unchanged at 6 (the blurred capture still shows the install block competing).
`decide --scores 58,73,78 --rounds-used 3` prints `CONTINUE` with detail
`within-noise: +5 against T=8; 3 of 5 rounds used`. Score log
`3 | v2 | 78 | ... | +5 | within noise`.

## 6. Round 4

Three findings, two DO NOW (C9 move the output above the fold; C8 landmark roles, a
moderate axe issue the reviewer raised). Scorer `80/100`: C9 4 -> 6 (command and two
output lines visible, the third cut at 900px; between the 4 and 8 anchors); C8
unchanged at 6 (the newsletter input label is still missing; the scorer's Notes flag it
for the third time, and triage never picked it up because no reviewer raised it).

```
$ score_math.py decide --threshold 8 --target 85 --max-rounds 5 --scores 58,73,78,80 --rounds-used 4 --gates PASS
STOP plateau
detail: last two deltas +5 and +2 are both inside T=8 and total +7, not more than T; stop even with budget left and tell the user what is left
```

Round 5 is not run even though budget remains. Score log
`4 | v2 | 80 | ... | +2 | plateau: +5, +2 both inside T=8, total 7`.
Two consecutive rounds each failed to register a real move and together did not show
one either; that is the plateau. The remaining budget does not change the decision,
and the +2 is reported as within noise, not as progress toward 85.

## 7. Final report (as the user read it)

**Result: 58 -> 80 on rubric v2 (the v1 baseline of 43 is a different instrument and
not comparable), T=8, 4 of 5 rounds used, stopped on plateau.** All gates pass: G1
PRESERVE PASS/PASS, G2 ACHIEVE FAIL -> PASS
(round 2), G3 PRESERVE PASS/PASS.

| # | Criterion | Max (v2) | Round 0 (v1) | Final |
|---|-----------|-----|---------|-------|
| C1 | Hero sentence | 14 | 4 | 14 |
| C2 | Install above fold | 12 | 6 | 12 |
| C3 | Typographic hierarchy | 12 | 6 | 6 |
| C4 | Mobile scroll | 10 | 0 | 10 |
| C5 | Proof block | 12 | 3 | 6 |
| C6 | Copy density | 10 | 10 | 10 |
| C7 | Performance | 10 | 5 | 10 |
| C8 | Accessibility | 12 | 9 | 6 |
| C9 | Example above fold | 8 | 0 | 6 |
| **Total** | | 100 | 43 | 80 |

The Round 0 column is the v1 baseline on v1 maxes (C6 out of 14, C8 out of 16, no C9),
shown for orientation only; the comparable series is the v2 one, reference 58 -> 80.

Changed: hero rewritten to one sentence; install block and a six-line terminal example
moved into the hero; placeholder testimonials removed; grid and `<pre>` overflow fixed;
alt text, footer contrast, landmark roles added; font self-hosted. Rejected: F3's
proposed quotes and F3' (invented testimonials, forbidden by Constraints), and
F14 (false claim). Deferred: F13
(type scale, folded into C3 work, not rewarded). Reviewer and scorer both ran as a
separate Codex process (isolated home, read-only sandbox, staged copy); no inline
grading. 12 child runs (8 scorer, 4 reviewer), about 80 minutes of child wall time by
the `.log` footers (scorer runs about six minutes each, reviewer runs about eight); spend
not reported by the CLI. C2, C3, C4, C7, C8 and C9 were scored from
orchestrator-produced evidence (manifests in `evidence/`); the round-1 N/A was the
orchestrator's lapse, corrected before round 2.

Remaining gap: C3 at 6/12, C5 at 6/12 and C9 at 6/8 all hinge on one decision, a hero
image versus a recorded terminal session. That is a taste call for the user, not
something another round can score its way through. C8 is one axe serious issue away
from 12: the newsletter input has no label. The scorer's Notes said so in rounds 2, 3
and 4 and triage never picked it up because no reviewer raised it; read the Notes.
Recommendation: fix the label, then pick one of the two hero directions and run a
single lite round if you want the last points.
