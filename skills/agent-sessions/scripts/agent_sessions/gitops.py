"""git helpers: the worktree summary that becomes the result's `git` block,
worktree creation from a base ref, copying ignored files, lost-work snapshots,
collection and the conflict forecast.

Every git call goes through `git()` (argv, never a shell). Snapshot commits
are authored as `agent-session` so they work without a configured identity.
"""

import os
import re
import shutil
import time

from agent_sessions.common import CHANGED_FILES_CAP, STATE_ROOT, append_jsonl, read_jsonl, run

__all__ = [
    "git", "worktree_summary", "changed_files", "repo_root", "resolve_commit", "is_dirty", "dirty_files",
    "copy_into", "snapshot", "snapshot_ref_of_commit", "snapshot_records", "list_snapshots", "delete_ref",
    "branch_tip", "commits_ahead", "is_merged", "SNAPSHOT_NS",
]

SNAPSHOT_NS = "refs/agent-sessions"
SNAPSHOT_ENV = {"GIT_AUTHOR_NAME": "agent-session", "GIT_AUTHOR_EMAIL": "agent-session@localhost",
                "GIT_COMMITTER_NAME": "agent-session", "GIT_COMMITTER_EMAIL": "agent-session@localhost"}


def git(root, *args, **kw):
    env = kw.pop("env", None)
    assert not kw, kw
    full = None
    if env:
        full = dict(os.environ)
        full.update(env)
    return run(["git", "-C", root] + list(args), env=full)


def _lines(cp):
    return [l for l in cp.stdout.splitlines() if l.strip()] if cp.returncode == 0 else []


def repo_root(path):
    cp = git(path, "rev-parse", "--show-toplevel")
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None


def resolve_commit(root, ref):
    cp = git(root, "rev-parse", "--verify", "--quiet", "%s^{commit}" % ref)
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None


def branch_tip(root, branch):
    return resolve_commit(root, "refs/heads/%s" % branch)


def is_merged(root, commit):
    """commit is reachable from the root checkout's HEAD (what `git branch -d` accepts)."""
    return git(root, "merge-base", "--is-ancestor", commit, "HEAD").returncode == 0


def commits_ahead(root, base, head):
    cp = git(root, "rev-list", "--count", "%s..%s" % (base, head))
    if cp.returncode == 0 and cp.stdout.strip().isdigit():
        return int(cp.stdout.strip())
    return 0


def dirty_files(path):
    """Modified tracked and untracked non-ignored paths of a working tree."""
    found = []
    for line in _lines(git(path, "status", "--porcelain", "--untracked-files=all")):
        entry = line[3:] if len(line) > 3 else ""
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if entry:
            found.append(entry)
    return sorted(set(found))


def is_dirty(path):
    cp = git(path, "status", "--porcelain")
    return cp.returncode != 0 or bool(cp.stdout.strip())


def changed_files(path, base):
    """Paths changed since `base`: committed (base..HEAD), dirty tracked and
    untracked non-ignored files, sorted and unique."""
    found = set()
    if base:
        found.update(_lines(git(path, "diff", "--name-only", base)))
    found.update(dirty_files(path))
    return sorted(found)


def worktree_summary(meta):
    """(git_block, warnings) for a worktree child, or (None, []) otherwise.
    Counts come from `git diff --numstat <base>` (committed plus uncommitted
    tracked changes); changed_files adds untracked paths and is capped."""
    wt = meta.get("worktree")
    if not isinstance(wt, dict) or not wt.get("path") or not os.path.isdir(wt["path"]):
        return None, []
    path, base = wt["path"], wt.get("base_commit")
    warnings = []
    head = (git(path, "rev-parse", "HEAD").stdout or "").strip() or None
    commits = commits_ahead(path, base, "HEAD") if base and head else 0
    files_changed = insertions = deletions = 0
    if base:
        for line in _lines(git(path, "diff", "--numstat", base)):
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files_changed += 1
            if parts[0].isdigit():
                insertions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
    files = changed_files(path, base)
    if len(files) > CHANGED_FILES_CAP:
        warnings.append("changed_files capped at %d of %d paths" % (CHANGED_FILES_CAP, len(files)))
        files = files[:CHANGED_FILES_CAP]
    block = {"branch": wt.get("branch"), "base_commit": base, "head_commit": head, "commits": commits,
             "files_changed": files_changed, "insertions": insertions, "deletions": deletions,
             "dirty": is_dirty(path), "changed_files": files}
    return block, warnings


def copy_into(root, dest, rels):
    """Copy paths (relative to the repo root; files or directories, typically
    ignored ones such as .env or node_modules) into a new worktree. Returns a
    list of warning strings; a failed copy never fails the spawn."""
    warnings = []
    for raw in rels:
        rel = os.path.normpath(raw.strip()) if raw.strip() else ""
        if not rel or rel == "." or os.path.isabs(raw.strip()) or rel == ".." or rel.startswith(".." + os.sep) \
                or rel == ".git" or rel.startswith(".git" + os.sep):
            warnings.append("--copy %s skipped: paths must be inside the repository root (and not .git)" % raw)
            continue
        src = os.path.join(root, rel)
        dst = os.path.join(dest, rel)
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                # Merge into a directory the checkout already has; files the
                # parent tree has win, files only the worktree has stay.
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            elif os.path.lexists(src):
                os.makedirs(os.path.dirname(dst) or dest, exist_ok=True)
                shutil.copy2(src, dst, follow_symlinks=False)
            else:
                warnings.append("--copy %s skipped: not found in %s" % (rel, root))
        except (OSError, shutil.Error) as exc:
            warnings.append("--copy %s failed: %s" % (rel, exc))
    return warnings


