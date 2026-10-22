"""The check pipeline, single steps (lint, typecheck) and the test wrapper with history.

check: lint(changed) -> typecheck(changed) -> affected tests -> map drift, stage by stage
across the detected languages, stopping at the first failing gate. Each gate records name,
language, command, status (pass | fail | skipped), seconds, the first 25 output lines and a
fix hint. test: the full suite per language; with --record it writes one JSONL file per run
under .devkit/runs/ from the runner's report (pytest JUnit XML, vitest or jest JSON,
`go test -json`, a bundled unittest reporter), or one target-level record with the wall time.
"""
import datetime
import importlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from devkit.affected import _rel, affected, gather_changed, warn_if_empty
from devkit.common import (GroupTimer, Parser, UsageError, devkit_dir, first_match, fmt_time,
                           is_test_data, kill_group, load_config, note, print_json, repo_root,
                           start_group, to_posix, wait_unreaped)
from devkit.detect import (LANG_ORDER, PYTHON, detect, display_of, js_exec, language_of,
                           resolve, safe_path, skip_hint, unittest_pieces)

HEAD_LINES = 25
TAIL_LINES = 15     # runners print their summary last; kept when the output is longer
SHOW_ARGS = 8

FIX_HINTS = {
    ("lint", "ruff"): "fix the findings above; `ruff check --fix` applies the safe ones",
    ("lint", "compileall"): "fix the syntax error above",
    ("lint", "go vet"): "run gofmt -w on the listed files, then fix the go vet findings",
    ("lint", "clippy"): "run cargo fmt, then fix the clippy findings",
    ("lint", "check"): "run cargo fmt, then fix the compiler errors",
    ("typecheck", None): "fix the type errors above",
    ("lint", None): "fix the lint findings above",
}


# ---------------------------------------------------------------- process running


def _argv_exe(argv):
    return [sys.executable] + argv[1:] if argv and argv[0] == PYTHON else list(argv)


def run_process(cmd, cwd, shell=False, env=None, stream=False, line_filter=None, timeout=None):
    """Run cmd with stdout and stderr merged. Returns (returncode, output, seconds);
    returncode is None when the command cannot start. stream echoes lines to stdout
    (through line_filter, which may return None to hide a line)."""
    t0 = time.perf_counter()
    try:
        proc = start_group(cmd if shell else _argv_exe(cmd), cwd=cwd, shell=shell,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, env=env, universal_newlines=True,
                           errors="replace")
    except OSError as e:
        return None, "cannot start: %s" % (e.strerror or e), time.perf_counter() - t0
    timer = GroupTimer(proc, timeout)       # kills sh -c and all it started, not only sh
    lines = []
    try:
        with proc.stdout:
            for line in proc.stdout:
                lines.append(line)
                if stream:
                    shown = line_filter(line) if line_filter else line
                    if shown is not None:
                        sys.stdout.write(shown)
                        sys.stdout.flush()
        if wait_unreaped(proc):
            timer.exited()
        proc.wait()
    except BaseException:
        if proc.returncode is None:
            kill_group(proc)
        raise
    finally:
        timer.exited()
    out = "".join(lines)
    if timer.fired:
        out += "\ndev.py: killed after %s (--timeout)\n" % fmt_time(timeout)
        return 124, out, time.perf_counter() - t0
    return proc.returncode, out, time.perf_counter() - t0


def _with_env(extra, base=None):
    """os.environ (or base) with extra applied; PYTHONPATH is prepended, not replaced."""
    env = dict(base if base is not None else os.environ)
    for k, v in extra.items():
        env[k] = v + os.pathsep + env[k] if (k == "PYTHONPATH" and env.get(k)) else v
    return env


def _short(args):
    if len(args) <= SHOW_ARGS:
        return " ".join(shlex.quote(a) for a in args)
    return " ".join(shlex.quote(a) for a in args[:SHOW_ARGS]) + " ... (+%d more)" % (
        len(args) - SHOW_ARGS)


def _packages(files):
    """Go package directories (./dir) of files relative to the module root."""
    out = sorted(set("./" + os.path.dirname(f) if os.path.dirname(f) else "." for f in files))
    return out


def gate(name, lang, status, command=None, seconds=0.0, output=None, fix_hint=None,
         reason=None):
    """A gate record. head: the first HEAD_LINES output lines; tail: the last TAIL_LINES
    after them (empty when the output fits in head); omitted: lines in neither."""
    lines = (output or "").splitlines()
    head = lines[:HEAD_LINES]
    tail = lines[max(HEAD_LINES, len(lines) - TAIL_LINES):]
    return {"name": name, "language": lang, "command": command, "status": status,
            "seconds": round(seconds, 3), "head": head, "tail": tail,
            "omitted": len(lines) - len(head) - len(tail), "fix_hint": fix_hint,
            "reason": reason}


