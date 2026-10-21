#!/usr/bin/env python3
"""Run the competitive-systems scenarios headlessly, with and without the skill. SPENDS TOKENS.

usage: run_eval.py [--runner claude|codex] [--arms none,skill] [--scenarios S-C,S-E]
                   [--seeds N] [--out DIR] [--jobs N] [--timeout S] [--model M]
                   [--max-budget-usd X] [--dry-run] [--trigger] [--force]

Each run copies its fixture (built by fixtures/make_fixtures.py) into a throwaway work
directory and runs the agent there with the scenario's user message. Arms: `none` has no skill
text; `skill` appends SKILL.md to the system prompt (claude) or prepends it to the prompt
(codex) and adds a snapshot of the skill directory (OUT/skill-snapshot/, without evals/ and
tests/, so the agent can neither edit the shipped skill nor read the rubric). The model's sampling cannot be seeded, so --seeds N only
labels N repeats (s1..sN). Outputs per run under OUT/<arm>/: <SID>.s<k>.md (rendered
transcript) and <SID>.s<k>.jsonl (raw event stream). The work directory lives under
--workroot (default: the system temp directory), outside any repository so the `none` arm
cannot find this skill by walking up; it is kept for grade.py's command probes. A finished run (status ok) is skipped on rerun unless --force.

Isolation: the claude runner passes --setting-sources project, --strict-mcp-config and
--disable-slash-commands (no user settings, MCP servers or skills), but Bash runs unsandboxed
as you, so run the evals in a container or a disposable account (README.md). The codex runner
uses --sandbox workspace-write and an isolated CODEX_HOME holding only auth.json. grade.py
reports any no-skill transcript that touched an installed copy of the skill.

--trigger instead asks the model, once per prompt in trigger.json, which skill it would load
from a list of descriptions (this skill and three decoys, order shuffled per prompt with a
fixed seed); results go to OUT/trigger/. --dry-run prints every command and spends nothing.
Never run from tests/run.sh. Standard library only, Python 3.8+.
"""
import argparse
import concurrent.futures
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCENARIOS = os.path.join(HERE, "scenarios.json")
TRIGGER = os.path.join(HERE, "trigger.json")
MAKE_FIXTURES = os.path.join(HERE, "fixtures", "make_fixtures.py")
SUFFIX = "The relevant files are in the current directory."
TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "Bash"]
SELF_NAME = "competitive-systems"


def default_out():
    """eval-runs/competitive-systems/phase2-runs inside the skills repo, else ./eval-runs."""
    repo = os.path.dirname(os.path.dirname(SKILL))
    if os.path.isdir(os.path.join(repo, "eval-runs")) or os.path.isfile(os.path.join(repo, "scripts", "validate.py")):
        return os.path.join(repo, "eval-runs", "competitive-systems", "phase2-runs")
    return os.path.join(os.getcwd(), "eval-runs")


def die(msg, code=2):
    sys.stderr.write("run_eval.py: %s\n" % msg)
    sys.exit(code)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def skill_description():
    text = read(os.path.join(SKILL, "SKILL.md"))
    m = re.search(r'^description:\s*"(.*)"\s*$', text, re.M)
    if m:
        return m.group(1)
    m = re.search(r"^description:\s*>-?\s*\n((?:[ \t]+.*\n?)+)", text, re.M)
    if m:
        return " ".join(line.strip() for line in m.group(1).splitlines()).strip()
    m = re.search(r"^description:\s*(.*?)\s*$", text, re.M)
    return m.group(1).strip("'\"") if m else ""


def skill_preamble(skill_dir):
    return ("A skill is loaded for this session. Its SKILL.md follows. The skill's directory is "
            "%s; read files under %s/references/ when the skill tells you to, and run its scripts "
            "from there.\n\n%s" % (skill_dir, skill_dir, read(os.path.join(SKILL, "SKILL.md"))))


def snapshot_skill(out):
    """Copy the skill (without evals/ and tests/) under OUT so a run can neither edit the
    shipped files nor see the rubric, and the evaluated version is kept with the results."""
    dest = os.path.join(out, "skill-snapshot", SELF_NAME)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(SKILL, dest, ignore=shutil.ignore_patterns("evals", "tests", "__pycache__", "*.pyc"))
    return dest


