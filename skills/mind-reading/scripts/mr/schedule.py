"""Generate a portable weekly candidate runner. Installation and promotion are separate user actions."""
import datetime as dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import read_json, skill_dir, write_json
from .corpus import now_iso
from .preferences import _private_json, _safe_path, _check_output, _private_bytes, _json_bytes


class ScheduleError(ValueError):
    pass


API_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_TYPE", "CODEX_API_KEY", "CODEX_BASE_URL",
    "AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_BASE_URL",
}


def subscription_environment(environ=None):
    """Use existing CLI sign-in; never inherit an API key, provider switch or alternate billing endpoint."""
    return {k: v for k, v in (os.environ if environ is None else environ).items() if k not in API_ENV}


def _execution_path(cfg, engine):
    """Freeze executable lookup while the interactive shell can find the CLI."""
    executable = None
    if engine != "fake":
        command = (cfg.get("engine_cmd") or {}).get(engine) or [engine]
        if not isinstance(command, list) or not command or not isinstance(command[0], str):
            raise ScheduleError("engine command needs an executable")
        head = os.path.expanduser(command[0])
        executable = shutil.which(head)
        if not executable:
            raise ScheduleError("engine executable is not available: %s; install or set an absolute engine command before planning" % command[0])
        executable = str(Path(executable).absolute())
    entries = ([str(Path(executable).parent)] if executable else [])
    entries += [str(Path(part).expanduser().absolute()) for part in os.environ.get("PATH", os.defpath).split(os.pathsep) if part]
    entries += os.defpath.split(os.pathsep)
    return executable, os.pathsep.join(dict.fromkeys(entries))


def plan(paths, cfg, out_dir, weekday=0, hour=9, minute=0, timezone=None, max_calls=12, engine=None):
    """Write reviewable files only. Weekday 0 is Monday. launchd uses the Mac's local timezone."""
    for value, low, high, name in ((weekday, 0, 6, "weekday"), (hour, 0, 23, "hour"), (minute, 0, 59, "minute"), (max_calls, 1, 100, "max_calls")):
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ScheduleError("%s must be an integer from %d to %d" % (name, low, high))
    selected_engine = engine or cfg.get("engine", "claude")
    if selected_engine not in ("claude", "codex", "fake"):
        raise ScheduleError("weekly execution needs claude or codex (fake is for offline tests)")
    out = _safe_path(out_dir)
    live = paths.home.resolve()
    if out == live or live in out.parents or out in live.parents:
        raise ScheduleError("schedule directory must be separate from the live data home")
    executable, executable_path = _execution_path(cfg, selected_engine)
    spec = {"schema_version": 1, "live_home": str(live), "candidate_root": str(out / "candidates"),
            "state_dir": str(out / "state"), "cli": str(skill_dir() / "scripts" / "mind_reading.py"),
            "python": sys.executable, "engine": selected_engine, "max_calls_per_stage": max_calls,
            "engine_auth": "subscription",
            "engine_executable": executable, "executable_path": executable_path,
            "weekday": weekday, "hour": hour, "minute": minute, "timezone": timezone or cfg.get("timezone", "UTC"),
            "promotion": "never automatic", "installed": False}
    spec_path = out / "weekly.json"
    _safe_path(spec_path)
    if spec_path.exists() and read_json(spec_path) != spec:
        raise ScheduleError("schedule plan already exists with different settings; preserve it and choose another directory")
    runner = out / "weekly.py"
    # A standalone file imports only this exact installed skill and reads its frozen plan beside itself.
    runner_data = ("#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\nfrom mr.schedule import run_weekly\nresult = run_weekly(Path(__file__).with_name('weekly.json'))\nprint(__import__('json').dumps(result, sort_keys=True))\nsys.exit(1 if result.get('status') == 'failed' else 0)\n" % str(skill_dir() / "scripts")).encode("utf-8")
    label = "local.mind-reading.weekly-" + __import__("hashlib").sha256(str(live).encode()).hexdigest()[:12]
    plist = out / (label + ".plist")
    plist_data = plistlib.dumps({"Label": label, "ProgramArguments": [sys.executable, "-I", str(runner)],
                      "StartCalendarInterval": {"Weekday": (weekday + 1) % 7, "Hour": hour, "Minute": minute},
                      "RunAtLoad": False, "ProcessType": "Background",
                      "EnvironmentVariables": {"PATH": executable_path},
                      "StandardOutPath": str(out / "launchd.log"), "StandardErrorPath": str(out / "launchd-error.log")})
    for target, content in ((spec_path, _json_bytes(spec)), (runner, runner_data), (plist, plist_data)):
        _check_output(target, content)
    for target in (out / "launchd.log", out / "launchd-error.log"):
        _safe_path(target)
        if target.exists() and not spec_path.exists():
            raise ScheduleError("schedule output already contains an unrelated log")
    _private_json(spec_path, spec)
    _private_bytes(runner, runner_data)
    os.chmod(runner, 0o700)
    _private_bytes(plist, plist_data)
    return {"plan": str(spec_path), "runner": str(runner), "launchd_plist": str(plist), "label": label,
            "installed": False, "timezone": spec["timezone"], "max_calls_per_stage": max_calls,
            "engine_executable": executable, "training_boundary": "run date at 00:00 UTC, candidate only",
            "maximum_engine_calls": 3 * max_calls, "promotion": "never automatic",
            "note": "launchd calendar uses the Mac's local timezone; use the app scheduler or a system scheduler in the requested timezone. Existing schedules must be checked before installation."}


