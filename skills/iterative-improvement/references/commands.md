# Every command, in order

The exact sequence for a lite loop and the additions for full mode, for when you come back
to a loop and need the next line rather than the reasoning. SKILL.md and
`references/full-protocol.md` say why each step exists. `S` is the `scripts/` folder next
to SKILL.md (SKILL.md, "Paths"); ROOT, LOOP and the slug are as in SKILL.md section 3.
Run `S/loop.py status --loop LOOP` first when resuming: it names the step you are on.

## Lite

Before the reply: write the proposal and end your turn. Nothing below runs until the user
says ok, except the two read-only lines that fill the proposal:

```
S/snapshot.py create --root ROOT --path <artifact> --path <checked-against> --dry-run   # the file list
S/spawn_child.sh --check                                                              # free: which engines exist
```

After ok:

```
S/loop.py init --root ROOT --slug SLUG --scope <artifact> --scope <file it is checked against> \
  --engines "<engine, model (vendor), run cap>" [--off-limits <path>]
S/spawn_child.sh --preflight <engine> [--model M] [--same-engine]      # reviewer
S/spawn_child.sh --preflight <engine> --model M [--same-engine]        # scorer: the pin
# write LOOP/SLUG-criteria.md (template: references/criteria-guide.md)
S/score_math.py check-criteria LOOP/SLUG-criteria.md --mode lite
```

Every child run below takes minutes: add `--background` to each `spawn_child.sh` line
(it returns at once) and wait for `<out>.status` (the exit code) before the next step.

Round 0:

```
S/snapshot.py create --root ROOT --path <artifact> --path <checked-against> --out LOOP/snapshots/round-0
S/strip_criteria.py LOOP/SLUG-criteria.md LOOP/criteria-for-scorer.md
S/evidence.py run --dir LOOP/evidence/round-0 --snapshot LOOP/snapshots/round-0 --check G1 [--cwd ROOT] -- <check line>
#   ... once per gate and per check line the scorer cannot run; a page on a dev server:
S/evidence.py run --dir LOOP/evidence/round-0 --check C2 --cwd LOOP --collect 'shot-*.png' \
  --serve '<start command>' --serve-cwd ROOT --wait-for <url> -- node tools/shot.mjs <url> 1440 900 shot-1440.png
S/evidence.py view --dir LOOP/evidence/round-0 --out LOOP/evidence/view-round-0
# write LOOP/prompts/scorer-round-0.md from references/scorer-brief.md
S/spawn_child.sh <engine> scorer LOOP/prompts/scorer-round-0.md LOOP/snapshots/round-0 \
  LOOP/scores/round-0.md --model M --evidence LOOP/evidence/round-0
S/score_math.py check-scorer LOOP/scores/round-0.md --criteria LOOP/SLUG-criteria.md   # prints the reference token
# mark each gate [PRESERVE] or [ACHIEVE] in the doc
```

Round 1 (round 2 is the same with N = 2, the round-2 reviewer brief and `--rounds-used 2`):

```
# write LOOP/prompts/reviewer-round-1.md from references/reviewer-brief.md (round 1: blind)
S/spawn_child.sh <engine> reviewer LOOP/prompts/reviewer-round-1.md LOOP/snapshots/round-0 \
  LOOP/reviews/round-1.md --evidence LOOP/evidence/view-round-0
# triage table and KEEP list in LOOP/rounds.md
# amend now if a verified defect has no criterion: bump the version, then
#   S/spawn_child.sh <engine> scorer ... LOOP/snapshots/round-0 LOOP/scores/round-0r.md ...   (the new reference)
# fix DO NOW, then DO, sweeping each fix's siblings; after each change re-run the PRESERVE gates
S/snapshot.py create --root ROOT --path <artifact> --path <checked-against> --out LOOP/snapshots/round-1
S/strip_criteria.py LOOP/SLUG-criteria.md LOOP/criteria-for-scorer.md
S/evidence.py run --dir LOOP/evidence/round-1 --snapshot LOOP/snapshots/round-1 --check G1 [--cwd ROOT] -- <check line>
S/evidence.py view --dir LOOP/evidence/round-1 --out LOOP/evidence/view-round-1
S/spawn_child.sh <engine> scorer LOOP/prompts/scorer-round-1.md LOOP/snapshots/round-1 \
  LOOP/scores/round-1.md --model M --evidence LOOP/evidence/round-1
S/score_math.py check-scorer LOOP/scores/round-1.md --criteria LOOP/SLUG-criteria.md
S/score_math.py decide --threshold 3 --target 90 --max-rounds 2 --scores <ref>,<round 1> --rounds-used 1 --gates PASS|FAIL
```

A revert, when one is needed (`--expect` names the snapshot holding the state the loop
left; restore refuses files someone else changed):

```
S/snapshot.py restore --snapshot LOOP/snapshots/round-<N-1> --root ROOT --only <files the loop changed> \
  [--expect LOOP/snapshots/round-N] --dry-run
S/snapshot.py restore --snapshot LOOP/snapshots/round-<N-1> --root ROOT --only <files the loop changed> \
  [--expect LOOP/snapshots/round-N]
```

Before the report (full mode: the blind judge first, `references/judge-brief.md`):

```
S/snapshot.py restore --snapshot LOOP/snapshots/round-<last scored> --root ROOT --dry-run   # must list nothing
S/snapshot.py restore --snapshot LOOP/snapshots/round-0 --root ROOT --dry-run    # net changed files
grep -h '^# duration' LOOP/scores/*.log LOOP/reviews/*.log                       # wall time
grep -h '^# usage: cost USD' LOOP/scores/*.log LOOP/reviews/*.log                # spend, claude --max-usd only
```

## Full mode changes

- `S/loop.py init ... --mode full`; the criteria doc lives at
  `ROOT/docs/criteria/SLUG-criteria.md` (repo, uncommitted unless commits are allowed)
  and is checked with `S/score_math.py check-criteria <doc>` (6 to 12 criteria).
- Round 0 scores twice, `scores/round-0a.md` and `round-0b.md`, then
  `S/score_math.py baseline <A> <B>`; on `third-run=needed`, `round-0c.md` and
  `S/score_math.py baseline <A> <B> <C>`. `scores/round-0a.cmd` is the pin.
- Every later decide: `S/score_math.py decide --threshold <T> --target <X> --max-rounds <R>
  --scores <series> --rounds-used <N> --gates PASS|FAIL`; under a money or time budget,
  first `S/score_math.py budget --total <B> --spent <now minus kickoff> --per-round <cost>
  --rounds <ceiling> --rounds-used <N>` and pass its `max-rounds` as `--max-rounds`.
- `CONFIRM target-reached`: score the same snapshot again into `scores/round-N-confirm.md`,
  then rerun decide with `--confirm-score <token>`.
- An amendment rescores one snapshot twice (`scores/round-<K>a.md`, `round-<K>b.md`) and
  takes the new reference from `S/score_math.py baseline`; which snapshot is
  `references/full-protocol.md` section 5.
- A large scope: one reviewer per slice (`references/troubleshooting.md`, "Large
  artifacts"); a lite loop that escalates: `references/troubleshooting.md`, "Lite mode
  escalation".
