#!/usr/bin/env python3
"""Arithmetic for the improvement loop: baseline, noise floor, budget and the stop rules.

usage: score_math.py baseline A B [C] [--json]
       score_math.py budget --total X --spent Y --per-round Z [--rounds R]
                            [--rounds-used N] [--json]
       score_math.py decide --threshold T --target X --max-rounds R --scores S0,S1,...
                            --gates PASS|FAIL [--rounds-used N | --first-round K]
                            [--confirm-score C] [--json]
       score_math.py check-criteria DOC [--mode lite|full]
       score_math.py check-scorer OUT [--criteria DOC]

baseline A B   Two scorer runs on the same unmodified snapshot and evidence. Prints
               baseline=<mean, half up> noise=<|A-B|> threshold=<max(3, 2*noise)>
               soft=false third-run=<needed|no>. A third run is needed when the two
               agree exactly (no information about spread) or differ by more than 8
               (one run may be aberrant); softness is judged only once the third run
               is in. Lite mode skips this and uses T=3.

baseline A B C Three runs. Sorted lo <= mid <= hi with gaps g1 = mid-lo, g2 = hi-mid:
                 range <= 8                   mean of three, noise = range
                 one end run is more than 8   that run is the outlier; baseline and
                 from its neighbour and the   noise come from the other two; log it.
                 other gap is 8 or less       soft=true when it sits more than 20 away
                                              (the instrument disagreed badly once)
                 otherwise (both gaps over 8, mean of three, noise = range, soft=true:
                 or both 8 or less with a     tighten the criteria and rebaseline once
                 range over 8)
               Line 1 ends third-run=done outlier=<value|none>; a detail line follows
               when there is an outlier or the check lines are soft. A rebaseline is
               allowed once; if the second set is soft too, proceed with the T printed
               and tell the user the instrument is noisy.

budget         Units are the caller's (minutes or dollars) and must be consistent.
               remaining = total - spent (may be negative); rounds-affordable =
               max(0, floor(remaining / per-round)), the rounds still to come;
               max-rounds = rounds-used + rounds-affordable, capped at --rounds when
               given. max-rounds is a total ceiling on the same scale as decide's
               rounds used, so pass it to decide as --max-rounds unchanged (decide
               then says STOP rounds-exhausted when no round fits).
               --rounds-used is the number of real rounds already run (default 0,
               right only before round 1). Exit 2 when --total or --per-round is not
               positive, or --spent or --rounds-used is negative.

decide         --scores is the comparable series, first entry the baseline (or the
               rescored reference after a rubric change), last entry the latest
               round. Each entry is "58" or "49/80" (raw/adjusted max); rounds are
               compared on the scaled value round(raw*100/max) and a round whose
               adjusted max differs from the previous one is incomparable (no
               delta). Raw must satisfy 0 <= raw <= max (exit 2 otherwise).
               --max-rounds is the total round ceiling: the one the user agreed to,
               or budget's max-rounds (already capped by that ceiling). --target is
               0 to 100; --confirm-score takes the same forms as --scores and must
               have the same adjusted max as the last entry.

               Rounds used is NOT the series length. After a rubric amendment the
               series restarts from the rescored reference, so pass --rounds-used N
               (the real round number of the last entry) or --first-round K (the
               real round number of the first entry; rounds used = K + len - 1).
               Default --first-round 0, which is only right before any amendment.

               Rules, first match wins:
                 1 gates FAIL and rounds used >= max   HALT gate-fail-rounds-exhausted
                 2 last delta <= -T, or the last two
                   deltas sum to <= -T                 STOP regression
                 3 scaled >= target, no N/A, gates PASS
                     no --confirm-score                CONFIRM target-reached
                     mean(last, C) >= target           STOP target-hit
                     otherwise                         CONTINUE target-unconfirmed
                 4 rounds used >= max                  STOP rounds-exhausted
                 5 same adjusted max < 100 twice       BLOCKED na-repeated
                 6 last two deltas each inside T
                   (|d| < T) and together <= T         STOP plateau
                 7 otherwise                           CONTINUE
               Two consecutive deltas inside T whose total exceeds T are a slow real
               trend (reported as such) and the loop continues. A single drop smaller
               than T is reported as within-noise, exactly like a rise smaller than T.
               Budget left is never a reason to continue. --gates is required: a
               forgotten flag must not turn a HALT into a STOP. Exit 0 for every
               decision, 2 on bad input (including nan and infinite values).

check-criteria Lints a criteria doc before round 0: criteria headed "### C<n>. <name>
               (<w> pts)", optionally followed by "[ASSUMED: ...]" tags, with weights
               summing to 100; 3 to 5 criteria in lite, 6 to 12 in full (--mode, else
               the doc's Mode label, else 3 to 12); each with a "How to check it" line
               and numeric anchors (bullets "- `0`: ...", or inline on the Anchors
               line) whose first is 0 and last is the weight; every gate line "- **G<n>.**" carries "check:"; a DERIVED doc
               has a "Source:" line per criterion. Prints "ok" or one line per problem
               and exits 3.

check-scorer   Checks a scorer's output before decide reads it: the points of the
               scored criteria sum to the Score line, their maxes to its adjusted max,
               every max sums to 100, Scaled = round(points * 100 / max), and each N/A
               criterion is listed under Unscoreable. With --criteria, each max must
               equal that criterion's weight in the doc. Formatting habits are read as
               the brief's format: emphasis and code ticks, any heading level, a
               trailing "pts" or "points", "30/50" in a points cell whose max is 50,
               and "/100" after Scaled. Prints the series token
               ("49/80", or "58" at max 100), the N/A list and the gate results; exits
               3 with one line per problem when the arithmetic does not hold.
"""

