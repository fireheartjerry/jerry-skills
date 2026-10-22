#!/usr/bin/env python3
"""Run evidence checks once, log them, and build the reviewer's view.

usage: evidence.py run  --dir DIR --check ID [--retake REASON] [--cwd DIR] [--collect GLOB ...]
                        [--timeout S] [--serve CMD [--serve-cwd DIR]] [--wait-for URL]
                        [--wait-timeout S] [--snapshot SNAP] -- CMD [ARG ...]
       evidence.py view --dir DIR --out DIR

run executes CMD once (argv as given; pass "bash -c '...'" for a pipeline), with stdin
from /dev/null, and writes its stdout and stderr to DIR/<ID>-<attempt>.log. The working
directory is DIR itself unless --cwd says otherwise, so a capture the command writes
by relative name (home-390.png, axe.json) lands in DIR, never in the work tree. A
command that must run elsewhere (npm run build in the repo, a helper in LOOP/tools)
takes --cwd, and --collect GLOB (repeatable, relative to --cwd) moves every matching
file the run created into DIR; a matching file that existed before the run and changed
is copied instead and named on stderr. Every file the run left in DIR is listed in the
manifest row.

It appends a row to DIR/manifest.md: check, command, UTC time, attempt, and a note with
the exit status, the files produced, the snapshot digest and the retake reason. A
second run of the same check is refused (exit 2) unless --retake REASON says why: a
retake is allowed only when the tool failed before it measured (crash, server down,
timeout), never because the number disappointed. Every attempt stays in the manifest;
the last complete one counts. The command's own exit status is recorded, not
propagated: a failing gate is evidence, not an error.

--timeout S (default 600, 0 disables) kills the command's whole process group after S
seconds and records "timed out after Ss" (status 124); that attempt may be retaken.
--serve CMD starts a server first (via /bin/sh -c, in --serve-cwd, default the current
directory, in its own process group, output to DIR/<ID>-<attempt>.serve.log), and
--wait-for URL polls until the URL answers any HTTP status (at most --wait-timeout S,
default 60); --wait-for also works alone for a server you started. The server is
stopped when the check ends, whatever happens. A server that exits or never answers
is a tool failure: nothing is recorded, exit 3. --snapshot SNAP compares the files in
ROOT with the snapshot SNAP (its manifest's root and paths) before running and refuses
(exit 2) when they differ, so the evidence describes the snapshot the scorer reads;
the row then carries the first 12 hex digits of the snapshot manifest's sha256.

Exit 2 on bad arguments or a tree that differs from --snapshot, 3 when CMD or the
server cannot be started or the server never answers.

view copies DIR to a fresh --out directory for a reviewer: everything except
manifest.md, with any file named after a check id (C3-1.log, G2-1.log) renamed to a
neutral capture-<n> name, so a blind reviewer is not told what is measured. Exit 2
when --out exists and is not empty.
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

MANIFEST = "manifest.md"
HEADER = ("| Check | Command | Timestamp (UTC) | Attempts | Note |\n"
          "|-------|---------|-----------------|----------|------|\n")
CHECK_ID = re.compile(r"^[A-Z]\d+$")
ID_NAMED = re.compile(r"^[CG]\d+[-_.]")
DEFAULT_TIMEOUT = 600
DEFAULT_WAIT = 60


def die(message, code=2):
    sys.stderr.write("evidence: %s\n" % message)
    sys.exit(code)


def manifest_rows(directory):
    path = os.path.join(directory, MANIFEST)
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4 and CHECK_ID.match(cells[0]):
                rows.append(cells)
    return rows


def load_snapshot_module():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import snapshot  # noqa: E402
    return snapshot


def snapshot_digest(snap_dir):
    """Compare ROOT with the snapshot; return the manifest digest or die naming the differences."""
    snapshot = load_snapshot_module()
    snap_dir = os.path.abspath(snap_dir)
    manifest_path = snapshot.manifest_path_for(snap_dir)
    if not os.path.isfile(manifest_path):
        die("snapshot manifest not found: %s" % manifest_path)
    with open(manifest_path, "rb") as handle:
        raw = handle.read()
    manifest = json.loads(raw.decode("utf-8"))
    root = manifest.get("root") or ""
    if not os.path.isdir(root):
        die("the snapshot's root no longer exists: %s" % root)
    expected = manifest.get("files", {})
    paths = snapshot.normalise_paths(manifest.get("paths", []))
    current = snapshot.file_list(root, paths, manifest.get("excludes", []), report_symlinks=False,
                                 include_globs=manifest.get("includes", []))
    problems = []
    for rel in sorted(expected):
        full = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(full):
            problems.append("missing: %s" % rel)
        elif snapshot.sha256_of(full) != expected[rel]["sha256"]:
            problems.append("changed: %s" % rel)
    for rel in current:
        if rel not in expected:
            problems.append("added: %s" % rel)
    if problems:
        shown = "; ".join(problems[:10]) + ("; ..." if len(problems) > 10 else "")
        die("the files in %s differ from the snapshot %s (%s). Evidence must describe the snapshot the "
            "scorer reads: take the snapshot again, or revert the edit, then run the check" % (root, snap_dir, shown))
    return hashlib.sha256(raw).hexdigest()[:12]


def stop_group(proc, grace=5):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()


def wait_for(url, limit, server=None):
    """True once URL answers any HTTP status within LIMIT seconds; False otherwise."""
    deadline = time.time() + limit
    while time.time() < deadline:
        if server is not None and server.poll() is not None:
            return False
        try:
            urlopen(url, timeout=2).close()
            return True
        except HTTPError:
            return True
        except (URLError, OSError, ValueError):
            time.sleep(0.5)
    return False


def file_state(base, patterns):
    """{relative path: (mtime_ns, size)} for files under BASE matching any of PATTERNS."""
    found = {}
    for pattern in patterns:
        for path in glob.glob(os.path.join(base, pattern), recursive=True):
            if os.path.isfile(path) and not os.path.islink(path):
                stat = os.stat(path)
                found[os.path.relpath(path, base)] = (stat.st_mtime_ns, stat.st_size)
    return found


def dir_files(directory):
    found = {}
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for name in filenames:
            path = os.path.join(dirpath, name)
            stat = os.stat(path)
            found[os.path.relpath(path, directory)] = (stat.st_mtime_ns, stat.st_size)
    return found


def cmd_run(args):
    if not CHECK_ID.match(args.check):
        die("--check takes an id such as G1 or C4, got %r" % args.check)
    argv = list(args.command)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        die("no command given after --")
    if args.timeout < 0 or args.wait_timeout <= 0:
        die("--timeout must be 0 or more and --wait-timeout positive")
    if args.serve_cwd and not args.serve:
        die("--serve-cwd needs --serve")
    directory = os.path.abspath(args.dir)
    cwd = os.path.abspath(args.cwd) if args.cwd else directory
    if args.cwd and not os.path.isdir(cwd):
        die("--cwd not found: %s" % args.cwd)
    if args.collect and cwd == directory:
        die("--collect moves files from --cwd into --dir; without --cwd the command already runs in --dir")
    os.makedirs(directory, exist_ok=True)
    attempts = [r for r in manifest_rows(directory) if r[0] == args.check]
    if attempts and not args.retake:
        die("%s was already captured this round (%d attempt%s). Evidence runs once; a retake needs "
            "--retake REASON and is allowed only when the tool failed before it measured"
            % (args.check, len(attempts), "" if len(attempts) == 1 else "s"))
    digest = snapshot_digest(args.snapshot) if args.snapshot else None
    attempt = len(attempts) + 1
    log_name = "%s-%d.log" % (args.check, attempt)
    serve_name = "%s-%d.serve.log" % (args.check, attempt)
    log_path = os.path.join(directory, log_name)
    collect_before = file_state(cwd, args.collect) if args.collect else {}
    dir_before = dir_files(directory)
    server = None
    serve_tmp = None
    status = None
    timed_out = False
    stamp = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if args.serve:
            serve_cwd = os.path.abspath(args.serve_cwd) if args.serve_cwd else os.getcwd()
            handle, serve_tmp = tempfile.mkstemp(prefix="evidence-serve.", suffix=".log")
            os.close(handle)
            with open(serve_tmp, "wb") as serve_log, open(os.devnull, "rb") as devnull:
                try:
                    server = subprocess.Popen(["/bin/sh", "-c", args.serve], cwd=serve_cwd, stdin=devnull,
                                              stdout=serve_log, stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as error:
                    die("cannot start the server (%s)" % error, 3)
        if args.wait_for and not wait_for(args.wait_for, args.wait_timeout, server):
            tail = ""
            if serve_tmp:
                with open(serve_tmp, "r", encoding="utf-8", errors="replace") as handle:
                    tail = "".join(handle.readlines()[-10:])
            reason = "the server exited" if server is not None and server.poll() is not None else \
                "no answer within %ds" % args.wait_timeout
            die("%s did not come up (%s); nothing recorded. Server output:\n%s" % (args.wait_for, reason, tail), 3)
        try:
            with open(log_path, "wb") as log, open(os.devnull, "rb") as devnull:
                proc = subprocess.Popen(argv, stdin=devnull, stdout=log, stderr=subprocess.STDOUT, cwd=cwd,
                                        start_new_session=True)
                try:
                    status = proc.wait(timeout=args.timeout if args.timeout > 0 else None)
                except subprocess.TimeoutExpired:
                    stop_group(proc)
                    timed_out = True
                    status = 124
                except KeyboardInterrupt:
                    stop_group(proc)
                    raise
        except OSError as error:
            if os.path.exists(log_path):
                os.remove(log_path)
            die("cannot run %s (%s)" % (argv[0], error), 3)
    finally:
        if server is not None:
            stop_group(server)
        if serve_tmp and os.path.exists(serve_tmp):
            if status is not None:
                shutil.move(serve_tmp, os.path.join(directory, serve_name))
            else:
                os.remove(serve_tmp)
    collected = []
    for rel, state in sorted(file_state(cwd, args.collect).items()) if args.collect else []:
        if collect_before.get(rel) == state:
            continue
        source = os.path.join(cwd, rel)
        target = os.path.join(directory, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if rel in collect_before:
            shutil.copy2(source, target)
            sys.stderr.write("evidence: %s existed before the run; copied, left in place\n" % rel)
        else:
            shutil.move(source, target)
            parent = os.path.dirname(source)
            while parent != cwd and parent.startswith(cwd + os.sep) and not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
        collected.append(rel)
    if args.collect and not collected:
        sys.stderr.write("evidence: --collect matched no file the run wrote under %s\n" % cwd)
    produced = sorted(rel for rel, state in dir_files(directory).items()
                      if rel not in (log_name, serve_name, MANIFEST) and dir_before.get(rel) != state)
    note = "exit %d; output %s" % (status, log_name)
    if timed_out:
        note = "timed out after %ds; output %s" % (args.timeout, log_name)
    if produced:
        note += "; files: %s" % ", ".join(produced)
    if args.serve:
        note += "; server log %s" % serve_name
    if digest:
        note += "; snapshot %s" % digest
    if args.retake:
        note += "; retake: %s" % args.retake
    note = note.replace("|", "/")
    command = " ".join(shlex.quote(a) for a in argv).replace("|", "\\|")
    path = os.path.join(directory, MANIFEST)
    new = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8") as handle:
        if new:
            handle.write(HEADER)
        handle.write("| %s | %s | %s | %d | %s |\n" % (args.check, command, stamp, attempt, note))
    print("evidence: %s attempt %d, %s -> %s%s" % (args.check, attempt,
                                                  "timed out" if timed_out else "exit %d" % status, log_path,
                                                  ("; files: " + ", ".join(produced)) if produced else ""))
    return 0


def cmd_view(args):
    source = os.path.abspath(args.dir)
    if not os.path.isdir(source):
        die("evidence dir not found: %s" % args.dir)
    out = os.path.abspath(args.out)
    if os.path.exists(out) and os.listdir(out):
        die("view dir exists and is not empty: %s" % args.out)
    count = 0
    renamed = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, source)
        for name in sorted(filenames):
            if rel_dir == "." and name == MANIFEST:
                continue
            target_name = name
            if ID_NAMED.match(name):
                renamed += 1
                target_name = "capture-%d%s" % (renamed, os.path.splitext(name)[1])
            target = os.path.join(out, rel_dir, target_name) if rel_dir != "." else os.path.join(out, target_name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(dirpath, name), target)
            count += 1
    os.makedirs(out, exist_ok=True)
    print("view: %d files -> %s (manifest left out, %d renamed)" % (count, out, renamed))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="evidence.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action")
    sub.required = True
    run = sub.add_parser("run", help="run one check once and log it in the manifest")
    run.add_argument("--dir", required=True, help="the round's evidence dir, for example LOOP/evidence/round-1")
    run.add_argument("--check", required=True, help="the gate or criterion id, for example G1 or C4")
    run.add_argument("--retake", default=None, help="why the previous attempt produced no measurement")
    run.add_argument("--cwd", default=None, help="directory to run the command in (default: --dir)")
    run.add_argument("--collect", action="append", default=[],
                     help="glob, relative to --cwd, of files the command writes; moved into --dir (repeatable)")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                     help="seconds before the command's process group is killed (default %d, 0 disables)" % DEFAULT_TIMEOUT)
    run.add_argument("--serve", default=None, help="server command to start first and stop afterwards")
    run.add_argument("--serve-cwd", default=None, help="directory to start the server in (default: current directory)")
    run.add_argument("--wait-for", default=None, help="URL to poll until it answers before the command runs")
    run.add_argument("--wait-timeout", type=int, default=DEFAULT_WAIT,
                     help="seconds to wait for --wait-for (default %d)" % DEFAULT_WAIT)
    run.add_argument("--snapshot", default=None,
                     help="snapshot dir the tree must match; its manifest digest goes into the row")
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command and its arguments")
    run.set_defaults(func=cmd_run)
    view = sub.add_parser("view", help="copy an evidence dir for a reviewer, without the manifest")
    view.add_argument("--dir", required=True, help="the round's evidence dir")
    view.add_argument("--out", required=True, help="the reviewer's view dir, for example LOOP/evidence/view-round-1")
    view.set_defaults(func=cmd_view)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
