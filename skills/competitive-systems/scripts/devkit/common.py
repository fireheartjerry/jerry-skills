"""Shared helpers: errors with exit codes, repository root, git facts, hashing, JSON and
time formatting, changed files. Standard library only, Python 3.8+.

Exit codes used by every command: 0 success or verdict produced; 1 a gate failed or a
measured command failed; 2 usage error; 3 an external tool or command could not be started.
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import posixpath
import re
import signal
import subprocess
import sys
import threading

DEVKIT_DIR = ".devkit"


class DevError(Exception):
    """An expected failure: printed as `dev.py <cmd>: <message>` without a traceback."""
    code = 1

    def __init__(self, message, details=None, code=None):
        Exception.__init__(self, message)
        self.message = message
        self.details = list(details or [])
        if code is not None:
            self.code = code


class UsageError(DevError):
    code = 2


class StartError(DevError):
    """The measured command or an external tool could not be started."""
    code = 3


class CommandFailed(DevError):
    """The measured command ran and exited non-zero; details carry its stderr tail."""
    code = 1


class Parser(argparse.ArgumentParser):
    """argparse whose errors are one `dev.py <cmd>: ...` line (exit 2), not a usage dump."""

    def error(self, message):
        raise UsageError("%s; run `%s --help` for the options" % (message, self.prog))


def warn(msg):
    sys.stderr.write("warning: %s\n" % msg)


def note(msg):
    sys.stderr.write("note: %s\n" % msg)


def print_json(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=False) + "\n")


def fmt_time(seconds):
    """8.10 s / 90.3 ms / 50 us; an hour or more also in hours: 122400.00 s (34.0 h)."""
    if seconds != seconds:
        return "n/a"
    if seconds == float("inf"):
        return "inf s"
    if seconds >= 1e7:
        return "%.3g s (%.3g h)" % (seconds, seconds / 3600.0)
    if seconds >= 3600.0:
        return "%.2f s (%.1f h)" % (seconds, seconds / 3600.0)
    if seconds >= 1.0:
        return "%.2f s" % seconds
    if seconds >= 0.001:
        return "%.1f ms" % (seconds * 1000.0)
    return "%.0f us" % (seconds * 1e6)


def fmt_bytes(n):
    if n is None:
        return "n/a"
    if n >= 1 << 30:
        return "%.2f GiB" % (n / float(1 << 30))
    if n >= 1 << 20:
        return "%.1f MiB" % (n / float(1 << 20))
    return "%.0f KiB" % (n / 1024.0)


def now_iso():
    """Local time, ISO 8601 with offset, seconds precision."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


class GroupTimer(object):
    """Runs a child in its own process group and kills the whole group after `timeout`
    seconds, so `sh -c` and everything it started die together. The timer never signals a
    reaped (possibly recycled) pid: call exited() once the child has exited but before it is
    reaped (wait_unreaped does that), then reap."""

    def __init__(self, proc, timeout):
        self.proc = proc
        self.fired = False
        self._done = False
        self._lock = threading.Lock()
        self._timer = threading.Timer(timeout, self._fire) if timeout else None
        if self._timer:
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            if not self._done:
                self.fired = True
                kill_group(self.proc)

    def exited(self):
        with self._lock:
            self._done = True
        if self._timer:
            self._timer.cancel()


def start_group(cmd, **kw):
    """subprocess.Popen in a new session (POSIX), so kill_group reaches every descendant."""
    if os.name == "posix":
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def kill_group(proc):
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass


def wait_unreaped(proc):
    """Block until proc exits without reaping it, so its pid and process group stay
    reserved while the timer is cancelled. Returns False, without waiting, where os.waitid
    is unavailable."""
    if not (hasattr(os, "waitid") and hasattr(os, "WNOWAIT")):
        return False
    try:
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
    except ChildProcessError:
        pass
    return True


def run_quiet(argv, cwd=None):
    """Run argv, return (returncode, stdout) or (None, '') when it cannot start."""
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True)
    except OSError:
        return None, ""
    return p.returncode, p.stdout