def fix_hint(name, st):
    if name == "tests":
        return None
    tool = st.get("tool") or ""
    return FIX_HINTS.get((name, tool)) or FIX_HINTS.get((name, None))


def execute(name, st, cwd, args=None, timeout=None, extra_env=None):
    """Run a resolved step. args: files or targets relative to cwd, or None for a whole
    project run. Returns a gate record."""
    lang = st["language"]
    hint = skip_hint(lang, st["step"])
    if st["status"] == "skipped":
        return gate(name, lang, "skipped", reason=st["reason"], fix_hint=hint)
    if st["status"] == "missing":
        return gate(name, lang, "skipped", command=st["command"], reason=st["reason"],
                    fix_hint="install it or " + hint)
    pieces = st["pieces"]
    if args is not None and any(p.get("unittest_files") for p in pieces):
        pieces = unittest_pieces(args, set(st.get("package_dirs") or ()))
        args = None
    if args is not None and lang != "rust":      # Rust args are `-p crate` flags
        args = [safe_path(a) for a in args]
    shown, output, total = [], [], 0.0
    for i, piece in enumerate(pieces):
        env = None
        if "shell" in piece:
            cmd = piece["shell"]
            ph = piece["placeholder"]
            if ph in cmd:
                cmd = cmd.replace(ph, " ".join(shlex.quote(a) for a in (args or [])))
            shown.append(cmd)
            rc, out, secs = run_process(cmd, cwd, shell=True, timeout=timeout)
        else:
            argv = list(piece["argv"])
            if piece.get("files") and args is not None:
                extra = _packages(args) if (st.get("packages_for") == i) else list(args)
                argv += extra
                shown.append(" ".join(shlex.quote(a) for a in piece["argv"]) + " " + _short(extra))
            else:
                shown.append(display_of([piece]))
            if piece.get("env"):
                env = _with_env(piece["env"])
            if argv[0] != PYTHON and shutil.which(argv[0]) is None:
                return gate(name, lang, "skipped", command=" && ".join(shown),
                            reason="%s not found on PATH" % argv[0],
                            fix_hint="install it or " + hint)
            if st.get("tool") == "compileall":
                env = dict(env or os.environ)
                env["PYTHONPYCACHEPREFIX"] = tempfile.mkdtemp(prefix="devkit-pycache-")
            if extra_env:
                env = dict(env or os.environ)
                env.update(extra_env)
            rc, out, secs = run_process(argv, cwd, env=env, timeout=timeout)
            if env and "PYTHONPYCACHEPREFIX" in env and st.get("tool") == "compileall":
                shutil.rmtree(env["PYTHONPYCACHEPREFIX"], ignore_errors=True)
        total += secs
        output.append(out)
        if rc is None:
            return gate(name, lang, "skipped", command=" && ".join(shown), seconds=total,
                        reason=out.strip(), fix_hint="install it or " + hint)
        failed = rc != 0 or (piece.get("fail_on_output") and out.strip())
        if failed:
            text = "".join(output)
            if piece.get("fail_on_output") and rc == 0:
                text = "files need formatting:\n" + text
            return gate(name, lang, "fail", command=" && ".join(shown), seconds=total,
                        output=text, fix_hint=fix_hint(name, st))
    return gate(name, lang, "pass", command=" && ".join(shown), seconds=total)


# ---------------------------------------------------------------- the check pipeline


def _lang_root(info, lang):
    return info["langs"].get(lang, {}).get("root", "")


def _files_for(lang, paths, info, cfg):
    lroot = _lang_root(info, lang)
    excluded = set(cfg["exclude_dirs"])
    out = []
    for p in paths:
        if language_of(p, [lang]) and (not lroot or p.startswith(lroot + "/")):
            if any(seg in excluded for seg in p.split("/")[:-1]):
                continue
            out.append(_rel(p, lroot))
    return out


def stage_files(root, info, cfg, changed):
    """{lang: files relative to the language root} for existing changed files."""
    existing = [p for s, p, _ in changed if s != "D" and os.path.isfile(os.path.join(root, p))]
    return dict((lang, _files_for(lang, existing, info, cfg)) for lang in info["languages"])


def run_stage(name, root, info, cfg, per_lang, timeout=None, lint_gates=None):
    """Gates for lint or typecheck over the languages that have files."""
    gates = []
    for lang in info["languages"]:
        files = per_lang.get(lang) or []
        if not files:
            continue
        st = resolve(info, lang, name, cfg)
        if st.get("covered"):
            lint = (lint_gates or {}).get(lang)
            ok = lint is not None and lint["status"] == "pass"
            gates.append(gate(name, lang, "pass" if ok else "skipped",
                              command="(%s)" % st["reason"],
                              reason=None if ok else "%s, which did not run" % st["reason"]))
            continue
        cwd = os.path.join(root, _lang_root(info, lang))
        gates.append(execute(name, st, cwd, files, timeout))
        if gates[-1]["status"] == "fail":
            break
    return gates