def _snapshot_name(root, name):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    ref = "%s/%s/snapshot-%s" % (SNAPSHOT_NS, name, stamp)
    k = 1
    while resolve_commit(root, ref):
        k += 1
        ref = "%s/%s/snapshot-%s-%d" % (SNAPSHOT_NS, name, stamp, k)
    return ref


def _record(root, ref, name, kind):
    try:
        os.makedirs(STATE_ROOT, 0o700, exist_ok=True)
        append_jsonl(os.path.join(STATE_ROOT, "snapshots.jsonl"),
                     {"root": root, "ref": ref, "name": name, "kind": kind,
                      "at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "at_epoch": int(time.time())})
    except OSError:
        pass


def snapshot(root, worktree, name):
    """Commit the worktree's full state (tracked, modified and untracked
    non-ignored files) with HEAD as parent, using a temporary index so the
    worktree's own index is untouched, and store it under
    refs/agent-sessions/<name>/snapshot-<ts>. Returns (ref, error)."""
    head = resolve_commit(worktree, "HEAD")
    if not head:
        return None, "cannot resolve HEAD in %s" % worktree
    index = os.path.join(STATE_ROOT, ".snapshot-index-%d" % os.getpid())
    env = dict(SNAPSHOT_ENV, GIT_INDEX_FILE=index)
    try:
        cp = git(worktree, "read-tree", "HEAD", env=env)
        if cp.returncode == 0:
            cp = git(worktree, "add", "-A", env=env)
        if cp.returncode != 0:
            return None, "git add failed: %s" % cp.stderr.strip()
        cp = git(worktree, "write-tree", env=env)
        tree = cp.stdout.strip()
        if cp.returncode != 0 or not tree:
            return None, "git write-tree failed: %s" % cp.stderr.strip()
        msg = "agent-session snapshot of %s at %s" % (name, time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        cp = git(worktree, "commit-tree", tree, "-p", head, "-m", msg, env=SNAPSHOT_ENV)
        commit = cp.stdout.strip()
        if cp.returncode != 0 or not commit:
            return None, "git commit-tree failed: %s" % cp.stderr.strip()
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass
    return snapshot_ref_of_commit(root, commit, name, "worktree")


def snapshot_ref_of_commit(root, commit, name, kind="branch"):
    """Keep an existing commit reachable under refs/agent-sessions/. Returns (ref, error)."""
    ref = _snapshot_name(root, name)
    cp = git(root, "update-ref", ref, commit)
    if cp.returncode != 0:
        return None, "git update-ref failed: %s" % cp.stderr.strip()
    _record(root, ref, name, kind)
    return ref, None


def snapshot_records():
    return read_jsonl(os.path.join(STATE_ROOT, "snapshots.jsonl"))


_STAMP_RE = re.compile(r"/snapshot-(\d{8}T\d{6})(?:-\d+)?$")


def _taken_epoch(ref, recorded):
    """When a snapshot was taken: snapshots.jsonl's at_epoch, else the local
    timestamp in the ref name. Never the commit date: a --discard snapshot
    points at a branch tip whose commit can be years older than the snapshot."""
    if isinstance(recorded, (int, float)):
        return int(recorded)
    m = _STAMP_RE.search(ref)
    if m:
        try:
            return int(time.mktime(time.strptime(m.group(1), "%Y%m%dT%H%M%S")))
        except (ValueError, OverflowError):
            pass
    return None


def list_snapshots(roots):
    """[{root, ref, name, commit, epoch}] for every refs/agent-sessions/* ref in
    the given repos; `epoch` is when the snapshot was taken."""
    recorded = {}
    for rec in snapshot_records():
        if isinstance(rec, dict) and rec.get("ref"):
            recorded[(rec.get("root"), rec["ref"])] = rec.get("at_epoch")
    found = []
    seen = set()
    for root in roots:
        if not root or root in seen or not os.path.isdir(root):
            continue
        seen.add(root)
        cp = git(root, "for-each-ref", "--format=%(refname)\t%(objectname)\t%(committerdate:unix)", SNAPSHOT_NS)
        for line in _lines(cp):
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            ref, commit, epoch = parts
            rest = ref[len(SNAPSHOT_NS) + 1:]
            name = rest.split("/", 1)[0]
            taken = _taken_epoch(ref, recorded.get((root, ref)))
            if taken is not None:
                epoch = taken
            else:
                try:
                    epoch = int(epoch)
                except ValueError:
                    epoch = 0
            found.append({"root": root, "ref": ref, "name": name, "commit": commit, "epoch": epoch})
    found.sort(key=lambda s: (s["epoch"], s["ref"]))
    return found


def delete_ref(root, ref):
    return git(root, "update-ref", "-d", ref).returncode == 0
