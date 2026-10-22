#!/usr/bin/env python3
"""Routing eval for the agent-sessions description (evals/triggers.json).

An approximation of host routing, not the host itself: for each phrase, a
cheap classifier model (`claude -p`, haiku by default) is given the name and
description of every skill in this repository and asked which one a coding
agent should load, or none. The phrase counts as fired when it names
agent-sessions. The classifier runs with no tools, no MCP servers and no
settings files, in an empty temporary directory, one fresh session per phrase.

It spends tokens. `--max-usd` (default 0.80) is a hard cap on the summed
`total_cost_usd` the CLI reports: a call starts only while the remaining
budget covers its own `--max-budget-usd` ceiling. `--dry-run` prints the plan
and the classifier prompt and spends nothing.

    python3 evals/run_routing.py [--max-usd 0.8] [--model haiku] [--repeat N] [--dry-run] [--json]

With --repeat N each phrase is classified N times and counts as fired on a
majority; the per-phrase vote is printed for every miss.

Prints recall on should_fire and specificity on should_not_fire separately,
with every miss, as evals/rubric.md asks. Stdlib only.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SKILLS_DIR = os.path.dirname(SKILL)
TARGET = "agent-sessions"
PER_CALL_USD = 0.03

SYSTEM = """You are the skill router of a coding agent. One user message arrives. These skills are
installed; each line is the skill name, a colon and its description:

%s

Load a skill only when its description says it applies to this message; otherwise load none.
Reply with exactly one line: the name of the one skill to load, or none. No other text."""


def frontmatter_description(text):
    """(name, description) from a SKILL.md: a folded `description: >` block or one line."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None
    front = parts[1]
    name = re.search(r"^name:\s*(\S+)\s*$", front, re.M)
    m = re.search(r"^description: >-?\n((?:[ \t]+.*\n?)+)", front, re.M)
    if m:
        desc = " ".join(line.strip() for line in m.group(1).splitlines() if line.strip())
    else:
        m = re.search(r"^description:\s*(.+)$", front, re.M)
        desc = m.group(1).strip().strip("'\"") if m else None
    return (name.group(1) if name else None), desc


def skill_descriptions(skills_dir=SKILLS_DIR):
    found = []
    for entry in sorted(os.listdir(skills_dir)):
        path = os.path.join(skills_dir, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            name, desc = frontmatter_description(fh.read())
        if name and desc:
            found.append((name, desc))
    return found


def decision(answer, names):
    """The skill the classifier named (one of `names`), or "none"."""
    words = re.findall(r"[A-Za-z0-9_-]+", (answer or "").strip().lower())
    for word in words[:3]:
        if word in names:
            return word
    return "none"


def summarize(rows):
    """{"recall": (fired, n), "specificity": (not fired, n), "misses": [...]} from
    rows of {"list", "phrase", "why", "fired"}; rows with fired None were not run."""
    fire = [r for r in rows if r["list"] == "should_fire" and r["fired"] is not None]
    quiet = [r for r in rows if r["list"] == "should_not_fire" and r["fired"] is not None]
    misses = [r for r in fire if not r["fired"]] + [r for r in quiet if r["fired"]]
    return {"recall": (sum(1 for r in fire if r["fired"]), len(fire)),
            "specificity": (sum(1 for r in quiet if not r["fired"]), len(quiet)),
            "misses": misses, "not_run": sum(1 for r in rows if r["fired"] is None)}


def classify(binary, system, phrase, model, cwd):
    """(decision text, cost_usd) for one phrase."""
    argv = [binary, "-p", "--output-format", "json", "--model", model, "--system-prompt", system,
            "--tools", "", "--strict-mcp-config", "--restricted", "--no-session-persistence",
            "--max-budget-usd", str(PER_CALL_USD)]
    cp = subprocess.run(argv, input=phrase, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        encoding="utf-8", errors="replace", timeout=180)
    try:
        doc = json.loads(cp.stdout)
    except ValueError:
        raise RuntimeError("claude -p gave no JSON (exit %d): %s" % (cp.returncode, (cp.stderr or cp.stdout)[-300:]))
    cost = doc.get("total_cost_usd") if isinstance(doc.get("total_cost_usd"), (int, float)) else PER_CALL_USD
    return str(doc.get("result") or ""), float(cost)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max-usd", type=float, default=0.80)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    with open(os.path.join(HERE, "triggers.json"), encoding="utf-8") as fh:
        triggers = json.load(fh)
    skills = skill_descriptions()
    names = set(n for n, _d in skills)
    if TARGET not in names:
        sys.stderr.write("run_routing: %s not found under %s\n" % (TARGET, SKILLS_DIR))
        return 2
    system = SYSTEM % "\n".join("%s: %s" % (n, d) for n, d in skills)
    rows = [{"list": key, "phrase": item["phrase"], "why": item["why"], "fired": None, "decision": None,
             "votes": []} for key in ("should_fire", "should_not_fire") for item in triggers[key]]
    if args.dry_run:
        print("would classify %d phrases %d time(s) with %s, at most $%.2f in total ($%.2f ceiling per call)"
              % (len(rows), args.repeat, args.model, args.max_usd, PER_CALL_USD))
        print("skills: %s" % ", ".join(sorted(names)))
        print("--- system prompt ---\n%s\n--- end ---" % system)
        return 0
    binary = shutil.which("claude")
    if not binary:
        sys.stderr.write("run_routing: claude is not on PATH\n")
        return 2
    spent = 0.0
    work = tempfile.mkdtemp(prefix="routing-eval.")
    try:
        stopped = False
        for row in rows:
            for _k in range(max(1, args.repeat)):
                if spent + PER_CALL_USD > args.max_usd:
                    stopped = True
                    break
                answer, cost = classify(binary, system, row["phrase"], args.model, work)
                spent += cost
                row["votes"].append(decision(answer, names))
            if stopped and len(row["votes"]) < max(1, args.repeat):
                row["votes"] = []
                break
            fired = sum(1 for v in row["votes"] if v == TARGET)
            row["fired"] = fired * 2 > len(row["votes"])
            row["decision"] = max(set(row["votes"]), key=row["votes"].count)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    summary = summarize(rows)
    if args.json:
        print(json.dumps({"model": args.model, "spent_usd": round(spent, 6), "rows": rows,
                          "recall": summary["recall"], "specificity": summary["specificity"]}, indent=2))
        return 0
    print("model       : %s (classifier approximation of host routing)" % args.model)
    print("recall      : %d / %d should_fire phrases loaded %s" % (summary["recall"] + (TARGET,)))
    print("specificity : %d / %d should_not_fire phrases left it alone" % summary["specificity"])
    if summary["not_run"]:
        print("not run     : %d phrases (the spend cap stopped the run)" % summary["not_run"])
    for r in summary["misses"]:
        print("miss        : [%s -> %s, votes %s] %s   (why: %s)" % (
            r["list"], r["decision"], "/".join(r["votes"]), r["phrase"], r["why"]))
    print("spent       : $%.4f of the $%.2f cap" % (spent, args.max_usd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