def user_prompt(message):
    """The scenario message, closed as a sentence, then the suffix on its own paragraph."""
    msg = message.rstrip()
    if not msg.endswith((".", "?", "!")):
        msg += "."
    return msg + "\n\n" + SUFFIX


def isolated_codex_home(out):
    """An empty CODEX_HOME for both arms holding only the login (auth.json), so installed
    skills, AGENTS.md and config in the real ~/.codex cannot reach either arm."""
    home = os.path.join(out, "codex-home")
    if os.path.isdir(home):
        shutil.rmtree(home)
    os.makedirs(home)
    real = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    auth = os.path.join(real, "auth.json")
    if os.path.isfile(auth):
        shutil.copy2(auth, os.path.join(home, "auth.json"))
    return home


def find_bin(name, env_var):
    path = os.environ.get(env_var) or shutil.which(name)
    return path or name


# ------------------------------------------------------------------ command construction
def scenario_command(runner, arm, message, work, model, budget, skill_dir):
    """Return (argv, display) for one scenario run; display elides the skill text."""
    prompt = user_prompt(message)
    if runner == "claude":
        argv = [find_bin("claude", "CLAUDE_BIN"), "-p", prompt, "--output-format", "stream-json",
                "--verbose", "--setting-sources", "project", "--strict-mcp-config",
                "--disable-slash-commands", "--no-session-persistence",
                "--permission-mode", "dontAsk", "--tools"] + TOOLS + ["--allowedTools"] + TOOLS
        if model:
            argv += ["--model", model]
        if budget:
            argv += ["--max-budget-usd", str(budget)]
        if arm == "skill":
            argv += ["--append-system-prompt", skill_preamble(skill_dir), "--add-dir", skill_dir]
    else:
        if arm == "skill":
            prompt = skill_preamble(skill_dir) + "\n\nUser request:\n" + prompt
        argv = [find_bin("codex", "CODEX_BIN"), "exec", "--json", "--skip-git-repo-check",
                "--ephemeral", "--sandbox", "workspace-write", "-C", work]
        if model:
            argv += ["--model", model]
        if arm == "skill":
            argv += ["--add-dir", skill_dir]
        argv.append(prompt)
    shown = []
    for a in argv:
        if len(a) > 400 and "SKILL.md follows" in a:
            shown.append("<skill preamble + SKILL.md, %d chars>" % len(a))
        elif "\n" in a:                  # bash/zsh ANSI-C quoting keeps one line per command
            shown.append("$'%s'" % a.replace("\\", "\\\\").replace("'", "\\'")
                         .replace("\n", "\\n"))
        else:
            shown.append(shlex.quote(a))
    return argv, "cd %s && %s" % (shlex.quote(work), " ".join(shown))


ROUTER = ("You route user requests to skills. Below are the available skills, each with a name and "
          "a description. Reply with exactly one line: the name of the single skill you would load "
          "for the user's message, or `none` if no skill fits. Do not answer the request itself.\n\n%s")


