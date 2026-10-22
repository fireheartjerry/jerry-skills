"""Backends: where the runner process lives.

tmux (default when installed): every child lives in its own tmux session
`as-<name>` on the socket recorded in its meta.json, so a caller whose
environment lost AGENT_SESSIONS_TMUX_SOCKET still finds it, and a human can
attach. process (AGENT_SESSIONS_BACKEND=process, or the fallback when tmux is
missing): the runner is a detached process group whose output goes to
<state>/<name>/pane.log; liveness comes from the runner pid and start time in
progress.json, exactly as for tmux children.
"""

import os
import shutil
import signal
import subprocess
import sys

from agent_sessions.common import PREFIX, SCRIPT, TMUX_INSTALL, die, read_json, run, sdir_of, tail_text, write_json
from agent_sessions.procs import process_matches, ps_lstart, runner_alive, wait_dead

__all__ = [
    "tmux_bin", "ENV_SOCKET", "env_socket", "socket_of", "tmux_argv", "tmux", "session_of", "target",
    "tmux_alive", "live_sessions", "Liveness", "pane_id", "tmux_kill", "server_env", "start", "new_session",
    "BACKENDS", "choose_backend", "backend_of", "capture", "end_session", "alive", "kill",
]

BACKENDS = ("tmux", "process")


def choose_backend(requested=None):
    """(backend, notice). Explicit choice first, then AGENT_SESSIONS_BACKEND,
    then tmux when installed, else process with a one-line notice."""
    choice = requested or os.environ.get("AGENT_SESSIONS_BACKEND") or ""
    if choice:
        if choice not in BACKENDS:
            die("backend must be tmux or process, got %r" % choice)
        if choice == "tmux" and not tmux_bin(required=False):
            die("tmux not found on PATH (backend tmux was requested)",
                "%s, or use --backend process" % TMUX_INSTALL)
        return choice, None
    if tmux_bin(required=False):
        return "tmux", None
    return "process", "notice: tmux not found; using the process backend (no attach; peek reads pane.log)"


def backend_of(meta):
    return (meta or {}).get("backend") or "tmux"

_TMUX = None


def tmux_bin(required=True):
    global _TMUX
    if _TMUX is None:
        _TMUX = shutil.which("tmux") or ""
    if not _TMUX and required:
        die("tmux not found on PATH", TMUX_INSTALL)
    return _TMUX or None


ENV_SOCKET = object()   # sentinel: "use AGENT_SESSIONS_TMUX_SOCKET from the environment"


def env_socket():
    return os.environ.get("AGENT_SESSIONS_TMUX_SOCKET") or None


def socket_of(meta):
    """The tmux socket a child lives on. Recorded in meta.json at spawn, because
    the caller's environment may no longer carry AGENT_SESSIONS_TMUX_SOCKET
    (agent hosts do not keep shell variables between tool calls)."""
    if isinstance(meta, dict) and "tmux_socket" in meta:
        return meta.get("tmux_socket") or None
    return env_socket()


def tmux_argv(*args, **kw):
    sock = kw.pop("sock", ENV_SOCKET)
    assert not kw, kw
    if sock is ENV_SOCKET:
        sock = env_socket()
    argv = [tmux_bin()]
    if sock:
        argv += ["-L", sock]
    conf = os.environ.get("AGENT_SESSIONS_TMUX_CONF")
    if conf:
        argv += ["-f", conf]
    argv.extend(args)
    return argv


def tmux(*args, **kw):
    return run(tmux_argv(*args, **kw))


def session_of(name):
    return "%s-%s" % (PREFIX, name)


def target(name):
    return "=" + session_of(name)


def tmux_alive(name, meta):
    """has-session on the socket this child was spawned on (false for process children)."""
    if backend_of(meta) != "tmux" or not tmux_bin(required=False):
        return False
    return tmux("has-session", "-t", target(name), sock=socket_of(meta)).returncode == 0


def live_sessions(sock=ENV_SOCKET):
    """Exact set of live tmux session names on one socket."""
    if not tmux_bin(required=False):
        return set()
    cp = tmux("list-sessions", "-F", "#{session_name}", sock=sock)
    if cp.returncode != 0:
        return set()
    return set(line.strip() for line in cp.stdout.splitlines() if line.strip())


class Liveness(object):
    """Caches list-sessions per socket for one pass over many children."""

    def __init__(self):
        self.by_sock = {}

    def tmux(self, name, meta):
        if backend_of(meta) != "tmux":
            return False
        sock = socket_of(meta)
        if sock not in self.by_sock:
            self.by_sock[sock] = live_sessions(sock)
        return session_of(name) in self.by_sock[sock]

    def alive(self, name, meta):
        return self.tmux(name, meta) or runner_alive(name)


def pane_id(name, meta):
    cp = tmux("list-panes", "-s", "-t", target(name), "-F", "#{pane_id}", sock=socket_of(meta))
    if cp.returncode != 0:
        return None
    lines = [l.strip() for l in cp.stdout.splitlines() if l.strip()]
    return lines[0] if lines else None


def tmux_kill(name, meta):
    tmux("kill-session", "-t", target(name), sock=socket_of(meta))


