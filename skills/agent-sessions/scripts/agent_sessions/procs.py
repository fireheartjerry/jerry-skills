"""Process liveness (ps based, locale independent), process-group kill, owner
tokens and engine binary lookup."""

import os
import shutil
import sys
import time

from agent_sessions.common import OWNER_COMMS, PS_ENV, read_json, run, sdir_of

__all__ = [
    "ps", "ps_lstart", "pid_running", "process_matches", "engine_alive", "runner_alive",
    "killpg", "wait_dead", "ps_parent", "child_token", "owner_token", "owner_short", "find_binary",
]


def ps(*args):
    """ps under the C locale, so lstart and comm parse the same everywhere."""
    return run(["ps"] + list(args), env=PS_ENV)


def ps_lstart(pid):
    cp = ps("-o", "lstart=", "-p", str(pid))
    text = " ".join(cp.stdout.split())
    return text or None


def pid_running(pid):
    """True while pid exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cp = ps("-o", "stat=", "-p", str(pid))
    stat = cp.stdout.strip()
    if cp.returncode != 0 or not stat:
        return False
    return not stat.startswith("Z")


def process_matches(pid, lstart):
    """pid is live and started when we recorded it (guards against pid reuse)."""
    if not pid or not lstart or not pid_running(pid):
        return False
    now = ps_lstart(pid)
    return now is not None and now == " ".join(str(lstart).split())


def engine_alive(info):
    """engine.pid names a live process of ours: same pid, same start time."""
    if not isinstance(info, dict):
        return False
    return process_matches(info.get("pid"), info.get("lstart"))


def runner_alive(name):
    """The runner process recorded in progress.json is still running. Used as
    a second opinion before a child is declared lost or reaped."""
    prog = read_json(os.path.join(sdir_of(name), "progress.json")) or {}
    return process_matches(prog.get("runner_pid"), prog.get("runner_lstart"))


def killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def wait_dead(pid, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not pid_running(pid):
            return True
        time.sleep(0.2)
    return not pid_running(pid)


def ps_parent(pid):
    # Under LC_ALL=C, lstart is always five tokens ("Tue Oct 20 17:26:46 2026").
    cp = ps("-o", "ppid=,lstart=,comm=", "-p", str(pid))
    toks = cp.stdout.split()
    if cp.returncode != 0 or len(toks) < 7:
        return None
    try:
        ppid = int(toks[0])
    except ValueError:
        return None
    return ppid, " ".join(toks[1:6]), " ".join(toks[6:])


def child_token(meta):
    """Owner token of everything a child spawns. Includes the start time so a
    reused name never claims an older child's descendants."""
    return "child:%s:%d" % (meta["name"], int(meta.get("started_epoch") or 0))


def owner_token():
    child = os.environ.get("AGENT_SESSION_NAME")
    if child:
        started = os.environ.get("AGENT_SESSION_STARTED") or "0"
        return "child:%s:%s" % (child, started if started.isdigit() else "0")
    explicit = os.environ.get("AGENT_SESSIONS_OWNER")
    if explicit:
        return "env:" + explicit
    pid = os.getppid()
    for _ in range(40):
        if pid <= 1:
            break
        info = ps_parent(pid)
        if not info:
            break
        ppid, lstart, comm = info
        base = os.path.basename(comm.strip())
        if base in OWNER_COMMS:
            return "proc:%s:%d:%s" % (base, pid, lstart)
        pid = ppid
    try:
        if sys.stdin.isatty():
            return "tty:" + os.ttyname(0)
    except (OSError, ValueError):
        pass
    return "user:%d" % os.getuid()


def owner_short(token):
    parts = (token or "?").split(":")
    if parts[0] == "proc" and len(parts) >= 3:
        return "%s:%s" % (parts[1], parts[2])
    if parts[0] == "child" and len(parts) >= 2:
        return "child:" + parts[1]
    if parts[0] == "tty":
        return "tty:" + os.path.basename(":".join(parts[1:]))
    return token or "?"


def find_binary(engine):
    """PATH first, then a login shell (nvm-style shims live in rc files)."""
    found = shutil.which(engine)
    if found:
        return os.path.abspath(found)
    cp = run(["bash", "-lc", 'command -v -- "$0"', engine], timeout=20)
    found = cp.stdout.strip().splitlines()
    if cp.returncode == 0 and found and found[-1].startswith("/"):
        return found[-1]
    return None
