#!/usr/bin/env python3
"""Strip a criteria document down to what the scorer may see.

usage: strip_criteria.py IN OUT [--allow-term TERM ...]

Removes, in this order:
  - every block from a line containing <!-- loop-only --> through the line
    containing <!-- /loop-only -->, inclusive (blocks may repeat); a block with no
    end marker is an error (exit 4), never stripped to the end of the file;
  - the "## Score log" and "## Amendments" sections, each from its heading up to
    the next "## " heading outside a code fence, or end of file;
  - the [PRESERVE] and [ACHIEVE] class tags on gate lines;
  - any line whose label is Target, Target score, Budget, Baseline..., Noise floor,
    Move threshold, Scorer pin, Loop dir, Mode or Rubric version, after any bullet,
    number or quote prefix: in bold anywhere ("**Target:** 85", "1. **Mode:** full",
    "> **Budget:** 2 rounds"), plain only above the first "## " heading
    ("Target: 85"), so a criterion line such as "Budget: under 50 KB" survives. The
    template keeps Rubric version above the loop-only block for the reader of the
    doc; this rule removes it;
  - runs of three or more blank lines (collapsed to two).

Then checks the result with whole-word matching and fails closed: if a forbidden
term remains it prints the term, the source line number and the line, writes
nothing and exits 4. Two kinds of term:
  - loop residue, never allowed: loop-only, [PRESERVE], [ACHIEVE], rubric v<N>, and
    a Target or Budget label anywhere on a line;
  - loop vocabulary that can also be an ordinary domain word: score log, round <N>,
    noise floor, move threshold, plateau. --allow-term TERM (repeatable, one of those
    five, for example --allow-term plateau or --allow-term "round <N>") permits it when the
    criterion really means the domain word; otherwise reword the source criterion.
Never hand-edit the stripped copy: it is regenerated every round. "around 16px" and
"<baseline SHA>" do not trip it; write the literal SHA in check lines anyway, so the
check can be run. Exit 2 when IN is missing, is not UTF-8, or --allow-term names an
unknown term.
"""

import re
import sys

LOOP_ONLY_START = "<!-- loop-only -->"
LOOP_ONLY_END = "<!-- /loop-only -->"
REMOVED_SECTIONS = ("## Score log", "## Amendments")
LABELS = r"(?:Target(?: score)?|Budget|Baseline[^:*]*|Noise floor|Move threshold|Scorer pin|Loop dir|Mode|Rubric version)"
PREFIX = r"^\s*(?:(?:[-*+>]|\d+[.)])\s*)*"
BOLD_LABEL_LINE = re.compile(PREFIX + r"\*\*\s*" + LABELS + r"\s*:", re.I)
PLAIN_LABEL_LINE = re.compile(PREFIX + LABELS + r"\s*:", re.I)
RESIDUE = (
    ("loop-only", re.compile(r"loop-only", re.I)),
    ("[PRESERVE]", re.compile(r"\[preserve\]", re.I)),
    ("[ACHIEVE]", re.compile(r"\[achieve\]", re.I)),
    ("rubric v<N>", re.compile(r"\brubric v\d", re.I)),
    ("Target or Budget label", re.compile(r"\*\*\s*(?:target(?: score)?|budget)\s*:", re.I)),
)
ALLOWABLE = (
    ("score log", re.compile(r"\bscore log\b", re.I)),
    ("round <N>", re.compile(r"\bround \d+\b", re.I)),
    ("noise floor", re.compile(r"\bnoise floor\b", re.I)),
    ("move threshold", re.compile(r"\bmove threshold\b", re.I)),
    ("plateau", re.compile(r"\bplateau", re.I)),
)
GATE_TAG = re.compile(r"\[(PRESERVE|ACHIEVE)\] ?")
FENCE = re.compile(r"^\s*(```|~~~)")


class StripError(Exception):
    pass


def usage(code):
    stream = sys.stdout if code == 0 else sys.stderr
    stream.write("usage: strip_criteria.py IN OUT [--allow-term TERM ...]\n")
    if code == 0:
        stream.write(__doc__.split("\n", 2)[2])
    sys.exit(code)