def run_capture(argv, cwd=None):
    """Run argv, return (returncode, stdout, stderr) or (None, '', reason) when it cannot
    start."""
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
    except OSError as e:
        return None, "", str(e)
    return p.returncode, p.stdout, p.stderr


def git_refusal_hint(root):
    """A one-line fix when git refuses root (safe.directory), else None."""
    rc, _, err = run_capture(["git", "-C", root, "rev-parse", "--is-inside-work-tree"])
    if rc and "dubious ownership" in err:
        return ("git refuses %s (dubious ownership: the directory belongs to another user); "
                "run `git config --global --add safe.directory %s`" % (root, root))
    return None


def repo_root(override=None):
    """(root, note). Order: override, DEVKIT_REPO, git toplevel, current directory."""
    if override:
        root = os.path.abspath(override)
        if not os.path.isdir(root):
            raise UsageError("--repo %s is not a directory" % override)
        return root, None
    env = os.environ.get("DEVKIT_REPO")
    if env and os.path.isdir(env):
        return os.path.abspath(env), None
    rc, out = run_quiet(["git", "rev-parse", "--show-toplevel"])
    if rc == 0 and out.strip():
        return out.strip(), None
    hint = git_refusal_hint(os.getcwd())
    if hint:
        return os.getcwd(), hint + "; using the current directory as the root"
    return os.getcwd(), "not a git repository; using the current directory as the root"


def git_info(root):
    """{'rev': sha or None, 'dirty': bool or None}; None values when git is unavailable."""
    rc, out = run_quiet(["git", "rev-parse", "HEAD"], cwd=root)
    if rc != 0 or not out.strip():
        return {"rev": None, "dirty": None}
    rc2, status = run_quiet(["git", "status", "--porcelain"], cwd=root)
    return {"rev": out.strip(), "dirty": (bool(status.strip()) if rc2 == 0 else None)}


def environment():
    """Facts recorded with every measurement so two files can be compared honestly."""
    try:
        load = list(os.getloadavg())
    except (AttributeError, OSError):
        load = None
    return {"python": platform.python_version(), "platform": platform.platform(),
            "cpu_count": os.cpu_count(), "loadavg": load, "timestamp": now_iso()}


