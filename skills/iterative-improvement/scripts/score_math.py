#!/usr/bin/env python3
"""Arithmetic for the improvement loop: baseline, noise floor and the stop rules.

usage: score_math.py baseline A B [--json]
       score_math.py decide --threshold T --target X --max-rounds R --scores S0,S1,...
                            [--rounds-used N | --first-round K]
                            [--gates PASS|FAIL] [--confirm-score C] [--json]

baseline A B   A and B are two scorer runs on the same unmodified snapshot. Prints
               baseline=<mean, half up> noise=<|A-B|> threshold=<max(3, 2*noise)>
               soft=<true when noise > 8>. Lite mode skips this and uses T=3.

decide         --scores is the comparable series, first entry the baseline (or the
               rescored reference after a rubric change), last entry the latest
               round. Each entry is "58" or "49/80" (raw/adjusted max); rounds are
               compared on the scaled value round(raw*100/max) and a round whose
               adjusted max differs from the previous one is incomparable (no
               delta). Raw must satisfy 0 <= raw <= max (exit 2 otherwise).

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
               Budget left is never a reason to continue. Exit 0 for every decision,
               2 on bad input.
"""

import argparse
import json
import sys
from decimal import ROUND_HALF_UP, Decimal


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
    if adjusted <= 0:
        fail("adjusted max must be positive: %r" % token)
    if raw < 0:
        fail("score is negative: %r" % token)
    if raw > adjusted:
        fail("score exceeds its adjusted max: %r" % token)
    scaled = round_half_up(raw * 100 / adjusted)
    return {"raw": float(raw), "max": float(adjusted), "scaled": scaled, "na": adjusted < 100}


def cmd_baseline(args):
    try:
        a = Decimal(args.a)
        b = Decimal(args.b)
    except Exception:
        fail("baseline takes two numeric scores, got %r %r" % (args.a, args.b))
    if a < 0 or b < 0 or a > 100 or b > 100:
        fail("baseline scores must be between 0 and 100, got %s %s" % (args.a, args.b))
    noise = abs(a - b)
    result = {
        "baseline": round_half_up((a + b) / 2),
        "noise": as_number(noise),
        "threshold": as_number(max(Decimal(3), 2 * noise)),
        "soft": noise > 8,
    }
    if args.json:
        print(json.dumps(result))
    else:
        print("baseline=%s noise=%s threshold=%s soft=%s" % (
            result["baseline"], result["noise"], result["threshold"], "true" if result["soft"] else "false"))
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
    if args.threshold < 1:
        fail("--threshold must be at least 1")
    if args.max_rounds < 1:
        fail("--max-rounds must be at least 1")
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
    outcome = decide(scores, args.threshold, args.target, args.max_rounds, rounds_used, gates_pass, args.confirm_score)
    if args.json:
        print(json.dumps(outcome))
    else:
        print("%s %s" % (outcome["decision"], outcome["reason"]))
        print("detail: %s" % outcome["detail"])
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="score_math.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    baseline = sub.add_parser("baseline", help="mean, noise floor and move threshold from two runs")
    baseline.add_argument("a")
    baseline.add_argument("b")
    baseline.add_argument("--json", action="store_true")
    baseline.set_defaults(func=cmd_baseline)

    decision = sub.add_parser("decide", help="apply the stop rules to a score series")
    decision.add_argument("--threshold", type=float, required=True, help="move threshold T")
    decision.add_argument("--target", type=float, required=True, help="target score out of 100")
    decision.add_argument("--max-rounds", type=int, required=True, help="round budget")
    decision.add_argument("--scores", required=True, help="comma-separated comparable series, reference first")
    decision.add_argument("--rounds-used", type=int, default=None,
                          help="real round number of the last entry (required after a rubric amendment)")
    decision.add_argument("--first-round", type=int, default=None,
                          help="real round number of the first entry (alternative to --rounds-used; default 0)")
    decision.add_argument("--gates", default="PASS", choices=["PASS", "FAIL", "pass", "fail"],
                          help="PASS when every gate passes in the latest round (from the orchestrator's gate runs)")
    decision.add_argument("--confirm-score", type=float, default=None,
                          help="the second scorer run on the latest snapshot, after CONFIRM")
    decision.add_argument("--json", action="store_true")
    decision.set_defaults(func=cmd_decide)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "decide":
        for name in ("threshold", "target"):
            value = getattr(args, name)
            if value == int(value):
                setattr(args, name, int(value))
        if args.confirm_score is not None and args.confirm_score == int(args.confirm_score):
            args.confirm_score = int(args.confirm_score)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
