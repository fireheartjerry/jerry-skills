# Example lite loop: the tidylog README

The default path, end to end, on a fictional artifact: the user wrote "polish the
README, it reads like a first draft" in the repo of `tidylog`, an invented Python CLI
with a Makefile and a pytest suite. One file, no rounds or budget named, so lite.
Engines: codex as reviewer, claude (`--same-engine`, since the orchestrator is Claude
Code) as the pinned scorer. Every number below is checked by `tests/test_scripts.py`:
the criteria doc lints clean in lite mode and the decide lines print what is shown.

## 1. The proposal message (verbatim), then the turn ends

Two read-only commands fill it first: `snapshot.py create --root . --path README.md
--path src/tidylog/cli.py --dry-run` (2 files, 9 KB) and `spawn_child.sh --check`
(codex and claude ok).

```
Lite mode: one README, no rounds, target or budget named.

Criteria (100 points):
C1. First screen says what tidylog does and shows a run (35 pts)
    Check: read the README up to the first ## heading; quote the first sentence and any
    code block. 0: no sentence names input and output. 17: a sentence does, no example.
    35: one sentence names input and output and a command with its output follows.
    Today: about 0 (the README opens with a badge row and "A better way to read logs").
C2. Install and first command paste clean (30 pts)
    Check: run every command in the README's Install and Quick start blocks in a fresh
    venv (I run this and hand over the log). 0: install fails. 15: install works, a
    later command fails. 30: every command exits 0. Today: about 15 (the quick start
    uses a --group flag the CLI renamed to --by).
C3. Scannable structure (35 pts)
    Check: count the problems: paragraphs over 60 words, a missing Usage heading.
    0: three or more. 17: one or two. 35: none. Today: about 35 (short paragraphs,
    clear headings).

Gates:
G1. [PRESERVE] tests pass; check: `make test` exits 0 (the repo's verify command, from
    the Makefile).
G2. [ACHIEVE] every flag the README names exists; check: `python3 tools/flags.py
    README.md` (a helper in the loop dir comparing against `tidylog --help`) prints
    nothing.

Out of scope: the docs/ site, CHANGELOG.md. Constraints: no new badges; keep the MIT
notice.
Scope: README.md, plus src/tidylog/cli.py as what it is checked against (2 files).
Budget: 2 rounds max, no commits, T = 3.
Spend: codex (OpenAI) reviews, claude (Anthropic) scores; 2 preflights and 3 to 7
child runs of 6 to 10 minutes each, so 20 to 70 minutes of child time. Each run is a
billed call that sends those 2 files to that vendor.

Reply ok or edit.
```

The user replied "ok, but C3 should also count code blocks with no language tag".
C3's check line gained that clause; nothing else changed.

## 2. Setup and the criteria doc

```
S/loop.py init --root . --slug readme --scope README.md --scope src/tidylog/cli.py \
  --engines "codex default (OpenAI) reviewer; claude claude-x-1 (Anthropic) scorer; 7 runs"
S/spawn_child.sh --preflight codex
S/spawn_child.sh --preflight claude --model claude-x-1 --same-engine
S/score_math.py check-criteria LOOP/readme-criteria.md --mode lite
```

The last line prints `ok: 3 criteria summing to 100 (lite mode), 2 gates`. The doc:

```markdown
# Criteria: tidylog README

**Artifact:** `README.md`, checked against `src/tidylog/cli.py`
**Status:** APPROVED by user on 2026-10-22
**Rubric version:** v1

<!-- loop-only -->
**Mode:** lite
**Target:** 90/100   **Budget:** 2 rounds
**Loop dir:** .git/iterative-improvement/readme/
<!-- /loop-only -->

## Context

tidylog folds noisy log files into a grouped summary. The README is what a developer
reads after finding the package; success means they know what it does from the first
screen and can paste the install and first command without an error.

## Gates (binary, reported separately from the score)

- **G1.** [PRESERVE] Test suite passes | *check:* `make test` exits 0
- **G2.** [ACHIEVE] README flags exist | *check:* `python3 tools/flags.py README.md` prints every `--flag` the README names that `tidylog --help` lacks; pass when it prints nothing

## Criteria (100 points)

### C1. First screen says what tidylog does and shows a run (35 pts)
**How to check it:** Read the README up to the first `##` heading. Quote the first sentence and any code block.
**Anchors:**
- `0`: no sentence names the input (log files) and the output (a grouped summary)
- `17`: such a sentence, but no example run
- `35`: that sentence, then a command and at least three lines of its output

### C2. Install and first command paste clean (30 pts)
**How to check it:** Read the evidence log of the README's Install and Quick start commands run in a fresh venv.
**Anchors:**
- `0`: install fails
- `15`: install works, a later command fails
- `30`: every command exits 0