def test_gates(root, info, cfg, res, timeout=None):
    """Gates for the affected-test stage from an affected() result."""
    gates = []
    if res["mode"] == "full":
        plan = [(lang, resolve(info, lang, "test", cfg), None) for lang in info["languages"]]
    else:
        plan = []
        for lang in [l for l in LANG_ORDER if l in res["targets"]]:
            lroot = _lang_root(info, lang)
            if lang == "rust":
                args = list(res["args"][lang])
            elif lang == "go":
                args = list(res["targets"][lang])
            else:
                args = [_rel(t, lroot) for t in res["targets"][lang]]
            plan.append((lang, resolve(info, lang, "test_targets", cfg), args))
    for lang, st, args in plan:
        g = execute("tests", st, os.path.join(root, _lang_root(info, lang)), args, timeout)
        if g["status"] == "fail":
            g["fix_hint"] = "rerun the targets alone: %s" % g["command"]
        gates.append(g)
        if g["status"] == "fail":
            break
    return gates


def map_gate(root):
    """The map-drift gate when .devkit/repo-map.json exists, else None (with a note)."""
    if not os.path.isfile(os.path.join(devkit_dir(root), "repo-map.json")):
        return None, None
    try:
        repomap = importlib.import_module("devkit.repomap")
    except ImportError:
        return None, "map drift not checked: this dev.py has no map command"
    drift = getattr(repomap, "drift", None)
    if drift is None:
        return None, "map drift not checked: devkit.repomap has no drift()"
    t0 = time.perf_counter()
    changed = drift(root)
    secs = time.perf_counter() - t0
    if changed:
        return gate("map", None, "fail", command="dev.py map --check", seconds=secs,
                    output="\n".join("changed: %s" % p for p in changed),
                    fix_hint="run `dev.py map` to refresh .devkit/repo-map.json"), None
    return gate("map", None, "pass", command="dev.py map --check", seconds=secs), None


UNSUPPORTED = ("no language dev.py supports was detected (Python, TypeScript/JavaScript, Go, "
               "Rust), so no gate can run; use the repository's own build and test commands")


def executed(gates):
    """Gates that ran a command (a pass or fail), not skipped or covered by another gate."""
    return [g for g in gates if g["status"] in ("pass", "fail")
            and not str(g.get("command") or "").startswith("(")]


def run_check(root, changed, cfg=None, info=None, strict=False, no_tests=False, timeout=None):
    cfg = cfg if cfg is not None else load_config(root)
    info = info if info is not None else detect(root, cfg)
    notes = list(info["notes"])
    gates = []
    result = {"schema": "devkit-check/1", "status": "pass", "failed_gate": None,
              "files": [p for _, p, _ in changed], "languages": info["languages"],
              "gates": gates, "affected": None, "notes": notes, "strict": strict}
    if not changed:
        notes.append("no changed files")
    per_lang = stage_files(root, info, cfg, changed)

    def failed():
        return any(g["status"] == "fail" for g in gates)

    lint = run_stage("lint", root, info, cfg, per_lang, timeout)
    gates.extend(lint)
    if not failed():
        lint_by_lang = dict((g["language"], g) for g in lint)
        gates.extend(run_stage("typecheck", root, info, cfg, per_lang, timeout, lint_by_lang))
    if not failed() and changed:
        if no_tests:
            notes.append("tests not run (--no-tests)")
        else:
            res = affected(root, changed, cfg, info)
            notes.extend(res["notes"])
            result["affected"] = {"mode": res["mode"], "reason": res["reason"],
                                  "targets": res["targets"]}
            if res["mode"] == "none":
                notes.append("tests: none affected (%s)" % res["reason"])
            else:
                tg = test_gates(root, info, cfg, res, timeout)
                if res["mode"] == "full":
                    notes.append(("tests: full run (%s)" if tg else
                                  "tests: a full run is required (%s), but no detected "
                                  "language can run it") % res["reason"])
                gates.extend(tg)
    if not failed():
        g, n = map_gate(root)
        if g:
            gates.append(g)
        if n:
            notes.append(n)
    bad = [g for g in gates if g["status"] == "fail"]
    skipped = [g for g in gates if g["status"] == "skipped"]
    relevant = [p for _, p, _ in changed
                if not first_match(cfg["ignore"], p) or is_test_data(p)]
    if relevant and not info["languages"]:
        notes.append(UNSUPPORTED)
    result["executed"] = len(executed(gates))
    if bad:
        result.update(status="fail", failed_gate=gate_label(bad[0]))
    elif relevant and not result["executed"]:
        result.update(status="fail", failed_gate="nothing was checked: %d relevant changed "
                      "file(s) and no gate ran" % len(relevant))
    elif strict and skipped:
        result.update(status="fail", failed_gate=gate_label(skipped[0]) + " (skipped, --strict)")
    elif strict and not changed:
        result.update(status="fail", failed_gate="no changed files (--strict)")
    result["skipped"] = len(skipped)
    return result


