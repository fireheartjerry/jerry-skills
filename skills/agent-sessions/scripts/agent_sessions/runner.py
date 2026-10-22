"""The runner: the process `agent-session _run <state-dir>` that lives inside
the child's tmux pane (or detached, with the process backend). It starts the
engine for each turn, streams events into log and events.jsonl, and enforces
everything the parent asked for: the turn timeout and the child deadline, the
stall watchdog, budgets, bounded retries, structured-output validation, the
verify gate and the --on-finish hook. It writes result.json once (resume moves
it aside first).

It is the only code that executes an engine with permissions bypassed; the
flags come from engines.py. The two places that run a user-supplied shell
command are `run_verify` (--verify) and `run_on_finish` (--on-finish).
"""

import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback

from agent_sessions.common import (
    HOOK_DONE, IDLE, LADDER_S, LINGER, POST_RESULT_WAIT, RETRYABLE, STALL_DEFAULT, STDERR_TAIL,
    append_jsonl, compact, die, now_iso, passes_env, read_json, read_text, tail_text, write_json, write_text,
)
from agent_sessions.procs import group_alive, killpg, ps_lstart
from agent_sessions.state import (
    add_usage, build_result, empty_usage, ledger_add, ledger_exhausted, load_ledger, load_turns, write_pause,
)
from agent_sessions.engines import (
    OVERLOADED_RE, RATE_LIMIT_RE, USAGE_LIMIT_RE, engine_for, estimate_cost, price_of, reset_delay, resume_message,
    schema_fixup_message, verify_fixup_message,
)
from agent_sessions.schema import unknown_keywords, validate
from agent_sessions.gitops import worktree_summary

__all__ = ["Runner", "cmd_run", "cmd_hook", "run_on_finish", "extract_json_object", "backoff_seconds",
           "pause_seconds"]

FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.S)
HOOK_TIMEOUT = 30


def gate_env(meta):
    """AGENT_SESSION_BASE (the commit the child's changes are measured from:
    the worktree's base commit, else the HEAD of a shared tree at spawn) and
    AGENT_SESSION_WORKTREE (the worktree path, empty without one), for
    --verify and --on-finish, so a gate can check the diff's scope with
    `git diff --name-only "$AGENT_SESSION_BASE"`."""
    wt = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else {}
    return {"AGENT_SESSION_BASE": wt.get("base_commit") or meta.get("head_at_spawn") or "",
            "AGENT_SESSION_WORKTREE": wt.get("path") or ""}


def run_on_finish(meta, sdir, res):
    """Run the child's --on-finish command once for this result: /bin/sh -c CMD
    in the workdir (the state dir when the workdir is gone), with
    AGENT_SESSION_NAME, _STATUS, _FAILURE_CLASS, _RESULT, _BASE and _WORKTREE set, a 30 s limit
    and output appended to <name>/hooks.log. The marker file hook.done (created
    exclusively) makes a second call for the same result a no-op; resume
    removes it. A failing hook adds a warning to result.json and never changes
    the status. Returns the problem text, or None."""
    cmd = (meta or {}).get("on_finish")
    if not cmd or not isinstance(res, dict):
        return None
    try:
        fd = os.open(os.path.join(sdir, HOOK_DONE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        return None
    result_p = os.path.join(sdir, "result.json")
    env = os.environ.copy()
    env.update({"AGENT_SESSION_NAME": meta["name"], "AGENT_SESSION_STATUS": res.get("status") or "",
                "AGENT_SESSION_FAILURE_CLASS": res.get("failure_class") or "",
                "AGENT_SESSION_RESULT": result_p})
    env.update(gate_env(meta))
    workdir = meta.get("workdir") if os.path.isdir(meta.get("workdir") or "") else sdir
    log_p = os.path.join(sdir, "hooks.log")
    problem = None
    try:
        with open(log_p, "ab") as fh:
            fh.write(("[%s] on-finish (%s): %s\n" % (now_iso(), res.get("status"), cmd)).encode("utf-8"))
            fh.flush()
            proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=workdir, stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=env,
                                    close_fds=True)
            started = time.monotonic()
            while proc.poll() is None:
                if time.monotonic() - started > HOOK_TIMEOUT:
                    killpg(proc.pid, signal.SIGKILL)
                    problem = "timed out after %ds" % HOOK_TIMEOUT
                    break
                time.sleep(0.1)
            proc.wait()
            if problem is None and proc.returncode != 0:
                problem = "exited %d" % proc.returncode
    except OSError as exc:
        problem = "could not run: %s" % exc
    if problem:
        current = read_json(result_p)
        if isinstance(current, dict) and current.get("finished_iso") == res.get("finished_iso"):
            text = "--on-finish hook %s (see %s)" % (problem, log_p)
            warnings = current.get("warnings") if isinstance(current.get("warnings"), list) else []
            if text not in warnings:
                current["warnings"] = warnings + [text]
                write_json(result_p, current)
            res["warnings"] = current["warnings"]
    return problem


def cmd_hook(args):
    """`_hook <state-dir>`: run the --on-finish hook of a child whose result was
    written without a runner (finalized while queued, or by kill). Internal."""
    if len(args) != 1:
        die("usage: _hook <state-dir> (internal)")
    sdir = os.path.realpath(args[0])
    meta = read_json(os.path.join(sdir, "meta.json"))
    res = read_json(os.path.join(sdir, "result.json"))
    if not isinstance(meta, dict) or not isinstance(res, dict):
        return 2
    # Present the hook with this child's identity, not that of whichever
    # process finalized it (often another child's runner running kick).
    if meta.get("env_path"):
        os.environ["PATH"] = meta["env_path"]
    os.environ["AGENT_SESSION_DEPTH"] = str(meta.get("depth", 1))
    os.environ["AGENT_SESSION_STARTED"] = str(int(meta.get("started_epoch") or 0))
    ledger = (meta.get("budget") or {}).get("ledger")
    if ledger:
        os.environ["AGENT_SESSIONS_LEDGER"] = ledger
    else:
        os.environ.pop("AGENT_SESSIONS_LEDGER", None)
    run_on_finish(meta, sdir, res)
    return 0


def extract_json_object(answer, engine_value=None):
    """Structured output candidate: the engine's own field, else the whole
    answer as JSON, else the first fenced json block. Returns (obj, source) or
    (None, None)."""
    if isinstance(engine_value, (dict, list)):
        return engine_value, "engine"
    if not isinstance(answer, str) or not answer.strip():
        return None, None
    try:
        return json.loads(answer.strip()), "answer"
    except ValueError:
        pass
    for m in FENCE_RE.finditer(answer):
        try:
            return json.loads(m.group(1)), "fence"
        except ValueError:
            continue
    return None, None


def backoff_seconds(failure_class, attempt, parsed_delay):
    """Wait before retry `attempt + 1`: a parsed reset delay for rate limits,
    else 60 s x attempt (capped at 900 s); other classes 5 s x attempt."""
    if failure_class == "rate_limit":
        if parsed_delay:
            return min(int(parsed_delay), 3600)
        return min(60 * attempt, 900)
    return 5 * attempt


def pause_seconds(parsed_delay, text):
    """How long an engine pause lasts after a rate_limit failure."""
    if parsed_delay:
        return int(parsed_delay)
    if text and USAGE_LIMIT_RE.search(text):
        return 3600
    return 300


# Variables a runner keeps from its backend environment: terminal and session
# basics that carry no secret. Everything else is rebuilt (see setup_env).
RUNNER_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
                   "TMPDIR", "TMUX", "TMUX_PANE", "TMUX_TMPDIR")


