#!/usr/bin/env python3
"""Offline consistency check for the agent-sessions evals.

Validates evals/scenarios.json and evals/triggers.json against their expected
shapes, then checks every `agent-session <command>` named in a scenario's
`ideal` steps against `agent-session --help`, and every `--flag` named there
against references/commands.md. Exit 0 when consistent, 1 on any mismatch,
2 when a file cannot be read. Stdlib only; spends no tokens.

    python3 evals/check_scenarios.py
"""

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
ENTRY = os.path.join(SKILL, "scripts", "agent-session")
COMMANDS_MD = os.path.join(SKILL, "references", "commands.md")

SCENARIO_KEYS = ("id", "situation", "user_message", "ideal", "failure_modes", "probes")
PROBES = ("phase1", "phase2")
N_SCENARIOS = 24
N_TRIGGERS = 30
COMMAND_RE = re.compile(r"agent-session\s+([a-z][a-z-]*)")
FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("check_scenarios: cannot read %s: %s\n" % (path, exc))
        sys.exit(2)


def nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def check_scenarios(doc, problems):
    items = doc.get("scenarios") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        problems.append("scenarios.json: top level must be an object with a 'scenarios' list")
        return []
    if len(items) != N_SCENARIOS:
        problems.append("scenarios.json: expected %d scenarios, found %d" % (N_SCENARIOS, len(items)))
    seen = set()
    for i, sc in enumerate(items):
        where = "scenarios.json[%d]" % i
        if not isinstance(sc, dict):
            problems.append("%s: not an object" % where)
            continue
        missing = [k for k in SCENARIO_KEYS if k not in sc]
        extra = sorted(set(sc) - set(SCENARIO_KEYS))
        if missing:
            problems.append("%s: missing keys %s" % (where, ", ".join(missing)))
        if extra:
            problems.append("%s: unknown keys %s" % (where, ", ".join(extra)))
        sid = sc.get("id")
        if not (nonempty_str(sid) and re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", sid)):
            problems.append("%s: id must be kebab-case" % where)
        elif sid in seen:
            problems.append("%s: duplicate id %s" % (where, sid))
        else:
            seen.add(sid)
            where = "scenario %s" % sid
        for key in ("situation", "user_message"):
            if key in sc and not nonempty_str(sc[key]):
                problems.append("%s: %s must be a non-empty string" % (where, key))
        for key in ("ideal", "failure_modes"):
            if key in sc and not (isinstance(sc[key], list) and sc[key] and all(nonempty_str(s) for s in sc[key])):
                problems.append("%s: %s must be a non-empty list of strings" % (where, key))
        probes = sc.get("probes")
        if "probes" in sc and not (isinstance(probes, list) and probes and all(p in PROBES for p in probes)
                                   and len(set(probes)) == len(probes)):
            problems.append("%s: probes must be a non-empty subset of %s" % (where, ", ".join(PROBES)))
    return [sc for sc in items if isinstance(sc, dict)]


def check_triggers(doc, problems):
    if not isinstance(doc, dict):
        problems.append("triggers.json: top level must be an object")
        return
    phrases = {}
    for key in ("should_fire", "should_not_fire"):
        items = doc.get(key)
        if not isinstance(items, list):
            problems.append("triggers.json: '%s' must be a list" % key)
            continue
        if len(items) != N_TRIGGERS:
            problems.append("triggers.json: expected %d %s phrases, found %d" % (N_TRIGGERS, key, len(items)))
        for i, item in enumerate(items):
            where = "triggers.json %s[%d]" % (key, i)
            if not (isinstance(item, dict) and set(item) == {"phrase", "why"}
                    and nonempty_str(item.get("phrase")) and nonempty_str(item.get("why"))):
                problems.append("%s: must be {\"phrase\", \"why\"} with non-empty strings" % where)
                continue
            norm = " ".join(item["phrase"].lower().split())
            if norm in phrases:
                problems.append("%s: phrase repeats %s" % (where, phrases[norm]))
            else:
                phrases[norm] = where


def help_commands():
    try:
        cp = subprocess.run([sys.executable, "-I", ENTRY, "--help"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    except OSError as exc:
        sys.stderr.write("check_scenarios: cannot run %s: %s\n" % (ENTRY, exc))
        sys.exit(2)
    if cp.returncode != 0:
        sys.stderr.write("check_scenarios: agent-session --help exited %d\n%s" % (cp.returncode, cp.stderr))
        sys.exit(2)
    return set(re.findall(r"^\s+agent-session\s+([a-z][a-z-]*)", cp.stdout, re.M))


def check_references(scenarios, problems):
    commands = help_commands()
    try:
        with open(COMMANDS_MD, encoding="utf-8") as fh:
            documented = set(FLAG_RE.findall(fh.read()))
    except OSError as exc:
        sys.stderr.write("check_scenarios: cannot read %s: %s\n" % (COMMANDS_MD, exc))
        sys.exit(2)
    named_cmds = named_flags = 0
    for sc in scenarios:
        for step in sc.get("ideal") or []:
            if not isinstance(step, str):
                continue
            for cmd in COMMAND_RE.findall(step):
                named_cmds += 1
                if cmd not in commands:
                    problems.append("scenario %s: `agent-session %s` is not in agent-session --help"
                                    % (sc.get("id"), cmd))
            for flag in FLAG_RE.findall(step):
                named_flags += 1
                if flag not in documented:
                    problems.append("scenario %s: %s is not documented in references/commands.md"
                                    % (sc.get("id"), flag))
    return named_cmds, named_flags


def main():
    problems = []
    scenarios = check_scenarios(load(os.path.join(HERE, "scenarios.json")), problems)
    check_triggers(load(os.path.join(HERE, "triggers.json")), problems)
    n_cmds, n_flags = check_references(scenarios, problems)
    if problems:
        for p in problems:
            sys.stderr.write("FAIL %s\n" % p)
        sys.stderr.write("check_scenarios: %d problem(s)\n" % len(problems))
        return 1
    print("check_scenarios: ok (%d scenarios, %d+%d trigger phrases, %d command and %d flag references checked)"
          % (len(scenarios), N_TRIGGERS, N_TRIGGERS, n_cmds, n_flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
