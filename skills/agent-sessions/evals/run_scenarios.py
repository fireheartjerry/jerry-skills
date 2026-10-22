#!/usr/bin/env python3
"""Behavioural eval of SKILL.md against evals/scenarios.json, with and without
the skill.

For each chosen scenario and arm, an executor model (`claude -p`, haiku by
default) plays the parent agent: it gets the scenario's situation and user
message, Bash as its only tool, and, in the `skill` arm, SKILL.md as context
(the `none` arm gets no skill). Its Bash runs in a temporary git repository
with the fake engines of tests/fake-engine/ first on PATH and a private
agent-sessions state root and tmux socket, so any child it spawns is a fake
and spends nothing. The transcript (tool calls, outputs, final reply) is saved,
then a grader call scores it 0 to 10 against evals/rubric.md without knowing
the arm.

Limits of the method: the state a situation describes (a child that already
failed, a verdict already given) is not on disk; the executor is told to take
the situation as true when command output disagrees. n is 1 per scenario and
arm unless --repeat says otherwise.

It spends tokens. `--max-usd` (default 0.80) is a hard cap on the summed
`total_cost_usd` the CLI reports: a call starts only while the remaining
budget covers its own `--max-budget-usd` ceiling, so the run stops early
rather than exceed the cap. `--dry-run` prints the plan and spends nothing.

    python3 evals/run_scenarios.py [--scenarios a,b] [--arms skill,none] [--max-usd 0.8]
                                   [--executor haiku] [--grader haiku] [--out DIR] [--dry-run] [--json]

Prints one row per scenario and arm (score, missed steps, failure modes) and
the per-arm means, never the mean alone, as evals/rubric.md asks. Stdlib only.
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
ENTRY = os.path.join(SKILL, "scripts", "agent-session")
FAKE_DIR = os.path.join(SKILL, "tests", "fake-engine")
EXEC_CALL_USD = 0.06
GRADE_CALL_USD = 0.02
MAX_TURNS = 14
# The scenarios the skill spends the most words on and the ones most likely to
# separate the arms; the default set when --scenarios is not given.
HARD = ("slow-child-why", "verify-fail-fixup", "judge-revise-stop", "worktree-missing-deps", "asked-to-push",
        "audit-findings-check", "injected-instructions", "fanout-hard-cap")

BASE_SYSTEM = """You are a coding agent working for a user in the git repository at %(repo)s.
Your only tool is Bash. Work as you would for a real user: act, then reply to the user.
This is a simulation. The situation below is true even when command output disagrees with it
(state it describes may not exist on disk). Do not ask the user questions you can answer yourself.
%(skill)s"""

SKILL_BLOCK = """
A skill is installed. Its SKILL.md follows; its directory is %(dir)s, so the script is
%(entry)s and the references are under %(dir)s/references (read them with cat when SKILL.md
points to them).

--- SKILL.md ---
%(text)s
--- end SKILL.md ---"""

GRADER_SYSTEM = """You grade one transcript of a coding agent against one scenario, using this rubric:

%(rubric)s

Scenario (JSON):
%(scenario)s

