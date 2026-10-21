# Criteria: tidylog landing page

**Artifact:** `site/index.html` and `site/styles.css`, served by `npm run dev` at http://localhost:4321
**Status:** APPROVED by user on 2026-10-20
**Rubric version:** v2

<!-- loop-only -->
**Mode:** full
**Target:** 85/100   **Budget:** 5 rounds
**Baseline score:** 43   **Noise floor:** n=4   **Move threshold:** T=8
**Baseline SHA:** a1b2c3d
**Scorer pin:** codex, model gpt-5, flags as recorded in .iterative-improvement/landing-page/scores/round-0a.cmd
**Loop dir:** .iterative-improvement/landing-page/
<!-- /loop-only -->

## Context

tidylog is an open-source command-line tool that folds noisy log files into a short, grouped summary. The landing page is the first thing a developer sees after following a link from a README or a package registry. Success means a visitor understands what the tool does within a few seconds, sees proof that it works on real output, and can copy the install command without hunting for it.

## Gates (binary, reported separately from the score)

A failed gate means the work is not deliverable regardless of points. Gate checks are run by the orchestrator before scoring; their output is supplied as evidence.
<!-- loop-only -->
Class is set at round 0: PRESERVE passes at baseline and must stay passing; ACHIEVE fails at baseline and must pass by the end.
<!-- /loop-only -->

- **G1.** [PRESERVE] `npm run build` exits 0 | *check:* run `npm run build` in the repo root and read the exit code
- **G2.** [ACHIEVE] No horizontal overflow at 390px wide | *check:* with the dev server running, `node tools/check-overflow.mjs http://localhost:4321 390` prints the document scrollWidth; pass when it is 390 or less
- **G3.** [PRESERVE] `site/tokens.css` unchanged | *check:* `git diff --quiet a1b2c3d -- site/tokens.css` exits 0

## Criteria (100 points)

### C1. Hero states what the tool does in one sentence (14 pts)
**What it means:** The first sentence a visitor reads says what tidylog does to what input, in plain words.
**How to check it:** Read the first heading and the first paragraph in `site/index.html`. Quote them. Count the words in the first sentence.
**Anchors:**
- `0`: the heading is a slogan or adjective ("Logs, tamed") and no sentence above the fold names the input or the output
- `7`: one sentence names the tool and the output but takes more than 25 words or two sentences to get there
- `14`: one sentence of at most 25 words names the input (log files), the action (groups or summarises) and the output (a readable summary)

### C2. Install path visible without scrolling at 1440px (12 pts)
**What it means:** A visitor on a laptop sees the install command and a copy affordance without scrolling.
**How to check it:** Screenshot at 1440x900. Note whether an install command (`npm i -g tidylog`, `brew install tidylog` or similar) is fully inside the first 900px, and whether a copy button or selectable code block accompanies it.
**Anchors:**
- `0`: no install command in the first screen
- `6`: the command is inside the first screen but partly cut off, or has no copy affordance
- `12`: command fully visible, in a code block, with a copy button that works

### C3. Typographic hierarchy (blur test) (12 pts)
**What it means:** A reader scanning for 3 seconds can identify the single most important message without reading body copy.
**How to check it:** Screenshot at 1440px, blur to 8px (`convert shot.png -blur 0x8 blurred.png` or the equivalent). The primary message should still be the first thing the eye lands on, by size or weight contrast alone.
**Anchors:**
- `0`: blurred, nothing dominates; three elements compete at the same weight
- `6`: one element dominates but a secondary element competes at the same level
- `12`: a clear single focal point, with a deliberate second and third tier

### C4. Mobile scroll integrity at 390x844 (10 pts)
**What it means:** Every screen of scroll on a phone shows something. No dead stretches, no horizontal overflow.
**How to check it:** With the dev server running, `node tools/frames.mjs http://localhost:4321 390 844 300` writes a screenshot every 300px to `frames/` and prints the document scrollWidth and the indices of any identical consecutive frames.
**Anchors:**
- `0`: any horizontal overflow, or more than 2 screens of identical frames
- `5`: no overflow, one dead stretch under a screen
- `10`: no overflow, no dead stretch, every frame carries content

