#!/usr/bin/env python3
"""Run the competitive-systems scenarios headlessly, with and without the skill. SPENDS TOKENS.

usage: run_eval.py [--runner claude|codex] [--arms none,skill] [--scenarios S-C,S-E]
                   [--seeds N] [--out DIR] [--jobs N] [--timeout S] [--model M]
                   [--max-budget-usd X] [--max-total-usd X] [--max-runs N] [--yes]
                   [--dry-run] [--trigger] [--force]

Spend guards: a live run needs --yes and first prints the worst case (runs x per-run cap).
--max-total-usd (default 1.00) stops submitting runs once the reported cost reaches it;
codex reports no cost, so a live codex run needs --max-runs. A timeout kills the agent's
whole process group, so commands it started do not outlive the run.

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
uses --sandbox workspace-write and an isolated CODEX_HOME holding only auth.json, created
in a private temporary directory (mode 0700, outside any repository) and deleted on exit. grade.py
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
import signal
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
    """eval-runs/competitive-systems/runs inside the skills repo, else ./eval-runs."""
    repo = os.path.dirname(os.path.dirname(SKILL))
    if os.path.isdir(os.path.join(repo, "eval-runs")) or os.path.isfile(os.path.join(repo, "scripts", "validate.py")):
        return os.path.join(repo, "eval-runs", "competitive-systems", "runs")
    return os.path.join(os.getcwd(), "eval-runs")


def die(msg, code=2):
    sys.stderr.write("run_eval.py: %s\n" % msg)
    sys.exit(code)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def decoy_description(d):
    """A decoy's description, or for {"from_skill": NAME} the live description of the sibling
    skill in this repository, so the routing check uses real neighbours and never drifts."""
    if d.get("from_skill"):
        path = os.path.join(os.path.dirname(SKILL), d["from_skill"], "SKILL.md")
        if os.path.isfile(path):
            return skill_description(path)
    return d.get("description", "")


def skill_description(path=None):
    text = read(path or os.path.join(SKILL, "SKILL.md"))
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


def isolated_codex_home():
    """An empty CODEX_HOME for both arms holding only the login (auth.json), so installed
    skills, AGENTS.md and config in the real ~/.codex cannot reach either arm. It is a private
    temporary directory (0700) outside any repository, so the credential never lands in a
    working tree; the caller deletes it."""
    home = tempfile.mkdtemp(prefix="cs-evals-codex-home-")
    os.chmod(home, 0o700)
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
    """Run argv in its own session; on timeout kill the whole process group, so the agent's
    Bash children die with it."""
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DEVKIT_DEV_PY"):
        env.pop(k, None)
    env.update(extra_env or {})
    kw = {"start_new_session": True} if os.name == "posix" else {}
    try:
        p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, env=env, **kw)
    except OSError as e:
        return "", str(e), "cannot start: %s" % e
    try:
        out, err = p.communicate(timeout=timeout)
        return out, err, ("ok" if p.returncode == 0 else "exit %d" % p.returncode)
    except subprocess.TimeoutExpired:
        kill_group(p)
        out, err = p.communicate()
        return out or "", err or "", "timeout"
    except BaseException:
        kill_group(p)
        raise


def kill_group(p):
    try:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGKILL)
        else:
            p.kill()
    except OSError:
        pass


def run_cost(events):
    """total_cost_usd from a claude stream's result event, or None."""
    for ev in reversed(events):
        if ev.get("type") == "result" and ev.get("total_cost_usd") is not None:
            try:
                return float(ev["total_cost_usd"])
            except (TypeError, ValueError):
                return None
    return None


# ------------------------------------------------------------------ scenario runs
def runner_env(a):
    return {"CODEX_HOME": a.codex_home} if getattr(a, "codex_home", None) else {}