def strip(text):
    """Return (stripped text, lines removed, source line number of each kept line)."""
    lines = text.splitlines()
    kept = []
    removed = 0
    loop_block_start = None
    in_removed_section = False
    in_fence = False
    seen_heading = False
    for number, line in enumerate(lines, 1):
        if loop_block_start is not None:
            removed += 1
            if LOOP_ONLY_END in line:
                loop_block_start = None
            continue
        if LOOP_ONLY_START in line:
            removed += 1
            if LOOP_ONLY_END not in line:
                loop_block_start = number
            continue
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            in_removed_section = any(line.rstrip() == h or line.startswith(h + " ") for h in REMOVED_SECTIONS)
        if in_removed_section:
            removed += 1
            continue
        in_header = not seen_heading
        if not in_fence and line.startswith("## "):
            seen_heading = True
        if not in_fence and (BOLD_LABEL_LINE.match(line) or (in_header and PLAIN_LABEL_LINE.match(line))):
            removed += 1
            continue
        kept.append((number, GATE_TAG.sub("", line)))
    if loop_block_start is not None:
        raise StripError("unterminated %s starting at line %d: add %s where the block ends"
                         % (LOOP_ONLY_START, loop_block_start, LOOP_ONLY_END))
    collapsed = []
    blank_run = 0
    for number, line in kept:
        if line.strip() == "":
            blank_run += 1
            if blank_run > 2:
                removed += 1
                continue
        else:
            blank_run = 0
        collapsed.append((number, line))
    while collapsed and collapsed[-1][1].strip() == "":
        collapsed.pop()
    return "\n".join(line for _, line in collapsed) + "\n", removed, [number for number, _ in collapsed]


def check(text, sources, allowed):
    """(term, source line number, line) of the first forbidden term, or None."""
    for index, line in enumerate(text.splitlines()):
        for label, pattern in RESIDUE + tuple(p for p in ALLOWABLE if p[0] not in allowed):
            if pattern.search(line):
                return label, sources[index] if index < len(sources) else index + 1, line
    return None


def main(argv):
    if len(argv) == 1 and argv[0] in ("-h", "--help"):
        usage(0)
    positional = []
    allowed = set()
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--allow-term":
            if index + 1 >= len(argv):
                usage(2)
            term = next((label for label, _ in ALLOWABLE if label.lower() == argv[index + 1].lower()), None)
            if term is None:
                sys.stderr.write("strip_criteria: --allow-term takes one of: %s (got %r)\n"
                                 % (", ".join(label for label, _ in ALLOWABLE), argv[index + 1]))
                return 2
            allowed.add(term)
            index += 2
            continue
        positional.append(arg)
        index += 1
    if len(positional) != 2:
        usage(2)
    source, target = positional
    try:
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        sys.stderr.write("strip_criteria: cannot read %s (%s)\n" % (source, error))
        return 2
    except ValueError as error:
        sys.stderr.write("strip_criteria: %s is not UTF-8 text (%s)\n" % (source, error))
        return 2
    try:
        result, removed, sources = strip(text)
    except StripError as error:
        sys.stderr.write("strip failed: %s; nothing written\n" % error)
        return 4
    found = check(result, sources, allowed)
    if found is not None:
        term, number, line = found
        hint = ("reword it in the source doc, or pass --allow-term \"%s\" if the criterion means the ordinary word"
                % term) if term in [label for label, _ in ALLOWABLE] else "remove it from the scorer-visible part of the source doc"
        sys.stderr.write('strip failed: found "%s" at line %d of %s: %s\n%s; never hand-edit the stripped copy\n'
                         % (term, number, source, line.strip(), hint))
        return 4
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(result)
    except OSError as error:
        sys.stderr.write("strip_criteria: cannot write %s (%s)\n" % (target, error))
        return 2
    print("stripped: %s -> %s (removed %d lines%s)" % (source, target, removed,
                                                      ("; allowed: %s" % ", ".join(sorted(allowed))) if allowed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
