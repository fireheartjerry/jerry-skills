#!/usr/bin/env python3
"""Measure whether the skill's description routes prompts to it. Stdlib only, python3 3.8+.

Usage:
  route_check.py --offline
  route_check.py --harness claude|codex [--trials 3] [--jobs 4] [--description-file F]
                 [--label NAME] [--model M] [--out DIR] [--only fire|no_fire|either]
                 [--ids F01,N03] [--timeout 180] [--codex-auth FILE] [--force] [--dry-run]
  route_check.py --summarize DIR [DIR ...]

--offline validates routing.json (schema, counts, category minimums, cue and
near-miss coverage) against the SKILL.md description and against every
candidate under evals/descriptions/ (queued/ included), so a candidate is
never measured on a set that lacks a near-miss for one of its cues, and prints
the cell count and estimated cost. It needs no harness binary and spends
nothing.

Live mode spends tokens. Per prompt and trial it builds a temp project
<tmp>/project (README, one python module, git init plus one commit) and copies
the skill without tests/ and evals/ to where the harness loads it: Claude Code
<tmp>/project/.claude/skills/high-agency (part of the commit), Codex
<tmp>/codex_home/skills/high-agency (outside the project). It replaces the
frontmatter description with --description-file if given, and runs, with cwd
<tmp>/project and a --timeout cap (default 180 s):
  claude -p --setting-sources project --strict-mcp-config --no-session-persistence
         --permission-mode default --max-turns 2 --output-format stream-json
         --verbose <prompt>
  CODEX_HOME=<tmp>/codex_home codex exec --ignore-user-config --skip-git-repo-check
         -s read-only -C <tmp>/project --json <prompt>
Fired (Claude): a Skill tool_use naming high-agency, or a tool call (Read, or a
shell read) of high-agency/SKILL.md. Fired (Codex): an item event in the JSON
output (a command, message or reasoning step) mentions high-agency/SKILL.md;
session metadata does not count. Each cell writes <out>/<label>/<prompt_id>/<trial>.json
and the raw transcript beside it (<trial>.jsonl). A Claude cell whose init event
does not list the skill is a setup error and is not recorded.

Only the first decision is measured, so routing cells get no write access:
Claude runs in the default permission mode, where a headless run is refused
any tool that needs approval (edits, most shell commands); the attempt still
shows in the transcript and still counts as fired. Codex runs with -s
read-only. The routing numbers in results.md were measured with
bypassPermissions, before this change, and were not re-measured.

A cell is skipped when <label>/<id>/<trial>.json exists with the same
harness, model, effective description (SHA-256 of the candidate, or of the
SKILL.md description when no --description-file is given) and --siblings
setting as this run; any other cell is re-run, and --force re-runs every cell.
--siblings installs the other skills from the same skills/ directory too
(label suffix -siblings), so a prompt another skill owns can route there; the
cell then records which skill loaded instead. Without it the project holds
high-agency alone, so its false-fire rate is a single-skill number. Ctrl-C,
SIGTERM and SIGHUP stop the grid: no new cell starts, running cells are
killed and not recorded, and the run exits 130. A cell whose harness
reports an error (non-success result other than the turn cap, a rejected
rate-limit event, a usage-limit or API-error stub, a Codex error event, no
output) or that hits --timeout is not recorded; the run counts these errors
and exits 1 when there was any.
Isolation: --setting-sources project keeps the user's own skills, CLAUDE.md and
memory out of the run; --strict-mcp-config with no config drops MCP servers.
Codex gets a CODEX_HOME holding only the skill (plus auth.json when
--codex-auth is given; otherwise CODEX_API_KEY must be set).

CLAUDE_CONFIG_DIR is left as you set it (it selects your login).

--summarize takes label directories (or their parent) and prints fire,
false-fire and either rates with Wilson 95% intervals, per-category rates,
per-prompt trial agreement, cost, and a Markdown table for results.md.
"""
import argparse
import glob
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
ROUTING = os.path.join(HERE, "routing.json")
DESCRIPTIONS = os.path.join(HERE, "descriptions")
LABELS = ("fire", "no_fire", "either")
MIN_COUNT = {"fire": 24, "no_fire": 24, "either": 6}
CATEGORY_MIN = {
    "fire": {"imperative-multistep": 6, "check-question": 6, "resume": 3,
             "ownership-phrase": 3, "plan-or-brief": 3, "long-job": 3, "investigation": 3},
    "no_fire": {"pure-explain": 6, "discuss-with-stop-cue": 5, "concept-question": 4,
                "other-skill": 5, "about-the-skill": 3, "status-nothing-in-flight": 3,
                "cue-collision": 5, "knowledge-check": 3, "sibling": 4},
}
# Phase-2 routing run (Claude Code 2.1.306, claude-opus-5-5, 2026-10-21):
# 540 cells, $36.60, so $0.068 per cell; 4 to 13 s each. Codex reports no cost.
EST_COST_USD = {"claude": 0.068, "codex": None}
EST_WALL_S = 10