def _lock(lock):
    """Kernel lock survives neither a crash nor a reboot; the lock inode is never unlinked while in use."""
    import fcntl
    lock = _safe_path(lock)
    if lock.exists():
        previous = read_json(lock)
        if not isinstance(previous, dict) or not isinstance(previous.get("pid"), int) or not isinstance(previous.get("created"), str):
            raise ScheduleError("existing lock is not a generated weekly lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    fh = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise ScheduleError("weekly candidate job is already running")
    fh.seek(0)
    fh.truncate()
    json.dump({"pid": os.getpid(), "created": now_iso()}, fh)
    fh.flush()
    return fh


def _check_state(path):
    path = _safe_path(path)
    if path.exists():
        data = read_json(path)
        if not isinstance(data, dict) or data.get("promotion") != "never automatic" or not isinstance(data.get("status"), str):
            raise ScheduleError("existing state is not a generated weekly artifact")


def run_weekly(spec_path, run_command=None, today=None):
    """One candidate per local ISO week; unresolved review, failure or a live lock stops further engine calls."""
    spec = read_json(spec_path)
    if not isinstance(spec, dict) or spec.get("promotion") != "never automatic" or spec.get("engine_auth") != "subscription":
        raise ScheduleError("invalid candidate-only weekly plan")
    if not isinstance(spec.get("executable_path"), str) or not spec["executable_path"].strip():
        raise ScheduleError("weekly plan needs a frozen executable PATH; regenerate the plan")
    live = Path(spec["live_home"]).expanduser().resolve()
    root = _safe_path(spec["candidate_root"])
    state_dir = _safe_path(spec["state_dir"])
    for p in (root, state_dir):
        if p == live or live in p.parents or p in live.parents:
            raise ScheduleError("weekly paths overlap the live data home")
    maximum = spec.get("max_calls_per_stage")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 100:
        raise ScheduleError("weekly plan needs a bounded positive max_calls_per_stage")
    if today is None:
        try:
            from zoneinfo import ZoneInfo
            today = dt.datetime.now(ZoneInfo(spec["timezone"])).date()
        except (ImportError, KeyError) as exc:
            raise ScheduleError("cannot resolve configured timezone") from exc
    year, week, _ = today.isocalendar()
    slot = "%04d-W%02d" % (year, week)
    candidate = root / slot
    lock, state_file = state_dir / "running.json", state_dir / "latest.json"
    attempt_path, log = state_dir / (slot + ".json"), state_dir / (slot + ".log")
    # Inspect every potential output before mkdir, chmod or lock-file truncation.
    for target in (lock, state_file, attempt_path, log, candidate):
        _safe_path(target)
    _check_state(state_file)
    _check_state(attempt_path)
    if lock.exists():
        previous = read_json(lock)
        if not isinstance(previous, dict) or not isinstance(previous.get("pid"), int) or not isinstance(previous.get("created"), str):
            raise ScheduleError("existing lock is not a generated weekly lock")
    if not attempt_path.exists():
        _check_output(log, b"")
    for p in (root, state_dir):
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = _lock(lock)
    try:
        unresolved = [p for p in sorted(root.glob("*/CANDIDATE.json"))
                      if (read_json(p) or {}).get("status") not in ("dismissed_by_user", "promoted_by_user")]
        if unresolved:
            result = {"status": "awaiting_user", "candidate": str(unresolved[-1].parent), "reason": "previous candidate requires human review or explicit dismissal", "promotion": "never automatic"}
            _private_json(state_file, result, replace=True)
            return result
        if attempt_path.exists():
            return {"status": "already_ran" if candidate.exists() else "already_attempted", "candidate": str(candidate),
                    "promotion": "never automatic", "reason": "this weekly slot was already attempted; inspect its recorded result"}
        if candidate.exists():
            return {"status": "already_ran", "candidate": str(candidate), "promotion": "never automatic"}
        cmd = [spec["python"], "-I", spec["cli"], "--home", str(live), "refresh", "--candidate-home", str(candidate),
               "--extend", "--corrections", "--engine", spec["engine"], "--max-calls", str(maximum), "--concurrency", "1",
               "--train-before", today.isoformat()]
        result = {"status": "running", "started": now_iso(), "candidate": str(candidate), "command": cmd, "promotion": "never automatic"}
        _private_json(state_file, result, replace=True)
        _private_json(attempt_path, result)
        with log.open("w", encoding="utf-8") as fh:
            os.chmod(log, 0o600)
            environment = subscription_environment()
            environment["PATH"] = spec["executable_path"]
            completed = (run_command or subprocess.run)(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, check=False,
                                                        env=environment)
        marker = read_json(candidate / "CANDIDATE.json", {}) or {}
        good = completed.returncode == 0 and marker.get("status") == "awaiting_human_review"
        result.update({"status": "awaiting_human_review" if good else "failed", "finished": now_iso(),
                       "exit_code": completed.returncode, "candidate_status": marker.get("status", "missing"), "log": str(log)})
        _private_json(state_file, result, replace=True)
        _private_json(attempt_path, result, replace=True)
        return result
    except Exception as exc:
        _private_json(state_file, {"status": "failed", "finished": now_iso(), "error_type": type(exc).__name__, "promotion": "never automatic"}, replace=True)
        raise
    finally:
        lock_handle.close()
