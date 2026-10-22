#!/usr/bin/env python3
"""Create the loop's private state, and say where a loop stands when you come back to it.

usage: loop.py init   --root ROOT --slug SLUG [--mode lite|full] [--scope REL ...]
                     [--rounds R] [--commits yes|no] [--budget TEXT] [--engines TEXT]
                     [--off-limits PATH ...]
       loop.py status --loop LOOP [--criteria DOC]

init creates LOOP where SKILL.md section 3 puts it: in a git repo
$(git rev-parse --absolute-git-dir)/iterative-improvement/SLUG/, outside one
ROOT/.iterative-improvement/SLUG/, so it is never inside the tracked tree. It makes
prompts/, reviews/, scores/, evidence/, snapshots/ and tools/, and writes rounds.md
with the mode, the kickoff time (UTC, as date -u gives it), ROOT, the scope and, in a
repo, HEAD and `git status --short -- <scope>`. It also records what the user decided,
so a resumed loop reads it from disk rather than from chat: the round ceiling (default
2 in lite, 5 in full), whether commits are allowed (default no), the time or money
budget, the engines with their vendors and the child-run cap, and the off-limits
paths. A dirty scope is printed as a warning:
round 0 then differs from HEAD, so either ask the user to commit first or write gates
against the round-0 snapshot instead of a SHA. Prints the LOOP path. Exit 2 when ROOT
is missing, SLUG is not lowercase letters, digits and hyphens, or LOOP already holds a
rounds.md (run status instead).

status reads LOOP (and the criteria doc: --criteria, or the one *-criteria.md in LOOP)
and prints: mode and kickoff; the snapshots, evidence and view dirs, reviews and
scores present; the scorer pin (scores/round-0a.cmd, else round-0.cmd) and every
scorer .cmd that differs from it in anything but the CLI's patch version (an engine
update from 2.1.306 to 2.1.308 is the same instrument; a model, flag, brief or
major.minor change is a new one); the rubric version and the score log's
comparable series under that version; the next step; and the decide command for the
latest scored round, filled from the doc's Target and Move threshold labels. It also
lists child runs started with spawn_child.sh --background that have not written their
.status file: running, or gone without one (killed; read its .spawn and .log). The round
ceiling is init's --rounds, else an "N rounds" phrase in the doc's Budget label, else
init's default, else <R>; a "90 minutes" budget is never read as rounds. Lite
defaults: T 3, target 90, 2 rounds. It reads files only. Exit 2 when LOOP is
missing.
"""

import argparse
import datetime
import os
import re
import subprocess
import sys

SUBDIRS = ("prompts", "reviews", "scores", "evidence", "snapshots", "tools")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ROUND_DIR = re.compile(r"^round-(\d+)$")
VIEW_DIR = re.compile(r"^view-round-(\d+)$")
SCORE_FILE = re.compile(r"^round-(\d+)([a-z]?)\.md$")
REVIEW_FILE = re.compile(r"^round-(\d+)\.md$")


def die(message, code=2):
    sys.stderr.write("loop: %s\n" % message)
    sys.exit(code)