def _load_run():
    spec = importlib.util.spec_from_file_location("ha_run", os.path.join(HERE, "run.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_RUN = _load_run()
classify_stream = _RUN.classify_stream


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def description(skill_md):
    """The frontmatter description, folded to one line (plain or block scalar)."""
    with open(skill_md, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise SystemExit("no frontmatter in %s" % skill_md)
    lines = m.group(1).splitlines()
    for i, line in enumerate(lines):
        if line.startswith("description:"):
            rest = line[len("description:"):].strip()
            if rest and rest[0] not in ">|":
                return rest.strip("\"'")
            body = []
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith(" "):
                    break
                body.append(nxt.strip())
            return " ".join(b for b in body if b)
    raise SystemExit("no description in %s" % skill_md)


def cue_patterns(desc):
    """Quoted cue phrases from the description as regexes; 'X' matches any text."""
    pats = []
    for cue in re.findall(r'"([^"]+)"', desc):
        rx = r"\s+".join(".+" if w == "X" else re.escape(w) for w in cue.split())
        pats.append((cue, re.compile(rx, re.I)))
    return pats


def content_words(text):
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 4}


def validate(data, desc):
    """Return a list of problems with routing.json (empty when valid)."""
    errs = []
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, list) or not prompts:
        return ["routing.json needs a non-empty 'prompts' list"]
    seen_ids, seen_text, cats = set(), set(), {}
    for p in prompts:
        pid = p.get("id")
        if not all(isinstance(p.get(k), str) and p.get(k) for k in ("id", "text", "label", "category")):
            errs.append("prompt %r needs string id, text, label and category" % pid)
            continue
        if p["label"] not in LABELS:
            errs.append("%s: label %r not in %s" % (pid, p["label"], LABELS))
        if pid in seen_ids:
            errs.append("duplicate id %s" % pid)
        norm = " ".join(p["text"].lower().split())
        if norm in seen_text:
            errs.append("duplicate text in %s" % pid)
        seen_ids.add(pid)
        seen_text.add(norm)
        cats.setdefault((p["label"], p["category"]), []).append(pid)
    by_label = {lab: [p for p in prompts if p.get("label") == lab] for lab in LABELS}
    for lab, n in MIN_COUNT.items():
        if len(by_label[lab]) < n:
            errs.append("%d %s prompts, need at least %d" % (len(by_label[lab]), lab, n))
    for lab, mins in CATEGORY_MIN.items():
        for cat, n in mins.items():
            have = len(cats.get((lab, cat), []))
            if have < n:
                errs.append("%s/%s has %d prompts, need at least %d" % (lab, cat, have, n))
    for (lab, cat), ids in sorted(cats.items()):
        if len(ids) < 3:
            errs.append("%s/%s has %d prompt(s); every category needs at least 3" % (lab, cat, len(ids)))
    for cue, rx in cue_patterns(desc):
        if not any(rx.search(p["text"]) for p in by_label["fire"]):
            errs.append("description cue %r appears in no fire prompt" % cue)
    for cue, rx in cue_patterns(desc):
        # A cue with no near-miss cannot show a false fire: every quoted cue
        # needs a no_fire prompt that contains it (a knowledge question, a
        # discussion, another skill's task).
        if not any(rx.search(p["text"]) for p in by_label["no_fire"]):
            errs.append("description cue %r appears in no no_fire prompt (needs a near-miss)" % cue)
    words = content_words(desc)
    near = [p["id"] for p in by_label["no_fire"] if content_words(p["text"]) & words]
    if len(near) < 8:
        errs.append("only %d no_fire prompts share a content word with the description, need 8" % len(near))
    return errs


def candidate_files(root=DESCRIPTIONS):
    """Every description candidate (*.txt) under *root*, queued/ included, sorted."""
    out = []
    for d, _dirs, names in os.walk(root):
        out.extend(os.path.join(d, n) for n in names if n.endswith(".txt"))
    return sorted(out)


def validate_candidates(data, files):
    """validate() against each candidate file; problems are prefixed with the file."""
    errs = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        rel = os.path.relpath(f, HERE)
        errs.extend("%s: %s" % (rel, e) for e in validate(data, text))
    return errs


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / float(n)
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, mid - half), min(1.0, mid + half))


