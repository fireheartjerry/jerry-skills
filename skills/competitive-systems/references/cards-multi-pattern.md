# Multi-pattern scanning cards

Covers many patterns checked against much text: log scanners, secret detectors, routing
rules. First confirm that matching, not reading, dominates: time the scan over lines already
in memory and compare it to the full run. If reading dominates, this file does not apply (the
fix is I/O: larger reads, fewer passes, compression). Then classify the patterns: literal,
regex with a required literal, literal-free. Report the three counts; they pick the card.
Mixed sets are the normal case: Aho-Corasick for the literals, the prefilter for the regexes
with a required literal, and the always-run set for the rest, with one pass over each line
feeding all three.

## Aho-Corasick for literal patterns

**Problem shape:** `for line in lines: for p in patterns: if p in line`, or k separate
`re.search` calls per line, where the patterns are fixed strings.

**Precondition test:** Patterns are literals (or reduce to literals), k >= 50, text size far
exceeds total pattern length, and the pattern set is fixed for many lines so the build cost
amortises. Count how many patterns are literals; if fewer than about 70%, the regex cards
below carry most of the work and this card handles only the literal share.

**Complexity:** O(L*k) -> O(L + P + z), L the text length, P the total pattern length, z the
number of matches.

**Sketch:**

```python
from collections import deque

def build(patterns):                       # returns (goto, fail, out)
    goto, out = [{}], [[]]
    for pid, p in enumerate(patterns):
        s = 0
        for ch in p:
            if ch not in goto[s]:
                goto.append({})
                out.append([])
                goto[s][ch] = len(goto) - 1
            s = goto[s][ch]
        out[s].append(pid)
    fail, q = [0] * len(goto), deque(goto[0].values())   # depth-1 nodes fail to root
    while q:                                              # BFS: fail targets are shallower
        r = q.popleft()
        for ch, s in goto[r].items():
            q.append(s)
            f = fail[r]
            while f and ch not in goto[f]:
                f = fail[f]
            fail[s] = goto[f].get(ch, 0)
            out[s] = out[s] + out[fail[s]]
    return goto, fail, out

def scan(text, goto, fail, out):           # yields (end_position, pattern_id)
    s = 0
    for i, ch in enumerate(text):
        while s and ch not in goto[s]:
            s = fail[s]
        s = goto[s].get(ch, 0)
        for pid in out[s]:
            yield i, pid
```

For production, name a library rather than pasting this: Python `pyahocorasick`, Rust
`aho-corasick`, Java has several; Go has none in the standard library. Emit (position,
pattern_id) and let the caller map ids back to patterns.

**Oracle:** Per-pattern `str.find` loop (all occurrences) on a sample of 2000 lines; compare
the sorted match sets (line, pattern_id, position) exactly.

**Traps:**
- Case-insensitivity: casefold both text and patterns, and note that casefolding can change
  string length in some scripts, which shifts positions.
- Word boundaries are not part of the automaton: post-filter matches.
- Overlapping matches, and whether the old code reported all matches or the first.
- bytes versus str: choose one, decode once.
- Patterns that are prefixes of other patterns: the automaton reports both, the old code may
  have reported one.

**Not when:**
- k < 50: a loop or one compiled alternation is fine (Gate 2: 40 patterns over 2,000 lines
  is 80k substring checks, well under any budget).
- Patterns change on every call: the build cost never amortises.
- The "patterns" are true regexes: next card.

## Literal prefilter for regex sets

**Problem shape:** k regexes per line, k in the hundreds or thousands.

**Precondition test:** For each regex, can you extract a literal substring that every match
must contain (the longest literal run not inside an optional group or alternation branch)?
Count the regexes with no such literal: the always-run set. The card applies when the
always-run set is under 20% of k; report its size either way.

**Complexity:** O(L*k*c) -> O(L + hits*c + L*a), c the cost of one regex, a the always-run
count.

**Sketch:**