def gate_label(g):
    return "%s (%s)" % (g["name"], g["language"]) if g["language"] else g["name"]


def print_gates(gates):
    for g in gates:
        label = gate_label(g)
        if g["status"] == "skipped":
            print("%-22s skipped  %s%s" % (label, g["reason"],
                                           "; " + g["fix_hint"] if g["fix_hint"] else ""))
            continue
        print("%-22s %-7s  %8s  %s" % (label, g["status"], fmt_time(g["seconds"]), g["command"]))
        if g["status"] == "fail":
            for line in g["head"]:
                print("    " + line)
            if g["omitted"]:
                print("    ... (%d line(s) omitted)" % g["omitted"])
            for line in g["tail"]:
                print("    " + line)
            if g["fix_hint"]:
                print("    fix: " + g["fix_hint"])


def _common_args(p, files_help):
    p.add_argument("files", nargs="*", help=files_help)
    p.add_argument("--base", default=None, help="changed files from REF...HEAD plus the working tree")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--strict", action="store_true", help="a skipped gate fails")
    p.add_argument("--timeout", type=float, default=None, help="seconds per command before it is killed")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def _setup(a):
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    if a.files and a.base:
        raise UsageError("pass FILES or --base, not both")
    return root


def cmd_check(args):
    p = Parser(prog="dev.py check",
               description="Fail-fast lint, typecheck and affected tests on the changed files.")
    _common_args(p, "files to check (default: git diff against HEAD plus untracked files)")
    p.add_argument("--no-tests", action="store_true", help="stop after lint and typecheck")
    a = p.parse_args(args)
    root = _setup(a)
    changed = gather_changed(root, a.files, a.base)
    warn_if_empty(changed, a.files, a.base)
    res = run_check(root, changed, strict=a.strict, no_tests=a.no_tests, timeout=a.timeout)
    if a.json:
        print_json(res)
    else:
        print_gates(res["gates"])
        for n in res["notes"]:
            print("note: " + n)
        if res["status"] == "pass":
            tests_ran = any(g["name"] == "tests" for g in executed(res["gates"]))
            extra = ""
            if res["skipped"]:
                extra = "; --strict fails on a skipped gate"
            aff = res["affected"]
            if not tests_ran and aff and aff["mode"] != "none":
                extra += "; no tests ran"
            print("check: PASS (%d file(s), %d gate(s) ran, %d skipped%s)" % (
                len(res["files"]), res["executed"], res["skipped"], extra))
        else:
            print("check: FAIL at %s" % res["failed_gate"])
    return 0 if res["status"] == "pass" else 1


def _single(name, args):
    p = Parser(prog="dev.py %s" % name, description="%s the changed (or given) files." % name)
    _common_args(p, "files (default: git diff against HEAD plus untracked files)")
    p.add_argument("--all", action="store_true", help="every file of each detected language")
    a = p.parse_args(args)
    root = _setup(a)
    cfg = load_config(root)
    info = detect(root, cfg)
    if a.all:
        if a.files or a.base:
            raise UsageError("--all takes no FILES or --base")
        changed = [("M", f, None) for f in info["files"]]
    else:
        changed = gather_changed(root, a.files, a.base)
        warn_if_empty(changed, a.files, a.base)
    per_lang = stage_files(root, info, cfg, changed)
    notes = []
    if not info["languages"] and changed:
        notes.append(UNSUPPORTED)
    if name == "typecheck":
        for lang in list(per_lang):
            st = resolve(info, lang, name, cfg)
            if st.get("covered") and per_lang[lang]:
                notes.append("%s: %s; run dev.py lint" % (lang, st["reason"]))
                per_lang[lang] = []
    gates = run_stage(name, root, info, cfg, per_lang, a.timeout)
    bad = [g for g in gates if g["status"] == "fail"]
    skipped = [g for g in gates if g["status"] == "skipped"]
    status = "fail" if bad or (a.strict and skipped) else "pass"
    if not info["languages"] and changed:
        status = "fail"
    if a.json:
        print_json({"schema": "devkit-%s/1" % name, "status": status, "gates": gates,
                    "notes": notes, "files": [p for _, p, _ in changed] if not a.all else None})
    else:
        if not gates and not notes:
            print("%s: no changed files of a detected language" % name)
        print_gates(gates)
        for n in notes:
            print("note: " + n)
    return 1 if status == "fail" else 0


def cmd_lint(args):
    return _single("lint", args)


def cmd_typecheck(args):
    return _single("typecheck", args)


# ---------------------------------------------------------------- test wrapper and reports