### C5. Proof: a real terminal example or output (12 pts)
**What it means:** The page shows tidylog running on real-looking input and the output it produces, so a visitor can judge whether it is useful.
**How to check it:** Search `site/index.html` for a `<pre>` or `<code>` block of more than three lines. Quote it. Check that it shows both a command and its output.
**Anchors:**
- `0`: no code block, or a block that shows only the install command
- `6`: a block shows output but not the command that produced it, or the output is obviously invented (lorem ipsum, "example output")
- `12`: a command and at least five lines of plausible output, consistent with the README

### C6. Copy density (10 pts)
**What it means:** Paragraphs are short and specific; no list of marketing adjectives stands in for a claim.
**How to check it:** Count words per paragraph in `site/index.html`. List every paragraph over 45 words. Search for three or more adjectives in a row ("fast, simple, powerful").
**Anchors:**
- `0`: two or more paragraphs over 45 words, or an adjective list in the hero
- `5`: one paragraph over 45 words and no adjective lists
- `10`: no paragraph over 45 words and no adjective lists anywhere

### C7. Performance (10 pts)
**What it means:** The static build loads fast on a mid-range phone.
**How to check it:** `npm run build`, serve `dist/` with `npx serve dist`, run `npx lighthouse http://localhost:3000 --only-categories=performance --output=json --quiet` and read `categories.performance.score`.
**Anchors:**
- `0`: performance score below 0.70
- `5`: 0.70 to 0.89
- `10`: 0.90 or above

### C8. Accessibility basics (12 pts)
**What it means:** No serious or critical accessibility defects.
**How to check it:** `npx @axe-core/cli http://localhost:4321 --exit` and count issues by impact.
**Anchors:**
- `0`: any critical issue
- `6`: no critical, one or two serious
- `12`: zero serious or critical issues

### C9. Working example above the fold (8 pts)
**What it means:** A visitor can see tidylog doing its job before scrolling.
**How to check it:** Screenshot at 1440x900. Note whether a code block showing a tidylog command and its output is at least partly visible inside the first 900px.
**Anchors:**
- `0`: no example in the first screen
- `4`: the example starts inside the first screen but the output is below it
- `8`: command and at least three lines of output visible without scrolling

## Out of scope

Dark mode, the blog, the docs pages, and the GitHub README. The reviewer should not spend findings on them and the scorer must not penalise their absence.

## Constraints

- `site/tokens.css` is the shared design token file and must not change.
- No new dependencies in `package.json`.
- Copy stays in English only.
- No testimonials, logos or user counts may be invented; the project has none yet.

## Score log

| Round | Rubric | Raw | Adj max | Scaled | Delta | N/A | Gates | Note |
|-------|--------|-----|---------|--------|-------|-----|-------|------|
| 0a    | v1     | 41  | 100     | 41     |       | -   | G1 P, G2 F, G3 P | baseline run A |
| 0b    | v1     | 45  | 100     | 45     |       | -   | G1 P, G2 F, G3 P | baseline run B |
| 0     | v1     | 43  | 100     | 43     |       | -   | G1 P, G2 F, G3 P | baseline = mean, n=4, T=8 |
| 1     | v1     | 49  | 80      | 61     | n/a   | C4, C7 | G1 P, G2 F, G3 P | two N/A (evidence not regenerated: orchestrator lapse); incomparable |
| 1a    | v2     | 56  | 100     | 56     |       | -   | G1 P, G2 F, G3 P | rescore A under v2 with evidence |
| 1b    | v2     | 60  | 100     | 60     |       | -   | G1 P, G2 F, G3 P | rescore B under v2 with evidence |
| 1     | v2     | 58  | 100     | 58     | n/a   | -   | G1 P, G2 F, G3 P | new reference = mean; n=4, T stays 8 |
| 2     | v2     | 73  | 100     | 73     | +15   | -   | G1 P, G2 P, G3 P | G2 now passes |
| 3     | v2     | 78  | 100     | 78     | +5    | -   | G1 P, G2 P, G3 P | within noise |
| 4     | v2     | 80  | 100     | 80     | +2    | -   | G1 P, G2 P, G3 P | plateau: +5, +2 both inside T=8, total 7 |

## Amendments

- v2 (after round 1): added C9 "Working example above the fold" (8 pts) from blind-review finding F8; C6 14 -> 10, C8 16 -> 12. Round 1 snapshot rescored twice under v2 with C4 and C7 evidence supplied: 49/80 (v1) -> 56 and 60 (v2), reference 58, n=4, T=8.