import argparse
import json
import re
import sys
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation


def round_half_up(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fail(message):
    sys.stderr.write("score_math: %s\n" % message)
    sys.exit(2)


def as_number(value):
    """Render a Decimal or float as an int when it is whole."""
    value = Decimal(str(value))
    return int(value) if value == value.to_integral_value() else float(value)


def parse_score(token):
    token = token.strip()
    if not token:
        fail("empty score in --scores")
    if "/" in token:
        raw_text, max_text = token.split("/", 1)
    else:
        raw_text, max_text = token, "100"
    try:
        raw = Decimal(raw_text)
        adjusted = Decimal(max_text)
    except Exception:
        fail("score is not numeric: %r" % token)
    if not raw.is_finite() or not adjusted.is_finite():
        fail("score is not a finite number: %r" % token)
    if adjusted <= 0:
        fail("adjusted max must be positive: %r" % token)
    if raw < 0:
        fail("score is negative: %r" % token)
    if raw > adjusted:
        fail("score exceeds its adjusted max: %r" % token)
    scaled = round_half_up(raw * 100 / adjusted)
    return {"raw": float(raw), "max": float(adjusted), "scaled": scaled, "na": adjusted < 100}


def fmt(value):
    """Render a Decimal for text output: whole numbers without a decimal point."""
    value = Decimal(str(value))
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def parse_decimal(text, what):
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        fail("%s is not numeric: %r" % (what, text))
    if not value.is_finite():
        fail("%s is not numeric: %r" % (what, text))
    return value


THIRD_RUN_ZERO = ("two identical runs say nothing about spread; run the scorer a third time "
                  "on the same snapshot and evidence and pass all three runs")
THIRD_RUN_WIDE = ("two runs %s apart may be one aberrant run or soft check lines; run a third "
                  "on the same snapshot and evidence before rewriting criteria")
OUTLIER_DETAIL = ("run %s is an outlier, %s from the nearest run, against %s and %s; log it in the score log, "
                  "diff its per-criterion table against the others and tighten the criteria where it disagrees")
OUTLIER_FAR = ("; it sits more than 20 away, so the check lines are soft: tighten them and rebaseline once")
SOFT_DETAIL = ("no single outlier: the three runs spread over %s (gaps %s and %s), more than 8; "
               "the check lines are soft: tighten the criteria with the largest per-criterion "
               "disagreement and take two fresh baseline runs, once; if those are soft too, proceed with this T "
               "and tell the user the instrument is noisy")


def baseline_two(a, b):
    noise = abs(a - b)
    detail = None
    if noise == 0:
        third_run, detail = "needed", THIRD_RUN_ZERO
    elif noise > 8:
        third_run, detail = "needed", THIRD_RUN_WIDE % fmt(noise)
    else:
        third_run = "no"
    return {
        "baseline": round_half_up((a + b) / 2),
        "noise": as_number(noise),
        "threshold": as_number(max(Decimal(3), 2 * noise)),
        "soft": False,
        "third_run": third_run,
        "outlier": None,
        "detail": detail,
    }


def baseline_three(runs):
    lo, mid, hi = sorted(runs)
    g1, g2 = mid - lo, hi - mid
    spread = hi - lo
    outlier = None
    detail = None
    if spread <= 8:
        kept = [lo, mid, hi]
        noise = spread
        soft = False
    elif (g1 > 8) != (g2 > 8):
        # Exactly one end run is more than 8 from its neighbour: it is the outlier, and
        # no other run shares its value; the kept pair stays in input order.
        outlier = lo if g1 > 8 else hi
        distance = max(g1, g2)
        kept = [r for r in runs if r != outlier]
        noise = min(g1, g2)
        soft = distance > 20
        detail = OUTLIER_DETAIL % (fmt(outlier), fmt(distance), fmt(kept[0]), fmt(kept[1]))
        if soft:
            detail += OUTLIER_FAR
    else:
        kept = [lo, mid, hi]
        noise = spread
        soft = True
        detail = SOFT_DETAIL % (fmt(spread), fmt(g1), fmt(g2))
    return {
        "baseline": round_half_up(sum(kept) / len(kept)),
        "noise": as_number(noise),
        "threshold": as_number(max(Decimal(3), 2 * noise)),
        "soft": soft,
        "third_run": "done",
        "outlier": None if outlier is None else as_number(outlier),
        "detail": detail,
    }


def cmd_baseline(args):
    if len(args.runs) not in (2, 3):
        fail("baseline takes two or three runs, got %d" % len(args.runs))
    runs = []
    for token in args.runs:
        value = parse_decimal(token, "baseline score")
        if value < 0 or value > 100:
            fail("baseline scores must be between 0 and 100, got %s" % token)
        runs.append(value)
    if len(runs) == 2:
        result = baseline_two(runs[0], runs[1])
    else:
        result = baseline_three(runs)
    result["runs"] = [as_number(r) for r in runs]
    if args.json:
        print(json.dumps(result))
        return 0
    line = "baseline=%s noise=%s threshold=%s soft=%s third-run=%s" % (
        result["baseline"], result["noise"], result["threshold"],
        "true" if result["soft"] else "false", result["third_run"])
    if result["third_run"] == "done":
        line += " outlier=%s" % ("none" if result["outlier"] is None else result["outlier"])
    print(line)
    if result["detail"]:
        print("detail: %s" % result["detail"])
    return 0


def cmd_budget(args):
    total = parse_decimal(args.total, "--total")
    spent = parse_decimal(args.spent, "--spent")
    per_round = parse_decimal(args.per_round, "--per-round")
    if total <= 0:
        fail("--total must be positive, got %s" % args.total)
    if spent < 0:
        fail("--spent must be 0 or more, got %s" % args.spent)
    if per_round <= 0:
        fail("--per-round must be positive, got %s" % args.per_round)
    if args.rounds is not None and args.rounds < 1:
        fail("--rounds must be at least 1")
    used = args.rounds_used
    if used < 0:
        fail("--rounds-used must be 0 or more")
    remaining = total - spent
    affordable = int((remaining / per_round).to_integral_value(rounding=ROUND_FLOOR))
    affordable = max(0, affordable)
    max_rounds = used + affordable
    if args.rounds is not None:
        max_rounds = min(max_rounds, args.rounds)
    if affordable > 0 and max_rounds > used:
        detail = "a round costs %s; %d more round%s fit%s in the remaining %s; %d run so far; pass --max-rounds %d to decide" % (
            fmt(per_round), affordable, "" if affordable == 1 else "s", "s" if affordable == 1 else "",
            fmt(remaining), used, max_rounds)
    elif affordable > 0:
        detail = "the round ceiling %d is reached (%d run); stop and report" % (max_rounds, used)
    else:
        detail = "the next round would exceed the budget; stop now and report"
    result = {
        "remaining": as_number(remaining),
        "rounds_used": used,
        "rounds_affordable": affordable,
        "max_rounds": max_rounds,
        "per_round": as_number(per_round),
        "detail": detail,
    }
    if args.json:
        print(json.dumps(result))
    else:
        print("remaining=%s rounds-used=%d rounds-affordable=%d max-rounds=%d" % (fmt(remaining), used, affordable, max_rounds))
        print("detail: %s" % detail)
    return 0


def decide(scores, threshold, target, max_rounds, rounds_used, gates_pass, confirm):
    scaled = [s["scaled"] for s in scores]
    deltas = []
    for index in range(1, len(scores)):
        if scores[index]["max"] != scores[index - 1]["max"]:
            deltas.append(None)
        else:
            deltas.append(scaled[index] - scaled[index - 1])
    last = scores[-1]
    last_delta = deltas[-1] if deltas else None
    prev_delta = deltas[-2] if len(deltas) >= 2 else None
    two_quiet_rounds = (last_delta is not None and prev_delta is not None
                        and abs(prev_delta) < threshold and abs(last_delta) < threshold)
    budget = "%d of %d rounds used" % (rounds_used, max_rounds)

    def result(decision, reason, detail):
        return {"decision": decision, "reason": reason, "detail": detail, "scaled": scaled,
                "deltas": deltas, "rounds_used": rounds_used, "max_rounds": max_rounds}

    if not gates_pass and rounds_used >= max_rounds:
        return result("HALT", "gate-fail-rounds-exhausted",
                      "a gate still fails after %s; report it, do not extend the budget" % budget)
    if last_delta is not None and last_delta <= -threshold:
        return result("STOP", "regression",
                      "last delta %+d is at or beyond -T (T=%s); diff per-criterion scores, revert or explain" % (last_delta, threshold))
    if last_delta is not None and prev_delta is not None and prev_delta + last_delta <= -threshold:
        return result("STOP", "regression",
                      "last two deltas (%+d, %+d) sum to %+d, at or beyond -T (T=%s); diff per-criterion scores, revert or explain"
                      % (prev_delta, last_delta, prev_delta + last_delta, threshold))
    target_by_number = last["scaled"] >= target
    if target_by_number and not last["na"] and gates_pass:
        if confirm is None:
            return result("CONFIRM", "target-reached",
                          "run the scorer once more on the same snapshot and pass --confirm-score")
        mean = round_half_up((Decimal(str(last["scaled"])) + Decimal(str(confirm))) / 2)
        if mean >= target:
            return result("STOP", "target-hit",
                          "scores %s and %s on the same snapshot, mean %s >= target %s" % (last["scaled"], confirm, mean, target))
        return result("CONTINUE", "target-unconfirmed",
                      "confirmation run scored %s; mean %s is below target %s; %s" % (confirm, mean, target, budget))
    if rounds_used >= max_rounds:
        return result("STOP", "rounds-exhausted", budget)
    if len(scores) >= 2 and last["na"] and scores[-2]["na"] and last["max"] == scores[-2]["max"]:
        return result("BLOCKED", "na-repeated",
                      "same adjusted max %s two rounds running; produce evidence or amend" % as_number(last["max"]))
    if two_quiet_rounds and prev_delta + last_delta <= threshold:
        return result("STOP", "plateau",
                      "last two deltas %+d and %+d are both inside T=%s and total %+d, not more than T; "
                      "stop even with budget left and tell the user what is left"
                      % (prev_delta, last_delta, threshold, prev_delta + last_delta))
    if last_delta is None:
        detail = "incomparable: adjusted max changed, no delta" if len(scores) > 1 else "baseline only"
    elif two_quiet_rounds:
        detail = ("slow real trend: %+d and %+d are each inside T=%s but add up to %+d, beyond T"
                  % (prev_delta, last_delta, threshold, prev_delta + last_delta))
    elif abs(last_delta) < threshold:
        detail = "within-noise: %+d against T=%s" % (last_delta, threshold)
    else:
        detail = "%+d" % last_delta
    detail += "; " + budget
    if target_by_number and not gates_pass:
        detail += "; score meets target but gates FAIL, so target is not reached"
    elif target_by_number and last["na"]:
        detail += "; score meets target but a criterion is N/A, so target is not reached"
    return result("CONTINUE", "continue", detail)


def cmd_decide(args):
    for name in ("threshold", "target"):
        value = getattr(args, name)
        if value != value or value in (float("inf"), float("-inf")):
            fail("--%s must be a finite number" % name)
    if args.threshold < 1:
        fail("--threshold must be at least 1")
    if args.max_rounds < 0:
        fail("--max-rounds must be 0 or more")
    if not 0 <= args.target <= 100:
        fail("--target must be between 0 and 100, got %s" % as_number(args.target))
    if args.rounds_used is not None and args.first_round is not None:
        fail("pass --rounds-used or --first-round, not both")
    tokens = [t for t in args.scores.split(",")]
    if not tokens or tokens == [""]:
        fail("--scores is empty")
    scores = [parse_score(t) for t in tokens]
    if args.rounds_used is not None:
        if args.rounds_used < 0:
            fail("--rounds-used must be 0 or more")
        if args.rounds_used < len(scores) - 1:
            fail("--rounds-used %d is smaller than the %d rounds in --scores" % (args.rounds_used, len(scores) - 1))
        rounds_used = args.rounds_used
    else:
        first = args.first_round if args.first_round is not None else 0
        if first < 0:
            fail("--first-round must be 0 or more")
        rounds_used = first + len(scores) - 1
    gates_pass = args.gates.upper() == "PASS"
    confirm = None
    if args.confirm_score is not None:
        parsed = parse_score(args.confirm_score)
        if parsed["max"] != scores[-1]["max"]:
            fail("--confirm-score %s has a different adjusted max from the last score %s; "
                 "the confirmation run must grade the same snapshot and evidence"
                 % (args.confirm_score, tokens[-1].strip()))
        confirm = parsed["scaled"]
    outcome = decide(scores, args.threshold, args.target, args.max_rounds, rounds_used, gates_pass, confirm)
    if args.json:
        print(json.dumps(outcome))
    else:
        print("%s %s" % (outcome["decision"], outcome["reason"]))
        print("detail: %s" % outcome["detail"])
    return 0


# A DERIVED doc may tag the heading with one or more "[ASSUMED: ...]" notes after "(w pts)".
CRITERION_HEAD = re.compile(r"^### (C\d+)\. (.*?)\((\d+) pts\)(?:\s*\[[^\]\n]*\])*\s*$", re.M)
GATE_LINE = re.compile(r"^\s*[-*] \*\*(G\d+)\.\*\*(.*)$", re.M)
ANCHOR = re.compile(r"^\s*- `([^`]+)`:", re.M)
INLINE_ANCHORS = re.compile(r"^\*\*Anchors:\*\*(.*)$", re.M)


def anchors_of(block):
    """Anchor labels of one criterion: the bullet form, else the inline form
    "**Anchors:** `0`: ... `6`: ... `12`: ..." on the Anchors line itself."""
    found = ANCHOR.findall(block)
    if found:
        return found
    inline = INLINE_ANCHORS.search(block)
    return re.findall(r"`([^`]+)`:", inline.group(1)) if inline else []


def criteria_blocks(text):
    """[(id, name, weight, block text)] for each "### C<n>. name (w pts)" criterion."""
    heads = list(CRITERION_HEAD.finditer(text))
    blocks = []
    for index, match in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        following = re.search(r"^## ", text[match.end():end], re.M)
        if following:
            end = match.end() + following.start()
        blocks.append((match.group(1), match.group(2).strip(), int(match.group(3)), text[match.start():end]))
    return blocks


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, ValueError) as error:
        fail("cannot read %s (%s)" % (path, error))