def git(root, *args):
    try:
        proc = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, check=False)
    except OSError:
        return None
    return proc.stdout.rstrip("\n") if proc.returncode == 0 else None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_init(args):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        die("root not found: %s" % args.root)
    if not SLUG.match(args.slug):
        die("--slug takes lowercase letters, digits and hyphens, got %r" % args.slug)
    if args.rounds is not None and args.rounds < 1:
        die("--rounds must be 1 or more")
    git_dir = git(root, "rev-parse", "--absolute-git-dir")
    if git_dir:
        loop = os.path.join(git_dir, "iterative-improvement", args.slug)
    else:
        loop = os.path.join(root, ".iterative-improvement", args.slug)
    rounds = os.path.join(loop, "rounds.md")
    if os.path.exists(rounds):
        die("%s already exists; this loop has started. Run: loop.py status --loop %s" % (rounds, loop))
    for name in SUBDIRS:
        os.makedirs(os.path.join(loop, name), exist_ok=True)
    scope = list(args.scope or [])
    lines = ["# Rounds: %s" % args.slug, "",
             "- Mode: %s" % args.mode,
             "- Kickoff (UTC): %s" % utc_now(),
             "- ROOT: %s" % root,
             "- Scope: %s" % (", ".join(scope) if scope else "(whole ROOT)"),
             "- Round ceiling: %s" % (args.rounds if args.rounds else
                                      "%d (default)" % (5 if args.mode == "full" else 2)),
             "- Commits allowed: %s" % args.commits,
             "- Budget: %s" % (args.budget or "none named (the round ceiling is the only limit)"),
             "- Engines and vendors: %s" % (args.engines or "not recorded; add the consented engines here"),
             "- Off limits: %s" % (", ".join(args.off_limits) if args.off_limits else "none named")]
    dirty = ""
    if git_dir:
        head = git(root, "rev-parse", "HEAD") or "none (no commits)"
        dirty = git(root, "status", "--short", "--", *(scope or ["."])) or ""
        lines.append("- HEAD at kickoff: %s" % head)
        lines.append("- Scope status at kickoff: %s" % ("clean" if not dirty else "dirty, see below"))
        if dirty:
            lines += ["", "```", dirty, "```"]
    else:
        lines.append("- Not a git repo: undo is snapshot.py restore from snapshots/round-0")
    lines += ["",
              "## Rounds",
              "",
              "<!-- One block per round: '### Round N (started <date -u>)', the triage table, the",
              "changelog (one line per change, naming its criterion), the decide line. -->",
              "",
              "## KEEP",
              "",
              "## Incidents",
              ""]
    with open(rounds, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print(loop)
    if dirty:
        sys.stderr.write("loop: the scope has uncommitted changes, so round 0 is not HEAD:\n%s\n"
                         "loop: ask the user to commit first, or write gates against snapshots/round-0 "
                         "instead of a SHA\n" % dirty)
    return 0


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def numbered(directory, pattern):
    if not os.path.isdir(directory):
        return []
    found = []
    for name in os.listdir(directory):
        match = pattern.match(name)
        if match:
            found.append(int(match.group(1)))
    return sorted(set(found))


def header_value(text, label):
    match = re.search(r"^- %s: (.*)$" % re.escape(label), text, re.M)
    return match.group(1).strip() if match else None


def bold_value(text, label):
    match = re.search(r"\*\*%s:\*\*\s*([^*\n]+)" % re.escape(label), text)
    return match.group(1).strip() if match else None


def score_log(text):
    """Rows (round, rubric, raw, adj) of the criteria doc's score log, in order."""
    start = text.find("## Score log")
    if start < 0:
        return []
    rows = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("## "):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.match(r"^\d+[a-z]?$", cells[0]) and re.match(r"^\d+$", cells[2] or "x") \
                and re.match(r"^\d+$", cells[3] or "x"):
            rows.append((cells[0], cells[1], int(cells[2]), int(cells[3])))
    return rows


def series_for(rows, rubric):
    """(tokens, last round number) of plain-numbered rows under RUBRIC."""
    tokens = []
    last = None
    for rnd, version, raw, adj in rows:
        if version != rubric or not rnd.isdigit():
            continue
        tokens.append(str(raw) if adj == 100 else "%d/%d" % (raw, adj))
        last = int(rnd)
    return tokens, last


ENGINE_LINE = re.compile(r"^(# engine: \S+ \().*\)$", re.M)


def pin_form(text):
    """A .cmd record with the engine version cut to major.minor, for pin comparison."""
    def cut(match):
        version = re.search(r"\d+\.\d+", match.group(0)[len(match.group(1)):])
        return match.group(1) + (version.group(0) if version else "unknown") + ")"
    return ENGINE_LINE.sub(cut, text)


def find_criteria(loop, given):
    if given:
        if not os.path.isfile(given):
            die("criteria doc not found: %s" % given)
        return given
    found = [n for n in sorted(os.listdir(loop)) if n.endswith("-criteria.md")]
    return os.path.join(loop, found[0]) if len(found) == 1 else None


def cmd_status(args):
    loop = os.path.abspath(args.loop)
    if not os.path.isdir(loop):
        die("loop dir not found: %s" % args.loop)
    rounds_path = os.path.join(loop, "rounds.md")
    rounds = read(rounds_path) if os.path.isfile(rounds_path) else ""
    mode = header_value(rounds, "Mode") or "unknown"
    print("loop: %s" % loop)
    print("mode: %s; kickoff (UTC): %s" % (mode, header_value(rounds, "Kickoff (UTC)") or "not recorded"))
    ceiling = header_value(rounds, "Round ceiling")
    print("decisions: rounds %s; commits %s; budget %s; engines %s; off limits %s"
          % (ceiling or "not recorded", header_value(rounds, "Commits allowed") or "not recorded",
             header_value(rounds, "Budget") or "not recorded",
             header_value(rounds, "Engines and vendors") or "not recorded",
             header_value(rounds, "Off limits") or "not recorded"))
    snapshots = numbered(os.path.join(loop, "snapshots"), ROUND_DIR)
    evidence = numbered(os.path.join(loop, "evidence"), ROUND_DIR)
    views = numbered(os.path.join(loop, "evidence"), VIEW_DIR)
    reviews = numbered(os.path.join(loop, "reviews"), REVIEW_FILE)
    scores_dir = os.path.join(loop, "scores")
    score_names = sorted(n for n in os.listdir(scores_dir) if SCORE_FILE.match(n)) if os.path.isdir(scores_dir) else []
    scored = sorted(set(int(SCORE_FILE.match(n).group(1)) for n in score_names))
    show = lambda values: ", ".join(str(v) for v in values) or "none"
    print("snapshots: %s; evidence: %s; views: %s" % (show(snapshots), show(evidence), show(views)))
    print("reviews: %s; scores: %s" % (show(reviews), ", ".join(score_names) or "none"))
    unfinished = []
    for sub in ("reviews", "scores"):
        directory = os.path.join(loop, sub)
        for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            if not name.endswith(".pid"):
                continue
            base = os.path.join(directory, name[:-4])
            if os.path.exists(base + ".status"):
                continue
            try:
                pid = int(read(base + ".pid").strip())
                os.kill(pid, 0)
                state = "running, pid %d" % pid
            except (ValueError, OSError):
                state = "gone without a .status (killed); read %s.spawn and %s.log" % (name[:-4], name[:-4])
            unfinished.append("%s/%s (%s)" % (sub, name[:-4], state))
    print("children not finished: %s" % ("; ".join(unfinished) or "none"))
    pin = None
    for name in ("round-0a.cmd", "round-0.cmd"):
        if os.path.isfile(os.path.join(scores_dir, name)):
            pin = name
            break
    if pin:
        pinned = pin_form(read(os.path.join(scores_dir, pin)))
        drift = [n for n in sorted(os.listdir(scores_dir))
                 if n.endswith(".cmd") and n != pin and pin_form(read(os.path.join(scores_dir, n))) != pinned]
        print("scorer pin: scores/%s; differing: %s" % (pin, ", ".join(drift) if drift else "none"))
    else:
        print("scorer pin: none yet")
    criteria = find_criteria(loop, args.criteria)
    tokens, last_round = [], None
    target, threshold = "90", "3"
    # An explicit --rounds beats the doc; the doc's "N rounds" beats init's default.
    stated_ceiling = re.match(r"^(\d+)( \(default\))?$", ceiling or "")
    max_rounds = stated_ceiling.group(1) if stated_ceiling and not stated_ceiling.group(2) else None
    if criteria:
        text = read(criteria)
        rubric = (bold_value(text, "Rubric version") or "v1").split()[0]
        tokens, last_round = series_for(score_log(text), rubric)
        print("criteria: %s; rubric %s; series %s" % (criteria, rubric, ",".join(tokens) or "empty"))
        if (bold_value(text, "Mode") or mode).startswith("full"):
            target_label = bold_value(text, "Target") or ""
            budget_label = bold_value(text, "Budget") or ""
            threshold_label = bold_value(text, "Move threshold") or ""
            target = (re.findall(r"\d+", target_label) or ["<X>"])[0]
            if max_rounds is None:
                # Only "N rounds" is a round count: "90 minutes (5 rounds max)" is 5, not 90.
                stated = re.search(r"(\d+)\s*rounds?\b", budget_label)
                max_rounds = stated.group(1) if stated else None
            threshold = (re.findall(r"\d+", threshold_label.split("=")[-1]) or ["<T>"])[0]
    else:
        print("criteria: not found (pass --criteria DOC)")
    if max_rounds is None:
        max_rounds = stated_ceiling.group(1) if stated_ceiling else ("2" if mode == "lite" else "<R>")
    latest = snapshots[-1] if snapshots else None
    if latest is None:
        step = "round 0: snapshot the scope to snapshots/round-0, strip the criteria, run evidence, score it"
    elif latest not in scored:
        step = "score round %d (snapshot exists, no scores/round-%d*.md)" % (latest, latest)
    elif latest + 1 not in reviews:
        step = ("run decide below; on CONTINUE build evidence/view-round-%d if missing, then review round %d "
                "on snapshots/round-%d" % (latest, latest + 1, latest))
    else:
        step = ("triage reviews/round-%d.md in rounds.md, fix, then snapshot round %d"
                % (latest + 1, latest + 1))
    print("next: %s" % step)
    if tokens:
        print("decide: score_math.py decide --threshold %s --target %s --max-rounds %s --scores %s "
              "--rounds-used %d --gates PASS|FAIL" % (threshold, target, max_rounds, ",".join(tokens), last_round))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="loop.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action")
    sub.required = True
    init = sub.add_parser("init", help="create LOOP and its rounds.md")
    init.add_argument("--root", required=True, help="ROOT: the repo root, or the folder holding the artifact")
    init.add_argument("--slug", required=True, help="short name for the loop, for example landing-page")
    init.add_argument("--mode", choices=("lite", "full"), default="lite", help="lite or full (default lite)")
    init.add_argument("--scope", action="append", default=[], help="a --path the snapshots will take (repeatable)")
    init.add_argument("--rounds", type=int, default=None, help="the agreed round ceiling (default 2 lite, 5 full)")
    init.add_argument("--commits", choices=("yes", "no"), default="no", help="whether the loop may commit (default no)")
    init.add_argument("--budget", default=None, help='the time or money budget as agreed, for example "90 minutes"')
    init.add_argument("--engines", default=None,
                      help='consented engines, vendors and child-run cap, for example "codex gpt-5 (OpenAI), 7 runs"')
    init.add_argument("--off-limits", action="append", default=[], help="a path the loop must not change (repeatable)")
    init.set_defaults(func=cmd_init)
    status = sub.add_parser("status", help="print where the loop stands and the next step")
    status.add_argument("--loop", required=True, help="the LOOP directory")
    status.add_argument("--criteria", default=None, help="the criteria doc (default: the *-criteria.md in LOOP)")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
