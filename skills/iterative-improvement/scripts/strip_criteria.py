#!/usr/bin/env python3
"""Strip a criteria document down to what the scorer may see.

usage: strip_criteria.py IN OUT

Removes, in this order:
  - every block from a line containing <!-- loop-only --> through the line
    containing <!-- /loop-only -->, inclusive (blocks may repeat);
  - the "## Score log" and "## Amendments" sections, each from its heading up to
    the next "## " heading or end of file;
  - the [PRESERVE] and [ACHIEVE] class tags on gate lines;
  - any line starting with **Target:**, **Budget:**, **Baseline, **Noise floor:**,
    **Move threshold:**, **Scorer pin:**, **Loop dir:**, **Mode:** or
    **Rubric version:** (for documents written before the markers existed);
  - runs of three or more blank lines (collapsed to two).

Then checks the result with whole-word matching: if any of the forbidden terms
remain (score log, rubric v<N>, round <N>, noise floor, move threshold, loop-only,
[PRESERVE], [ACHIEVE], plateau) it prints "strip failed: found ..." to stderr,
writes nothing and exits 4. "around 16px" and "<baseline SHA>" do not trip it;
write the literal SHA in check lines anyway, so the scorer can run them. Exit 2
when IN is missing.
"""

import re
import sys

LOOP_ONLY_START = "<!-- loop-only -->"
LOOP_ONLY_END = "<!-- /loop-only -->"
REMOVED_SECTIONS = ("## Score log", "## Amendments")
REMOVED_LINE_PREFIXES = (
    "**Target:**",
    "**Budget:**",
    "**Baseline",
    "**Noise floor:**",
    "**Move threshold:**",
    "**Scorer pin:**",
    "**Loop dir:**",
    "**Mode:**",
    "**Rubric version:**",
)
FORBIDDEN_PATTERNS = (
    ("score log", re.compile(r"\bscore log\b", re.I)),
    ("rubric v<N>", re.compile(r"\brubric v\d", re.I)),
    ("round <N>", re.compile(r"\bround \d+\b", re.I)),
    ("noise floor", re.compile(r"\bnoise floor\b", re.I)),
    ("move threshold", re.compile(r"\bmove threshold\b", re.I)),
    ("loop-only", re.compile(r"loop-only", re.I)),
    ("[PRESERVE]", re.compile(r"\[preserve\]", re.I)),
    ("[ACHIEVE]", re.compile(r"\[achieve\]", re.I)),
    ("plateau", re.compile(r"\bplateau", re.I)),
)
GATE_TAG = re.compile(r"\[(PRESERVE|ACHIEVE)\] ?")


def usage(code):
    stream = sys.stdout if code == 0 else sys.stderr
    stream.write("usage: strip_criteria.py IN OUT\n")
    if code == 0:
        stream.write(__doc__.split("\n", 2)[2])
    sys.exit(code)


def strip(text):
    lines = text.splitlines()
    kept = []
    removed = 0
    in_loop_block = False
    in_removed_section = False
    for line in lines:
        if in_loop_block:
            removed += 1
            if LOOP_ONLY_END in line:
                in_loop_block = False
            continue
        if LOOP_ONLY_START in line:
            removed += 1
            if LOOP_ONLY_END not in line:
                in_loop_block = True
            continue
        if line.startswith("## "):
            in_removed_section = any(line.rstrip() == h or line.startswith(h + " ") for h in REMOVED_SECTIONS)
        if in_removed_section:
            removed += 1
            continue
        stripped = line.lstrip("- ").lstrip()
        if any(stripped.startswith(prefix) for prefix in REMOVED_LINE_PREFIXES):
            removed += 1
            continue
        line = GATE_TAG.sub("", line)
        kept.append(line)
    collapsed = []
    blank_run = 0
    for line in kept:
        if line.strip() == "":
            blank_run += 1
            if blank_run > 2:
                removed += 1
                continue
        else:
            blank_run = 0
        collapsed.append(line)
    return "\n".join(collapsed).rstrip("\n") + "\n", removed


def check(text):
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in FORBIDDEN_PATTERNS:
            if pattern.search(line):
                return label, number
    return None, None


def main(argv):
    if len(argv) == 1 and argv[0] in ("-h", "--help"):
        usage(0)
    if len(argv) != 2:
        usage(2)
    source, target = argv
    try:
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        sys.stderr.write("strip_criteria: cannot read %s (%s)\n" % (source, error))
        return 2
    result, removed = strip(text)
    term, number = check(result)
    if term is not None:
        sys.stderr.write('strip failed: found "%s" at line %d of the stripped output\n' % (term, number))
        return 4
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(result)
    except OSError as error:
        sys.stderr.write("strip_criteria: cannot write %s (%s)\n" % (target, error))
        return 2
    print("stripped: %s -> %s (removed %d lines)" % (source, target, removed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