# Runs unittest like `python -m unittest` and writes one JSON record per test to argv[1]:
# `-c CODE OUT discover START PATTERN` or `-c CODE OUT targets PATH...`.
UNITTEST_REPORTER = r'''
import json, os, re, sys, time, unittest
out, mode, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
records = []

def file_of(test):
    mod = sys.modules.get(type(test).__module__)
    f = getattr(mod, "__file__", None)
    return os.path.relpath(f).replace(os.sep, "/") if f else None

class Result(unittest.TextTestResult):
    def startTest(self, test):
        self._t0, self._outcome = time.perf_counter(), "passed"
        unittest.TextTestResult.startTest(self, test)
    def _mark(self, test, outcome):
        if not isinstance(test, unittest.TestCase) or getattr(self, "_t0", None) is None:
            name = getattr(test, "description", str(test))
            m = re.search(r"\(([\w.]+)\)", name)
            parts, f = (m.group(1).split(".") if m else []), None
            for i in range(len(parts), 0, -1):
                mf = getattr(sys.modules.get(".".join(parts[:i])), "__file__", None)
                if mf:
                    f = os.path.relpath(mf).replace(os.sep, "/")
                    break
            records.append({"id": name, "file": f, "outcome": outcome, "duration": 0.0})
        elif outcome == "error" or self._outcome == "passed" or self._outcome == "skipped":
            self._outcome = outcome
    def addFailure(self, t, e): unittest.TextTestResult.addFailure(self, t, e); self._mark(t, "failed")
    def addError(self, t, e): unittest.TextTestResult.addError(self, t, e); self._mark(t, "error")
    def addSkip(self, t, r): unittest.TextTestResult.addSkip(self, t, r); self._mark(t, "skipped")
    def addUnexpectedSuccess(self, t): unittest.TextTestResult.addUnexpectedSuccess(self, t); self._mark(t, "failed")
    def addSubTest(self, t, s, e):
        unittest.TextTestResult.addSubTest(self, t, s, e)
        if e is not None:
            self._mark(t, "failed" if issubclass(e[0], t.failureException) else "error")
    def stopTest(self, test):
        unittest.TextTestResult.stopTest(self, test)
        if getattr(self, "_t0", None) is not None:
            records.append({"id": test.id(), "module": type(test).__module__,
                            "file": file_of(test), "outcome": self._outcome,
                            "duration": time.perf_counter() - self._t0})
        self._t0 = None

loader = unittest.TestLoader()
if mode == "discover":
    suite = loader.discover(rest[0], pattern=rest[1])
else:
    names = []
    for a in rest:
        if a.endswith(".py") and os.path.isfile(a):
            a = os.path.relpath(a)[:-3].replace(os.sep, ".")
        names.append(a)
    suite = loader.loadTestsFromNames(names)
res = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, resultclass=Result).run(suite)
with open(out, "w") as f:
    json.dump(records, f)
sys.exit(0 if res.wasSuccessful() else 1)
'''


# Runs the reporter (argv[1]) once per group in argv[3], a JSON list of [pythonpath or null,
# test file...], each in its own process, and merges the records into argv[2]; exits
# non-zero when any group failed.
UNITTEST_GROUPS = r'''
import json, os, subprocess, sys
reporter, out, groups = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
records, rc = [], 0
for i, group in enumerate(groups):
    part = "%s.%d" % (out, i)
    env = dict(os.environ)
    if group[0]:
        env["PYTHONPATH"] = os.pathsep.join(p for p in (group[0], env.get("PYTHONPATH")) if p)
    p = subprocess.run([sys.executable, "-c", reporter, part, "targets"] + group[1:], env=env)
    rc = rc or p.returncode
    try:
        with open(part) as f:
            records.extend(json.load(f))
    except (OSError, ValueError):
        rc = rc or 1
with open(out, "w") as f:
    json.dump(records, f)
sys.exit(rc)
'''


def unittest_groups(st):
    """[pythonpath or None, file...] per piece of a unittest step that lists its files, else
    None (a single discover run)."""
    out = []
    for piece in st["pieces"]:
        argv = piece.get("argv") or []
        if argv[:3] != [PYTHON, "-m", "unittest"] or "discover" in argv or len(argv) < 4:
            return None
        out.append([(piece.get("env") or {}).get("PYTHONPATH")] + argv[3:])
    return out


def _outcome_js(status):
    return {"passed": "passed", "failed": "failed"}.get(status, "skipped")


def parse_unittest(path, lroot):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for r in data:
        f = r.get("file")
        target = _join(lroot, f) if f else "unknown"
        rid = r.get("id", "")
        mod = r.get("module")
        rest = rid[len(mod) + 1:] if mod and rid.startswith(mod + ".") else rid
        out.append({"nodeid": "%s::%s" % (target, rest.replace(".", "::")), "target": target,
                    "outcome": r.get("outcome", "error"),
                    "duration_seconds": round(float(r.get("duration") or 0.0), 6)})
    return out


def _join(lroot, rel):
    rel = to_posix(rel)
    return "%s/%s" % (lroot, rel) if lroot else rel


