"""Shared harness for the agent-session test suite.

Each test gets a private sandbox: its own state root, fake-engine home, codex
home, work directory and tmux socket. Nothing touches the real state root or
the default tmux server, and no real engine is ever called.
"""

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPT = os.path.join(SKILL, "scripts", "agent-session")
FAKE_DIR = os.path.join(HERE, "fake-engine")
ROOT = os.path.realpath(os.environ.get("AGENT_SESSIONS_TEST_ROOT") or tempfile.mkdtemp(prefix="agent-sessions-test."))
_COUNTER = itertools.count()
_MODULE = None


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def script_module():
    """The agent-session script imported as a module, for unit tests of its
    helpers (ps parsing, event parsers). Loaded once per test process."""
    global _MODULE
    if _MODULE is None:
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("agent_session_under_test", SCRIPT)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _MODULE = mod
    return _MODULE


def ps_lstart_under(locale, pid):
    env = dict(os.environ, LC_ALL=locale, LANG=locale)
    cp = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, universal_newlines=True)
    return " ".join(cp.stdout.split())


def localized_locale(pid=None):
    """A locale installed on this machine whose `ps -o lstart=` output differs
    from the C locale, or None when none of the candidates is installed."""
    pid = pid or os.getpid()
    base = ps_lstart_under("C", pid)
    for loc in ("de_DE.UTF-8", "ja_JP.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8"):
        text = ps_lstart_under(loc, pid)
        if text and text != base:
            return loc
    return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cp = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, universal_newlines=True)
    stat = cp.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def wait_until(pred, timeout=20, interval=0.2, message="condition not met"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("%s within %ss" % (message, timeout))


class SessionTest(unittest.TestCase):
    linger = 1
    idle = 3600
    max_children = 3

    def setUp(self):
        self.box = os.path.join(ROOT, "%s.%s" % (self.__class__.__name__, self._testMethodName))
        shutil.rmtree(self.box, ignore_errors=True)
        self.state = os.path.join(self.box, "state")
        self.fake_home = os.path.join(self.box, "fake")
        self.codex_home = os.path.join(self.box, "codex-home")
        self.work = os.path.join(self.box, "work")
        for d in (self.state, self.fake_home, self.codex_home, self.work):
            os.makedirs(d)
        self.sock = "as-test-%d-%d" % (os.getpid(), next(_COUNTER))
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", self.box),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "AGENT_SESSIONS_STATE": self.state,
            "AGENT_SESSIONS_TMUX_SOCKET": self.sock,
            # A fresh server per test must not load the developer's ~/.tmux.conf.
            "AGENT_SESSIONS_TMUX_CONF": "/dev/null",
            "AGENT_SESSIONS_LINGER": str(self.linger),
            "AGENT_SESSIONS_IDLE": str(self.idle),
            "AGENT_SESSIONS_MAX": str(self.max_children),
            "AGENT_SESSIONS_OWNER": "test-owner",
            "FAKE_ENGINE_HOME": self.fake_home,
            "CODEX_HOME": self.codex_home,
        }

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.sock, "kill-server"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        for extra in getattr(self, "extra_socks", []):
            subprocess.run(["tmux", "-L", extra, "kill-server"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

    def another_socket(self):
        """A second private tmux socket, killed in tearDown."""
        sock = "as-test-%d-%d" % (os.getpid(), next(_COUNTER))
        self.extra_socks = getattr(self, "extra_socks", []) + [sock]
        return sock

    # -- running the script
    def environ(self, env=None):
        merged = dict(self.env)
        for key, value in (env or {}).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged

    def run_as(self, *args, **kw):
        env = self.environ(kw.pop("env", None))
        timeout = kw.pop("timeout", 60)
        stdin = kw.pop("input", None)
        cwd = kw.pop("cwd", self.work)
        assert not kw, kw
        return subprocess.run([sys.executable, SCRIPT] + list(args), env=env, input=stdin, cwd=cwd,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout)

    def ok(self, *args, **kw):
        cp = self.run_as(*args, **kw)
        self.assertEqual(cp.returncode, 0, "%r failed (%d)\n%s%s" % (args, cp.returncode, cp.stdout, cp.stderr))
        return cp

    def spawn(self, engine, name, prompt, *extra, **kw):
        directory = kw.pop("directory", self.work)
        return self.ok("spawn", engine, name, directory, prompt, *extra, **kw)

    def wait(self, *names, **kw):
        budget = kw.pop("timeout", 30)
        return self.run_as("wait", "--timeout", str(budget), *names, **kw)

    # -- state access
    def sdir(self, name):
        return os.path.join(self.state, name)

    def read(self, name, *parts):
        return read_json(os.path.join(self.sdir(name), *parts))

    def meta(self, name):
        return self.read(name, "meta.json")

    def result(self, name):
        return self.read(name, "result.json")

    def turn(self, name, n):
        return self.read(name, "turns", "%d.json" % n)

    def has(self, name, *parts):
        return os.path.exists(os.path.join(self.sdir(name), *parts))

    def wait_until(self, pred, timeout=20, interval=0.2, message="condition not met"):
        return wait_until(pred, timeout, interval, message)

    def wait_result(self, name, timeout=20):
        return self.wait_until(lambda: self.has(name, "result.json") and self.result(name),
                               timeout, message="no result for %s" % name)

    def calls(self, name=None, engine=None):
        path = os.path.join(self.fake_home, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        if name is not None:
            entries = [e for e in entries if e["env"].get("AGENT_SESSION_NAME") == name]
        if engine is not None:
            entries = [e for e in entries if e["engine"] == engine]
        return entries

    def queued_id(self, stdout):
        for line in stdout.splitlines():
            if line.startswith("queued"):
                return line.split(":", 1)[1].strip()
        raise AssertionError("no queued line in %r" % stdout)

    # -- tmux and git
    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null"] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)

    def tmux_alive(self, name):
        return self.tmux("has-session", "-t", "=as-%s" % name).returncode == 0

    def git(self, path, *args):
        return subprocess.run(["git", "-C", path, "-c", "user.name=test", "-c", "user.email=test@example.com",
                               "-c", "commit.gpgsign=false"] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def make_repo(self, path):
        os.makedirs(os.path.join(path, "packages", "api"), exist_ok=True)
        with open(os.path.join(path, "README.md"), "w") as fh:
            fh.write("committed\n")
        with open(os.path.join(path, "packages", "api", "app.py"), "w") as fh:
            fh.write("print('api')\n")
        self.assertEqual(self.git(path, "init", "-q", "-b", "main").returncode, 0)
        self.assertEqual(self.git(path, "add", "-A").returncode, 0)
        cp = self.git(path, "commit", "-q", "-m", "init")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return os.path.realpath(path)

    def head(self, path):
        return self.git(path, "rev-parse", "HEAD").stdout.strip()

    def branch_exists(self, path, branch):
        return self.git(path, "show-ref", "--verify", "--quiet", "refs/heads/%s" % branch).returncode == 0

    # -- fixtures
    def fake_meta(self, name, **over):
        """A meta.json for a child whose tmux session no longer exists."""
        sdir = self.sdir(name)
        for sub in ("inbox", "turns", "messages"):
            os.makedirs(os.path.join(sdir, sub), exist_ok=True)
        meta = {"version": 2, "name": name, "engine": "claude", "model": "haiku", "tier": "cheap",
                "steerable": False, "readonly": False, "origin": self.work, "workdir": self.work,
                "worktree": None, "timeout_s": 3600, "depth": 1, "owner": "env:test-owner",
                "conversation_id": "00000000-0000-4000-8000-000000000000", "binary": "/nonexistent/claude",
                "python": sys.executable, "tmux_socket": self.sock, "env_path": os.environ["PATH"],
                "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "started_epoch": time.time(),
                "spawner_pid": os.getpid()}
        meta.update(over)
        with open(os.path.join(sdir, "meta.json"), "w") as fh:
            json.dump(meta, fh)
        return meta