def server_env():
    """Environment for the tmux client that may start the server. If this call
    starts the server, the server inherits the environment for its lifetime and
    hands it to every later child, so it gets the minimum."""
    keep = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
            "TMPDIR", "TMUX_TMPDIR")
    return dict((k, os.environ[k]) for k in keep if k in os.environ)


EXIT_UNATTACHED = ("tmux removed the session right after creating it: the tmux server option exit-unattached is "
                   "on (from AGENT_SESSIONS_TMUX_CONF or ~/.tmux.conf), which ends a server that has no attached "
                   "client; set it off (or use another AGENT_SESSIONS_TMUX_SOCKET with a config that does not set "
                   "it), or use --backend process")


def new_session(name, workdir, env, command, sock=ENV_SOCKET):
    """Create a detached tmux session running `command` (argv). Returns None
    on success, else the error text. In the same tmux command list, before
    the server can act on them, the user's tmux.conf options that would end
    or keep this session are switched off for it: destroy-unattached (the
    session is never attached) and remain-on-exit (a dead pane would keep it
    forever). exit-unattached is a server option: switching it off would
    change the user's server, so a session it would end is refused instead."""
    argv = tmux_argv("new-session", "-d", "-s", session_of(name), "-c", workdir, sock=sock)
    for key in sorted(env):
        argv += ["-e", "%s=%s" % (key, env[key])]
    argv += ["--"] + list(command)
    argv += [";", "set-option", "destroy-unattached", "off", ";", "set-option", "-w", "remain-on-exit", "off",
             ";", "show-options", "-s", "-v", "exit-unattached"]
    # If this call starts the tmux server, the server inherits this
    # environment for its lifetime and hands it to every later child.
    # Give it the minimum, so no API key or stale setting lives there.
    cp = run(argv, env=server_env())
    if cp.returncode != 0:
        return "tmux could not start the session: %s" % (cp.stderr.strip() or cp.stdout.strip())
    if (cp.stdout.strip().splitlines() or [""])[-1].strip() == "on":
        tmux("kill-session", "-t", target(name), sock=sock)
        return EXIT_UNATTACHED
    return None


def start(name, workdir, env, sdir, backend="tmux", sock=ENV_SOCKET):
    """Start the runner for one child. Returns None on success, else the error
    text. `env` holds only non-secret variables: for tmux they travel through
    `new-session -e`, which `ps` can show; secrets go through env.json."""
    if backend == "process":
        return _start_process(workdir, env, sdir)
    return new_session(name, workdir, env, [sys.executable, SCRIPT, "_run", sdir], sock=sock)


def _start_process(workdir, env, sdir):
    """Detached process group (start_new_session), stdin from /dev/null,
    output to pane.log. The pid and start time are written to progress.json
    before returning, so the child is never mistaken for lost while the
    runner starts."""
    full = server_env()
    full.update(env)
    log_p = os.path.join(sdir, "pane.log")
    try:
        with open(log_p, "ab") as log_fh:
            proc = subprocess.Popen([sys.executable, SCRIPT, "_run", sdir], cwd=workdir, env=full,
                                    stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True)
    except OSError as exc:
        return "cannot start the runner process: %s" % exc
    prog_p = os.path.join(sdir, "progress.json")
    if (read_json(prog_p) or {}).get("runner_pid") != proc.pid:   # the runner may already have written its own
        write_json(prog_p, {"phase": "starting", "runner_pid": proc.pid, "runner_lstart": ps_lstart(proc.pid),
                            "attempt": 1, "events": 0, "tool_calls": 0})
    return None


def capture(name, meta, lines):
    """Recent pane output: tmux capture-pane, or the tail of pane.log."""
    if backend_of(meta) == "process":
        text = tail_text(os.path.join(sdir_of(name), "pane.log"), 200 * max(lines, 1))
        if text is None:
            return None
        return "\n".join(text.splitlines()[-lines:]) + "\n"
    if not tmux_alive(name, meta):
        return None
    pid = pane_id(name, meta)
    if not pid:
        return None
    cp = tmux("capture-pane", "-p", "-t", pid, "-S", "-%d" % lines, sock=socket_of(meta))
    return cp.stdout


def end_session(name, meta):
    """End the tmux session (linger) of a child; nothing to do for process children."""
    if backend_of(meta) == "tmux" and tmux_alive(name, meta):
        tmux_kill(name, meta)


def alive(name, meta):
    """The child's backend still holds it: its tmux session exists, or its
    runner process (pid and start time from progress.json) is running."""
    return tmux_alive(name, meta) or runner_alive(name)


def kill(name, meta, grace=3):
    """End the child's backend: the tmux session, and for both backends the
    runner process itself when it is still running (TERM, then KILL after
    `grace` seconds). Used when the runner did not write a result in time, so
    a hung or stopped runner never outlives `kill`. Returns True when nothing
    of the child's backend is left running."""
    if backend_of(meta) == "tmux" and tmux_alive(name, meta):
        tmux_kill(name, meta)
    prog = read_json(os.path.join(sdir_of(name), "progress.json")) or {}
    pid, lstart = prog.get("runner_pid"), prog.get("runner_lstart")
    if not process_matches(pid, lstart) or pid == os.getpid():
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    if wait_dead(pid, grace):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return True
    return wait_dead(pid, 5)