def _junit_file(case, cwd):
    f = case.get("file")
    if f:
        return to_posix(f)
    parts = (case.get("classname") or "").split(".")
    for i in range(len(parts), 0, -1):
        cand = "/".join(parts[:i]) + ".py"
        if os.path.isfile(os.path.join(cwd, cand)):
            return cand
    return None


def parse_junit(path, cwd, lroot):
    out = []
    for case in ET.parse(path).getroot().iter("testcase"):
        f = _junit_file(case, cwd)
        target = _join(lroot, f) if f else "unknown"
        cls = case.get("classname") or ""
        mod = f[:-3].replace("/", ".") if f else ""
        rest = cls[len(mod) + 1:] if mod and cls.startswith(mod + ".") else (
            "" if cls == mod else cls)
        outcome = "passed"
        for child in case:
            tag = child.tag
            if tag == "failure":
                outcome = "failed"
            elif tag == "error":
                outcome = "error"
            elif tag == "skipped" and outcome == "passed":
                outcome = "skipped"
        nodeid = target + ("::" + rest.replace(".", "::") if rest else "") + "::" + (
            case.get("name") or "")
        out.append({"nodeid": nodeid, "target": target, "outcome": outcome,
                    "duration_seconds": round(float(case.get("time") or 0.0), 6)})
    return out