def rate(k, n):
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return "%d/%d (%d%%, CI %d-%d%%)" % (k, n, round(100.0 * k / n), round(100 * lo), round(100 * hi))


# ------------------------------------------------------------------ live cells

SKILL_IGNORE = shutil.ignore_patterns("tests", "evals", "__pycache__", ".runs")


def sibling_skills():
    """Other skill directories next to this one (each holds a SKILL.md)."""
    root = os.path.dirname(SKILL_DIR)
    return sorted(n for n in os.listdir(root)
                  if n != os.path.basename(SKILL_DIR)
                  and os.path.isfile(os.path.join(root, n, "SKILL.md")))


def make_project(tmp, harness, desc_text, siblings=False):
    """Build <tmp>/project with the skill installed for *harness*; return its path.

    siblings=True also installs every sibling skill from the same skills/
    directory, so a prompt another skill owns can route there instead.
    """
    proj = os.path.join(tmp, "project")
    os.makedirs(proj)
    if harness == "claude":
        skills = os.path.join(proj, ".claude", "skills")
    else:
        skills = os.path.join(tmp, "codex_home", "skills")
    skill = os.path.join(skills, "high-agency")
    shutil.copytree(SKILL_DIR, skill, ignore=SKILL_IGNORE)
    if desc_text:
        replace_description(os.path.join(skill, "SKILL.md"), desc_text)
    if siblings:
        for name in sibling_skills():
            shutil.copytree(os.path.join(os.path.dirname(SKILL_DIR), name),
                            os.path.join(skills, name), ignore=SKILL_IGNORE)
    with open(os.path.join(proj, "README.md"), "w") as fh:
        fh.write("# stockfeed\n\nSyncs stock levels from a supplier CSV feed into the shop database.\n\n"
                 "Run `python3 -m unittest` before pushing.\n")
    with open(os.path.join(proj, "stockfeed.py"), "w") as fh:
        fh.write('"""Parse a supplier feed."""\nimport csv\n\n\ndef load(path):\n'
                 '    with open(path, newline="") as fh:\n        return list(csv.DictReader(fh))\n')
    git = ["git", "-c", "user.name=eval", "-c", "user.email=eval@example.invalid", "-c", "commit.gpgsign=false"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "initial"]):
        subprocess.run(git + args, cwd=proj, capture_output=True, check=True, env=_RUN.clean_env())
    return proj