def cmd_check_criteria(args):
    text = read_text(args.doc)
    mode = args.mode
    if mode is None:
        label = re.search(r"\*\*Mode:\*\*\s*(\w+)", text)
        mode = label.group(1).lower() if label and label.group(1).lower() in ("lite", "full") else None
    bounds = {"lite": (3, 5), "full": (6, 12), None: (3, 12)}[mode]
    blocks = criteria_blocks(text)
    problems = []
    if not blocks:
        problems.append("no criteria found (each needs a heading like '### C1. Name (20 pts)')")
    total = sum(weight for _, _, weight, _ in blocks)
    if blocks and total != 100:
        problems.append("weights sum to %d, not 100" % total)
    if blocks and not bounds[0] <= len(blocks) <= bounds[1]:
        problems.append("%d criteria; %s mode takes %d to %d" % (len(blocks), mode or "either", bounds[0], bounds[1]))
    seen = set()
    derived = re.search(r"\*\*Status:\*\*\s*DERIVED", text) is not None
    for cid, name, weight, block in blocks:
        if cid in seen:
            problems.append("%s appears twice" % cid)
        seen.add(cid)
        if "**How to check it:**" not in block:
            problems.append("%s has no **How to check it:** line" % cid)
        anchors = anchors_of(block)
        numeric = [a for a in anchors if re.fullmatch(r"\d+(?:\.\d+)?", a.strip())]
        if len(anchors) < 3:
            problems.append("%s has %d anchors; it needs 0, a middle one and %d" % (cid, len(anchors), weight))
        elif len(numeric) != len(anchors):
            problems.append("%s anchors must be numbers (0, a middle value, %d), not %s"
                            % (cid, weight, ", ".join(a for a in anchors if a not in numeric)))
        else:
            if Decimal(numeric[0]) != 0:
                problems.append("%s first anchor is %s, not 0" % (cid, numeric[0]))
            if Decimal(numeric[-1]) != weight:
                problems.append("%s last anchor is %s, not its weight %d" % (cid, numeric[-1], weight))
        if derived and "**Source:**" not in block:
            problems.append("%s has no **Source:** line (the doc is DERIVED)" % cid)
    for gid, rest in GATE_LINE.findall(text):
        if "check:" not in rest:
            problems.append("%s has no check: command" % gid)
    if problems:
        for line in problems:
            print("problem: %s" % line)
        return 3
    print("ok: %d criteria summing to 100 (%s mode), %d gates" % (len(blocks), mode or "unstated",
                                                                   len(GATE_LINE.findall(text))))
    return 0


