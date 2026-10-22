"""Process liveness (ps based, locale independent), process-group kill, owner
tokens and engine binary lookup."""

import os
import shutil
import sys
import time

from agent_sessions.common import OWNER_COMMS, PS_ENV, read_json, run, sdir_of

__all__ = [
    "ps", "ps_lstart", "pid_running", "process_matches", "engine_alive", "runner_alive", "proc_stat",
    "killpg", "group_alive", "wait_dead", "ps_parent", "ps_argv0", "child_token", "owner_token", "owner_short",
    "owner_gone", "owns", "find_binary", "found_via_login_shell", "shell_lookup", "interpreter_dirs",
]


def ps(*args):
    """ps under the C locale, so lstart and comm parse the same everywhere."""
    return run(["ps"] + list(args), env=PS_ENV)


def proc_stat(pid, line=None):
    """(state, starttime in clock ticks) from /proc/<pid>/stat (Linux), or None.
    The command name in field 2 may hold spaces and parentheses, so fields are
    counted from the last ')'."""
    if line is None:
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as fh:
                line = fh.read()
        except (OSError, ValueError):
            return None
    head, sep, rest = line.rpartition(")")
    fields = rest.split()
    if not sep or not head.strip() or len(fields) < 20:
        return None
    return fields[0], fields[19]


def ps_lstart(pid):
    """Start time of pid as a comparable string: ps lstart under the C locale,
    or on a system without a working ps, `proc:<starttime>` from /proc."""
    cp = ps("-o", "lstart=", "-p", str(pid))
    text = " ".join(cp.stdout.split())
    if text:
        return text
    if cp.returncode == 127 or not cp.stdout:
        st = proc_stat(pid)
        if st:
            return "proc:%s" % st[1]
    return None


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
        st = proc_stat(pid)
        if st:
            return st[0] != "Z"
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


def group_alive(pgid):
    """Some process of the group still exists (signal 0 reaches it)."""
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
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


def ps_argv0(pid):
    """argv[0] of pid, or None. `ps -o comm=` is argv[0] on macOS but the
    executable's own name on Linux, so a process started as `claude` (exec -a,
    a launcher that names its child) shows there under another name. /proc
    gives argv exactly; elsewhere the first word of `ps -o args=` stands in."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return fh.read().split(b"\0", 1)[0].decode("utf-8", "replace") or None
    except (OSError, ValueError):
        pass
    cp = ps("-o", "args=", "-p", str(pid))
    words = cp.stdout.split()
    return words[0] if cp.returncode == 0 and words else None


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
        if base not in OWNER_COMMS:
            base = os.path.basename(ps_argv0(pid) or "")
        if base in OWNER_COMMS:
            return "proc:%s:%d:%s" % (base, pid, lstart)
        pid = ppid
    try:
        if sys.stdin.isatty():
            return "tty:" + os.ttyname(0)
    except (OSError, ValueError):
        pass
    return "user:%d" % os.getuid()


_GONE = {}


def owner_gone(token):
    """True when a `proc:` owner token names a host process that no longer
    runs (or whose pid now belongs to a later process). Other tokens (env:,
    tty:, user:, child:) never count as gone: an explicit owner keeps its
    children until it reaps them."""
    if not isinstance(token, str) or not token.startswith("proc:"):
        return False
    if token in _GONE:
        return _GONE[token]
    parts = token.split(":", 3)
    gone = False
    if len(parts) == 4 and parts[2].isdigit():
        pid = int(parts[2])
        if not pid_running(pid):
            gone = True
        else:
            now = ps_lstart(pid)
            gone = now is not None and now != " ".join(parts[3].split())
    _GONE[token] = gone
    return gone


def owns(owner, me):
    """The caller (token `me`) may treat a child owned by `owner` as its own:
    the same token, or the token of a host session that is gone (adopted).
    A child agent never adopts: what it may touch is what it spawned."""
    if owner == me:
        return True
    if not me or me.startswith("child:"):
        return False
    return owner_gone(owner)


def owner_short(token):
    parts = (token or "?").split(":")
    if parts[0] == "proc" and len(parts) >= 3:
        return "%s:%s" % (parts[1], parts[2])
    if parts[0] == "child" and len(parts) >= 2:
        return "child:" + parts[1]
    if parts[0] == "tty":
        return "tty:" + os.path.basename(":".join(parts[1:]))
    return token or "?"


_VIA_SHELL = set()


def shell_lookup(name):
    """Absolute path of `name` as a login shell sees it, or None."""
    cp = run(["bash", "-lc", 'command -v -- "$0"', name], timeout=20)
    found = cp.stdout.strip().splitlines()
    if cp.returncode == 0 and found and found[-1].startswith("/"):
        return found[-1]
    return None


def find_binary(engine):
    """PATH first, then a login shell (nvm-style shims live in rc files)."""
    found = shutil.which(engine)
    if found:
        return os.path.abspath(found)
    found = shell_lookup(engine)
    if found:
        _VIA_SHELL.add(found)
    return found


def found_via_login_shell(binary):
    return binary in _VIA_SHELL


def interpreter_dirs(binary):
    """Directories a script binary needs on PATH: its own, and that of the
    interpreter its `#!/usr/bin/env X` line names when X is not on PATH (a
    node CLI installed by nvm or Homebrew outside the caller's PATH)."""
    dirs = [os.path.dirname(binary)]
    try:
        with open(binary, "rb") as fh:
            first = fh.readline(256).decode("utf-8", "replace").strip()
    except OSError:
        return dirs
    if not first.startswith("#!"):
        return dirs
    words = first[2:].split()
    if not words:
        return dirs
    interp = words[0]
    if os.path.basename(interp) == "env":
        rest = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
        if not rest:
            return dirs
        interp = rest[0]
    if os.path.isabs(interp):
        return dirs
    if shutil.which(interp):
        return dirs
    beside = os.path.join(os.path.dirname(binary), interp)
    found = beside if os.access(beside, os.X_OK) else shell_lookup(interp)
    if found and os.path.dirname(found) not in dirs:
        dirs.append(os.path.dirname(found))
    return dirs