def replace_description(skill_md, text):
    with open(skill_md, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("description:"))
    end = start + 1
    while end < len(lines) and lines[end].startswith(" "):
        end += 1
    words, rows, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > 76:
            rows.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    rows.append(cur)
    lines[start:end] = ["description: >-"] + ["  " + r for r in rows]
    with open(skill_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def fired_claude(stream):
    """(fired, evidence, cost, listed, other): listed is whether the init event
    lists the skill; other names a different skill the agent loaded first."""
    cost, listed, hit, other = None, None, None, None
    for line in stream.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "system" and isinstance(obj.get("skills"), list):
            listed = "high-agency" in obj["skills"]
        if obj.get("type") == "result":
            cost = obj.get("total_cost_usd")
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            blob = json.dumps(inp)
            if hit is None and b.get("name") == "Skill" and "high-agency" in blob:
                hit = "Skill: " + blob[:80]
            elif hit is None and "high-agency/SKILL.md" in blob:
                hit = "%s: %s" % (b.get("name"), blob[:80])
            elif other is None and hit is None and b.get("name") == "Skill":
                other = str(inp.get("skill") or inp.get("name") or blob[:60])
    if hit is None and other:
        return False, "loaded another skill: %s" % other, cost, listed, other
    return hit is not None, hit or "no skill load in 2 turns", cost, listed, other


def fired_codex(stream):
    """(fired, evidence): an item event (not session metadata) names the skill file."""
    for line in stream.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        item = obj.get("item") if isinstance(obj, dict) else None
        if isinstance(item, dict) and "high-agency/SKILL.md" in json.dumps(item):
            return True, "%s: %s" % (item.get("type"), json.dumps(item)[:80])
    return False, "no skill load in the output"


def effective_description(a):
    """The description this run installs: the candidate, else SKILL.md's own."""
    return getattr(a, "desc_text", None) or description(os.path.join(SKILL_DIR, "SKILL.md"))


def desc_sha(text):
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def cell_key(a):
    """What a recorded cell must match to be reused."""
    return {"harness": getattr(a, "harness", None) or "claude",
            "model": a.model or "harness default",
            "description_sha256": desc_sha(effective_description(a)),
            "siblings": bool(getattr(a, "siblings", False))}


def cached(out, a):
    """True when *out* holds a cell recorded with this run's harness, model,
    effective description (by SHA-256) and sibling setting."""
    if a.force or not os.path.exists(out):
        return False
    try:
        rec = load_json(out)
    except ValueError:
        return False
    return isinstance(rec, dict) and all(rec.get(k) == v for k, v in cell_key(a).items())


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def run_cell(p, trial, a):
    """Run one prompt once. Returns (status, error): error is True for a cell
    that was not recorded because the harness failed."""
    out = os.path.join(a.out, a.label, p["id"], "%d.json" % trial)
    if not a.dry_run and cached(out, a):
        return "skip (done)", False
    if _RUN.STOP.is_set():
        raise _RUN.Interrupted("stopped before the cell started")
    tmp = _RUN.track_dir(tempfile.mkdtemp(prefix="ha-route-"))
    try:
        proj = make_project(tmp, a.harness, a.desc_text, getattr(a, "siblings", False))
        env = _RUN.clean_env()
        if a.harness == "claude":
            cmd = ["claude", "-p", "--setting-sources", "project", "--strict-mcp-config",
                   "--no-session-persistence", "--permission-mode", "default",
                   "--max-turns", "2", "--output-format", "stream-json", "--verbose"]
        else:
            env["CODEX_HOME"] = os.path.join(tmp, "codex_home")
            if a.codex_auth:
                shutil.copy(a.codex_auth, os.path.join(env["CODEX_HOME"], "auth.json"))
            cmd = ["codex", "exec", "--ignore-user-config", "--skip-git-repo-check", "-s", "read-only",
                   "-C", proj, "--json"]
        if a.model:
            cmd += ["--model" if a.harness == "claude" else "-m", a.model]
        cmd.append(p["text"])
        if a.dry_run:
            return "dry-run: " + " ".join(cmd[:-1]) + " " + repr(p["text"][:60]), False
        started = time.time()
        # Own process group, killed on Ctrl-C, SIGTERM or SIGHUP (run.py's run_grid).
        rc, stdout, _err, timed_out = _RUN.run_tracked(cmd, timeout=a.timeout, cwd=proj, env=env)
        other = None
        if a.harness == "claude":
            fired, why, cost, listed, other = fired_claude(stdout)
        else:
            (fired, why), cost, listed = fired_codex(stdout), None, None
        rec = {"id": p["id"], "label": p["label"], "category": p["category"], "trial": trial,
               "fired": fired, "evidence": why, "other_skill": other,
               "description": a.desc_text or "as in SKILL.md", "wall_s": round(time.time() - started, 1),
               "cost_usd": cost, "exit": rc, "timed_out": timed_out, "skill_listed": listed}
        rec.update(cell_key(a))
        if rc != 0 and not stdout.strip():
            return "HARNESS ERROR (exit %d, no output): check login and flags" % rc, True
        if timed_out:
            return "HARNESS ERROR (timed out after %ds); cell not recorded" % a.timeout, True
        why = classify_stream(stdout)
        if why:
            return "HARNESS ERROR (%s); cell not recorded" % why, True
        if listed is False:
            return "SETUP ERROR: the harness did not list high-agency; cell not recorded", True
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out[:-len(".json")] + ".jsonl", "w") as fh:
            fh.write(stdout)  # raw transcript, kept for audit
        write_json(out, rec)
        state = "FIRED" if fired else ("quiet (%s)" % why if other else "quiet")
        return "%s in %.0fs" % (state, rec["wall_s"]), False
    finally:
        _RUN.release_dir(tmp)


# ------------------------------------------------------------------ summarize

def load_cells(d):
    """Cells of one label directory (<label>/<prompt_id>/<trial>.json); a file
    that does not parse is named on stderr and skipped."""
    out = []
    for f in sorted(glob.glob(os.path.join(d, "*", "*.json"))):
        try:
            out.append(load_json(f))
        except ValueError:
            print("skipping unreadable cell %s" % f, file=sys.stderr)
    return out