def section(text, heading):
    """The body under a heading of any level, up to the next heading."""
    match = re.search(r"^#{1,6}[ \t]*%s[ \t]*$(.*?)(?=^#{1,6}[ \t]|\Z)" % re.escape(heading), text, re.M | re.S)
    return match.group(1) if match else None


def plain(cell):
    """A cell or heading without markdown emphasis or code ticks: "**30**" -> "30"."""
    return re.sub(r"[*`]", "", cell).strip()


def table(block):
    rows = []
    for line in (block or "").splitlines():
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|[-\s|:]+\|$", line):
            continue
        rows.append([plain(cell) for cell in line.strip("|").split("|")])
    return rows[1:] if rows else []


UNIT = re.compile(r"\s*(?:pts?|points?)\.?$", re.I)


def plain_text(text):
    """Headings without emphasis or ticks; the Score and Scaled lines at any heading
    level read as "##", without a trailing "points" or "pts" or a "/100" on Scaled."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*#{1,6}[ \t]", line):
            line = plain(line)
        if re.match(r"^\s*#{1,6}[ \t]*(?:Score|Scaled):", line):
            line = re.sub(r"^\s*#{1,6}[ \t]*", "## ", line)
            line = UNIT.sub("", line)
            line = re.sub(r"^(## Scaled:\s*\d+)\s*/\s*100$", r"\1", line)
        lines.append(line)
    return "\n".join(lines)


def points_cell(cell, maximum):
    """The points in a Points cell: "30", "30 pts" or "30/50" when 50 is the row's max."""
    cell = UNIT.sub("", cell)
    over = re.fullmatch(r"(.+?)\s*/\s*(\d+)", cell)
    if over and int(over.group(2)) == maximum:
        return over.group(1).strip()
    return cell