def trigger_command(runner, prompt, skills, model):
    listing = "\n".join("- %s: %s" % (s["name"], s["description"]) for s in skills)
    if runner == "claude":
        argv = [find_bin("claude", "CLAUDE_BIN"), "-p", prompt, "--output-format", "json",
                "--system-prompt", ROUTER % listing, "--tools", "", "--setting-sources", "project",
                "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
        if model:
            argv += ["--model", model]
    else:
        argv = [find_bin("codex", "CODEX_BIN"), "exec", "--json", "--skip-git-repo-check",
                "--ephemeral", "--sandbox", "read-only"]
        if model:
            argv += ["--model", model]
        argv.append(ROUTER % listing + "\n\nUser message:\n" + prompt)
    return argv, " ".join(shlex.quote(a) if len(a) < 300 else "<router prompt, %d chars>" % len(a)
                          for a in argv)


# ------------------------------------------------------------------ rendering
def clip(text, n):
    text = str(text or "")
    return text[:n] + ("\n...[truncated]" if len(text) > n else "")


def render_claude(events):
    out, model = [], None
    for ev in events:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            model = ev.get("model")
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block["text"].strip():
                    out.append("### assistant\n" + block["text"].strip() + "\n")
                elif block.get("type") == "tool_use":
                    inp = block.get("input", {})
                    key = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    key = " ".join(str(key).split())[:300]
                    body = ""
                    if block["name"] in ("Write", "Edit", "MultiEdit"):
                        body = inp.get("content") or inp.get("new_string") or json.dumps(inp.get("edits", ""))
                    elif block["name"] == "Bash" and len(str(inp.get("command", ""))) > 300:
                        body = inp["command"]
                    out.append("### tool_use %s: %s\n%s" % (
                        block["name"], key, ("```\n" + clip(body, 6000) + "\n```\n") if body else ""))
        elif t == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    out.append("### tool_result\n```\n" + clip(c, 2000) + "\n```\n")
        elif t == "result":
            out.append("### result: %s, turns=%s, cost_usd=%s, duration_ms=%s\n" % (
                ev.get("subtype"), ev.get("num_turns"), ev.get("total_cost_usd"), ev.get("duration_ms")))
    return "\n".join(out), model


def render_codex(events):
    out, usage, turns = [], {}, 0
    for ev in events:
        t = ev.get("type", "")
        item = ev.get("item") or {}
        if t == "item.completed":
            it = item.get("type") or item.get("item_type")
            if it in ("agent_message", "assistant_message") and (item.get("text") or "").strip():
                out.append("### assistant\n" + item["text"].strip() + "\n")
            elif it == "command_execution":
                cmd = " ".join(str(item.get("command", "")).split())
                out.append("### tool_use Bash: %s\n" % cmd[:300])
                out.append("### tool_result\n```\n" + clip(item.get("aggregated_output"), 2000) +
                           "\n```\n")
            elif it == "file_change":
                for ch in item.get("changes", []):
                    out.append("### tool_use file_change: %s\n" % ch.get("path", ""))
        elif t == "turn.completed":
            turns += 1
            usage = ev.get("usage") or usage
    out.append("### result: success, turns=%d, cost_usd=None, tokens=%s\n" % (turns, json.dumps(usage)))
    return "\n".join(out), None


def parse_events(raw):
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def run_proc(argv, cwd, timeout, extra_env=None):
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DEVKIT_DEV_PY"):
        env.pop(k, None)
    env.update(extra_env or {})
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout, env=env)
        return p.stdout, p.stderr, ("ok" if p.returncode == 0 else "exit %d" % p.returncode)
    except subprocess.TimeoutExpired as e:
        dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")  # noqa: E731
        return dec(e.stdout), dec(e.stderr), "timeout"
    except OSError as e:
        return "", str(e), "cannot start: %s" % e


# ------------------------------------------------------------------ scenario runs
def runner_env(a):
    return {"CODEX_HOME": a.codex_home} if getattr(a, "codex_home", None) else {}


def run_one(job, a):
    arm, s, seed, work, argv = job["arm"], job["scenario"], job["seed"], job["work"], job["argv"]
    base = os.path.join(a.out, arm, "%s.s%d" % (s["id"], seed))
    md = base + ".md"
    if os.path.exists(md) and not a.force and "\nstatus: ok\n" in read(md)[:4000]:
        return arm, s["id"], seed, "skipped (done)", 0.0
    if os.path.exists(work):
        shutil.rmtree(work)
    shutil.copytree(os.path.join(a.fixtures, s["fixture"]), work, symlinks=True)
    t0 = time.time()
    raw, err, status = run_proc(argv, work, a.timeout, runner_env(a))
    events = parse_events(raw)
    body, model = (render_claude if a.runner == "claude" else render_codex)(events)
    with open(base + ".jsonl", "w", encoding="utf-8") as f:
        f.write(raw)
    header = "# %s / %s / s%d\n\nstatus: %s\nwork: %s\nmodel: %s\nrunner: %s\nwall_s: %.0f\nuser: %s\n\n" % (
        arm, s["id"], seed, status, work, model, a.runner, time.time() - t0,
        user_prompt(s["user_message"]).replace("\n\n", " "))
    with open(md, "w", encoding="utf-8") as f:
        f.write(header + body)
        if err.strip():
            f.write("\n### stderr\n```\n" + err[-4000:] + "\n```\n")
    return arm, s["id"], seed, status, time.time() - t0


