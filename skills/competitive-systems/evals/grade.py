#!/usr/bin/env python3
"""Grade eval transcripts for the competitive-systems skill. Offline, standard library only.

usage:
  grade.py --transcripts DIR [--scenarios FILE] [--out grades.json|grades.md]
      Apply each scenario's mechanical probes to every transcript under DIR. Score per
      transcript = must probes matched minus failure probes matched; every hit is listed with
      the matching line. A second "outcome" score leaves out the probes and musts tagged as
      requiring the skill's own tool (`requires_skill_tool`, `skill_tool_musts`), which the
      no-skill arm cannot meet by construction. Each no-skill transcript is also checked for
      any action that touched an installed copy of the skill (SKILL.md, competitive-systems). Writes Markdown and JSON: --out X.md also writes X.json beside it;
      --out X.json writes only JSON; without --out, DIR/grades.md and DIR/grades.json.
  grade.py --prompt SID TRANSCRIPT [--scenarios FILE]
      Print a prompt for an independent grader model: the scenario's rubric, the transcript,
      and the required answer format (one line per must and failure mode, with a quote).
      Save the grader's reply next to the transcript as <transcript>.grade.txt.
  grade.py --merge GRADES.json [--out merged.json|merged.md]
      Combine mechanical grades with the model grades found in <transcript>.grade.txt files.
  grade.py --rubric [--scenarios FILE] [--out rubric.md]
      Write the human-readable rubric generated from scenarios.json.

Transcripts are the Markdown files run_eval.py writes: a `# <arm> / <SID> / s<seed>` header,
`work:`, `status:` and `user:` lines, then `### assistant`, `### tool_use <Tool>: <key>`,
`### tool_result` and `### result:` sections.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCENARIOS = os.path.join(HERE, "scenarios.json")
SCHEMA = "cs-evals-grades/1"
HEADER = re.compile(r"^# (\S+) / (S-[A-Z]) / s(\d+)\s*$", re.M)
SECTION = re.compile(r"^### (assistant|tool_use [^\n]*|tool_result|result:[^\n]*|stderr)\s*$", re.M)
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "file_change")
SCOPES = ("text", "final", "actions", "edits", "write_paths", "all")
GRADE_LINE = re.compile(r"^\s*([MF])(\d+)\s*:\s*(yes|no)\b\s*\|?\s*(.*)$", re.I | re.M)


def die(msg, code=2):
    sys.stderr.write("grade.py: %s\n" % msg)
    sys.exit(code)


def load_scenarios(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        die("cannot read scenarios %s: %s" % (path, e))
    return dict((s["id"], s) for s in data)


# ------------------------------------------------------------------ transcript parsing
def parse_transcript(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    m = HEADER.search(text)
    if not m:
        return None
    meta = {"path": os.path.abspath(path), "arm": m.group(1), "sid": m.group(2), "seed": int(m.group(3))}
    for key in ("status", "work", "model"):
        km = re.search(r"^%s: (.*)$" % key, text, re.M)
        meta[key] = km.group(1).strip() if km else None
    cm = re.findall(r"^### result:.*cost_usd=([0-9.]+)", text, re.M)
    meta["cost_usd"] = float(cm[-1]) if cm else None
    sections = []
    marks = list(SECTION.finditer(text))
    for i, mk in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        sections.append((mk.group(1), text[mk.end():end].strip("\n")))
    meta["sections"] = sections
    return meta


def scope_texts(t, scope):
    secs = t["sections"]
    assistant = [body for head, body in secs if head == "assistant"]
    tool_uses = [(head[len("tool_use "):], body) for head, body in secs if head.startswith("tool_use ")]
    if scope == "text":
        return assistant
    if scope == "final":
        return assistant[-1:]
    if scope == "actions":
        return [h + "\n" + b for h, b in tool_uses]
    if scope == "edits":
        return [b for h, b in tool_uses if h.split(":", 1)[0].strip() in EDIT_TOOLS]
    if scope == "write_paths":
        return [h.split(":", 1)[1].strip() for h, b in tool_uses
                if h.split(":", 1)[0].strip() in EDIT_TOOLS and ":" in h]
    if scope == "all":
        return assistant + [h + "\n" + b for h, b in tool_uses]
    raise ValueError("unknown scope %r" % scope)


def evidence_line(text, m):
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    line = text[start:end if end >= 0 else len(text)].strip()
    return line[:200]


def apply_probe(t, probe, run_commands=True):
    """Return (state, evidence): state is 'hit', 'miss' or 'skipped'."""
    if probe["kind"] == "regex":
        rx = re.compile(probe["pattern"], re.I | re.M)
        for chunk in scope_texts(t, probe.get("scope", "all")):
            m = rx.search(chunk)
            if m:
                return "hit", evidence_line(chunk, m)
        return "miss", ""
    if probe["kind"] == "command":
        work = t.get("work")
        if not run_commands or not work or not os.path.isdir(work):
            return "skipped", "work directory not available"
        try:
            p = subprocess.run(probe["command"], shell=True, cwd=work, timeout=60,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            return "skipped", "command timed out"
        return ("hit" if p.returncode == 0 else "miss"), "$ %s -> exit %d" % (probe["command"], p.returncode)
    raise ValueError("unknown probe kind %r" % probe["kind"])


LEAK = re.compile(r"SKILL\.md|competitive-systems", re.I)


def touched_skill(t):
    """The first action line that reads or runs an installed copy of the skill, or None."""
    for chunk in scope_texts(t, "actions"):
        m = LEAK.search(chunk)
        if m:
            return evidence_line(chunk, m)
    return None


def grade_one(t, scenario, run_commands=True):
    musts = [p for p in scenario["probes"] if p["must"]]
    fails = [p for p in scenario["probes"] if not p["must"]]
    tool = set(p["id"] for p in musts if p.get("requires_skill_tool"))
    out = {"path": t["path"], "arm": t["arm"], "sid": t["sid"], "seed": t["seed"],
           "status": t["status"], "model": t["model"], "cost_usd": t["cost_usd"],
           "musts_total": len(musts), "musts_hit": [], "failures_hit": [], "skipped": [],
           "evidence": {}}
    for p in scenario["probes"]:
        state, ev = apply_probe(t, p, run_commands)
        if state == "skipped":
            out["skipped"].append(p["id"])
        elif state == "hit":
            out["musts_hit" if p["must"] else "failures_hit"].append(p["id"])
            out["evidence"][p["id"]] = ev
    out["failures_total"] = len(fails)
    out["score"] = len(out["musts_hit"]) - len(out["failures_hit"])
    out["outcome_score"] = (len([p for p in out["musts_hit"] if p not in tool])
                            - len(out["failures_hit"]))
    out["touched_skill"] = touched_skill(t) if t["arm"] != "skill" else None
    return out


def find_transcripts(root):
    found = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.endswith(".work") and d != "fixtures")
        for n in sorted(names):
            if n.endswith(".md") and not n.endswith(".grade.md"):
                t = parse_transcript(os.path.join(dirpath, n))
                if t:
                    found.append(t)
    return found


def summarize(rows, key="score"):
    cells = {}
    for r in rows:
        if r.get(key) is None:
            continue
        cells.setdefault(r["sid"], {}).setdefault(r["arm"], []).append(r[key])
    return dict((sid, dict((arm, {"n": len(v), "mean": round(statistics.mean(v), 2),
                                  "min": min(v), "max": max(v)}) for arm, v in arms.items()))
                for sid, arms in cells.items())


# ------------------------------------------------------------------ output
def fmt_cell(cell):
    if not cell:
        return "not run"
    if cell["n"] == 1:
        return "%g (n=1)" % cell["mean"]
    return "%g (n=%d, %g..%g)" % (cell["mean"], cell["n"], cell["min"], cell["max"])


def table(summary, scenarios, arms, title, model=False, outcome=False):
    lines = ["| Scenario | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for sid in sorted(scenarios):
        s = scenarios[sid]
        if model:
            maxm = len(s["musts"]) - (len(s.get("skill_tool_musts", [])) if outcome else 0)
        else:
            maxm = sum(1 for p in s["probes"] if p["must"]
                       and not (outcome and p.get("requires_skill_tool")))
        row = ["%s %s (max %d)" % (sid, s["title"], maxm)]
        for arm in arms:
            row.append(fmt_cell(summary.get(sid, {}).get(arm)))
        lines.append("| " + " | ".join(row) + " |")
    return "### %s\n\n" % title + "\n".join(lines) + "\n"


def write_md(path, rows, scenarios, merged=False):
    arms = sorted(set(r["arm"] for r in rows)) or ["none", "skill"]
    cost = sum(r["cost_usd"] or 0 for r in rows)
    out = ["# Grades", "",
           "%d transcripts; total reported cost %.2f USD. Mechanical score = must probes matched "
           "minus failure probes matched; cells show the mean over repeats with the range." % (len(rows), cost), ""]
    out.append(table(summarize(rows), scenarios, arms, "Mechanical score per scenario and arm"))
    out.append(table(summarize(rows, "outcome_score"), scenarios, arms,
                     "Mechanical outcome score (skill-tool probes left out)", outcome=True))
    if merged:
        out.append(table(summarize(rows, "model_score"), scenarios, arms,
                         "Independent grader score (musts yes minus failure modes yes)", model=True))
        out.append(table(summarize(rows, "model_outcome_score"), scenarios, arms,
                         "Independent grader outcome score (skill-tool musts left out)",
                         model=True, outcome=True))
    out.append(per_run_means(rows, merged))
    leaks = [r for r in rows if r.get("touched_skill")]
    out.append("No-skill transcripts that touched an installed skill copy: %d of %d%s\n" % (
        len(leaks), sum(1 for r in rows if r["arm"] != "skill"),
        "".join("\n- %s %s s%d: `%s`" % (r["sid"], r["arm"], r["seed"],
                                         r["touched_skill"].replace("`", "'")) for r in leaks)))
    out.append("### Per transcript\n")
    for r in sorted(rows, key=lambda r: (r["sid"], r["arm"], r["seed"])):
        line = "- %s %s s%d: score %d (musts %d/%d%s%s)%s; status %s; cost %s" % (
            r["sid"], r["arm"], r["seed"], r["score"], len(r["musts_hit"]), r["musts_total"],
            "; failures " + ", ".join(r["failures_hit"]) if r["failures_hit"] else "",
            "; skipped " + ", ".join(r["skipped"]) if r["skipped"] else "",
            ("; model score %s" % r["model_score"]) if merged and r.get("model_score") is not None else "",
            r["status"], r["cost_usd"])
        out.append(line)
        missed = [p["id"] for p in scenarios[r["sid"]]["probes"] if p["must"] and p["id"] not in r["musts_hit"]
                  and p["id"] not in r["skipped"]]
        if missed:
            out.append("  - missed: " + ", ".join(missed))
        for pid in r["musts_hit"] + r["failures_hit"]:
            out.append("  - %s: `%s`" % (pid, r["evidence"].get(pid, "").replace("`", "'")))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def per_run_means(rows, merged):
    keys = [("score", "mechanical"), ("outcome_score", "mechanical outcome")]
    if merged:
        keys += [("model_score", "grader"), ("model_outcome_score", "grader outcome")]
    arms = sorted(set(r["arm"] for r in rows))
    lines = ["### Mean per run", "", "| Measure | " + " | ".join(arms) + " |",
             "|---|" + "---|" * len(arms)]
    for key, label in keys:
        cells = []
        for arm in arms:
            v = [r[key] for r in rows if r["arm"] == arm and r.get(key) is not None]
            cells.append("%.2f (n=%d)" % (statistics.mean(v), len(v)) if v else "not run")
        lines.append("| %s | %s |" % (label, " | ".join(cells)))
    return "\n".join(lines) + "\n"


def write_out(path, rows, scenarios, merged=False):
    data = {"schema": SCHEMA, "transcripts": rows, "summary": summarize(rows),
            "outcome_summary": summarize(rows, "outcome_score")}
    if merged:
        data["model_summary"] = summarize(rows, "model_score")
        data["model_outcome_summary"] = summarize(rows, "model_outcome_score")
    if path.endswith(".json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.write("\n")
    elif path.endswith(".md"):
        write_md(path, rows, scenarios, merged)
    else:
        die("--out must end in .json or .md: %s" % path)
    print("wrote %s" % path)


# ------------------------------------------------------------------ rubric and grader prompt
def rubric_block(s, probes=True):
    out = ["## %s %s" % (s["id"], s["title"]), "",
           "Situation: %s" % s["situation"], "",
           "User message: \"%s\"" % s["user_message"], "",
           "Fixture: `%s`. Guards a strength of the previous version: %s." % (s["fixture"], "yes" if s["guards_prior_strength"] else "no"), "",
           "Musts:"]
    out += ["- M%d. %s" % (i + 1, m) for i, m in enumerate(s["musts"])]
    out += ["", "Failure modes:"]
    out += ["- F%d. %s" % (i + 1, m) for i, m in enumerate(s["failure_modes"])]
    if not probes:
        return "\n".join(out) + "\n"
    out += ["", "Mechanical probes (regex is case-insensitive):", "",
            "| id | kind | scope | pattern or command | counts as |", "|---|---|---|---|---|"]
    for p in s["probes"]:
        pat = p.get("pattern") or p.get("command")
        out.append("| %s | %s | %s | `%s` | %s |" % (
            p["id"], p["kind"], p.get("scope", "work dir") if p["kind"] == "regex" else "work dir",
            pat.replace("|", "\\|"), "must" if p["must"] else "failure"))
    return "\n".join(out) + "\n"


def write_rubric(path, scenarios):
    head = ("# Rubric\n\nGenerated by `grade.py --rubric` from scenarios.json; do not edit by hand.\n\n"
            "Scopes: `text` is every assistant message, `final` the last one, `actions` the tool calls "
            "with their inputs, `edits` the bodies of file writes and edits, `write_paths` the paths "
            "written, `all` text plus actions. A command probe runs in the run's work directory after "
            "the run and matches when it exits 0. Mechanical score = must probes matched minus failure "
            "probes matched. The independent grader scores the Musts and Failure modes below.\n\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(rubric_block(scenarios[sid]) for sid in sorted(scenarios)))
    print("wrote %s" % path)


def grader_prompt(s, transcript_text):
    lines = ["You are grading one transcript of a coding agent against a rubric. You did not write "
             "the transcript and you have no stake in its outcome. Judge only what the transcript "
             "shows: the agent's messages, the commands it ran and their output.", "",
             rubric_block(s, probes=False), "",
             "Answer with exactly these lines, in this order, each with a verbatim quote from the "
             "transcript as evidence (or none), then the SCORE line:"]
    lines += ["M%d: yes|no | \"<quote>\"" % (i + 1) for i in range(len(s["musts"]))]
    lines += ["F%d: yes|no | \"<quote>\"" % (i + 1) for i in range(len(s["failure_modes"]))]
    lines += ["SCORE: <number of M yes> - <number of F yes> = <result>", "",
             "Mark a must yes only if the transcript itself shows it; mark a failure mode yes if the "
             "agent did it at any point, even if it later corrected course.", "",
             "<transcript>", transcript_text, "</transcript>"]
    return "\n".join(lines)


def parse_grade(text, s):
    got = {}
    for m in GRADE_LINE.finditer(text):
        got[(m.group(1).upper(), int(m.group(2)))] = (m.group(3).lower() == "yes", m.group(4).strip())
    nm, nf = len(s["musts"]), len(s["failure_modes"])
    complete = all(("M", i) in got for i in range(1, nm + 1)) and all(("F", i) in got for i in range(1, nf + 1))
    my = sum(1 for i in range(1, nm + 1) if got.get(("M", i), (False,))[0])
    fy = sum(1 for i in range(1, nf + 1) if got.get(("F", i), (False,))[0])
    tool = set(s.get("skill_tool_musts", []))
    my_outcome = sum(1 for i in range(1, nm + 1)
                     if i not in tool and got.get(("M", i), (False,))[0])
    return {"complete": complete, "musts_yes": my, "failures_yes": fy, "score": my - fy,
            "outcome_score": my_outcome - fy,
            "lines": dict(("%s%d" % k, {"yes": v[0], "quote": v[1][:240]}) for k, v in sorted(got.items()))}


def merge(grades_path, scenarios):
    try:
        with open(grades_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        die("cannot read %s: %s" % (grades_path, e))
    rows = data.get("transcripts", [])
    missing = 0
    for r in rows:
        gp = r["path"] + ".grade.txt"
        if not os.path.isfile(gp):
            r["model_score"] = r["model_outcome_score"] = None
            missing += 1
            continue
        with open(gp, encoding="utf-8") as f:
            g = parse_grade(f.read(), scenarios[r["sid"]])
        r["model_grade"] = g
        r["model_score"] = g["score"] if g["complete"] else None
        r["model_outcome_score"] = g["outcome_score"] if g["complete"] else None
        if not g["complete"]:
            sys.stderr.write("grade.py: incomplete model grade %s (missing M/F lines)\n" % gp)
    if missing:
        sys.stderr.write("grade.py: %d transcript(s) have no .grade.txt; their model cells stay empty\n" % missing)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grade competitive-systems eval transcripts.",
                                 epilog="See the module docstring (head of this file) for the four modes.")
    ap.add_argument("--transcripts", help="directory holding run_eval.py transcripts")
    ap.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    ap.add_argument("--out", help="output path; .json or .md")
    ap.add_argument("--prompt", nargs=2, metavar=("SID", "TRANSCRIPT"))
    ap.add_argument("--merge", metavar="GRADES_JSON")
    ap.add_argument("--rubric", action="store_true")
    ap.add_argument("--no-commands", action="store_true", help="skip command probes")
    a = ap.parse_args(argv)
    scenarios = load_scenarios(a.scenarios)
    modes = sum(bool(x) for x in (a.transcripts, a.prompt, a.merge, a.rubric))
    if modes != 1:
        die("choose exactly one of --transcripts, --prompt, --merge, --rubric")
    if a.rubric:
        write_rubric(a.out or os.path.join(HERE, "rubric.md"), scenarios)
        return 0
    if a.prompt:
        sid, path = a.prompt
        if sid not in scenarios:
            die("unknown scenario %s; known: %s" % (sid, ", ".join(sorted(scenarios))))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            die("cannot read transcript: %s" % e)
        print(grader_prompt(scenarios[sid], text))
        return 0
    if a.merge:
        rows = merge(a.merge, scenarios)
        out = a.out or re.sub(r"\.json$", "", a.merge) + "-merged.md"
        write_out(out, rows, scenarios, merged=True)
        return 0
    if not os.path.isdir(a.transcripts):
        die("not a directory: %s" % a.transcripts)
    ts = find_transcripts(a.transcripts)
    rows = []
    for t in ts:
        if t["sid"] not in scenarios:
            sys.stderr.write("grade.py: %s names unknown scenario %s; skipped\n" % (t["path"], t["sid"]))
            continue
        rows.append(grade_one(t, scenarios[t["sid"]], not a.no_commands))
    if not rows:
        sys.stderr.write("grade.py: no transcripts under %s (expected run_eval.py output)\n" % a.transcripts)
    if a.out:
        write_out(a.out, rows, scenarios)
        if a.out.endswith(".md"):             # --merge reads the JSON, so keep it beside the table
            write_out(a.out[:-3] + ".json", rows, scenarios)
    else:
        write_out(os.path.join(a.transcripts, "grades.json"), rows, scenarios)
        write_out(os.path.join(a.transcripts, "grades.md"), rows, scenarios)
    return 0


if __name__ == "__main__":
    sys.exit(main())