def parse_js_json(path, cwd, lroot):
    """vitest --reporter=json and jest --json share the testResults/assertionResults shape."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for suite in data.get("testResults") or []:
        name = suite.get("name") or suite.get("testFilePath") or ""
        rel = to_posix(os.path.relpath(name, cwd)) if os.path.isabs(name) else to_posix(name)
        target = _join(lroot, rel)
        cases = suite.get("assertionResults") or []
        for c in cases:
            title = " > ".join(list(c.get("ancestorTitles") or []) + [c.get("title") or ""])
            out.append({"nodeid": "%s::%s" % (target, title), "target": target,
                        "outcome": _outcome_js(c.get("status")),
                        "duration_seconds": round(float(c.get("duration") or 0.0) / 1000.0, 6)})
        if not cases and suite.get("status") == "failed":
            start = suite.get("startTime") or (suite.get("perfStats") or {}).get("start") or 0
            end = suite.get("endTime") or (suite.get("perfStats") or {}).get("end") or start
            out.append({"nodeid": target, "target": target, "outcome": "error",
                        "duration_seconds": round(max(0.0, (end - start) / 1000.0), 6)})
    return out


def parse_go_json(text, module, go_root):
    """Records from `go test -json` output; subtests are folded into their parent."""
    out, failed_pkgs, tested = [], [], set()

    def target_of(pkg):
        if module and (pkg == module or pkg.startswith(module + "/")):
            rel = pkg[len(module):].lstrip("/")
            return "./" + rel if rel else "."
        return pkg

    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        action, pkg, test = ev.get("Action"), ev.get("Package") or "", ev.get("Test")
        if action not in ("pass", "fail", "skip"):
            continue
        if test:
            if "/" in test:
                continue
            tested.add(pkg)
            t = target_of(pkg)
            out.append({"nodeid": "%s::%s" % (t, test), "target": t,
                        "outcome": {"pass": "passed", "fail": "failed"}.get(action, "skipped"),
                        "duration_seconds": round(float(ev.get("Elapsed") or 0.0), 6)})
        elif action == "fail":
            failed_pkgs.append((pkg, float(ev.get("Elapsed") or 0.0)))
    for pkg, secs in failed_pkgs:
        if pkg not in tested:
            t = target_of(pkg)
            out.append({"nodeid": t, "target": t, "outcome": "error",
                        "duration_seconds": round(secs, 6)})
    return out


def _go_filter(line):
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    if isinstance(ev, dict) and ev.get("Action") == "output":
        return ev.get("Output")
    return None


def full_argv(st, extra_args):
    """The full-suite argv with runner arguments: unittest and go replace their default
    target (discover, ./...); `npm run` needs `--` before script arguments."""
    argv = list(st["pieces"][0]["argv"])
    if not extra_args:
        return argv
    if st.get("runner") == "unittest":
        return [PYTHON, "-m", "unittest"] + extra_args
    if st.get("runner") == "go":
        return ["go", "test"] + extra_args
    if argv[:2] == ["npm", "run"]:
        return argv + ["--"] + extra_args
    return argv + extra_args


def record_plan(st, info, lang, tmp, extra_args):
    """(argv or shell text, shell flag, report kind, report path, line filter, note) for a
    full-suite run that writes a report, from the resolved test step."""
    report = os.path.join(tmp, "report")
    facts = info["langs"].get(lang, {})
    if st["source"] == "config":
        cmd = st["pieces"][0]["shell"].replace("{targets}", " ".join(
            shlex.quote(a) for a in extra_args))
        kind = st.get("report")
        if kind and "{report}" in cmd:
            return cmd.replace("{report}", shlex.quote(report)), True, kind, report, None, None
        return cmd, True, None, None, None, None
    runner = st.get("runner")
    if runner == "pytest":
        argv = st["pieces"][0]["argv"] + ["--junitxml=" + report, "-o", "junit_family=xunit1"]
        return argv + extra_args, False, "junit", report, None, None
    if runner == "unittest":
        groups = None if extra_args else unittest_groups(st)
        if extra_args:
            argv = [PYTHON, "-c", UNITTEST_REPORTER, report, "targets"] + extra_args
        elif groups:        # test files in several directories (detect.unittest_pieces)
            argv = [PYTHON, "-c", UNITTEST_GROUPS, UNITTEST_REPORTER, report, json.dumps(groups)]
        else:
            argv = [PYTHON, "-c", UNITTEST_REPORTER, report, "discover",
                    facts.get("tests_dir", "."), "test*.py"]
        return argv, False, "unittest", report, None, None
    if runner in ("vitest", "jest"):
        pm = facts.get("package_manager") or "npm"
        script = (facts.get("scripts") or {}).get("test")
        if st["pieces"][0]["argv"][1:2] == ["run"] and isinstance(script, str):
            try:
                words = shlex.split(script)
            except ValueError:
                words = None
            if words not in ([runner], [runner, "run"]):
                msg = ("per-test recording needs a bare `%s` test script; `%s` sets flags, "
                       "config or environment that a direct %s call would drop, so the script "
                       "runs as written and only its wall time is recorded" % (
                           runner, script, runner))
                return full_argv(st, extra_args), False, None, None, None, msg
        if runner == "vitest":
            argv = js_exec(pm, "vitest") + ["run", "--reporter=json", "--outputFile=" + report]
        else:
            argv = js_exec(pm, "jest") + ["--json", "--outputFile=" + report]
        msg = None
        if st["pieces"][0]["argv"][1:2] == ["run"]:
            msg = "--record runs %s directly (not the test script) so it can write its report" % runner
        return argv + extra_args, False, runner, report, None, msg
    if runner == "go":
        argv = ["go", "test", "-json"] + (extra_args or ["./..."])
        return argv, False, "go", None, _go_filter, None
    return full_argv(st, extra_args), False, None, None, None, None


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_records(root, lang, records, now=None):
    """Write records to .devkit/runs/<utc stamp>-<lang>.jsonl; returns the relative path."""
    runs = os.path.join(devkit_dir(root, create=True), "runs")
    if not os.path.isdir(runs):
        os.makedirs(runs)
    stamp = now or _stamp()
    run_id = "%s-%s" % (stamp, lang)
    n = 2
    while os.path.exists(os.path.join(runs, run_id + ".jsonl")):
        run_id = "%s-%s-%d" % (stamp, lang, n)
        n += 1
    ts = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")
    path = os.path.join(runs, run_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps({"run_id": run_id, "nodeid": r["nodeid"], "target": r["target"],
                                "outcome": r["outcome"],
                                "duration_seconds": r["duration_seconds"],
                                "timestamp": ts}) + "\n")
    return to_posix(os.path.relpath(path, root))


def run_tests(root, info, cfg, lang, record, extra_args, stream=True, timeout=None):
    """One language's full suite. Returns a result dict for the test command."""
    st = resolve(info, lang, "test", cfg)
    lroot = _lang_root(info, lang)
    cwd = os.path.join(root, lroot)
    res = {"language": lang, "command": st["command"], "status": None, "seconds": 0.0,
           "records": 0, "run_file": None, "outcomes": {}, "reason": None, "notes": []}
    if st["status"] in ("skipped", "missing"):
        res.update(status="skipped", reason="%s; %s" % (st["reason"], skip_hint(lang, "test")))
        return res
    tmp = tempfile.mkdtemp(prefix="devkit-test-")
    try:
        grouped = not extra_args and st.get("runner") == "unittest" and bool(unittest_groups(st))
        if record or grouped:       # several unittest runs: the group driver runs them all
            cmd, shell, kind, report, filt, msg = record_plan(st, info, lang, tmp, extra_args)
            if msg and stream:              # before the run, so the header is not misread
                sys.stdout.write("note: %s\n" % msg)
                sys.stdout.flush()
            elif msg:
                res["notes"].append(msg)
        else:
            kind = report = filt = None
            if st["source"] == "config":
                cmd, shell = st["pieces"][0]["shell"].replace("{targets}", " ".join(
                    shlex.quote(a) for a in extra_args)), True
            else:
                cmd, shell = full_argv(st, extra_args), False
        shown = cmd if shell else " ".join(shlex.quote(a) for a in cmd)
        if st.get("runner") == "unittest" and not shell and cmd[1:2] == ["-c"]:
            shown = "%s (unittest with the devkit timing reporter)" % (
                st["command"] if not extra_args else "python3 -m unittest " + _short(extra_args))
        res["command"] = shown
        if not shell and cmd[0] != PYTHON and shutil.which(cmd[0]) is None:
            res.update(status="skipped", reason="%s not found on PATH; install it or %s"
                       % (cmd[0], skip_hint(lang, "test")))
            return res
        rc, out, secs = run_process(cmd, cwd, shell=shell, stream=stream, line_filter=filt,
                                    timeout=timeout)
        res["seconds"] = round(secs, 3)
        if rc is None:
            res.update(status="skipped", reason=out.strip())
            return res
        res["status"] = "pass" if rc == 0 else "fail"
        if not stream:
            res["head"] = out.splitlines()[:HEAD_LINES]
            res["tail"] = out.splitlines()[HEAD_LINES:][-TAIL_LINES:]
        if not record:
            return res
        records = []
        try:
            if kind == "junit" and report and os.path.isfile(report):
                records = parse_junit(report, cwd, lroot)
            elif kind == "unittest" and report and os.path.isfile(report):
                records = parse_unittest(report, lroot)
            elif kind in ("vitest", "jest") and report and os.path.isfile(report):
                records = parse_js_json(report, cwd, lroot)
            elif kind == "go":
                records = parse_go_json(out, info["langs"][lang].get("module"), lroot)
        except (ValueError, OSError, ET.ParseError) as e:
            res["notes"].append("report unreadable (%s); recorded the run as one target" % e)
            records = []
        if not records:
            if kind:
                res["notes"].append("no per-test report; recorded the run as one target")
            records = [{"nodeid": st["command"], "target": st["command"],
                        "outcome": "passed" if rc == 0 else "failed",
                        "duration_seconds": round(secs, 6)}]
        res["run_file"] = write_records(root, lang, records)
        res["records"] = len(records)
        for r in records:
            res["outcomes"][r["outcome"]] = res["outcomes"].get(r["outcome"], 0) + 1
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def has_report(st):
    if st["status"] in ("skipped", "missing"):
        return False
    if st["source"] == "config":
        return bool(st.get("report")) and "{report}" in st["pieces"][0]["shell"]
    return st.get("runner") in ("pytest", "unittest", "vitest", "jest", "go")