def scenario_jobs(a, scenarios):
    jobs = []
    for seed in range(1, a.seeds + 1):
        for arm in a.arms:
            for s in scenarios:
                work = os.path.join(a.workroot, arm, "%s.s%d" % (s["id"], seed))
                argv, shown = scenario_command(a.runner, arm, s["user_message"], work, a.model, a.max_budget_usd,
                                               a.skill_dir)
                jobs.append({"arm": arm, "scenario": s, "seed": seed, "work": work, "argv": argv, "shown": shown})
    return jobs


# ------------------------------------------------------------------ trigger routing
def trigger_jobs(a):
    data = json.loads(read(TRIGGER))
    me = {"name": SELF_NAME, "description": skill_description()}
    items = [(p, True) for p in data["should_fire"]] + [(p, False) for p in data["should_not_fire"]]
    jobs = []
    for i, (prompt, should) in enumerate(items):
        skills = [me] + list(data["decoys"])
        random.Random(i).shuffle(skills)
        argv, shown = trigger_command(a.runner, prompt, skills, a.model)
        jobs.append({"i": i, "prompt": prompt, "should_fire": should, "argv": argv, "shown": shown,
                     "names": [s["name"] for s in skills]})
    return jobs


def parse_choice(text, names):
    t = (text or "").strip().strip("`*. ").lower()
    for n in sorted(names, key=len, reverse=True):
        if re.search(r"(^|[^a-z-])%s($|[^a-z-])" % re.escape(n.lower()), t):
            return n
    return "none" if "none" in t else "unparsed"


