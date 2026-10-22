#!/usr/bin/env python3
"""Run cells of the high-agency A/B evaluation. Stdlib only, python3 3.8+.

Usage:
  run.py --arm none,new,installed --scenario S02,S06 --trials 1 [--harness claude|codex]
         [--model M] [--timeout 900] [--jobs 1] [--judge none|claude|codex]
         [--judge-model M] [--baseline-rev REV|v1] [--skill-dir DIR]
         [--arm-suffix SFX] [--runs-dir DIR] [--codex-auth FILE]
         [--agent-settings FILE] [--codex-sandbox MODE] [--force] [--dry-run]

--arm defaults to none,new. Arm "old" needs the git history of the source
repository (it reads SKILL.md at --baseline-rev); a copy of the skill alone
stops with a message. Arm "core" injects core.md (the short always-on form)
with references/ beside the fixture, like "new". Arm "pointer" injects only
the one-paragraph pointer from the skill's README (POINTER_TEXT) and installs
the skill where "installed" does, so the agent has to open SKILL.md itself.

--baseline-rev is the git revision whose SKILL.md the "old" arm injects
(default 64b1e16, the phase-1 text; "v1" means 414c914, the first release).
--skill-dir installs a different skill directory (a description variant) for
the new/installed arms; --arm-suffix keeps its runs apart (<arm><sfx>/).
--judge-model defaults to sonnet for the claude judge (the model the phase-1
checks were calibrated with); result.json records the judge actually used.
--codex-auth copies that auth.json into a per-cell CODEX_HOME made with
mkdtemp outside the runs directory, removed when the agent run ends; the Codex
judge gets its own short-lived copy in a temp directory outside the cell.
Without it Codex needs CODEX_API_KEY in the environment.

Ctrl-C, SIGTERM and SIGHUP stop the grid at any --jobs: no new cell or agent
starts, every running agent and grader process group is killed, every temp
CODEX_HOME (the agent's and the judge's) is removed, unfinished cells write no
result.json, and run.py exits 130. Signals that arrive during that cleanup are
ignored until it ends. Only a SIGKILL of run.py itself skips it.

A graded cell (a result.json that parses) is skipped; one whose recorded
skill SHA-256 or --model differs from this run is reported as STALE. A cell
whose grader crashes or writes no result.json is an ERROR and makes run.py
exit 1, so it cannot drop out of the denominator unnoticed. Git variables that
point at another repository (GIT_DIR, GIT_INDEX_FILE, GIT_WORK_TREE and the
like) are removed from every build, agent and grader environment.

SECURITY: agent cells run unsandboxed with your full user privileges. Claude
runs with --permission-mode bypassPermissions and Codex with
--dangerously-bypass-approvals-and-sandbox, and four scenarios (S08, S15,
S18, S33) plant prompt-injection text on purpose. Nothing confines the agent to its
temp fixture: it can read your home directory, use your git and gh
credentials and reach the network. Run the grid inside a container, a VM or
as a throwaway user. --agent-settings FILE passes a Claude Code settings file
(for example one that enables its sandbox); --codex-sandbox workspace-write
replaces the Codex bypass flag. Neither was used for the published numbers,
and both can block the git pushes to the fixture's origin, which sits next to
the fixture rather than inside it.

Each cell builds a fresh ledger fixture (fixture/build.sh), injects the arm's
skill text, runs the harness headless with the scenario's user message in its
own process group (killed when the run ends or times out, together with any
process still working inside the cell, such as a detached benchmark), saves
transcript.jsonl, final.txt and meta.json (with the SHA-256 of the SKILL.md
the arm used) under
  <runs-dir>/<harness>/<arm>/<scenario>/<trial>/   (runs-dir default: evals/.runs)
and grades it with grade.py. Spends tokens: one agent run per cell plus one
judge call per judge check unless --judge none. A scenario with a "harness"
field (S23) runs only under that harness.

A cell whose harness reports an error (an is_error or non-success result
event other than the turn cap, a rejected rate-limit event, a usage-limit,
overload or API-error final message, a Codex error event, or no output at
all) is a HARNESS ERROR: it is not graded, writes no result.json, so the next
run retries it, and makes run.py exit 1. A build failure, a bad scenario
prefix or a missing baseline revision does the same in every --jobs mode.

Arm "installed" injects nothing and installs the skill directory where the
harness loads skills (Claude Code: <fixture>/.claude/skills/high-agency; Codex:
$CODEX_HOME/skills/high-agency), so the run also tests whether the description
routes to the skill (result.json records skill_invoked).

Isolation of context (not of privileges, see SECURITY): Claude runs with
--setting-sources project and --strict-mcp-config, so the user's own skills,
user CLAUDE.md, memory and MCP servers (including any installed copy of this
skill) do not reach the run; CLAUDE_CONFIG_DIR is left as you set it, so the
run uses your login. Codex runs with CODEX_HOME pointing at a directory that
holds only the arm's skill.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "fixture", "build.sh")
GRADE = os.path.join(HERE, "grade.py")
with open(os.path.join(HERE, "scenarios.json"), encoding="utf-8") as _fh:
    SCENARIOS = json.load(_fh)["scenarios"]
RUNS = os.path.join(HERE, ".runs")
BASELINE_REVS = {"v1": "414c914"}
DEFAULT_BASELINE = "64b1e16"
DEFAULT_JUDGE_MODEL = {"claude": "sonnet", "codex": None, "none": None}
# Never copied into an installed skill: test and eval material is not skill text.
SKILL_IGNORE = ("tests", "evals", "__pycache__", ".runs")

HEADER = 'Skill "high-agency" is loaded for this task:\n\n'
# The project-instruction pointer from the skill's README, verbatim modulo
# line breaks (tests/test_evals.py compares the two).
POINTER_TEXT = ("For multi-step builds, repairs, investigations, plans or briefs, and jobs "
                "that outlive a turn, read the high-agency skill's SKILL.md first and "
                "follow it.\n")
ARMS = ("none", "old", "new", "core", "pointer", "installed")
DEFAULT_ARMS = ["none", "new"]
# Final messages that are a harness or account stub, not an agent's answer.
LIMIT_RE = re.compile(r"(?i)\b(usage limit|rate limit|hit your limit|limit reached|limit will reset|"
                      r"overloaded|credit balance is too low|API Error:? \d{3})")
AUTH_RE = re.compile(r"Not logged in|Invalid API key|authentication_error|Please run /login")


class EvalError(RuntimeError):
    """A cell that cannot run or grade; counted as a failure in every --jobs mode."""


class Interrupted(EvalError):
    """The grid was stopped (Ctrl-C, SIGTERM or SIGHUP) before this cell finished."""


# ------------------------------------------------------------------ interrupts
#
# Ctrl-C reaches only the main thread. With --jobs > 1 the workers would keep
# popping cells and launching agents, so every worker checks STOP before it
# starts a cell or an agent, every agent and grader runs in its own process
# group recorded in _LIVE, and every temp directory holding a credential copy
# is recorded in _TEMP. On an interrupt the main thread sets STOP, kills the
# live groups and removes the temp directories itself, so a second Ctrl-C (or
# an exit before a worker's finally block runs) leaves nothing behind.

STOP = threading.Event()
_LIVE = set()
_TEMP = set()
_TRACK_LOCK = threading.Lock()

# Variables that point git at another repository or index. A run started from
# a git hook, `git rebase --exec` or a linked worktree inherits them, and every
# git call in the fixture or the agent would then act on the host repository.
GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
                 "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


def clean_env(base=None):
    """A copy of the environment with git's repository variables and the
    parent Claude Code session markers removed."""
    env = dict(os.environ if base is None else base)
    for k in GIT_REPO_VARS + ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    return env


def track_dir(path):
    with _TRACK_LOCK:
        _TEMP.add(path)
    return path


def release_dir(path):
    with _TRACK_LOCK:
        _TEMP.discard(path)
    shutil.rmtree(path, ignore_errors=True)


def run_tracked(cmd, timeout=None, **kw):
    """subprocess.run in its own process group, killed on an interrupt.

    Returns (returncode, stdout, stderr, timed_out). On a timeout the whole
    group is killed and the output so far is returned with returncode -1.
    """
    if STOP.is_set():
        raise Interrupted("stopped before %s started" % os.path.basename(cmd[0]))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         stdin=subprocess.DEVNULL, start_new_session=True, **kw)
    with _TRACK_LOCK:
        _LIVE.add(p)
    if STOP.is_set():  # the interrupt landed between the check above and Popen
        _kill_group(p.pid)
    timed_out = False
    try:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(p.pid)
            out, err = p.communicate()
    finally:
        _kill_group(p.pid)
        with _TRACK_LOCK:
            _LIVE.discard(p)
    if STOP.is_set():
        raise Interrupted("stopped while %s ran" % os.path.basename(cmd[0]))
    return (-1 if timed_out else p.returncode), out or "", err or "", timed_out


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def cleanup_now():
    """Kill every live process group and remove every tracked temp directory."""
    with _TRACK_LOCK:
        procs, dirs = list(_LIVE), list(_TEMP)
    for p in procs:
        _kill_group(p.pid)
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def cleanup_shielded():
    """cleanup_now with SIGINT, SIGTERM and SIGHUP ignored while it runs.

    A second Ctrl-C a few milliseconds after the first would otherwise land
    inside cleanup_now and skip the removal of the credential copies. Off the
    main thread signals cannot be changed and cannot be delivered either.
    """
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for sig in INTERRUPT_SIGNALS:
            saved[sig] = signal.signal(sig, signal.SIG_IGN)
    try:
        cleanup_now()
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt("signal %d" % signum)


def run_grid(items, jobs, work):
    """Call work(item) for each item on *jobs* threads; True if interrupted.

    SIGTERM and SIGHUP are turned into KeyboardInterrupt so the same path
    runs for all three. Workers never start a new item once STOP is set.
    """
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            saved[sig] = signal.signal(sig, _raise_interrupt)
    threads = []
    try:
        return _run_grid(items, jobs, work, threads)
    except KeyboardInterrupt:
        # A repeated signal that landed inside _run_grid's own handler, before
        # its cleanup was shielded: clean up here, absorbing any further ones.
        STOP.set()
        while True:
            try:
                cleanup_shielded()
                break
            except KeyboardInterrupt:
                continue
        return True
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _run_grid(items, jobs, work, threads):
    try:
        if jobs <= 1:
            for it in items:
                if STOP.is_set():
                    break
                work(it)
        else:
            pending = list(items)
            lock = threading.Lock()

            def worker():
                while not STOP.is_set():
                    with lock:
                        if not pending:
                            return
                        it = pending.pop(0)
                    work(it)
            threads.extend(threading.Thread(target=worker, daemon=True) for _ in range(jobs))
            for th in threads:
                th.start()
            for th in threads:
                while th.is_alive():
                    th.join(0.2)
        return STOP.is_set()
    except KeyboardInterrupt:
        STOP.set()
        cleanup_shielded()
        try:  # let workers unwind through their own finally blocks
            deadline = time.time() + 20
            for th in threads:
                th.join(max(0.0, deadline - time.time()))
        except KeyboardInterrupt:
            pass
        cleanup_shielded()  # anything a worker started or tracked after the first pass
        return True


def classify_stream(text):
    """Return why a transcript is a harness error, or None for a real agent run.

    Reads Claude Code stream-json and Codex exec --json. The turn cap
    (subtype error_max_turns) is a real result, not an error.
    """
    reason = None
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "rate_limit_event":
            status = str((obj.get("rate_limit_info") or {}).get("status", ""))
            if status and not status.startswith("allowed"):
                reason = "usage limit (rate_limit_event status %s)" % status
        elif kind == "result":
            sub = obj.get("subtype")
            msg = str(obj.get("result") or "")
            if sub == "error_max_turns":
                continue
            if sub != "success":
                reason = "result subtype %s" % sub
            elif obj.get("is_error"):
                reason = "API error: %s" % (msg.strip()[:80] or "no message")
            elif len(msg) < 400 and (LIMIT_RE.search(msg) or AUTH_RE.search(msg)):
                reason = "limit or auth stub: %s" % msg.strip()[:80]
        elif kind in ("error", "turn.failed"):
            err = obj.get("message") or obj.get("error") or ""
            reason = "codex %s: %s" % (kind, json.dumps(err)[:80])
    return reason


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def kill_cell_processes(pgid, cell):
    """Kill the agent's process group, then anything else whose cwd is in the cell."""
    _kill_group(pgid)
    for pid in pids_with_cwd_under(cell):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def pids_with_cwd_under(root):
    """PIDs whose working directory is inside *root* (Linux /proc, else lsof)."""
    root = os.path.realpath(root)
    pids = set()
    if os.path.isdir("/proc/self/cwd"):
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    cwd = os.path.realpath(os.readlink("/proc/%s/cwd" % d))
                except OSError:
                    continue
                if cwd == root or cwd.startswith(root + os.sep):
                    pids.add(int(d))
    elif shutil.which("lsof"):
        p = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-u", str(os.getuid())],
                           capture_output=True, text=True)
        pid = None
        for line in p.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid is not None:
                cwd = os.path.realpath(line[1:])
                if cwd == root or cwd.startswith(root + os.sep):
                    pids.add(pid)
    pids.discard(os.getpid())
    return sorted(pids)