def devkit_dir(root, create=False):
    """<root>/.devkit; when created here it gets a `.gitignore` of `*` so it ignores itself."""
    path = os.path.join(root, DEVKIT_DIR)
    if create and not os.path.isdir(path):
        os.makedirs(path)
        try:
            with open(os.path.join(path, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("*\n")
        except OSError:
            pass
    return path


def load_json_file(path, what="file"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise UsageError("cannot read %s %s: %s" % (what, path, e.strerror or e))
    except ValueError as e:
        raise UsageError("%s %s is not valid JSON: %s" % (what, path, e))




# ---------------------------------------------------------------- changed files


def _git_z(root, args):
    """NUL-split stdout of `git -C root <args>`, or None when the command fails."""
    rc, out = run_quiet(["git", "-C", root] + args)
    if rc != 0:
        return None
    return [t for t in out.split("\0")]


def changed_files(root, base=None):
    """[(status, path, old_path)] with status in A M D R U ? (U unmerged, ? untracked).

    Without base: `git diff --name-status HEAD` plus untracked files. With base:
    `git diff --name-status --find-renames BASE...HEAD` plus the working-tree diff; a base
    that does not resolve falls back to origin/BASE with a note (fresh clones and CI
    checkouts often have only the remote branch). A rename yields both the new path (R) and
    the old path (D). Copies and type changes count as M. Empty list when git is unavailable
    or root is not a work tree; callers then say `no git: pass files explicitly`. Paths are
    repository-relative with forward slashes.
    """
    out = []
    seen = set()

    def add(status, path, old=None):
        key = (status, path)
        if path and status != "U" and path in unmerged:
            return
        if path and key not in seen:
            seen.add(key)
            out.append((status, path, old))

    def parse(tokens):
        i = 0
        while i < len(tokens):
            code = tokens[i]
            if not code:
                i += 1
                continue
            letter = code[0]
            if letter in "RC" and i + 2 < len(tokens):
                old, new = tokens[i + 1], tokens[i + 2]
                if letter == "R":
                    add("R", new, old)
                    add("D", old)
                else:
                    add("A", new)
                i += 3
                continue
            if i + 1 >= len(tokens):
                break
            add(letter if letter in "AMDU" else "M", tokens[i + 1])
            i += 2

    rc, text = run_quiet(["git", "-C", root, "rev-parse", "--is-inside-work-tree"])
    if rc != 0 or text.strip() != "true":
        return []
    # `git diff --name-status HEAD` shows a conflicted path as M; ask for them separately.
    unmerged = set(t for t in (_git_z(root, ["diff", "--name-only", "-z", "--diff-filter=U"])
                               or []) if t)
    for path in sorted(unmerged):
        add("U", path)
    if base:
        if not _resolves(root, base) and _resolves(root, "origin/" + base):
            note("--base %s does not resolve; using origin/%s" % (base, base))
            base = "origin/" + base
        tokens = _git_z(root, ["diff", "--name-status", "-z", "--find-renames", "%s...HEAD" % base])
        if tokens is None:
            raise UsageError(base_diff_failure(root, base))
        parse(tokens)
    tokens = _git_z(root, ["diff", "--name-status", "-z", "--find-renames", "HEAD"])
    if tokens is not None:
        parse(tokens)
    tokens = _git_z(root, ["ls-files", "-z", "--others", "--exclude-standard"])
    for path in tokens or []:
        add("?", path)
    return out


def base_diff_failure(root, base):
    """Why `git diff BASE...HEAD` failed, with the fix: a missing ref, a shallow clone with no
    merge base (the default CI checkout), or git's own message."""
    rc, out, err = run_capture(["git", "-C", root, "diff", "--name-only", "%s...HEAD" % base])
    last = (err.strip().splitlines() or ["unknown error"])[-1]
    if not _resolves(root, base):
        return ("--base %s does not resolve; is it a valid ref? In a CI checkout fetch it first "
                "(git fetch origin %s, or actions/checkout with fetch-depth: 0)"
                % (base, base.split("/", 1)[-1]))
    rc2, shallow = run_quiet(["git", "-C", root, "rev-parse", "--is-shallow-repository"])
    if rc2 == 0 and shallow.strip() == "true":
        return ("git diff against %s failed (%s): this is a shallow clone, so %s and HEAD share "
                "no merge base; fetch full history (actions/checkout fetch-depth: 0, or git "
                "fetch --unshallow) or pass --base <merge-base sha>" % (base, last, base))
    return "git diff against %s failed: %s" % (base, last)


def _resolves(root, ref):
    rc, _ = run_quiet(["git", "-C", root, "rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    return rc == 0


# ---------------------------------------------------------------- paths and globs


def to_posix(path):
    return path.replace(os.sep, "/")


def rel_to_root(root, path):
    """A user-supplied path (absolute or relative to the current directory) as a
    repository-relative posix path; UsageError when it lies outside the root."""
    def outside(r):
        return r == os.pardir or r.startswith(os.pardir + os.sep)

    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if outside(rel):        # one side may be reached through a symlink (/tmp, /var on macOS)
        real_dir = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        rel = os.path.relpath(os.path.join(real_dir, os.path.basename(path)),
                              os.path.realpath(root))
    if outside(rel):
        raise UsageError("%s is outside the repository root %s" % (path, to_posix(root)))
    return to_posix(rel)


_GLOB_CACHE = {}


def _split_braces(pattern):
    """Expand `{a,b}` alternation (nested braces allowed) into a list of patterns; a brace
    without a matching close or without a comma stays literal."""
    depth, start = 0, None
    for i, c in enumerate(pattern):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth:
            depth -= 1
            if depth == 0:
                body = pattern[start + 1:i]
                alts, d, last = [], 0, 0
                for j, ch in enumerate(body):
                    if ch == "{":
                        d += 1
                    elif ch == "}":
                        d -= 1
                    elif ch == "," and d == 0:
                        alts.append(body[last:j])
                        last = j + 1
                alts.append(body[last:])
                if len(alts) < 2:
                    continue
                head, tail = pattern[:start], pattern[i + 1:]
                out = []
                for alt in alts:
                    out.extend(_split_braces(head + alt + tail))
                return out
    return [pattern]


def _glob_body(pattern):
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            k = i + 1
            negate = k < n and pattern[k] == "!"
            if negate:
                k += 1
            # a `]` right after `[` or `[!` is a literal member, as in fnmatch
            j = pattern.find("]", k + 1 if k < n and pattern[k] == "]" else k)
            if j == -1 or j == k:
                out.append("\\[")
                i += 1
            else:
                body = pattern[k:j]
                body = "".join("\\" + ch if ch in "\\]^[" else ch for ch in body)
                out.append("[" + ("^" if negate else "") + body + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def glob_regex(pattern):
    """Compile a gitignore-style glob: `**/` is zero or more directories, a trailing `/**`
    everything below, `*` and `?` stay inside one path segment, `[...]` is a class (`[!...]`
    negates, a leading `]` is literal), `{a,b}` is alternation. UsageError for a pattern
    that cannot compile, such as a reversed range `[z-a]`."""
    rx = _GLOB_CACHE.get(pattern)
    if rx is not None:
        return rx
    alts = _split_braces(pattern)
    body = _glob_body(alts[0]) if len(alts) == 1 else \
        "(?:%s)" % "|".join(_glob_body(a) for a in alts)
    try:
        rx = re.compile(body + r"\Z")
    except re.error as e:
        raise UsageError("glob %r cannot be compiled: %s" % (pattern, e))
    _GLOB_CACHE[pattern] = rx
    return rx


def glob_match(pattern, relpath):
    """True when the repository-relative path, or its basename, matches the glob.

    A pattern without `/` is tried against the basename too (like .gitignore), so
    `conftest.py` matches `a/b/conftest.py`; a leading `/` anchors to the root.
    """
    relpath = to_posix(relpath)
    pat = pattern.lstrip("/") if pattern.startswith("/") else pattern
    if glob_regex(pat).match(relpath):
        return True
    if "/" not in pattern:
        return bool(glob_regex(pat).match(relpath.rsplit("/", 1)[-1]))
    return False


def first_match(patterns, relpath):
    """The first glob in patterns that matches relpath, or None."""
    for pat in patterns:
        if glob_match(pat, relpath):
            return pat
    return None


def git_listed_files(root):
    """Tracked plus untracked, not-ignored files of the git work tree at root (relative,
    posix, sorted), or None when root is not the top of a git work tree or git is absent."""
    rc, top = run_quiet(["git", "-C", root, "rev-parse", "--show-toplevel"])
    if rc != 0 or not top.strip():
        return None
    if os.path.realpath(top.strip()) != os.path.realpath(root):
        return None
    tokens = _git_z(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if tokens is None:
        return None
    return sorted(set(t for t in tokens if t))


def walk_files(root, exclude_dirs, use_git=True):
    """Sorted repository-relative posix paths of regular files, pruning excluded dirs.

    Inside a git work tree (root is its top level) the candidates come from `git ls-files
    --cached --others --exclude-standard`, so gitignored directories (virtual environments,
    generated code, output) are never read; elsewhere the tree is walked. Either way an
    exclude entry without `/` prunes a directory of that name anywhere, an entry with `/` is
    a glob on the directory's relative path, and a directory holding `pyvenv.cfg` (a virtual
    environment under any name) is pruned.
    """
    plain = set(d for d in exclude_dirs if "/" not in d and not any(ch in d for ch in "*?[{"))
    globbed = [d for d in exclude_dirs if d not in plain]
    listed = git_listed_files(root) if use_git else None
    if listed is not None:
        venvs = set(posixpath.dirname(f) for f in listed
                    if posixpath.basename(f) == "pyvenv.cfg")
        bad_dirs = {}

        def excluded_dir(d):
            hit = bad_dirs.get(d)
            if hit is None:
                parent = posixpath.dirname(d)
                hit = (bool(parent) and excluded_dir(parent)) or \
                    posixpath.basename(d) in plain or d in venvs or \
                    any(glob_match(g, d) for g in globbed) or \
                    os.path.islink(os.path.join(root, d))
                bad_dirs[d] = hit
            return hit

        out = []
        for f in listed:
            d = posixpath.dirname(f)
            if d and excluded_dir(d):
                continue
            if os.path.isfile(os.path.join(root, f)) and not os.path.islink(os.path.join(root, f)):
                out.append(f)
        return out
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = to_posix(os.path.relpath(dirpath, root))
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        if rel_dir and "pyvenv.cfg" in filenames:
            dirnames[:] = []
            continue
        keep = []
        for d in sorted(dirnames):
            if d in plain or any(glob_match(g, rel_dir + d) for g in globbed):
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.isfile(full):
                out.append(rel_dir + name)
    out.sort()
    return out


# ---------------------------------------------------------------- tools/dev.json

CONFIG_SCHEMA = "devkit-config/1"
CONFIG_PATH = "tools/dev.json"
DEFAULT_CONFIG = {
    "schema": CONFIG_SCHEMA,
    "languages": None,                 # None: detect
    "package_manager": None,           # None: detect from the lockfile
    "commands": {},                    # {lang: {step: null | "shell command"}}
    "sensitive": ["pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini",
                  "Pipfile", "requirements*.txt", "requirements*.in", "*.lock", "conftest.py",
                  "package.json", "pnpm-lock.yaml", "package-lock.json", "bun.lockb",
                  "tsconfig*.json", "vitest.config.*", "jest.config.*", "go.mod", "go.sum",
                  "Cargo.toml", ".github/workflows/**", "tools/dev.json"],
    "ignore": ["*.md", "*.rst", "docs/**", "LICENSE*", "COPYING*", "*.png", "*.jpg",
               "*.jpeg", "*.gif", "*.svg"],
    "exclude_dirs": [".git", "node_modules", ".venv", "venv", "env", ".conda", ".nox", "dist",
                     "build", "target", "out", "coverage", ".next", ".nuxt", ".turbo",
                     ".devkit", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
                     ".ruff_cache"],
    "test_globs": {
        "python": ["**/test_*.py", "**/*_test.py"],
        "typescript": ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"],
        "javascript": ["**/*.test.*", "**/*.spec.*", "**/__tests__/**"],
        "go": ["**/*_test.go"],
        "rust": ["**/tests/**/*.rs"],
    },
    "test_map": {},
    "always_run": [],
    "benchmarks": [],
    "size_audit": {"file_lines": 450, "function_lines": 70},
    "budget_seconds": 480,
    "max_changed_files": 25,
}
_LIST_KEYS = ("sensitive", "ignore", "exclude_dirs", "always_run", "benchmarks")
# A path with one of these directory names is test data: `ignore` never drops it, so a
# changed golden file, snapshot or fixture fails closed instead of selecting nothing.
TEST_DATA_DIRS = frozenset(("tests", "test", "__tests__", "__snapshots__", "snapshots",
                            "fixtures", "__fixtures__", "testdata", "test_data", "spec",
                            "golden", "goldens"))


def is_test_data(relpath):
    """True when a directory segment of relpath names a test or fixture directory."""
    return any(seg in TEST_DATA_DIRS for seg in to_posix(relpath).split("/")[:-1])
_DICT_KEYS = ("commands", "test_globs", "test_map", "size_audit")
STEPS = ("lint", "typecheck", "test", "test_report")


def load_config(root, warn_unknown=True):
    """tools/dev.json merged over DEFAULT_CONFIG. A key present in the file replaces the
    default (lists are not extended), except `test_globs`, merged per language. Unknown keys
    warn; `_doc` keys are comments. A missing file gives the defaults with `path` None."""
    import copy
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = os.path.join(root, CONFIG_PATH)
    cfg["path"] = None
    if not os.path.isfile(path):
        return cfg
    data = load_json_file(path, "config")
    where = CONFIG_PATH
    if not isinstance(data, dict):
        raise UsageError("%s must hold a JSON object" % where)
    schema = data.get("schema", CONFIG_SCHEMA)
    if schema != CONFIG_SCHEMA:
        raise UsageError("%s has schema %r; this dev.py reads %s" % (where, schema, CONFIG_SCHEMA))
    for key, value in data.items():
        if key.startswith("_doc") or key == "schema":
            continue
        if key not in DEFAULT_CONFIG:
            if warn_unknown:
                warn("%s: unknown key %r ignored" % (where, key))
            continue
        if key in _LIST_KEYS and not isinstance(value, list):
            raise UsageError("%s: %s must be a list" % (where, key))
        if key in _DICT_KEYS and not isinstance(value, dict):
            raise UsageError("%s: %s must be an object" % (where, key))
        if key in ("budget_seconds", "max_changed_files") and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise UsageError("%s: %s must be a non-negative number" % (where, key))
        if key == "languages" and value is not None and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise UsageError("%s: languages must be a list of names or null" % where)
        if key == "test_globs":
            for lang, globs in value.items():
                if lang.startswith("_doc"):
                    continue
                if not isinstance(globs, list):
                    raise UsageError("%s: test_globs.%s must be a list" % (where, lang))
                cfg["test_globs"][lang] = globs
            continue
        if key == "commands":
            for lang, steps in value.items():
                if lang.startswith("_doc"):
                    continue
                if not isinstance(steps, dict):
                    raise UsageError("%s: commands.%s must be an object" % (where, lang))
                for step, cmd in steps.items():
                    if step.startswith("_doc"):
                        continue
                    if step not in STEPS:
                        if warn_unknown:
                            warn("%s: unknown step commands.%s.%s ignored" % (where, lang, step))
                        continue
                    if cmd is not None and not isinstance(cmd, str):
                        raise UsageError("%s: commands.%s.%s must be a string or null"
                                         % (where, lang, step))
            cfg["commands"] = value
            continue
        if key == "test_map":
            for src, targets in value.items():
                if src.startswith("_doc"):
                    continue
                if not (isinstance(targets, list) and all(isinstance(t, str) for t in targets)):
                    raise UsageError("%s: test_map[%r] must be a list of paths" % (where, src))
        cfg[key] = value
    for key in ("sensitive", "ignore", "exclude_dirs", "always_run"):
        for g in cfg.get(key) or []:
            if not isinstance(g, str):
                raise UsageError("%s: %s entries must be strings" % (where, key))
            try:
                glob_regex(g.lstrip("/"))
            except UsageError as e:
                raise UsageError("%s: %s: %s" % (where, key, e.message))
    for lang, globs in cfg["test_globs"].items():
        for g in globs if isinstance(globs, list) else []:
            try:
                glob_regex(str(g).lstrip("/"))
            except UsageError as e:
                raise UsageError("%s: test_globs.%s: %s" % (where, lang, e.message))
    for g in cfg.get("test_map", {}):
        try:
            glob_regex(g.lstrip("/"))
        except UsageError as e:
            raise UsageError("%s: test_map: %s" % (where, e.message))
    cfg["path"] = where
    return cfg


def config_command(cfg, lang, step):
    """The configured string for commands.<lang>.<step>, or None (resolve at run time)."""
    steps = cfg.get("commands", {}).get(lang) or {}
    value = steps.get(step)
    return value if isinstance(value, str) and value.strip() else None