def run_trigger(jobs, a):
    tdir = os.path.join(a.out, "trigger")
    os.makedirs(tdir, exist_ok=True)

    def one(job):
        raw, err, status = run_proc(job["argv"], tdir, a.timeout, runner_env(a))
        text, cost, model = "", None, None
        if a.runner == "claude":
            try:
                d = json.loads(raw)
                text, cost = d.get("result", ""), d.get("total_cost_usd")
                model = ",".join(sorted((d.get("modelUsage") or {}).keys())) or None
            except ValueError:
                text = raw
        else:
            msgs = [e["item"]["text"] for e in parse_events(raw)
                    if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"]
            text = msgs[-1] if msgs else ""
        with open(os.path.join(tdir, "%02d.json" % job["i"]), "w", encoding="utf-8") as f:
            json.dump({"prompt": job["prompt"], "raw": raw, "stderr": err[-2000:], "status": status}, f, indent=1)
        return dict(job, argv=None, status=status, answer=text.strip()[:200], cost_usd=cost, model=model,
                    choice=parse_choice(text, job["names"]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        rows = list(ex.map(one, jobs))
    fired = [r for r in rows if r["choice"] == SELF_NAME]
    tp = sum(1 for r in fired if r["should_fire"])
    n_should = sum(1 for r in rows if r["should_fire"])
    summary = {"schema": "cs-evals-trigger/1", "runner": a.runner,
               "models": sorted(set(r["model"] for r in rows if r["model"])),
               "n": len(rows), "should_fire": n_should, "fired": len(fired), "true_positives": tp,
               "precision": round(tp / len(fired), 3) if fired else None,
               "recall": round(tp / n_should, 3) if n_should else None,
               "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
               "rows": [dict((k, r[k]) for k in ("i", "prompt", "should_fire", "choice", "answer", "status", "cost_usd"))
                        for r in rows]}
    with open(os.path.join(tdir, "trigger-results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    for r in rows:
        print("%-5s %-22s %s" % ("fire" if r["should_fire"] else "skip", r["choice"], r["prompt"][:70]))
    print("precision %s recall %s (fired %d, true positives %d of %d); cost %.4f USD; wrote %s" % (
        summary["precision"], summary["recall"], len(fired), tp, n_should, summary["cost_usd"],
        os.path.join(tdir, "trigger-results.json")))


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the competitive-systems evals (spends tokens unless --dry-run).")
    ap.add_argument("--runner", choices=("claude", "codex"), default="claude")
    ap.add_argument("--arms", default="none,skill")
    ap.add_argument("--scenarios", default="", help="comma-separated ids (default: all)")
    ap.add_argument("--seeds", type=int, default=2, help="repeats per cell, labelled s1..sN (default 2)")
    ap.add_argument("--out", default=None, help="output directory (default: %s)" % default_out())
    ap.add_argument("--fixtures", default=None, help="fixture directory (default: OUT/fixtures, built if missing)")
    ap.add_argument("--workroot", default=os.path.join(tempfile.gettempdir(), "cs-evals-work"),
                    help="parent of the per-run work directories; keep it outside any repository "
                         "so the agent cannot find this skill by walking up (default: %(default)s)")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=1500, help="seconds per run")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-budget-usd", type=float, default=3.0, help="per-run cap passed to claude (0 = none)")
    ap.add_argument("--dry-run", action="store_true", help="print the commands; spend nothing")
    ap.add_argument("--trigger", action="store_true", help="run the trigger-routing check instead")
    ap.add_argument("--force", action="store_true", help="rerun finished runs")
    a = ap.parse_args(argv)
    a.out = os.path.abspath(a.out or default_out())
    a.fixtures = os.path.abspath(a.fixtures or os.path.join(a.out, "fixtures"))
    a.workroot = os.path.abspath(a.workroot)
    a.arms = [x for x in a.arms.split(",") if x]
    if any(x not in ("none", "skill") for x in a.arms):
        die("--arms takes none and/or skill: %s" % ",".join(a.arms))
    if a.seeds < 1 or a.jobs < 1:
        die("--seeds and --jobs must be at least 1")
    a.codex_home = None
    if a.runner == "codex" and not a.dry_run:
        a.codex_home = isolated_codex_home(a.out)
    if a.trigger:
        jobs = trigger_jobs(a)
        if a.dry_run:
            for j in jobs:
                print("$ " + j["shown"])
            print("# %d trigger commands (dry run: nothing was run)" % len(jobs))
            return 0
        if shutil.which(find_bin(a.runner, a.runner.upper() + "_BIN")) is None:
            die("%s CLI not found on PATH" % a.runner, 3)
        run_trigger(jobs, a)
        return 0
    all_sc = json.loads(read(SCENARIOS))
    ids = [x for x in a.scenarios.split(",") if x]
    unknown = [x for x in ids if x not in [s["id"] for s in all_sc]]
    if unknown:
        die("unknown scenario(s) %s" % ", ".join(unknown))
    scenarios = [s for s in all_sc if not ids or s["id"] in ids]
    a.skill_dir = os.path.join(a.out, "skill-snapshot", SELF_NAME)
    jobs = scenario_jobs(a, scenarios)
    if a.dry_run:
        for j in jobs:
            print("$ " + j["shown"])
        print("# %d runs: %d scenario(s) x %d arm(s) x %d seed(s) (dry run: nothing was run)" % (
            len(jobs), len(scenarios), len(a.arms), a.seeds))
        return 0
    if shutil.which(find_bin(a.runner, a.runner.upper() + "_BIN")) is None:
        die("%s CLI not found on PATH" % a.runner, 3)
    if "skill" in a.arms:
        snapshot_skill(a.out)
    if not os.path.isdir(a.fixtures):
        subprocess.run([sys.executable, MAKE_FIXTURES, "--out", a.fixtures], check=True)
    for arm in a.arms:
        os.makedirs(os.path.join(a.out, arm), exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(run_one, j, a) for j in jobs]
        for fut in concurrent.futures.as_completed(futs):
            arm, sid, seed, status, secs = fut.result()
            print("%-5s %-4s s%d %-16s %4.0f s" % (arm, sid, seed, status, secs), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
