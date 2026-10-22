## Score: 49/80
## Scaled: 61

## Gates
| Gate | Result | Evidence |
|------|--------|----------|
| G1   | PASS   | evidence/G1-1.log and manifest.md: `npm run build` exit 0 (14 lines, ends "built in 412ms") |
| G2   | FAIL   | evidence/G2-1.log: `node tools/check-overflow.mjs http://localhost:4321 390` printed scrollWidth 412 |
| G3   | PASS   | evidence/G3-1.log is empty; manifest.md records exit 0 |

## Per-criterion
| # | Criterion | Points | Max | Evidence |
|---|-----------|--------|-----|----------|
| C1 | Hero sentence | 8 | 14 | h1 "tidylog turns noisy log files into a short, grouped summary" (index.html:18) names input, action and output in 11 words, but the 28-word paragraph below repeats it, so the first-read message is split across two sentences; between the 7 and 14 anchors, nearer 7 |
| C2 | Install path at 1440px | 12 | 12 | `npm i -g tidylog` in a `<pre>` at y=612 of 900 (evidence/c2-1440.png); copy button present, `data-copy` handler in index.html:44 |
| C3 | Typographic hierarchy | 6 | 12 | blurred capture (evidence/c3-blur.png): h1 dominates but the install block's dark background competes at the same visual weight; matches the 6 anchor |
| C4 | Mobile scroll integrity | N/A | 10 | no browser in this environment and no frames supplied under evidence/ |
| C5 | Proof block | 5 | 12 | one `<pre>` of 4 lines at index.html:71 shows output only, no command; the lines are plausible tidylog output ("INFO 3 entries grouped"), so just under the 6 anchor |
| C6 | Copy density | 10 | 14 | no paragraph over 45 words (the hero paragraph at index.html:36 is 28); the feature captions read "Fast. Small. Readable." (index.html:61), which I counted as an adjective list outside the hero; between the 7 and 14 anchors |
| C7 | Performance | N/A | 10 | lighthouse not available and no report supplied under evidence/ |
| C8 | Accessibility | 8 | 16 | evidence/C8-1.log: 0 critical, 2 serious (color-contrast on footer links, label missing on the newsletter input at index.html:120); matches the 8 anchor |

## Unscoreable
- C4: needs the output of `node tools/frames.mjs http://localhost:4321 390 844 300` (frame files plus the printed scrollWidth and identical-frame indices) under evidence/.
- C7: needs the JSON from `npx lighthouse http://localhost:3000 --only-categories=performance --output=json --quiet` on the served build.

## Notes
- C1's anchors do not say whether the paragraph under the heading counts as part of "the first sentence a visitor reads". I scored the heading alone and flagged the split.
- C5's "plausible output" needs an example of what plausible means; I treated output without a visible command as just under the 6 anchor.
- C6's anchors do not say whether an adjective list outside the hero counts; I scored it as costing part of the full marks.