Reply with only a JSON object, no prose and no code fences:
{"score": <integer 0-10>, "missed": [<ideal steps not done>], "failure_modes": [<failure modes shown>],
 "justification": "<one sentence>"}"""


def load_json(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def pick(scenarios, wanted):
    by_id = dict((s["id"], s) for s in scenarios)
    ids = [w for w in (wanted or HARD) if w]
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise SystemExit("run_scenarios: unknown scenario id(s): %s" % ", ".join(unknown))
    return [by_id[i] for i in ids]


def executor_system(arm, repo):
    skill = ""
    if arm == "skill":
        skill = SKILL_BLOCK % {"dir": SKILL, "entry": ENTRY, "text": read(os.path.join(SKILL, "SKILL.md"))}
    return BASE_SYSTEM % {"repo": repo, "skill": skill}


def executor_prompt(sc):
    return "Situation: %s\n\nThe user says: %s" % (sc["situation"], sc["user_message"])


def sandbox(root):
    """A small git repository and the environment the executor's Bash gets:
    fake engines first on PATH, a private state root, owner and tmux socket."""
    repo = os.path.join(root, "repo")
    os.makedirs(os.path.join(repo, "src"))
    with open(os.path.join(repo, "src", "app.py"), "w") as fh:
        fh.write("def parse(s):\n    return s.split(',')\n")
    with open(os.path.join(repo, "README.md"), "w") as fh:
        fh.write("demo\n")
    g = ["git", "-C", repo, "-c", "user.name=eval", "-c", "user.email=eval@example.com", "-c", "commit.gpgsign=false"]
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(g + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dirs = [FAKE_DIR]
    for tool in ("tmux", "python3", "git", "node"):
        found = shutil.which(tool)
        if found and os.path.dirname(found) not in dirs:
            dirs.append(os.path.dirname(found))
    env = dict(os.environ)
    env.update({"PATH": os.pathsep.join(dirs + ["/usr/bin", "/bin"]),
                "AGENT_SESSIONS_STATE": os.path.join(root, "state"),
                "AGENT_SESSIONS_TMUX_SOCKET": "as-eval-%d" % os.getpid(),
                "AGENT_SESSIONS_OWNER": "scenario-eval", "FAKE_ENGINE_HOME": os.path.join(root, "fake"),
                "AGENT_SESSIONS_LINGER": "1"})
    for key in ("AGENT_SESSION_DEPTH", "AGENT_SESSION_NAME", "AGENT_SESSIONS_LEDGER"):
        env.pop(key, None)
    os.makedirs(env["FAKE_ENGINE_HOME"])
    return repo, env


def transcript_of(events, limit=1500):
    """A readable transcript from claude stream-json events: assistant text,
    each Bash command and the start of its output, and the final reply."""
    lines = []
    for ev in events:
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append("ASSISTANT: %s" % block["text"].strip())
            elif block.get("type") == "tool_use":
                cmd = (block.get("input") or {}).get("command")
                lines.append("BASH: %s" % (cmd if isinstance(cmd, str) else json.dumps(block.get("input"))))
            elif block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                lines.append("OUTPUT: %s" % str(content or "")[:limit])
        if ev.get("type") == "result" and isinstance(ev.get("result"), str):
            lines.append("FINAL REPLY: %s" % ev["result"].strip())
    return "\n".join(lines)


def run_claude(argv, stdin, cwd, env, timeout):
    """(events, cost_usd or None) from `claude -p --output-format stream-json`."""
    cp = subprocess.run(argv, input=stdin, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        encoding="utf-8", errors="replace", timeout=timeout)
    events = []
    for raw in cp.stdout.splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    cost = None
    for ev in events:
        if ev.get("type") == "result" and isinstance(ev.get("total_cost_usd"), (int, float)):
            cost = float(ev["total_cost_usd"])
    if not events:
        raise RuntimeError("claude -p gave no events (exit %d): %s" % (cp.returncode, (cp.stderr or "")[-300:]))
    return events, cost


def parse_grade(text):
    """The grader's JSON object, or None. Tolerates a code fence or leading prose."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("score"), (int, float)):
        return None
    doc["score"] = max(0, min(10, int(round(doc["score"]))))
    return doc