def cmd_test(args):
    p = Parser(prog="dev.py test",
               description="Run the full test suite per detected language; record per-test "
                           "outcomes and durations under .devkit/runs/.")
    p.add_argument("args", nargs="*", help="runner arguments after `--` (one language only), "
                                           "for example test files")
    p.add_argument("--lang", default=None, help="only this language")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--record", dest="record", action="store_true", default=None,
                   help="write .devkit/runs/<utc>-<lang>.jsonl (default when the runner has "
                        "a report format)")
    g.add_argument("--no-record", dest="record", action="store_false", help="do not record")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--strict", action="store_true", help="a skipped language fails")
    p.add_argument("--timeout", type=float, default=None, help="seconds before the run is killed")
    p.add_argument("--json", action="store_true", help="print one JSON object (runner output "
                                                       "is not streamed)")
    a = p.parse_args(args)
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    cfg = load_config(root)
    info = detect(root, cfg)
    langs = info["languages"]
    if a.lang:
        if a.lang not in langs:
            raise UsageError("--lang %s is not a detected language (%s)" % (
                a.lang, ", ".join(langs) or "none"))
        langs = [a.lang]
    if a.args and len(langs) != 1:
        raise UsageError("runner arguments need one language; pass --lang")
    if not langs:
        raise UsageError("no language detected; set languages in tools/dev.json")
    results = []
    for lang in langs:
        st = resolve(info, lang, "test", cfg)
        record = a.record if a.record is not None else has_report(st)
        if not a.json:
            sys.stdout.write("test (%s): %s\n" % (lang, st["command"] or st["reason"]))
            sys.stdout.flush()
        results.append(run_tests(root, info, cfg, lang, record, list(a.args),
                                 stream=not a.json, timeout=a.timeout))
    bad = [r for r in results if r["status"] == "fail"]
    skipped = [r for r in results if r["status"] == "skipped"]
    status = "fail" if bad or (a.strict and skipped) else "pass"
    if a.json:
        print_json({"schema": "devkit-test/1", "status": status, "results": results})
    else:
        for r in results:
            if r["status"] == "skipped":
                print("test (%s): skipped: %s" % (r["language"], r["reason"]))
                continue
            line = "test (%s): %s in %s" % (r["language"], r["status"].upper(),
                                            fmt_time(r["seconds"]))
            if r["run_file"]:
                counts = ", ".join("%d %s" % (v, k) for k, v in sorted(r["outcomes"].items()))
                line += "; recorded %s (%d record(s): %s)" % (r["run_file"], r["records"], counts)
            print(line)
            for n in r["notes"]:
                print("note: " + n)
    return 1 if status == "fail" else 0