def label_dirs(dirs):
    """Expand each argument: a label directory stays, a parent yields its labels."""
    out = []
    for d in dirs:
        if glob.glob(os.path.join(d, "*", "*.json")):
            out.append(d)
        else:
            found = glob.glob(os.path.join(d, "*", "*", "*.json"))
            out.extend(sorted({os.path.dirname(os.path.dirname(f)) for f in found}) or [d])
    return out


def label_stats(cells):
    return {lab: (sum(1 for c in cells if c["label"] == lab and c["fired"]),
                  sum(1 for c in cells if c["label"] == lab)) for lab in LABELS}


def summarize(dirs):
    for d in label_dirs(dirs):
        cells = load_cells(d)
        if not cells:
            print("%s: no cells" % d)
            continue
        st = label_stats(cells)
        harnesses = sorted({c.get("harness") or "unknown" for c in cells})
        if len(harnesses) > 1:
            print("WARNING %s mixes harnesses (%s); give each harness its own --label" % (
                d, ", ".join(harnesses)))
        others = [c for c in cells if c.get("other_skill")]
        cost = sum(c.get("cost_usd") or 0 for c in cells)
        print("== %s (%d cells, cost $%.2f, median wall %.0fs)" % (
            d, len(cells), cost, sorted(c["wall_s"] for c in cells)[len(cells) // 2]))
        print("harness %s, siblings installed: %s" % (
            ", ".join(harnesses), "yes" if any(c.get("siblings") for c in cells) else "no"))
        print("fire rate        %s" % rate(*st["fire"]))
        if others:
            print("loaded another skill instead: %d cell(s): %s" % (len(others), ", ".join(
                sorted({"%s->%s" % (c["id"], c["other_skill"]) for c in others}))))
        print("false-fire rate  %s" % rate(*st["no_fire"]))
        print("either fired     %s" % rate(*st["either"]))
        cats = sorted({(c["label"], c["category"]) for c in cells})
        for lab, cat in cats:
            cs = [c for c in cells if c["label"] == lab and c["category"] == cat]
            print("  %-8s %-26s %s" % (lab, cat, rate(sum(1 for c in cs if c["fired"]), len(cs))))
        ids = sorted({c["id"] for c in cells})
        agree = []
        for pid in ids:
            fs = [c["fired"] for c in cells if c["id"] == pid]
            agree.append(max(sum(fs), len(fs) - sum(fs)) / float(len(fs)))
        split = [pid for pid, ag in zip(ids, agree) if ag < 1]
        print("per-prompt trial agreement: mean %d%%; split prompts: %s" % (
            round(100 * sum(agree) / len(agree)), ", ".join(split) or "none"))
        print("\n| Set | Fired k/n | Rate (Wilson 95%) |\n|---|---|---|")
        for lab, name in (("fire", "fire (should load)"), ("no_fire", "no_fire (false fire)"), ("either", "either")):
            k, n = st[lab]
            lo, hi = wilson(k, n)
            print("| %s | %d/%d | %s |" % (name, k, n, "-" if n == 0 else "%d%% (%d-%d%%)" % (
                round(100.0 * k / n), round(100 * lo), round(100 * hi))))
        print()
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        description="Routing measurement for the high-agency skill. --offline and --summarize "
                    "spend nothing; --harness runs live cells and spends tokens (about $0.07 per "
                    "Claude Code cell; --offline prints the estimate for a grid).")
    ap.add_argument("--offline", action="store_true",
                    help="validate routing.json against the SKILL.md description and print the plan; "
                         "no harness needed, spends nothing")
    ap.add_argument("--harness", choices=("claude", "codex"),
                    help="run live cells with this harness (spends tokens)")
    ap.add_argument("--summarize", nargs="+", metavar="DIR",
                    help="print rates for label directories (or their parent) and exit")
    ap.add_argument("--trials", type=int, default=3, help="trials per prompt (default 3)")
    ap.add_argument("--jobs", type=int, default=4, help="cells run at once (default 4)")
    ap.add_argument("--description-file",
                    help="replace the frontmatter description with this file's text (a candidate)")
    ap.add_argument("--label",
                    help="name of the result directory under --out (default: the description "
                         "file's base name, else 'current')")
    ap.add_argument("--model",
                    help="agent model passed to the harness (default: harness default; the "
                         "published numbers used claude-opus-5-5)")
    ap.add_argument("--out", default=os.path.join(HERE, ".runs", "routing"),
                    help="results root (default evals/.runs/routing)")
    ap.add_argument("--only", choices=LABELS, help="run only prompts with this label (default: all)")
    ap.add_argument("--ids", help="comma-separated prompt ids to run (default: all)")
    ap.add_argument("--timeout", type=int, default=180,
                    help="seconds per cell before it counts as a harness error (default 180)")
    ap.add_argument("--codex-auth",
                    help="auth.json copied into each Codex cell's temp CODEX_HOME "
                         "(default: none; CODEX_API_KEY must then be set)")
    ap.add_argument("--siblings", action="store_true",
                    help="also install every sibling skill from the same skills/ directory, so a "
                         "misroute to another skill shows (default: high-agency alone)")
    ap.add_argument("--force", action="store_true",
                    help="re-run cells that already have a matching result (default: skip them)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build each cell and print the harness command without running it")
    a = ap.parse_args(argv)
    if a.summarize:
        return summarize(a.summarize)
    if not a.offline and not a.harness:
        ap.error("pass --offline, --harness claude|codex, or --summarize DIR")
    data = load_json(ROUTING)
    errs = validate(data, description(os.path.join(SKILL_DIR, "SKILL.md")))
    if a.offline:
        errs += validate_candidates(data, candidate_files())
    elif a.description_file:
        errs += validate_candidates(data, [a.description_file])
    if errs:
        print("routing.json: %d problem(s)" % len(errs), file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)
        return 1
    ids = set(a.ids.split(",")) if a.ids else None
    prompts = [p for p in data["prompts"] if (not a.only or p["label"] == a.only) and (not ids or p["id"] in ids)]
    counts = {lab: sum(1 for p in prompts if p["label"] == lab) for lab in LABELS}
    harness = a.harness or "claude"
    cells = len(prompts) * a.trials
    print("routing.json OK: %d prompts (fire %d, no_fire %d, either %d)" % (
        len(prompts), counts["fire"], counts["no_fire"], counts["either"]))
    cost = ("$%.2f" % (cells * EST_COST_USD[harness])) if EST_COST_USD[harness] else "n/a (not reported)"
    print("plan: %d cells (%d prompts x %d trials), harness %s, est. cost %s, est. wall %d min at %d jobs" % (
        cells, len(prompts), a.trials, harness, cost,
        math.ceil(cells * EST_WALL_S / 60.0 / max(1, a.jobs)), a.jobs))
    if a.offline:
        print("candidates OK: %d description files validated" % len(candidate_files()))
        print("offline: nothing run, no harness binary needed")
        return 0
    if shutil.which(a.harness) is None and not a.dry_run:
        print("route_check: `%s` not found on PATH; install %s first" % (
            a.harness, "Claude Code" if a.harness == "claude" else "the Codex CLI"), file=sys.stderr)
        return 2
    if a.codex_auth and not os.path.isfile(a.codex_auth):
        ap.error("--codex-auth: no such file: %s" % a.codex_auth)
    a.desc_text = None
    if a.description_file:
        with open(a.description_file, encoding="utf-8") as fh:
            a.desc_text = " ".join(fh.read().split())
    base = os.path.splitext(os.path.basename(a.description_file))[0] if a.description_file else "current"
    a.label = a.label or (base + ("-siblings" if a.siblings else ""))
    todo = [(p, t) for p in prompts for t in range(1, a.trials + 1)]
    lock = threading.Lock()
    errors = []

    def work(item):
        p, t = item
        try:
            status, err = run_cell(p, t, a)
        except _RUN.Interrupted as e:
            status, err = "INTERRUPTED (%s)" % e, True
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # one broken cell must not stop the grid
            status, err = "ERROR %r" % e, True
        with lock:
            if err:
                errors.append((p["id"], t))
            print("%-4s %-8s t%d  %s" % (p["id"], p["label"], t, status), flush=True)

    if _RUN.run_grid(todo, max(1, a.jobs), work):
        print("route_check: interrupted; no new cell started, running cells were killed and not "
              "recorded", file=sys.stderr)
        return 130
    if a.dry_run:
        return 0
    summarize([os.path.join(a.out, a.label)])
    if errors:
        print("route_check: %d cell(s) errored and were not recorded: %s; re-run the same command "
              "to retry them" % (len(errors), ", ".join("%s/t%d" % e for e in errors)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