REFS_NOTE = "\n\nIts references are at .skills/high-agency/references/.\n"


def sh(args, **kw):
    kw.setdefault("env", clean_env())
    return subprocess.run(args, capture_output=True, text=True, **kw)


def write_json(path, obj):
    """Write JSON atomically: a reader or an interrupt never sees half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, default=str)
    os.replace(tmp, path)


def load_graded(cell):
    """The cell's result.json if it parses and holds checks, else None."""
    try:
        with open(os.path.join(cell, "result.json"), encoding="utf-8") as fh:
            res = json.load(fh)
    except (OSError, ValueError):
        return None
    return res if isinstance(res, dict) and isinstance(res.get("checks"), dict) else None


def stale_reason(cell, arm, opts):
    """Why a graded cell no longer measures this run's skill text or model."""
    try:
        with open(os.path.join(cell, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return "no readable meta.json"
    try:
        want = arm_skill_sha(arm, opts)
    except (OSError, EvalError):
        want = meta.get("skill_sha256")
    if meta.get("skill_sha256") != want:
        return "skill text changed"
    if opts["model"] and meta.get("model") != opts["model"]:
        return "model %s, this run %s" % (meta.get("model"), opts["model"])
    return None


def strip_frontmatter(text):
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return text[m.end():].lstrip("\n") if m else text


def baseline_skill(rev):
    """SKILL.md as of *rev*, read with a path relative to the skill directory."""
    p = sh(["git", "-C", SKILL_DIR, "show", "%s:./SKILL.md" % rev])
    if p.returncode != 0:
        raise EvalError("arm old needs SKILL.md at revision %s from the source repository's git "
                        "history (%s); run from a clone, pass --baseline-rev, or drop the old arm"
                        % (rev, p.stderr.strip() or "git failed"))
    return p.stdout


def skill_text(arm, rev=DEFAULT_BASELINE):
    if arm in ("none", "installed"):
        return ""
    if arm == "old":
        return HEADER + strip_frontmatter(baseline_skill(rev))
    if arm == "new":
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            return HEADER + strip_frontmatter(fh.read()) + REFS_NOTE
    if arm == "core":
        with open(os.path.join(SKILL_DIR, "core.md"), encoding="utf-8") as fh:
            return fh.read() + REFS_NOTE
    if arm == "pointer":
        return POINTER_TEXT
    raise EvalError("unknown arm: %s" % arm)  # one of ARMS


def arm_skill_sha(arm, opts):
    """SHA-256 of the SKILL.md text the arm used (None for the none arm)."""
    if arm == "none":
        return None
    if arm == "old":
        return sha256_text(baseline_skill(opts["baseline_rev"]))
    if arm in ("core", "pointer"):
        return sha256_text(skill_text(arm))
    src = SKILL_DIR if arm == "new" else (opts["skill_dir"] or SKILL_DIR)
    with open(os.path.join(src, "SKILL.md"), encoding="utf-8") as fh:
        return sha256_text(fh.read())


def scenario_by_prefix(prefix):
    hits = [s for s in SCENARIOS if s["id"].startswith(prefix)]
    if len(hits) != 1:
        raise EvalError("scenario %r matches %d entries" % (prefix, len(hits)))
    return hits[0]


def compose_message(fixture, scenario):
    prior = os.path.join(fixture, ".eval", "prior_transcript.md")
    msg = scenario["user_message"]
    if os.path.exists(prior):
        with open(prior, encoding="utf-8") as fh:
            msg = fh.read().rstrip() + "\n\n[new user message]\n" + msg
    return msg


def install_skill_files(arm, fixture, codex_home=None, skill_dir=None, rev=DEFAULT_BASELINE):
    """Copy references next to the fixture for the new arm; build a CODEX_HOME."""
    if arm in ("new", "core"):
        dest = os.path.join(fixture, ".skills", "high-agency")
        os.makedirs(dest, exist_ok=True)
        shutil.copytree(os.path.join(SKILL_DIR, "references"), os.path.join(dest, "references"))
        with open(os.path.join(fixture, ".git", "info", "exclude"), "a") as fh:
            fh.write(".skills/\n")
    if arm in ("installed", "pointer") and not codex_home:
        # Routing arm: the whole skill sits where Claude Code loads project
        # skills; nothing is injected, so the description must win the pick.
        dest = os.path.join(fixture, ".claude", "skills", "high-agency")
        shutil.copytree(skill_dir or SKILL_DIR, dest, ignore=shutil.ignore_patterns(*SKILL_IGNORE))
        with open(os.path.join(fixture, ".git", "info", "exclude"), "a") as fh:
            fh.write(".claude/\n")
    if codex_home:
        os.makedirs(os.path.join(codex_home, "skills"), exist_ok=True)
        if arm in ("new", "core", "installed", "pointer"):
            shutil.copytree(skill_dir or SKILL_DIR, os.path.join(codex_home, "skills", "high-agency"),
                            ignore=shutil.ignore_patterns(*SKILL_IGNORE))
        elif arm == "old":
            d = os.path.join(codex_home, "skills", "high-agency")
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write(baseline_skill(rev))


def final_from_stream(path):
    """Final assistant text from a Claude stream-json transcript, with token totals."""
    final = ""
    tokens = None
    model = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "result":
                final = str(obj.get("result") or final)
                usage = obj.get("usage") or {}
                tokens = {k: usage.get(k) for k in ("input_tokens", "output_tokens",
                                                    "cache_read_input_tokens", "cache_creation_input_tokens")}
                tokens["cost_usd"] = obj.get("total_cost_usd")
            elif obj.get("type") == "assistant":
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                model = msg.get("model") or model
                texts = [b.get("text", "") for b in msg.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
                if texts:
                    final = "\n".join(texts)
    return final, tokens, model


def run_cell(arm, sid, trial, opts):
    scenario = scenario_by_prefix(sid)
    cell = os.path.join(opts["runs_dir"], opts["harness"], arm + opts["arm_suffix"], scenario["id"], str(trial))
    if opts["dry_run"]:
        # Build in a throwaway directory: a dry run must never replace a real cell.
        cell = os.path.join(tempfile.mkdtemp(prefix="ha-dry-"), "cell")
    if not opts["force"] and not opts["dry_run"] and load_graded(cell) is not None:
        why = stale_reason(cell, arm, opts)
        if why:
            with _TRACK_LOCK:
                opts["stale"].append(cell)
            return "skip (graded; STALE: %s; --force re-runs it)" % why, cell
        return "skip (graded)", cell
    if STOP.is_set():
        raise Interrupted("stopped before the cell started")
    if os.path.exists(cell):
        shutil.rmtree(cell)  # the bare origin lives inside the cell, so this removes it too
    os.makedirs(cell)
    fixture = os.path.join(cell, "fixture")
    b = sh(["bash", BUILD, fixture, scenario["id"]])
    if b.returncode != 0:
        raise EvalError("build failed: %s" % b.stderr.strip()[-300:])

    message = compose_message(fixture, scenario)
    with open(os.path.join(cell, "message.txt"), "w", encoding="utf-8") as fh:
        fh.write(message)
    arm_file = os.path.join(cell, "arm.txt")
    with open(arm_file, "w", encoding="utf-8") as fh:
        fh.write(skill_text(arm, opts["baseline_rev"]))

    env = clean_env()
    codex_home = None
    try:
        if opts["harness"] == "claude":
            # CLAUDE_CONFIG_DIR stays as the user set it: it selects the login
            # (on macOS the OAuth token is in the Keychain under that config
            # dir). --setting-sources project is what keeps user skills, user
            # CLAUDE.md and memory out (probed: the session lists only built-in
            # skills and answers NO to having high-agency).
            install_skill_files(arm, fixture, skill_dir=opts["skill_dir"], rev=opts["baseline_rev"])
            cmd = ["claude", "-p", "--permission-mode", "bypassPermissions", "--max-turns", "80",
                   "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
                   "--setting-sources", "project", "--no-session-persistence",
                   "--append-system-prompt-file", arm_file]
            if opts["agent_settings"]:
                cmd += ["--settings", opts["agent_settings"]]
            if opts["model"]:
                cmd += ["--model", opts["model"]]
            cmd.append(message)
        else:
            # Outside the runs directory, removed in the finally block below, so
            # no credential copy survives an interrupt or a crash.
            codex_home = track_dir(tempfile.mkdtemp(prefix="ha-codex-home-"))
            install_skill_files(arm, fixture, codex_home, skill_dir=opts["skill_dir"], rev=opts["baseline_rev"])
            if opts["codex_auth"]:
                shutil.copy(opts["codex_auth"], os.path.join(codex_home, "auth.json"))
            env["CODEX_HOME"] = codex_home
            sandbox = (["--sandbox", opts["codex_sandbox"]] if opts["codex_sandbox"]
                       else ["--dangerously-bypass-approvals-and-sandbox"])
            cmd = ["codex", "exec"] + sandbox + ["--skip-git-repo-check",
                   "--ignore-user-config", "--json", "--color", "never", "-C", fixture,
                   "-o", os.path.join(cell, "final.txt")]
            if opts["model"]:
                cmd += ["-m", opts["model"]]
            if arm in ("old", "new", "core", "pointer"):
                message = skill_text(arm, opts["baseline_rev"]) + "\n\n" + message
            cmd.append(message)

        if opts["dry_run"]:
            shown = [c if c is not cmd[-1] else repr(c if len(c) < 70 else c[:67] + "...") for c in cmd]
            shutil.rmtree(os.path.dirname(cell), ignore_errors=True)
            return "dry-run: %s" % " ".join(shown), cell

        transcript = os.path.join(cell, "transcript.jsonl")
        started = time.time()
        timed_out = False
        with open(transcript, "w", encoding="utf-8") as out, open(os.path.join(cell, "stderr.txt"), "w") as err:
            # Own process group: a timeout or the end of the run kills the
            # agent and every job it started (nohup keeps the group), and any
            # straggler still working inside the cell, before grading reads it.
            if STOP.is_set():
                raise Interrupted("stopped before the agent started")
            p = subprocess.Popen(cmd, cwd=fixture, env=env, stdout=out, stderr=err,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            with _TRACK_LOCK:
                _LIVE.add(p)
            if STOP.is_set():
                _kill_group(p.pid)
            try:
                rc = p.wait(timeout=opts["timeout"])
            except subprocess.TimeoutExpired:
                rc, timed_out = -1, True
            finally:
                kill_cell_processes(p.pid, cell)
                p.wait()
                with _TRACK_LOCK:
                    _LIVE.discard(p)
        wall = time.time() - started
        if STOP.is_set():
            raise Interrupted("stopped while the agent ran; cell not graded")
    finally:
        if codex_home:
            release_dir(codex_home)

    tokens = model = None
    if opts["harness"] == "claude":
        final, tokens, model = final_from_stream(transcript)
        with open(os.path.join(cell, "final.txt"), "w", encoding="utf-8") as fh:
            fh.write(final)
    elif not os.path.exists(os.path.join(cell, "final.txt")):
        open(os.path.join(cell, "final.txt"), "w").close()

    meta = {"scenario": scenario["id"], "arm": arm + opts["arm_suffix"], "trial": trial, "harness": opts["harness"],
            "skill_dir": opts["skill_dir"] or SKILL_DIR,
            "skill_sha256": arm_skill_sha(arm, opts),
            "baseline_rev": opts["baseline_rev"] if arm == "old" else None,
            "model": model or opts["model"] or "harness default", "started": started,
            "wall_s": round(wall, 1), "exit": rc, "timed_out": timed_out, "tokens": tokens}
    write_json(os.path.join(cell, "meta.json"), meta)

    final_text = open(os.path.join(cell, "final.txt"), encoding="utf-8", errors="replace").read()
    stream = open(transcript, encoding="utf-8", errors="replace").read()
    why = classify_stream(stream)
    if why is None and AUTH_RE.search(final_text) and len(final_text) < 400:
        why = "auth: %s" % final_text.strip()[:80]
    if why is None and not timed_out and not final_text.strip() and not stream.strip():
        why = "no output (exit %s)" % rc
    if why:
        return "HARNESS ERROR (not graded, retried on the next run): %s" % why, cell

    # The grader's temp files (the judge's CODEX_HOME with its auth copy, its
    # empty working directory) go under a tracked directory: an interrupt
    # SIGKILLs the grader before its finally blocks run, and cleanup_now
    # removes this directory instead.
    grade_tmp = track_dir(tempfile.mkdtemp(prefix="ha-grade-"))
    try:
        grc, gout, gerr, _ = run_tracked([sys.executable, "-I", GRADE, cell, "--judge", opts["judge"]] +
                                         (["--judge-model", opts["judge_model"]] if opts["judge_model"] else []) +
                                         (["--codex-auth", opts["codex_auth"]]
                                          if opts["codex_auth"] and opts["judge"] == "codex" else []),
                                         env=dict(clean_env(), TMPDIR=grade_tmp))
    finally:
        release_dir(grade_tmp)
    with open(os.path.join(cell, "grade.txt"), "w") as fh:
        fh.write(gout + gerr)
    if load_graded(cell) is None:
        # The grader crashed or wrote nothing: the cell must not drop out of
        # the denominator silently, so it counts as a failure of this run.
        tail = (gerr.strip().splitlines() or gout.strip().splitlines() or ["no output"])[-1]
        return "ERROR grader (exit %d, no result.json): %s" % (grc, tail[:160]), cell
    last = (gout.strip().splitlines() or ["(no grade output)"])[-1]
    return "%s in %.0fs%s" % (last, wall, " TIMEOUT" if timed_out else ""), cell


# Median cost per behavior cell measured in the phase-2 run (evals/README.md,
# "Cost and time per cell"): agent run plus one sonnet judge call per judge
# check. Codex reports no cost.
EST_CELL_USD = {"claude": 0.20, "codex": None}
EST_JUDGE_USD = 0.04


def build_parser():
    import argparse
    ap = argparse.ArgumentParser(
        prog="run.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run cells of the high-agency A/B evaluation (spends tokens unless --dry-run).",
        epilog=__doc__)
    ap.add_argument("--arm", default=",".join(DEFAULT_ARMS),
                    help="comma-separated arms: %s (default none,new)" % ", ".join(ARMS))
    ap.add_argument("--scenario", help="comma-separated scenario ids or prefixes (default: all for the harness)")
    ap.add_argument("--trials", type=int, default=1, help="trials per arm and scenario (default 1)")
    ap.add_argument("--harness", choices=("claude", "codex"), default="claude")
    ap.add_argument("--model", help="agent model (default: harness default)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per agent run (default 900)")
    ap.add_argument("--jobs", type=int, default=1, help="cells run at once (default 1)")
    ap.add_argument("--judge", choices=("none", "claude", "codex"), default="none")
    ap.add_argument("--judge-model", help="judge model (default sonnet with --judge claude)")
    ap.add_argument("--baseline-rev", default=DEFAULT_BASELINE,
                    help="revision whose SKILL.md the old arm injects (default %s; v1 = %s)"
                         % (DEFAULT_BASELINE, BASELINE_REVS["v1"]))
    ap.add_argument("--skill-dir", help="install this skill directory for the new/installed arms")
    ap.add_argument("--arm-suffix", default="", help="suffix that keeps a variant's runs apart")
    ap.add_argument("--runs-dir", default=RUNS, help="results root (default evals/.runs)")
    ap.add_argument("--codex-auth", help="auth.json copied into each Codex cell's temp CODEX_HOME")
    ap.add_argument("--agent-settings", help="Claude Code settings file passed to every agent run")
    ap.add_argument("--codex-sandbox", choices=("read-only", "workspace-write"),
                    help="replace the Codex bypass flag with this sandbox mode")
    ap.add_argument("--force", action="store_true", help="re-run cells that already have a result.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="build each fixture in a throwaway directory and print the command")
    return ap


def parse_args(argv):
    a = build_parser().parse_args(argv)
    if a.trials < 1 or a.jobs < 1 or a.timeout < 1:
        raise SystemExit("--trials, --jobs and --timeout must be positive")
    opts = {"arms": [x for x in a.arm.split(",") if x],
            "scenarios": [x for x in a.scenario.split(",") if x] if a.scenario else None,
            "trials": a.trials, "harness": a.harness, "model": a.model, "timeout": a.timeout,
            "jobs": a.jobs, "judge": a.judge, "judge_model": a.judge_model, "dry_run": a.dry_run,
            "force": a.force, "skill_dir": os.path.abspath(a.skill_dir) if a.skill_dir else None,
            "arm_suffix": a.arm_suffix, "baseline_rev": BASELINE_REVS.get(a.baseline_rev, a.baseline_rev),
            "runs_dir": os.path.abspath(a.runs_dir),
            "codex_auth": os.path.abspath(a.codex_auth) if a.codex_auth else None,
            "agent_settings": os.path.abspath(a.agent_settings) if a.agent_settings else None,
            "codex_sandbox": a.codex_sandbox, "stale": []}
    bad = [x for x in opts["arms"] if x not in ARMS]
    if bad or not opts["arms"]:
        raise SystemExit("unknown arm(s): %s (arms: %s)" % (",".join(bad), ", ".join(ARMS)))
    if opts["judge_model"] is None:
        opts["judge_model"] = DEFAULT_JUDGE_MODEL[opts["judge"]]
    if opts["codex_auth"] and not os.path.isfile(opts["codex_auth"]):
        raise SystemExit("--codex-auth: no such file: %s" % opts["codex_auth"])
    if opts["agent_settings"] and not os.path.isfile(opts["agent_settings"]):
        raise SystemExit("--agent-settings: no such file: %s" % opts["agent_settings"])
    if "old" in opts["arms"]:
        try:
            baseline_skill(opts["baseline_rev"])
        except EvalError as e:
            raise SystemExit(str(e))
    if opts["scenarios"] is None:  # default: every scenario this harness can run
        opts["scenarios"] = [s["id"][:3] for s in SCENARIOS if s.get("harness", opts["harness"]) == opts["harness"]]
    for sid in opts["scenarios"]:
        try:
            need = scenario_by_prefix(sid).get("harness")
        except EvalError as e:
            raise SystemExit(str(e))
        if need and need != opts["harness"]:
            raise SystemExit("scenario %s runs only under --harness %s" % (sid, need))
    if not opts["dry_run"]:
        missing = [b for b in {opts["harness"], opts["judge"]} - {"none"} if shutil.which(b) is None]
        if missing:
            raise SystemExit("not found on PATH: %s (install Claude Code for `claude`, the Codex CLI for `codex`)"
                             % ", ".join(sorted(missing)))
    return opts


def cost_estimate(cells, opts):
    """A one-line spend estimate for the grid, from the phase-2 medians."""
    per = EST_CELL_USD[opts["harness"]]
    if per is None:
        return "est. cost n/a (Codex reports none)"
    judges = 0
    if opts["judge"] == "claude":
        for arm, sid, _t in cells:
            judges += sum(1 for c in scenario_by_prefix(sid)["checks"] if c["kind"] == "judge")
    return "est. cost $%.2f (median $%.2f per cell, $%.2f per judge call; up to about 2x)" % (
        len(cells) * per + judges * EST_JUDGE_USD, per, EST_JUDGE_USD)


def main(argv):
    opts = parse_args(argv)
    cells = [(arm, sid, t) for sid in opts["scenarios"] for arm in opts["arms"]
             for t in range(1, opts["trials"] + 1)]
    judge = opts["judge"] if opts["judge"] == "none" else "%s (model %s)" % (
        opts["judge"], opts["judge_model"] or "harness default")
    print("%d cells, harness=%s, judge=%s, baseline=%s, jobs=%d, runs=%s%s" % (
        len(cells), opts["harness"], judge, opts["baseline_rev"], opts["jobs"], opts["runs_dir"],
        "  [dry run: nothing is spent or kept]" if opts["dry_run"] else ""))
    if not opts["dry_run"]:
        print("plan: %s; graded cells are skipped" % cost_estimate(cells, opts))
        print("warning: agents run unsandboxed with your user privileges (see --help, SECURITY); "
              "use a container, VM or throwaway user", file=sys.stderr)
    lock = threading.Lock()
    failures = []

    def work(cell):
        arm, sid, t = cell
        try:
            status, path = run_cell(arm, sid, t, opts)
        except Interrupted as e:
            status, path = "INTERRUPTED (%s)" % e, ""
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # SystemExit too: a worker thread must not die silently
            status, path = "ERROR %r" % e, ""
        with lock:
            if status.startswith(("ERROR", "HARNESS ERROR", "INTERRUPTED")):
                failures.append(cell)
            print("%-5s %-4s t%d  %s" % (sid, arm, t, status), flush=True)

    interrupted = run_grid(cells, opts["jobs"], work)
    if opts["stale"]:
        print("%d graded cell(s) are stale (skill text or model changed since they ran); "
              "their numbers describe the old text; --force re-runs them" % len(opts["stale"]),
              file=sys.stderr)
    if interrupted:
        print("interrupted: no new cell started; running agents were killed and temp "
              "credential copies removed; unfinished cells have no result.json", file=sys.stderr)
        return 130
    if failures:
        print("%d cell(s) failed or hit a harness error; they have no result.json and run "
              "again next time" % len(failures), file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