### C3. Scannable structure (35 pts)
**How to check it:** Count the problems and list each with its line: a paragraph over 60 words, a fenced code block with no language tag, no heading named Usage.
**Anchors:**
- `0`: three or more problems
- `17`: one or two problems
- `35`: no problem

## Out of scope
The docs/ site, CHANGELOG.md.

## Constraints
- No new badges.  - Keep the MIT notice.
```

## 3. Round 0 and round 1

Round 0: snapshot, stripped doc, evidence (`make test`, the venv run, `tools/flags.py`),
one scorer run. `check-scorer` prints `token=41 scaled=41 na=none gates=G1:PASS,G2:FAIL`:
C1 0, C2 15, C3 26 (one problem, an untagged block at :70; the scorer put a single
trivial problem nearer full marks than the 17 anchor). G1 is PRESERVE, G2 ACHIEVE.
Status line posted to the user: `round 0: 41/100, G1 PASS, G2 FAIL, reference set,
14 min`.

The blind codex review of `round-0` returned seven findings. Triage in `rounds.md`:

| F# | Sev | Bucket | Verified | Criterion | Action or reason |
|----|-----|--------|----------|-----------|------------------|
| F1 | critical | DO NOW | yes: README.md:1-6 is badges and a slogan | C1 | one sentence naming input and output, then `tidylog app.log` with five output lines |
| F2 | major | DO NOW | yes: README.md:41 `--group`; cli.py:88 defines `--by` | C2, G2 | rename to `--by`; the sweep found it twice more (:57, :63) |
| F3 | major | DO | yes: no Configuration section; cli.py reads `TIDYLOG_LEVEL` | C3 | add a four-line Configuration section |
| F4 | major | REJECT | yes: the badges are the user's | - | Constraints: no badge changes either way |
| F5 | minor | DO NOW | yes: untagged block at :70 | C3 | tag it `text` |
| F6 | minor | DEFER | yes: no comparison with similar tools | - | outside every criterion; not worth an amendment |
| F7 | minor | REJECT | no: the venv run shows `pip install tidylog` works | C2 | claim false |

Fixes F1, F2 (three instances), F5, F3; `make test` re-run after each: PASS. Snapshot
`round-1`, evidence regenerated, one scorer run: C1 35, C2 15, C3 26, total 76. C2
stayed at 15 because the Quick start's `tidylog --follow app.log` never exits and the
venv run timed out on it; C3 traded the tagged block for a new problem, F3's section
is one 72-word paragraph. The scorer's rows quote both.

```
S/score_math.py decide --threshold 3 --target 90 --max-rounds 2 --scores 41,76 --rounds-used 1 --gates PASS
```

prints `CONTINUE continue` with `+35; 1 of 2 rounds used`. Status line:
`round 1: 76/100, G1 PASS, G2 PASS, CONTINUE, 41 min`.

## 4. Round 2 and the report

The round-2 review (stripped criteria, per-criterion scores, PRIOR FINDINGS with F4 and
F7's reasons, PROTECTED: the one-line install and the example run) raised the
`--follow` example and the long paragraph. Both verified: the example became
`tidylog app.log --by level`, and the paragraph was split, into 64 and 20 words. The
scorer: C1 35, C2 30, C3 26 (the 64-word half is still over 60), total 91.

```
S/score_math.py decide --threshold 3 --target 90 --max-rounds 2 --scores 41,76,91 --rounds-used 2 --gates PASS
```

prints `CONFIRM target-reached`. A second scorer run on `round-2` gives 89 (C3 24), and
the same line with `--confirm-score 89` prints `STOP target-hit` (mean 90).
`snapshot.py restore --snapshot LOOP/snapshots/round-2 --root . --dry-run` lists
nothing, so the score describes the tree. The report:

```
README: 41 -> 91, confirmed by a second run (89; mean 90 meets the target). T = 3, a
floor; noise not measured. Gates: G1 PRESERVE PASS, G2 ACHIEVE PASS (was FAIL:
--group renamed to --by in three places).
C1 0 -> 35: one-sentence summary and an example run on the first screen.
C2 15 -> 30: every Install and Quick start command runs clean (the --follow example
  that never exits is gone).
C3 26 -> 26: the untagged block is fixed, but the new Configuration section has a
  64-word paragraph; a one-line split finishes it.
Rejected: F4 (badges, a constraint), F7 (install works). Deferred: F6 (no criterion).
Children: codex reviewer (separate CLI), claude scorer (same engine, isolated);
2 rounds, 2 preflights + 6 child runs, 58 min from the .log durations; spend not
measured. The criteria doc is LOOP/readme-criteria.md, uncommitted.
```