def run_one(job, a):
    arm, s, seed, work, argv = job["arm"], job["scenario"], job["seed"], job["work"], job["argv"]
    base = os.path.join(a.out, arm, "%s.s%d" % (s["id"], seed))
    md = base + ".md"
    if os.path.exists(md) and not a.force and "\nstatus: ok\n" in read(md)[:4000]:
        return arm, s["id"], seed, "skipped (done)", 0.0, 0.0
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
    return arm, s["id"], seed, status, time.time() - t0, run_cost(events)


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
        skills = [me] + [{"name": d["name"], "description": decoy_description(d)}
                         for d in data["decoys"] if decoy_description(d)]
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

    rows = run_capped(jobs, one, a, lambda r: r.get("cost_usd"))
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
    ap.add_argument("--max-budget-usd", type=float, default=0.75,
                    help="per-run cap passed to claude (default 0.75; measured runs cost "
                         "0.05 to 0.53; 0 = none)")
    ap.add_argument("--max-total-usd", type=float, default=1.0,
                    help="stop submitting runs once the reported total cost reaches this "
                         "(default 1.00; raise it deliberately for a full run)")
    ap.add_argument("--max-runs", type=int, default=None,
                    help="run at most N jobs (required for a live codex run, which reports "
                         "no cost)")
    ap.add_argument("--yes", action="store_true",
                    help="confirm a live run that spends money (printed first: the worst case)")
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
    if a.max_runs is not None and a.max_runs < 1:
        die("--max-runs must be at least 1")
    if a.max_total_usd <= 0:
        die("--max-total-usd must be positive")
    a.codex_home = None
    try:
        return _main(a)
    finally:
        if a.codex_home:
            shutil.rmtree(a.codex_home, ignore_errors=True)


def confirm_spend(a, n):
    """Print the worst case for n runs and refuse without --yes (or a codex run without
    --max-runs). Returns the number of runs allowed."""
    allowed = min(n, a.max_runs) if a.max_runs else n
    if a.runner == "claude":
        cap = a.max_budget_usd or 0
        worst = ("%.2f USD (%d runs x %.2f USD per-run cap)" % (allowed * cap, allowed, cap)
                 if cap else "unbounded per run (--max-budget-usd 0)")
        print("live run: %d of %d job(s); worst case %s; stops submitting at a reported total "
              "of %.2f USD (--max-total-usd)" % (allowed, n, worst, a.max_total_usd))
    else:
        if not a.max_runs:
            die("codex reports no cost, so a live codex run needs --max-runs N")
        print("live run: %d of %d job(s) with codex (no cost reported; --max-runs bounds it)"
              % (allowed, n))
    if not a.yes:
        die("this spends money; rerun with --yes after reading the worst case above, or use "
            "--dry-run")
    return allowed


def run_capped(jobs, fn, a, cost_of):
    """Run fn over jobs with a.jobs workers, never submitting a job once the reported cost
    reaches a.max_total_usd or a.max_runs jobs ran. Results in job order (None: not run)."""
    jobs = list(jobs)[:a.max_runs] if a.max_runs else list(jobs)
    results, spent, stopped = [None] * len(jobs), [0.0], [False]
    pending = {}
    nxt = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        while nxt < len(jobs) or pending:
            while nxt < len(jobs) and len(pending) < a.jobs and not stopped[0]:
                if a.runner == "claude" and spent[0] >= a.max_total_usd:
                    stopped[0] = True
                    break
                pending[ex.submit(fn, jobs[nxt])] = nxt
                nxt += 1
            if not pending:
                break
            done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                i = pending.pop(fut)
                results[i] = fut.result()
                spent[0] += cost_of(results[i]) or 0.0
    if stopped[0] or nxt < len(jobs):
        print("stopped after %d of %d job(s): reported cost %.4f USD reached --max-total-usd "
              "%.2f" % (nxt, len(jobs), spent[0], a.max_total_usd))
    return [r for r in results if r is not None]


def _main(a):
    if a.runner == "codex" and not a.dry_run:
        a.codex_home = isolated_codex_home()
    if a.trigger:
        jobs = trigger_jobs(a)
        if a.dry_run:
            for j in jobs:
                print("$ " + j["shown"])
            print("# %d trigger commands (dry run: nothing was run)" % len(jobs))
            return 0
        if shutil.which(find_bin(a.runner, a.runner.upper() + "_BIN")) is None:
            die("%s CLI not found on PATH" % a.runner, 3)
        confirm_spend(a, len(jobs))
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
    confirm_spend(a, len(jobs))
    if "skill" in a.arms:
        snapshot_skill(a.out)
    if not os.path.isdir(a.fixtures):
        subprocess.run([sys.executable, MAKE_FIXTURES, "--out", a.fixtures], check=True)
    for arm in a.arms:
        os.makedirs(os.path.join(a.out, arm), exist_ok=True)
    def one(job):
        arm, sid, seed, status, secs, cost = run_one(job, a)
        print("%-5s %-4s s%d %-16s %4.0f s%s" % (arm, sid, seed, status, secs,
                                                  "  %.4f USD" % cost if cost else ""),
              flush=True)
        return cost

    run_capped(jobs, one, a, lambda cost: cost)
    return 0


if __name__ == "__main__":
    sys.exit(main())