```python
import re

def required_literal(pattern, flags=0):
    # Conservative: the longest run of plain characters at the top level, outside groups,
    # classes, alternation and quantifiers. Returns "" (always-run) when unsure.
    def longer(run, best):
        return run if len(run) > len(best) else best
    if flags & (re.IGNORECASE | re.VERBOSE) or re.match(r"\(\?[a-zA-Z]*[ix]", pattern):
        return ""                      # IGNORECASE: literal not exact; VERBOSE: spaces are not text
    best, run, depth, in_class, i = "", "", 0, False, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":                 # escape: end the run, skip the whole escape
            nxt, j = pattern[i + 1:i + 2], i + 2
            if nxt in ("x", "u", "U"):     # \xHH, \uHHHH, \UHHHHHHHH: the digits are not text
                j += {"x": 2, "u": 4, "U": 8}[nxt]
            elif nxt == "N" and pattern[j:j + 1] == "{":
                close = pattern.find("}", j)
                j = close + 1 if close != -1 else len(pattern)
            elif nxt.isdigit():            # octal escape or backreference: up to 3 digits
                while j < min(len(pattern), i + 4) and pattern[j].isdigit():
                    j += 1
            best, run, i = longer(run, best), "", j
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            best, run, in_class = longer(run, best), "", True
            if pattern[i + 1:i + 2] == "^":
                i += 1
            if pattern[i + 1:i + 2] == "]":
                i += 1                 # "[]...]" and "[^]...]": a leading "]" is a member
        elif ch == "|":
            if depth == 0:
                return ""              # top-level alternation: nothing is required
            run = ""
        elif ch == "(":
            depth += 1
            best, run = longer(run, best), ""
        elif ch == ")":
            depth -= 1
            run = ""
        elif ch in "*?+{":             # the previous char was optional or repeated
            best, run = longer(run[:-1], best), ""
            if ch == "{":
                close = pattern.find("}", i)
                i = close if close != -1 else len(pattern)
        elif depth == 0 and ch not in ".^$":
            run += ch
        else:
            best, run = longer(run, best), ""
        i += 1
    best = longer(run, best)
    return best if len(best) >= 3 else ""
```

Then: literal -> list of regex ids; Aho-Corasick over the line gives candidate ids; run only
those regexes plus the always-run set; collect matches in pattern_id order to keep the old
output order. Pass the compile flags into `required_literal` so a `re.VERBOSE` or
`re.IGNORECASE` given at compile time lands the pattern in the always-run set too.

**Oracle:** Run all k regexes on 2000 sampled lines, plus lines crafted to match the
always-run set, lines containing a literal but not matching the full regex, lines crafted for
`[]`-leading classes, numeric escapes and verbose patterns, and, for every regex, 50 random strings that match
it (generated by walking the pattern) checked to contain its extracted literal. Match sets
must be equal.

**Traps:**
- Extracting a literal from inside an alternation or optional group is wrong (it is not
  required); the sketch resets on `(`, `|` and quantifiers for that reason.
- Classes that start with `]` (`[]abc]`, `[^]]`): the first `]` is a member, not the end of
  the class; a scanner that ends the class there emits a literal the regex does not require.
- Numeric escapes and backreferences (`\x22`, `\u00e9`, `\U0001F600`, `\N{...}`, `\012`,
  `\1`): the digits belong to the escape. A scanner that skips only the backslash and one
  character glues `22token` together from `\x22token`, a literal no match contains.
- Verbose mode, inline `(?x)` or `re.VERBOSE`: whitespace in the pattern is not text, so any
  extracted literal containing a space is wrong; always-run.
- Flags: with IGNORECASE, casefold the literal and the text, or put the regex in the
  always-run set (the sketch does).
- Anchors and multiline mode change where a literal can appear, not whether it must.
- A literal shorter than 3 characters produces too many candidates to help.
- Any prefilter bug fails open (a dropped match), so the oracle above is not optional.

**Not when:**
- 20% or more of the regexes have no required literal: use a linear-time engine with a
  combined alternation (next card), or rewrite the patterns.

## Combined alternation on a linear-time engine

**Problem shape:** k regexes, mostly without usable literals.

**Precondition test:** A linear-time (DFA or lazy DFA) engine is available and the platform
allows the dependency: RE2, Go `regexp`, Rust `regex` (and its `RegexSet`), Hyperscan.
Python's `re` is a backtracking engine that tries alternatives in order, so a combined
alternation there is still O(k) per position and gives only modest gains.

**Complexity:** O(L*k) -> O(L) per line for the DFA, with compile cost proportional to total
pattern size and a memory cap to watch.

**Sketch:**
1. Build one pattern `(?P<p0>...)|(?P<p1>...)|...`, or use `RegexSet` in Rust, which reports
   which patterns matched without capturing.
2. Patterns using features linear engines reject (backreferences, lookaround) go to the
   always-run set and run separately with the backtracking engine.
3. Map group names back to pattern ids; emit matches in pattern_id order.
4. Check the compiled size against the engine's memory limit; split into several combined
   patterns if it is exceeded.

**Oracle:** Same as the previous card: the full k-regex loop on 2000 sampled lines, equal
match sets.

**Traps:**
- Leftmost-first alternation changes which pattern wins when two match at the same position;
  if the caller needs all matches, use a set-matching API, not a single regex.
- Group count limits: check the engine's (RE2 and Hyperscan have their own; Python `re` has
  had none since 3.5).
- Unsupported features must go to the always-run set, not be silently rewritten.

**Not when:**
- The platform cannot take the dependency and the always-run set is small: Literal prefilter
  with Python `re` is enough.

See also: if the same text is scanned repeatedly with the same patterns, cache the automaton
and results by content hash (`cards-repeated-work.md`).