class Budget(object):
    """Hard spend cap: a call may start only while what is left covers its ceiling."""

    def __init__(self, cap):
        self.cap = cap
        self.spent = 0.0

    def allows(self, ceiling):
        return self.spent + ceiling <= self.cap

    def charge(self, cost, ceiling):
        self.spent += cost if isinstance(cost, (int, float)) else ceiling


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--arms", default="skill,none")
    ap.add_argument("--max-usd", type=float, default=0.80)
    ap.add_argument("--executor", default="haiku")
    ap.add_argument("--grader", default="haiku")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    arms = [a for a in args.arms.split(",") if a]
    if not arms or any(a not in ("skill", "none") for a in arms):
        raise SystemExit("run_scenarios: --arms takes skill and/or none")
    chosen = pick(load_json("scenarios.json")["scenarios"], [s.strip() for s in args.scenarios.split(",")
                                                             if s.strip()])
    runs = [(sc, arm, k) for sc in chosen for arm in arms for k in range(max(1, args.repeat))]
    worst = len(runs) * (EXEC_CALL_USD + GRADE_CALL_USD)
    if args.dry_run:
        print("would run %d executor and %d grader calls (%s executor, %s grader): %s; arms %s"
              % (len(runs), len(runs), args.executor, args.grader, ", ".join(s["id"] for s in chosen),
                 ", ".join(arms)))
        print("ceiling $%.2f per run, $%.2f if every run hit it; hard cap $%.2f stops the run first"
              % (EXEC_CALL_USD + GRADE_CALL_USD, worst, args.max_usd))
        print("--- executor system prompt (skill arm, first 600 chars) ---\n%s\n--- end ---"
              % executor_system("skill", "<repo>")[:600])
        return 0
    binary = shutil.which("claude")
    if not binary:
        sys.stderr.write("run_scenarios: claude is not on PATH\n")
        return 2
    rubric = read(os.path.join(HERE, "rubric.md"))
    out_dir = args.out or tempfile.mkdtemp(prefix="scenario-eval-out.")
    os.makedirs(out_dir, exist_ok=True)
    budget = Budget(args.max_usd)
    rows = []
    for sc, arm, k in runs:
        if not budget.allows(EXEC_CALL_USD + GRADE_CALL_USD):
            rows.append({"id": sc["id"], "arm": arm, "rep": k, "score": None, "note": "not run: spend cap"})
            continue
        root = tempfile.mkdtemp(prefix="scenario-eval.")
        try:
            repo, env = sandbox(root)
            argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", args.executor,
                    "--system-prompt", executor_system(arm, repo), "--tools", "Bash", "--permission-mode",
                    "bypassPermissions", "--strict-mcp-config", "--no-session-persistence", "--max-turns",
                    str(MAX_TURNS), "--max-budget-usd", str(EXEC_CALL_USD)]
            events, cost = run_claude(argv, executor_prompt(sc), repo, env, 900)
            budget.charge(cost, EXEC_CALL_USD)
            transcript = transcript_of(events)
        finally:
            subprocess.run(["tmux", "-L", "as-eval-%d" % os.getpid(), "kill-server"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            shutil.rmtree(root, ignore_errors=True)
        base = os.path.join(out_dir, "%s.%s.%d" % (sc["id"], arm, k))
        with open(base + ".transcript.txt", "w", encoding="utf-8") as fh:
            fh.write(transcript + "\n")
        system = GRADER_SYSTEM % {"rubric": rubric, "scenario": json.dumps(sc, indent=1)}
        gargv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", args.grader,
                 "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                 "--max-budget-usd", str(GRADE_CALL_USD)]
        gevents, gcost = run_claude(gargv, transcript, out_dir, None, 300)
        budget.charge(gcost, GRADE_CALL_USD)
        final = [e.get("result") for e in gevents if e.get("type") == "result"]
        grade = parse_grade(final[-1] if final else "") or {"score": None, "note": "grader reply unreadable"}
        with open(base + ".grade.json", "w", encoding="utf-8") as fh:
            json.dump(grade, fh, indent=2)
        rows.append(dict(grade, id=sc["id"], arm=arm, rep=k))
    means = {}
    for arm in arms:
        scores = [r["score"] for r in rows if r["arm"] == arm and isinstance(r.get("score"), int)]
        means[arm] = (round(sum(scores) / float(len(scores)), 2) if scores else None, len(scores))
    if args.json:
        print(json.dumps({"rows": rows, "means": means, "spent_usd": round(budget.spent, 6),
                          "transcripts": out_dir}, indent=2))
        return 0
    for r in rows:
        print("%-24s %-5s score %-4s missed %s; failure modes %s%s" % (
            r["id"], r["arm"], r.get("score"), len(r.get("missed") or []), len(r.get("failure_modes") or []),
            "  (%s)" % r["note"] if r.get("note") else ""))
    for arm, (mean, n) in means.items():
        print("mean %-5s: %s over %d scored run(s)" % (arm, mean, n))
    print("spent      : $%.4f of the $%.2f cap; transcripts and grades in %s" % (budget.spent, args.max_usd, out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