class Runner(object):
    def __init__(self, sdir):
        self.sdir = sdir
        self.meta = read_json(os.path.join(sdir, "meta.json"))
        self.log_path = os.path.join(sdir, "log")
        self.events_path = os.path.join(sdir, "events.jsonl")
        self.signalled = None
        self.proc = None
        self.pgid = None
        self.turns = []
        self.turn_no = None
        self.attempt = 1
        self.warnings = []
        self.usage_so_far = empty_usage()
        self.cost_so_far = None
        self.last_progress = 0.0
        self.last_event = None
        self.last_event_epoch = None
        self.turn_started = None
        self.verify = None
        self.verify_running = None
        self.deadline_hit = False
        self.runner_lstart = ps_lstart(os.getpid())
        self.engine = engine_for(self.meta["engine"]) if self.meta else None
        self.stall_s = int(self.meta.get("stall_s", STALL_DEFAULT) or 0) if self.meta else 0

    # -- plumbing
    def path(self, *parts):
        return os.path.join(self.sdir, *parts)

    def marker(self, name):
        return os.path.exists(self.path(name))

    def stopping(self):
        return bool(self.signalled) or self.marker("kill")

    def deadline(self):
        d = self.meta.get("deadline_epoch")
        return d if isinstance(d, (int, float)) else None

    def past_deadline(self):
        d = self.deadline()
        return d is not None and time.time() >= d

    def log(self, line):
        text = "[%s] %s" % (time.strftime("%H:%M:%S"), line)
        try:
            with open(self.log_path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def event(self, kind, text, data=None):
        """Every event: one events.jsonl line, one log line, and the activity clock."""
        now = time.time()
        self.last_event = compact(text, 200)
        self.last_event_epoch = now
        append_jsonl(self.events_path, {
            "ts": round(now, 3), "iso": now_iso(), "turn": self.turn_no, "attempt": self.attempt,
            "type": kind, "text": text if isinstance(text, str) else str(text),
            "data": data if isinstance(data, dict) else None})
        self.log(text)

    def warn(self, text):
        if text and text not in self.warnings:
            self.warnings.append(text)
        self.log("warning: %s" % text)

    def progress(self, phase, turn=None, force=True, parser=None, events=0, stall_from=None):
        now = time.time()
        if not force and now - self.last_progress < 1.0:
            return
        self.last_progress = now
        stall_deadline = None
        if phase == "turn" and self.stall_s > 0:
            stall_deadline = (stall_from or self.turn_started or now) + self.stall_s
        usage = self.usage_so_far
        live = getattr(parser, "live_usage", None)
        if phase == "turn" and isinstance(live, dict):
            # Tokens of the running turn (claude reports them per API call);
            # they join usage_so_far for good when the turn ends.
            usage = add_usage(dict(self.usage_so_far), live)
        obj = {"phase": phase, "turn": turn, "attempt": self.attempt, "events": events,
               "tool_calls": getattr(parser, "tool_calls", 0),
               "last_event": self.last_event,
               "last_event_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.last_event_epoch)) if self.last_event_epoch else None,
               "last_event_epoch": self.last_event_epoch,
               "usage_so_far": usage, "cost_so_far": self.cost_so_far,
               "stall_deadline_epoch": stall_deadline, "deadline_epoch": self.deadline(),
               "turn_started_epoch": self.turn_started if phase == "turn" else None,
               "verify": self.verify_running,
               "engine_pid": self.proc.pid if self.proc else None,
               "runner_pid": os.getpid(), "runner_lstart": self.runner_lstart,
               "updated_iso": now_iso(), "updated_epoch": now}
        try:
            write_json(self.path("progress.json"), obj)
        except OSError:
            pass

    def on_signal(self, signum, _frame):
        try:
            self.signalled = signal.Signals(signum).name
        except (ValueError, AttributeError):
            self.signalled = "signal %d" % signum
        if self.pgid:
            killpg(self.pgid, signal.SIGTERM)

    def save_meta(self):
        write_json(self.path("meta.json"), self.meta)

    def sleep_checked(self, seconds):
        """Sleep, waking early for kill or the deadline. Returns False when interrupted."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stopping() or self.past_deadline():
                return False
            self.progress("waiting", turn=self.turn_no, force=False)
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))
        return True

    # -- main
    def setup_env(self):
        meta = self.meta
        # The spawner's passthrough environment arrives in a 0600 file, not in
        # tmux's argv, so API keys never show up in `ps`. Load it, then delete it.
        env_p = self.path("env.json")
        extra = read_json(env_p)
        # The runner inherits the tmux server's global environment, which on a
        # shared (default) server can hold anything the user exported when it
        # started. Rebuild the environment from what the spawner chose: a few
        # terminal basics, the launch variables recorded in meta.json and the
        # passthrough file. Nothing else from the server reaches the engine.
        base = dict((k, os.environ[k]) for k in RUNNER_BASE_ENV if k in os.environ)
        if not isinstance(extra, dict):
            # No passthrough file (should not happen): keep the passthrough-eligible
            # variables the runner has rather than starting the engine without them.
            extra = dict((k, v) for k, v in os.environ.items() if passes_env(k))
            self.warn("env.json missing; the engine gets the passthrough variables of the backend environment")
        os.environ.clear()
        os.environ.update(base)
        for key, value in (meta.get("launch_env") or {}).items():
            if isinstance(key, str) and isinstance(value, str):
                os.environ[key] = value
        for key, value in extra.items():
            if isinstance(key, str) and isinstance(value, str):
                os.environ[key] = value
        os.environ["PATH"] = meta.get("env_path") or base.get("PATH") or os.defpath
        os.environ["HOME"] = meta.get("env_home") or base.get("HOME") or os.path.expanduser("~")
        try:
            os.unlink(env_p)
        except OSError:
            pass
        os.environ["AGENT_SESSION_DEPTH"] = str(meta.get("depth", 1))
        os.environ["AGENT_SESSION_NAME"] = meta["name"]
        os.environ["AGENT_SESSION_STARTED"] = str(int(meta.get("started_epoch") or 0))
        ledger = (meta.get("budget") or {}).get("ledger")
        if ledger:
            os.environ["AGENT_SESSIONS_LEDGER"] = ledger
        else:
            os.environ.pop("AGENT_SESSIONS_LEDGER", None)

    def main(self):
        """Run the child. Whatever happens, env.json (the passthrough secrets)
        is gone afterwards; an exception nothing else handled stops the engine
        group and records a crash result instead of leaving an engine with
        permissions bypassed running with no runner."""
        try:
            return self._main()
        except Exception as exc:  # noqa: BLE001 - the runner's last line of defence
            return self.crashed(exc)
        finally:
            try:
                os.unlink(self.path("env.json"))
            except OSError:
                pass

    def crashed(self, exc):
        tb = traceback.format_exc()
        self.log("runner crashed:\n%s" % tb.rstrip())
        if self.pgid:
            self.sweep_group(self.pgid, grace=3)
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None
        self.pgid = None
        if self.meta and not os.path.exists(self.path("result.json")):
            last = [l.strip() for l in tb.strip().splitlines() if l.strip()][-3:]
            reason = "the runner crashed: %s: %s (the engine was stopped; traceback in %s: %s)" % (
                type(exc).__name__, exc, self.log_path, " | ".join(last))
            try:
                res = build_result(self.meta, self.turns, "error", "completed", compact(reason, 600),
                                   failure_class="crash", warnings=self.warnings)
                write_json(self.path("result.json"), res)
                if self.meta.get("on_finish"):
                    self.run_hook(res)
            except Exception as exc2:  # noqa: BLE001
                self.log("could not write the crash result: %s" % exc2)
        try:
            self.progress("finished", turn=len(self.turns))
        except Exception:  # noqa: BLE001
            pass
        self.kick_queue()
        return 2

    def sweep_group(self, pgid, grace=5):
        """Terminate whatever is left in the engine's process group (TERM,
        then KILL after `grace` seconds). True when something was there."""
        if not group_alive(pgid):
            return False
        killpg(pgid, signal.SIGTERM)
        end = time.monotonic() + grace
        while group_alive(pgid) and time.monotonic() < end:
            time.sleep(0.1)
        if group_alive(pgid):
            killpg(pgid, signal.SIGKILL)
        return True

    def kick_queue(self):
        try:
            from agent_sessions.launch import kick
            kick(timeout=60)
        except Exception as exc:  # scheduling must never undo a written result
            self.log("warning: kick failed: %s" % exc)

    def _main(self):
        meta = self.meta
        if not meta:
            sys.stderr.write("agent-session _run: no meta.json in %s\n" % self.sdir)
            return 2
        try:
            os.chdir(meta["workdir"])
        except OSError as exc:
            res = build_result(meta, [], "error", "completed", "cannot chdir: %s" % exc, failure_class="crash")
            write_json(self.path("result.json"), res)
            return 2
        self.setup_env()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.on_signal)
        # tmux sends SIGHUP when its session goes away: that is a kill. A
        # process-backend runner has no terminal and keeps running (nohup).
        if meta.get("backend") == "process":
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        else:
            signal.signal(signal.SIGHUP, self.on_signal)
        for w in meta.get("start_warnings") or []:
            self.warn(w)
        resume = meta.get("resume_pending")
        prior = []
        if resume:
            prior = load_turns(self.sdir)
            self.turns = list(prior)
            for t in prior:
                if isinstance(t.get("usage"), dict):
                    add_usage(self.usage_so_far, t["usage"])
                if isinstance(t.get("cost_usd"), (int, float)):
                    self.cost_so_far = round((self.cost_so_far or 0.0) + t["cost_usd"], 6)
        self.progress("starting")
        self.event("lifecycle", "=== %s child '%s' (%s, %s) in %s%s ===" % (
            meta["engine"], meta["name"], meta.get("model") or "default model",
            "steerable" if meta.get("steerable") else "one-shot", meta["workdir"], " (resumed)" if resume else ""),
            {"state": "resumed" if resume else "started", "engine": meta["engine"], "model": meta.get("model"),
             "role": meta.get("role") or "implementation"})

        first_new = len(self.turns)
        if resume:
            meta["resume_pending"] = None
            self.save_meta()
            text = resume.get("message")
            if text or not meta.get("steerable"):
                if not text:
                    text = resume_message(resume.get("reason"), resume.get("at_iso"))
                self.run_turn(len(self.turns) + 1, "resume", text, resume=True)
        else:
            prompt = read_text(self.path("prompt.txt"), "")
            prefix = self.engine.prompt_prefix(meta)
            self.run_turn(1, "initial", prefix + prompt if prefix else prompt)
        new_turns = lambda: self.turns[first_new:]  # noqa: E731
        if (meta.get("schema_file") and not meta.get("steerable") and new_turns()
                and new_turns()[-1]["status"] == "ok"):
            self.structured_round()
        ended = "completed"
        if meta.get("steerable"):
            ended = self.steer_loop()
        if self.stopping() or (new_turns() and new_turns()[-1]["status"] == "killed"
                               and not new_turns()[-1].get("interrupted")):
            ended = "killed"
        if meta.get("verify") and ended != "killed":
            ok_so_far = bool(new_turns()) and all(t["status"] == "ok" for t in new_turns() if not t.get("interrupted"))
            if ok_so_far and not self.past_deadline():
                self.verify_round()
            else:
                self.verify = self.verify_block("skipped", None, None, None, None, 0)
            if self.stopping():
                ended = "killed"

        status, failure_class, reason = self.final_status(new_turns(), ended)
        git_block, git_warnings = worktree_summary(meta)
        for w in git_warnings:
            self.warn(w)
        res = build_result(meta, self.turns, status, ended, reason, failure_class=failure_class,
                           warnings=self.warnings, git=git_block, verify=self.verify)
        write_json(self.path("result.json"), res)
        self.turn_no = None
        self.event("lifecycle", "=== session finished: %s (%s, %d turn%s) ===" % (
            status, ended, len(self.turns), "" if len(self.turns) == 1 else "s"),
            {"state": "finished", "status": status, "ended": ended, "failure_class": res.get("failure_class"),
             "cost_usd": res.get("cost_usd"), "usage": res.get("usage")})
        if meta.get("on_finish"):
            self.run_hook(res)
        self.kick_queue()
        if ended != "killed" and meta.get("backend") != "process":
            self.progress("linger", turn=len(self.turns))
            self.log("lingering %ds for inspection; agent-session kill %s ends it early" % (LINGER, meta["name"]))
            deadline = time.monotonic() + LINGER
            while time.monotonic() < deadline:
                if self.stopping():
                    break
                time.sleep(1)
        self.progress("finished", turn=len(self.turns))
        return 0

    def steer_loop(self):
        last_end = time.monotonic()
        while True:
            if self.stopping() or (self.turns and self.turns[-1]["status"] == "killed"
                                   and not self.turns[-1].get("interrupted")):
                return "killed"
            if self.past_deadline() or self.deadline_hit:
                self.deadline_hit = True
                return "stopped"
            try:
                pending = sorted(f for f in os.listdir(self.path("inbox")) if not f.startswith("."))
            except OSError:
                pending = []
            # Messages queued before `stop` still get their turn; only kill drops them.
            if not pending and self.marker("stop"):
                return "stopped"
            if pending:
                fname = pending[0]
                message_id = fname.split("-", 1)[1] if "-" in fname else fname
                text = read_text(self.path("inbox", fname), "")
                try:
                    os.unlink(self.path("inbox", fname))
                except OSError:
                    pass
                self.run_turn(len(self.turns) + 1, message_id, text)
                last_end = time.monotonic()
                if self.turns[-1].get("failure_class") == "budget":
                    return "stopped"
                continue
            if time.monotonic() - last_end > IDLE:
                return "idle"
            self.progress("waiting", turn=len(self.turns), force=False)
            time.sleep(0.25)

    def final_status(self, turns, ended):
        """(status, failure_class, reason) for the result. `turns` are the
        turns of this run (a resumed steerable child with no new turn is
        judged by its earlier ones)."""
        turns = turns or self.turns
        # A turn stopped by `send --interrupt` was replaced on purpose by the
        # next message; it does not decide the session's status (as in the
        # verify gate). Only when every turn was interrupted do they count.
        turns = [t for t in turns if not t.get("interrupted")] or turns
        failure_class = reason = None
        if ended == "killed":
            status, failure_class = "killed", "killed"
            reason = "killed (%s)" % (self.signalled or "kill requested")
        elif not turns:
            status, failure_class, reason = "error", "crash", "no turn ran"
        elif self.deadline_hit and any(t.get("failure_class") == "timeout" for t in turns):
            status, failure_class = "timeout", "timeout"
        elif not self.meta.get("steerable") and turns[0]["status"] == "timeout":
            status, failure_class = "timeout", "timeout"
        elif any(t["status"] != "ok" for t in turns):
            status = "error"
        else:
            status = "ok"
        if status != "ok":
            for t in reversed(turns):
                if t.get("status") != "ok":
                    reason = reason or t.get("reason")
                    failure_class = failure_class or t.get("failure_class")
                    if reason and failure_class:
                        break
        if status == "ok" and self.verify and self.verify["status"] in ("fail", "error"):
            status, failure_class = "error", "verify"
            fixups = max(0, (self.verify.get("attempts") or 1) - 1)
            if self.verify["status"] == "fail":
                reason = "verification failed after %d fixup%s" % (fixups, "" if fixups == 1 else "s")
            else:
                reason = "verification could not complete: %s" % (self.verify.get("error") or "error")
        return status, failure_class, reason

    # -- structured output
    def check_structured(self, rec, schema):
        """Validate a turn's answer against the schema; records the outcome in
        the turn file. Returns the error list (empty when valid)."""
        obj, source = extract_json_object(rec.get("answer"), rec.get("structured_output"))
        if obj is None:
            errors = ["no JSON object found in the answer"]
        else:
            errors = validate(obj, schema)
            if not isinstance(obj, dict) and not errors:
                errors = ["$: the reply must be a JSON object"]
        rec["schema_valid"] = not errors
        rec["structured_output"] = obj if not errors else None
        rec["structured_source"] = source
        write_json(self.path("turns", "%d.json" % rec["turn"]), rec)
        return errors

    def structured_round(self):
        schema = read_json(self.path("schema.json"))
        if not isinstance(schema, dict):
            self.warn("schema.json unreadable; structured output not validated")
            return
        unknown = unknown_keywords(schema)
        if unknown:
            self.warn("schema keywords not enforced by the local validator: %s" % ", ".join(unknown))
        rec = self.turns[-1]
        errors = self.check_structured(rec, schema)
        if not errors:
            self.event("result", "structured output valid", {"schema_valid": True})
            return
        self.event("error", "structured output invalid: %s" % "; ".join(errors[:5]),
                   {"schema_valid": False, "errors": errors[:50]})
        self.run_turn(len(self.turns) + 1, "schema-fixup", schema_fixup_message(errors))
        rec = self.turns[-1]
        if rec["status"] != "ok":
            return
        errors = self.check_structured(rec, schema)
        if not errors:
            self.event("result", "structured output valid after one fixup", {"schema_valid": True})
            return
        rec["status"] = "error"
        rec["failure_class"] = "schema"
        rec["reason"] = "structured output did not validate after one fixup: %s" % "; ".join(errors[:5])
        write_json(self.path("turns", "%d.json" % rec["turn"]), rec)
        self.event("error", "structured output still invalid after the fixup turn",
                   {"schema_valid": False, "errors": errors[:50]})

    # -- verify gate
    def verify_block(self, status, exit_code, duration, tail, out_file, attempts, error=None):
        block = {"command": self.meta.get("verify"), "status": status, "exit_code": exit_code,
                 "duration_s": duration, "output_tail": tail, "output_file": out_file, "attempts": attempts}
        if error:
            block["error"] = error
        return block

    def run_verify(self, k):
        """Run --verify once in the workdir with the child's environment.
        Returns (status, exit_code, duration, output_tail, out_file, error)."""
        cmd = self.meta["verify"]
        limit = int(self.meta.get("verify_timeout_s") or 600)
        out_file = self.path("verify.%d.out" % k)
        self.verify_running = {"command": cmd, "attempt": k, "started_iso": now_iso()}
        self.progress("verify", turn=len(self.turns))
        self.event("verify", "verify %d: %s" % (k, cmd), {"command": cmd, "attempt": k, "state": "started"})
        started = time.monotonic()
        try:
            with open(out_file, "wb") as fh:
                env = os.environ.copy()
                env.update(gate_env(self.meta))
                proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=self.meta["workdir"], stdin=subprocess.DEVNULL,
                                        stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except OSError as exc:
            self.verify_running = None
            return "error", None, 0.0, None, out_file, "cannot start /bin/sh: %s" % exc
        error = None
        term_at = None
        while proc.poll() is None:
            now = time.monotonic()
            if term_at is None:
                why = None
                if now - started > limit:
                    why = "timed out after %ds" % limit
                elif self.past_deadline():
                    why = "stopped at the --deadline"
                    self.deadline_hit = True
                elif self.stopping():
                    why = "stopped by kill"
                if why:
                    error = why
                    killpg(proc.pid, signal.SIGTERM)
                    term_at = now
            elif now - term_at > 5:
                killpg(proc.pid, signal.SIGKILL)
            self.progress("verify", turn=len(self.turns), force=False)
            time.sleep(0.2)
        rc = proc.returncode
        duration = round(time.monotonic() - started, 1)
        tail = tail_text(out_file, 4000)
        self.verify_running = None
        if error:
            status = "error"
            rc = None
        else:
            status = "pass" if rc == 0 else "fail"
        self.event("verify", "verify %d: %s%s (%ss)" % (k, status, "" if rc is None else " exit %d" % rc, duration),
                   {"command": cmd, "attempt": k, "state": status, "exit_code": rc, "duration_s": duration,
                    "output_file": out_file})
        return status, rc, duration, tail, out_file, error

    def verify_round(self):
        fixups = int(self.meta.get("verify_fixups") or 0)
        used = 0
        k = 0
        while True:
            k += 1
            status, rc, duration, tail, out_file, error = self.run_verify(k)
            self.verify = self.verify_block(status, rc, duration, tail, out_file, k, error)
            if status != "fail" or used >= fixups or self.stopping() or self.past_deadline():
                return
            used += 1
            self.run_turn(len(self.turns) + 1, "verify-fixup",
                          verify_fixup_message(self.meta["verify"], rc, self.meta["workdir"], tail))
            if self.turns[-1]["status"] != "ok":
                return

    # -- hook
    def run_hook(self, res):
        problem = run_on_finish(self.meta, self.sdir, res)
        if problem:
            self.log("warning: --on-finish hook %s" % problem)

    # -- one turn, with bounded retries
    def workdir_state(self):
        """(head, dirty paths) of a write-capable child's git workdir, else None."""
        if self.meta.get("readonly"):
            return None
        from agent_sessions.gitops import dirty_files, repo_root, resolve_commit
        wd = self.meta["workdir"]
        if not repo_root(wd):
            return None
        return resolve_commit(wd, "HEAD"), set(dirty_files(wd))

    def retry_note(self, rec, before):
        """A retried first turn starts a fresh conversation that knows nothing
        of the failed attempt; if that attempt could have changed files, say
        so at the top of the prompt. '' when nothing suggests partial work."""
        if self.meta.get("readonly") or not rec.get("tool_calls"):
            return ""
        what = "an earlier attempt at this task stopped (%s) after %d tool call%s" % (
            rec.get("failure_class"), rec["tool_calls"], "" if rec["tool_calls"] == 1 else "s")
        after = self.workdir_state()
        if before is not None and after is not None:
            changed = sorted(after[1] - before[1])
            moved = before[0] and after[0] and before[0] != after[0]
            if not changed and not moved:
                return ""
            parts = []
            if changed:
                parts.append("files changed: %s%s" % (", ".join(changed[:20]),
                                                      " (and %d more)" % (len(changed) - 20) if len(changed) > 20 else ""))
            if moved:
                parts.append("new commits since %s" % before[0][:12])
            detail = "; ".join(parts)
        else:
            detail = "it may have changed files in this directory"
        return ("Note from the runner: %s; %s. Inspect that state before you edit: keep what is correct, "
                "redo what is not, then finish the task below.\n\n" % (what, detail))

    def run_turn(self, n, message_id, text, resume=False):
        retries = []
        attempt = 1
        limit = int(self.meta.get("retries") or 0)
        fresh = n == 1 and not resume
        before = self.workdir_state() if fresh and limit > 0 else None
        prompt = text
        while True:
            self.attempt = attempt
            rec = self.attempt_turn(n, message_id, prompt, resume=resume or n > 1)
            cls = rec.get("failure_class")
            retryable = (rec["status"] != "ok" and cls in RETRYABLE
                         and not (cls == "engine_error" and rec.get("answer")))
            if rec["status"] == "ok" or not retryable or attempt > limit or self.stopping():
                break
            parsed = rec.pop("_reset_delay", None)
            wait = backoff_seconds(cls, attempt, parsed)
            d = self.deadline()
            if d is not None and time.time() + wait >= d:
                rec["reason"] = "%s; no retry: waiting %ds would cross the --deadline" % (rec.get("reason"), wait)
                break
            self.archive_attempt(n, attempt)
            entry = {"attempt": attempt, "failure_class": cls, "reason": rec.get("reason"), "waited_s": wait,
                     "finished_iso": rec.get("finished_iso")}
            retries.append(entry)
            self.event("retry", "retry: turn %d attempt %d failed (%s); attempt %d in %ds" % (
                n, attempt, cls, attempt + 1, wait), dict(entry))
            if not self.sleep_checked(wait):
                rec["reason"] = "%s; retry abandoned (kill or deadline)" % rec.get("reason")
                break
            if fresh:
                self.fresh_conversation()
                note = self.retry_note(rec, before)
                if note:
                    self.event("retry", "retry: the new conversation is told about the failed attempt's changes",
                               {"attempt": attempt + 1})
                prompt = note + text
            attempt += 1
        rec.pop("_reset_delay", None)
        rec["retries"] = retries
        rec["attempt"] = attempt
        write_json(self.path("turns", "%d.json" % n), rec)
        if message_id not in ("initial", "resume", "schema-fixup", "verify-fixup"):
            write_text(self.path("messages", message_id), "%d\n" % n)
        self.turns.append(rec)
        self.attempt = 1
        return rec

    def fresh_conversation(self):
        """A retried first turn starts a new conversation: claude with a fresh
        --session-id (the failed attempt may have claimed the old one), codex
        and gemini with a fresh session."""
        import uuid
        if self.meta["engine"] == "claude":
            self.meta["conversation_id"] = str(uuid.uuid4())
        else:
            self.meta["conversation_id"] = None
        self.save_meta()

    def archive_attempt(self, n, attempt):
        turns_dir = self.path("turns")
        for suffix in (".json", ".events.jsonl", ".stderr", ".answer.txt"):
            src = os.path.join(turns_dir, "%d%s" % (n, suffix))
            if os.path.exists(src):
                try:
                    os.replace(src, os.path.join(turns_dir, "%d.attempt%d%s" % (n, attempt, suffix)))
                except OSError:
                    pass

    def maybe_pause(self, rec, parser, text):
        """A rate_limit failure pauses the engine for every later spawn."""
        delay = reset_delay(parser.retry_delay_ms, text)
        rec["_reset_delay"] = delay
        seconds = pause_seconds(delay, text)
        reason = "usage limit" if USAGE_LIMIT_RE.search(text or "") else "rate limit"
        try:
            write_pause(self.meta["engine"], time.time() + seconds, reason, self.meta["name"])
            self.log("paused %s for %ds (%s)" % (self.meta["engine"], seconds, reason))
        except OSError as exc:
            self.log("warning: could not write the pause marker: %s" % exc)

    def turn_cap_usd(self):
        from agent_sessions.launch import ledger_cap_for_turn
        cap = ledger_cap_for_turn(self.meta)
        if cap is None:
            return None
        child_limit = (self.meta.get("budget") or {}).get("usd_limit")
        if isinstance(child_limit, (int, float)) and isinstance(self.cost_so_far, (int, float)):
            cap = min(cap, round(child_limit - self.cost_so_far, 6))
        return cap

    def budget_breach(self):
        """Reason text when a limit is exceeded after a turn, else None."""
        b = self.meta.get("budget") or {}
        if isinstance(b.get("usd_limit"), (int, float)) and isinstance(self.cost_so_far, (int, float)) \
                and self.cost_so_far > b["usd_limit"]:
            return "spent $%s of the $%s budget (--budget-usd)" % (self.cost_so_far, b["usd_limit"])
        if isinstance(b.get("tokens_limit"), int) and self.usage_so_far["total_tokens"] > b["tokens_limit"]:
            return "used %d tokens of the %d budget (--max-tokens)" % (
                self.usage_so_far["total_tokens"], b["tokens_limit"])
        if b.get("ledger") and self.meta["engine"] != "claude":
            ledger = load_ledger(b["ledger"])
            if ledger and ledger_exhausted(ledger):
                from agent_sessions.launch import exhausted_text
                return exhausted_text(ledger)
        return None

    def attempt_turn(self, n, message_id, text, resume=False):
        meta = self.meta
        started = time.time()
        # Intervals (turn timeout, stall watchdog, signal ladder, post-result
        # linger) use the monotonic clock: a laptop sleep or a clock step must
        # not fire them. Only the absolute --deadline compares wall time.
        started_mono = time.monotonic()
        self.turn_no = n
        self.turn_started = started
        turns_dir = self.path("turns")
        events_path = os.path.join(turns_dir, "%d.events.jsonl" % n)
        stderr_path = os.path.join(turns_dir, "%d.stderr" % n)
        answer_path = os.path.join(turns_dir, "%d.answer.txt" % n)
        paths = {"answer": answer_path,
                 "schema": self.path("schema.json") if meta.get("schema_file") else None,
                 "resume": resume}
        rec = {"turn": n, "attempt": self.attempt, "message_id": message_id, "status": None,
               "failure_class": None, "exit_code": None,
               "started_iso": now_iso(), "finished_iso": None, "duration_s": None,
               "answer": None, "answer_chars": 0, "answer_file": answer_path,
               "session_id": None, "cost_usd": None, "cost_source": None, "usage": None, "num_turns": None,
               "structured_output": None, "schema_valid": None, "denials": [],
               "events": 0, "tool_calls": 0, "events_file": events_path, "stderr_file": stderr_path}
        self.event("turn_start", "=== turn %d (message %s%s) ===" % (
            n, message_id, ", attempt %d" % self.attempt if self.attempt > 1 else ""),
            {"message_id": message_id, "chars": len(text or ""), "attempt": self.attempt})
        self.progress("turn", turn=n)

        def finish(status, failure_class=None, reason=None):
            rec["status"] = status
            rec["failure_class"] = failure_class if status != "ok" else None
            if reason:
                rec["reason"] = reason
            rec["finished_iso"] = now_iso()
            rec["duration_s"] = round(time.time() - started, 1)
            if status != "ok":
                tail = tail_text(stderr_path, STDERR_TAIL)
                if tail:
                    rec["stderr_tail"] = tail
            write_json(os.path.join(turns_dir, "%d.json" % n), rec)
            self.event("turn_end", "=== turn %d finished: %s (%ss) ===" % (n, status, rec["duration_s"]),
                       {"status": status, "failure_class": rec["failure_class"], "reason": reason,
                        "duration_s": rec["duration_s"], "usage": rec["usage"], "cost_usd": rec["cost_usd"]})
            return rec

        if (n > 1 or resume) and not meta.get("conversation_id"):
            msg = "no %s %s from an earlier turn; cannot resume" % (
                meta["engine"], "thread id" if meta["engine"] == "codex" else "session id")
            self.log("error: " + msg)
            return finish("error", "engine_error", msg)
        if self.past_deadline():
            self.deadline_hit = True
            return finish("timeout", "timeout", "the --deadline passed before turn %d started" % n)
        b = meta.get("budget") or {}
        if b.get("ledger"):
            ledger = load_ledger(b["ledger"])
            if ledger and ledger_exhausted(ledger):
                from agent_sessions.launch import exhausted_text
                self.event("budget", "budget: %s" % exhausted_text(ledger), {"ledger": b["ledger"]})
                return finish("error", "budget", exhausted_text(ledger))
        if meta["engine"] == "claude":
            cap = self.turn_cap_usd()
            if cap is not None:
                if cap <= 0:
                    return finish("error", "budget", "no USD budget left for turn %d" % n)
                paths["max_budget_usd"] = cap

        cmd = self.engine.build_cmd(meta, n, paths)
        self.log("cmd: %s" % " ".join(shlex.quote(c) if len(c) < 400 else "'<%d chars>'" % len(c) for c in cmd))
        errfh = open(stderr_path, "wb")
        try:
            proc = subprocess.Popen(cmd, cwd=meta["workdir"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=errfh,
                                    start_new_session=True, env=os.environ.copy())
        except OSError as exc:
            errfh.close()
            self.log("error: cannot start engine: %s" % exc)
            return finish("error", "crash", "cannot start engine: %s" % exc)
        errfh.close()
        self.proc = proc
        try:
            self.pgid = os.getpgid(proc.pid)
        except OSError:
            self.pgid = proc.pid
        write_json(self.path("engine.pid"), {"pid": proc.pid, "pgid": self.pgid,
                                             "lstart": ps_lstart(proc.pid), "turn": n,
                                             "runner_pid": os.getpid()})

        def feed():
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
        threading.Thread(target=feed, daemon=True).start()

        parser = self.engine.parser(self.event)
        events_fh = open(events_path, "ab")
        fd = proc.stdout.fileno()
        state = {"buf": b"", "last_line": time.monotonic()}
        eof = False
        exited_at = None
        stop = {"status": None, "reason": None, "steps": [], "next_at": None}
        linger = {"terminal_at": None, "term_at": None, "killed": False}
        turns_limit = b.get("turns_limit")

        unparsed = {"count": 0}

        def handle_line(raw):
            state["last_line"] = time.monotonic()
            if not raw.strip():
                return
            events_fh.write(raw + b"\n")
            events_fh.flush()
            line = raw.decode("utf-8", "replace")
            try:
                obj = json.loads(line)
            except ValueError:
                self.event("assistant", line.rstrip(), {"raw": True})
                return
            if not isinstance(obj, dict):
                return
            rec["events"] += 1
            try:
                parser.feed(obj)
            except Exception as exc:  # noqa: BLE001 - an odd event shape must not end the child
                unparsed["count"] += 1
                if unparsed["count"] <= 3:
                    self.log("warning: engine event not understood (%s: %s), skipped: %s"
                             % (type(exc).__name__, exc, compact(line, 200)))
            # Any parsed event is activity, rendered or not (claude stream_event,
            # codex item.started), so ACTIVITY in `list` means "silent", not "idle".
            self.last_event_epoch = time.time()

        def request_stop(status, reason, ladder):
            """ladder 'int': SIGINT, then TERM, then KILL, LADDER_S apart (an
            interrupted claude ends its turn and saves the session). 'term':
            TERM, then KILL after 10 s (kill: stopping now is the point)."""
            stop["status"], stop["reason"] = status, reason
            if ladder == "int":
                stop["steps"] = [(signal.SIGTERM, LADDER_S), (signal.SIGKILL, LADDER_S)]
                first, gap = signal.SIGINT, LADDER_S
            else:
                stop["steps"] = [(signal.SIGKILL, 10)]
                first, gap = signal.SIGTERM, 10
            self.log("%s: %s; sending %s to engine group %s" % (status, reason, first.name, self.pgid))
            killpg(self.pgid, first)
            stop["next_at"] = time.monotonic() + gap

        interrupt_p = self.path("interrupt")
        while True:
            if not eof:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.5)
                except (InterruptedError, OSError, ValueError):
                    ready = []
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        eof = True
                    else:
                        state["buf"] += chunk
                        while b"\n" in state["buf"]:
                            line, state["buf"] = state["buf"].split(b"\n", 1)
                            handle_line(line)
            else:
                time.sleep(0.25)
            now = time.monotonic()
            # Settled: a terminal event arrived, no background task is pending and
            # no new segment began after it (claude reports a result per segment).
            # Until then the turn timeout, the deadline and the stall watchdog apply.
            settled = parser.turn_settled()
            # Decide on kill/timeout before checking for exit, so an engine that
            # died from our own signal is recorded as such, not as an error.
            if stop["status"] is None and proc.poll() is None:
                if self.signalled:
                    request_stop("killed", "runner received %s" % self.signalled, "term")
                elif self.marker("kill"):
                    request_stop("killed", "kill requested", "term")
                elif self._interrupt_requested(interrupt_p, n):
                    rec["interrupted"] = True
                    request_stop("killed", "interrupted by send --interrupt", "int")
                elif not settled and now - started_mono > meta["timeout_s"]:
                    request_stop("timeout", "turn exceeded %ds" % meta["timeout_s"], "int")
                elif not settled and self.deadline() is not None and time.time() >= self.deadline():
                    self.deadline_hit = True
                    request_stop("timeout", "the --deadline was reached", "int")
                elif (self.stall_s > 0 and not settled and now - state["last_line"] > self.stall_s):
                    self.event("stall", "stall: no engine output for %ds" % self.stall_s,
                               {"stall_s": self.stall_s, "last_event": self.last_event})
                    request_stop("stall", "no engine output for %ds (--stall %d)" % (self.stall_s, self.stall_s),
                                 "int")
                elif (isinstance(turns_limit, int) and meta["engine"] != "claude"
                      and parser.tool_calls > turns_limit):
                    msg = "%d tool calls exceeded --max-turns %d" % (parser.tool_calls, turns_limit)
                    self.event("budget", "budget: %s" % msg, {"tool_calls": parser.tool_calls,
                                                              "turns_limit": turns_limit})
                    request_stop("budget", msg, "int")
            elif stop["status"] is not None and stop["steps"] and now >= stop["next_at"] and proc.poll() is None:
                sig, gap = stop["steps"].pop(0)
                self.log("engine still alive; sending %s" % sig.name)
                killpg(self.pgid, sig)
                stop["next_at"] = now + gap
            # Post-result linger: the turn settled but the process stays. Activity
            # after a result (a background task, a new segment) restarts the clock.
            if settled and linger["terminal_at"] is None:
                linger["terminal_at"] = now
            elif not settled and linger["term_at"] is None:
                linger["terminal_at"] = None
            if (stop["status"] is None and linger["terminal_at"] is not None and proc.poll() is None):
                if linger["term_at"] is None and now - linger["terminal_at"] > POST_RESULT_WAIT:
                    self.log("engine still alive %ds after its result; sending TERM" % POST_RESULT_WAIT)
                    killpg(self.pgid, signal.SIGTERM)
                    linger["term_at"] = now
                    linger["killed"] = True
                elif linger["term_at"] is not None and now - linger["term_at"] > 5:
                    killpg(self.pgid, signal.SIGKILL)
            if proc.poll() is not None:
                if exited_at is None:
                    exited_at = now
                if eof or now - exited_at > 2.0:
                    break
            self.progress("turn", turn=n, force=False, parser=parser, events=rec["events"],
                          stall_from=time.time() - (time.monotonic() - state["last_line"]))
        if state["buf"]:
            handle_line(state["buf"])
        if stop["status"] is None and self.stopping():
            stop["status"] = "killed"
            stop["reason"] = "runner received %s" % self.signalled if self.signalled else "kill requested"
        events_fh.close()
        rc = proc.wait()
        # The engine is gone; anything it started and left running (a dev
        # server, a watcher) is still in its process group. End it now: with
        # engine.pid removed nothing else could find it.
        if self.sweep_group(self.pgid):
            self.warn("processes the engine left running in its process group were terminated after it exited")
        self.proc = None
        self.pgid = None
        try:
            os.unlink(self.path("engine.pid"))
        except OSError:
            pass
        if unparsed["count"]:
            self.warn("%d engine event%s could not be parsed and %s skipped (raw lines in %s)" % (
                unparsed["count"], "" if unparsed["count"] == 1 else "s",
                "was" if unparsed["count"] == 1 else "were", events_path))
        if linger["killed"]:
            self.warn("engine lingered after its result and was terminated")
            rc = 0 if rc in (-signal.SIGTERM, -signal.SIGKILL, 143, 137) else rc
        if parser.got_terminal and not parser.turn_settled() and stop["status"] is None:
            # The engine exited on its own after a result but with work outstanding.
            pending = sorted(parser.pending_tasks.values()) or sorted(parser.pending_tasks)
            if pending:
                self.warn("engine exited with %d background task%s still pending (%s); the answer may be incomplete"
                          % (len(pending), "" if len(pending) == 1 else "s", compact(", ".join(pending), 160)))
            else:
                self.warn("engine exited after starting a new segment without a closing result; "
                          "the answer is from the previous result")

        info = self.engine.extract(parser, paths, meta, n, started, self.log)
        answer = info.get("answer")
        if info.get("conversation_id") and info["conversation_id"] != meta.get("conversation_id"):
            meta["conversation_id"] = info["conversation_id"]
            self.save_meta()
        rec["session_id"] = info.get("session_id") or meta.get("conversation_id")
        rec["answer"] = answer
        rec["answer_chars"] = len(answer) if answer is not None else 0
        rec["tool_calls"] = parser.tool_calls
        rec["num_turns"] = parser.num_turns
        rec["usage"] = parser.usage
        rec["denials"] = list(parser.denials)
        if parser.structured_output is not None:
            rec["structured_output"] = parser.structured_output
        if isinstance(parser.cost, (int, float)):
            rec["cost_usd"] = parser.cost
            rec["cost_source"] = "engine"
        elif parser.usage:
            try:
                price = price_of(meta["engine"])
            except ValueError as exc:
                price = None
                self.warn("AGENT_SESSIONS_%s_PRICE ignored: %s" % (meta["engine"].upper(), exc))
            est = estimate_cost(parser.usage, price)
            if est is not None:
                rec["cost_usd"] = est
                rec["cost_source"] = "estimate"
        if parser.usage:
            add_usage(self.usage_so_far, parser.usage)
        parser.live_usage = None   # now part of usage_so_far
        if isinstance(rec["cost_usd"], (int, float)):
            self.cost_so_far = round((self.cost_so_far or 0.0) + rec["cost_usd"], 6)
        ledger_id = b.get("ledger")
        if ledger_id and (rec["cost_usd"] is not None or parser.usage):
            ledger_add(ledger_id, meta["name"], rec["cost_usd"],
                       parser.usage["total_tokens"] if parser.usage else None)
        denial_line = parser.denial_summary(bool(meta.get("readonly")))
        if denial_line:
            self.warn(denial_line)
        stopped = (stop["status"], stop["reason"]) if stop["status"] is not None else None
        if stopped:
            rec["exit_code"] = 124 if stop["status"] == "timeout" else None
        else:
            rec["exit_code"] = rc
        stderr_tail = tail_text(stderr_path, STDERR_TAIL) if rc != 0 or parser.is_error else None
        status, failure_class, reason = self.engine.classify(parser, rc, stderr_tail, stopped)
        if status == "ok" or failure_class not in ("killed", "timeout", "stall"):
            breach = self.budget_breach()
            if breach and status in ("ok", "error") and failure_class != "budget":
                self.event("budget", "budget: %s" % breach, {"cost_so_far": self.cost_so_far,
                                                             "usage_so_far": self.usage_so_far})
                status, failure_class, reason = "error", "budget", breach
            elif (status == "ok" and isinstance(turns_limit, int) and meta["engine"] != "claude"
                  and parser.tool_calls > turns_limit):
                status, failure_class = "error", "budget"
                reason = "%d tool calls exceeded --max-turns %d" % (parser.tool_calls, turns_limit)
        if failure_class == "rate_limit":
            text_all = " ".join(t for t in (parser.error_text, parser.last_retry_error, stderr_tail) if t)
            if RATE_LIMIT_RE.search(text_all) or not OVERLOADED_RE.search(text_all):
                self.maybe_pause(rec, parser, text_all)
            else:
                # Provider overload: retry with backoff, but other spawns may proceed.
                rec["_reset_delay"] = reset_delay(parser.retry_delay_ms, text_all)
                self.log("provider overloaded; %s not paused" % meta["engine"])
        self.progress("turn", turn=n, parser=parser, events=rec["events"])
        return finish(status, failure_class, reason)

    def _interrupt_requested(self, path, n):
        """`send --interrupt` writes the number of the turn it means; a stale
        marker for another turn is dropped."""
        text = read_text(path)
        if text is None:
            return False
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            wanted = int(text.strip())
        except ValueError:
            return False
        return wanted == n


def cmd_run(args):
    if len(args) != 1:
        die("usage: _run <state-dir> (internal; launched by spawn)")
    return Runner(os.path.realpath(args[0])).main()