def cmd_check_scorer(args):
    text = plain_text(read_text(args.out))
    problems = []
    head = re.search(r"^## Score:\s*(\d+(?:\.\d+)?)\s*/\s*(\d+)\s*$", text, re.M)
    scaled_line = re.search(r"^## Scaled:\s*(\d+)\s*$", text, re.M)
    if not head:
        fail("no '## Score: <points>/<adjusted max>' line in %s" % args.out)
    points, adjusted = Decimal(head.group(1)), int(head.group(2))
    rows = [r for r in table(section(text, "Per-criterion")) if r and re.fullmatch(r"C\d+", r[0])]
    if not rows:
        problems.append("no per-criterion rows")
    scored_points = Decimal(0)
    scored_max = 0
    all_max = 0
    na = []
    for row in rows:
        if len(row) < 4:
            problems.append("%s row has %d cells, expected 5" % (row[0], len(row)))
            continue
        try:
            maximum = int(UNIT.sub("", row[3]))
        except ValueError:
            problems.append("%s max is not a whole number: %r" % (row[0], row[3]))
            continue
        all_max += maximum
        row[2] = points_cell(row[2], maximum)
        row[3] = str(maximum)
        if row[2].upper() == "N/A":
            na.append(row[0])
            continue
        try:
            value = Decimal(row[2])
        except InvalidOperation:
            problems.append("%s points are not a number: %r" % (row[0], row[2]))
            continue
        if not 0 <= value <= maximum:
            problems.append("%s scores %s outside 0 to %d" % (row[0], row[2], maximum))
        scored_points += value
        scored_max += maximum
    if rows and scored_points != points:
        problems.append("per-criterion points sum to %s, but the Score line says %s" % (fmt(scored_points), fmt(points)))
    if rows and scored_max != adjusted:
        problems.append("maxes of the scored criteria sum to %d, but the adjusted max is %d" % (scored_max, adjusted))
    if rows and all_max != 100:
        problems.append("every criterion's max sums to %d, not 100" % all_max)
    if adjusted <= 0:
        problems.append("adjusted max is 0: nothing was scored")
    elif scaled_line is None:
        problems.append("no '## Scaled:' line")
    elif int(scaled_line.group(1)) != round_half_up(points * 100 / adjusted):
        problems.append("Scaled is %s, but round(%s * 100 / %d) is %d"
                        % (scaled_line.group(1), fmt(points), adjusted, round_half_up(points * 100 / adjusted)))
    unscoreable = section(text, "Unscoreable") or ""
    for cid in na:
        if not re.search(r"\b%s\b" % cid, unscoreable):
            problems.append("%s is N/A but not listed under ## Unscoreable" % cid)
    if args.criteria:
        weights = dict((cid, weight) for cid, _, weight, _ in criteria_blocks(read_text(args.criteria)))
        for row in rows:
            if len(row) >= 4 and row[0] in weights and row[3].isdigit() and int(row[3]) != weights[row[0]]:
                problems.append("%s max is %s, but its weight in the criteria doc is %d" % (row[0], row[3], weights[row[0]]))
        missing = sorted(set(weights) - set(r[0] for r in rows))
        if missing:
            problems.append("criteria missing from the output: %s" % ", ".join(missing))
    gates = [(r[0], r[1].upper()) for r in table(section(text, "Gates")) if len(r) >= 2 and re.fullmatch(r"G\d+", r[0])]
    if problems:
        for line in problems:
            print("problem: %s" % line)
        return 3
    token = fmt(points) if adjusted == 100 else "%s/%d" % (fmt(points), adjusted)
    print("token=%s scaled=%s na=%s gates=%s" % (token, scaled_line.group(1), ",".join(na) or "none",
                                                 ",".join("%s:%s" % g for g in gates) or "none"))
    if any(result != "PASS" for _, result in gates):
        print("detail: a gate is not PASS in the scorer's table; compare with your own gate runs before passing --gates")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="score_math.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    baseline = sub.add_parser("baseline", help="mean, noise floor and move threshold from two or three runs")
    baseline.add_argument("runs", nargs="+", metavar="RUN", help="two or three scorer runs, each 0 to 100")
    baseline.add_argument("--json", action="store_true")
    baseline.set_defaults(func=cmd_baseline)

    budget = sub.add_parser("budget", help="rounds that fit in the remaining budget")
    budget.add_argument("--total", required=True, help="total budget in the caller's unit (minutes or dollars)")
    budget.add_argument("--spent", required=True, help="amount already spent, same unit")
    budget.add_argument("--per-round", required=True, help="measured or estimated cost of one round, same unit")
    budget.add_argument("--rounds", type=int, default=None, help="the total round ceiling the user agreed to")
    budget.add_argument("--rounds-used", type=int, default=0,
                        help="real rounds already run (default 0); max-rounds counts them")
    budget.add_argument("--json", action="store_true")
    budget.set_defaults(func=cmd_budget)

    decision = sub.add_parser("decide", help="apply the stop rules to a score series")
    decision.add_argument("--threshold", type=float, required=True, help="move threshold T")
    decision.add_argument("--target", type=float, required=True, help="target score out of 100")
    decision.add_argument("--max-rounds", type=int, required=True, help="round budget")
    decision.add_argument("--scores", required=True, help="comma-separated comparable series, reference first")
    decision.add_argument("--rounds-used", type=int, default=None,
                          help="real round number of the last entry (required after a rubric amendment)")
    decision.add_argument("--first-round", type=int, default=None,
                          help="real round number of the first entry (alternative to --rounds-used; default 0)")
    decision.add_argument("--gates", required=True, choices=["PASS", "FAIL", "pass", "fail"],
                          help="PASS when every gate passes in the latest round (from the orchestrator's gate runs)")
    decision.add_argument("--confirm-score", default=None,
                          help="the second scorer run on the latest snapshot, after CONFIRM (\"86\" or \"69/80\")")
    decision.add_argument("--json", action="store_true")
    decision.set_defaults(func=cmd_decide)

    lint = sub.add_parser("check-criteria", help="lint a criteria doc: weights, anchors, check lines, gates")
    lint.add_argument("doc", help="the criteria doc")
    lint.add_argument("--mode", choices=("lite", "full"), default=None, help="lite (3 to 5 criteria) or full (6 to 12)")
    lint.set_defaults(func=cmd_check_criteria)

    scorer = sub.add_parser("check-scorer", help="check a scorer output's arithmetic and print its series token")
    scorer.add_argument("out", help="the scorer's output file")
    scorer.add_argument("--criteria", default=None, help="the criteria doc, to check each max against its weight")
    scorer.set_defaults(func=cmd_check_scorer)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "decide":
        for name in ("threshold", "target"):
            value = getattr(args, name)
            if value == value and value not in (float("inf"), float("-inf")) and value == int(value):
                setattr(args, name, int(value))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
