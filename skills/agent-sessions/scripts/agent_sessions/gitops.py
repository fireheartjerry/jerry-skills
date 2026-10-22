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
    "branch_tip", "commits_ahead", "is_merged", "SNAPSHOT_NS", "status_entries", "names_z", "tree_diff",
    "ignored_entries", "UNTRACKED_DIFF_FILES", "repo_features", "tree_fingerprint", "changed_since",
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


def names_z(cp):
    """Paths from a `-z` listing (NUL separated, never quoted)."""
    if cp.returncode != 0:
        return []
    return [p for p in cp.stdout.split("\0") if p]


def status_entries(path, untracked="all"):
    """[(XY, path)] from `git status --porcelain -z`, paths relative to the
    repository root and never C-quoted (spaces, non-ASCII, a literal ' -> ').
    A rename or copy record carries its source path in the next field, which
    is skipped: the entry names the new path."""
    cp = git(path, "status", "--porcelain", "-z", "--untracked-files=%s" % untracked)
    if cp.returncode != 0:
        return []
    fields = cp.stdout.split("\0")
    found, i = [], 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, entry = rec[:2], rec[3:]
        if "R" in xy or "C" in xy:
            i += 1
        found.append((xy, entry))
    return found


def dirty_files(path):
    """Modified tracked and untracked non-ignored paths of a working tree."""
    return sorted(set(p for _xy, p in status_entries(path)))


def is_dirty(path):
    cp = git(path, "status", "--porcelain")
    return cp.returncode != 0 or bool(cp.stdout.strip())


def changed_files(path, base):
    """Paths changed since `base`: committed (base..HEAD), dirty tracked and
    untracked non-ignored files, sorted and unique."""
    found = set()
    if base:
        found.update(names_z(git(path, "diff", "--name-only", "-z", base)))
    found.update(dirty_files(path))
    return sorted(found)


# Untracked files above this count are left out of a judge or review diff
# (and named in its note): hashing thousands of build outputs nobody ignored
# would take longer than the review.
UNTRACKED_DIFF_FILES = 200


def _git_path(path, what):
    cp = git(path, "rev-parse", "--git-path", what)
    out = cp.stdout.strip()
    if cp.returncode != 0 or not out:
        return None
    return out if os.path.isabs(out) else os.path.normpath(os.path.join(path, out))


def tree_diff(path, base, index, exclude=()):
    """(CompletedProcess of `git diff`, note) of the working tree at `path`
    against `base`: committed and uncommitted tracked changes plus untracked
    non-ignored files. It stages into a private index (`index`) and writes
    blobs to a private object directory next to it, so neither the
    repository's index nor its object store changes; both are removed before
    returning. `exclude` lists repository-relative paths to leave out (the
    user's work already uncommitted when a shared-tree child started). The
    note says what was left out, or is None."""
    objdir = index + ".objects"
    notes = []
    objects = _git_path(path, "objects")
    env = dict(SNAPSHOT_ENV, GIT_INDEX_FILE=index, GIT_OBJECT_DIRECTORY=objdir)
    if objects:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = objects
    spec = [":/"] + [":(top,exclude,literal)%s" % p for p in exclude]
    untracked = names_z(git(path, "ls-files", "--others", "--exclude-standard", "-z", "--", *spec))
    try:
        os.makedirs(objdir, exist_ok=True)
        cp = git(path, "read-tree", "HEAD", env=env)
        if cp.returncode == 0:
            if len(untracked) > UNTRACKED_DIFF_FILES:
                notes.append("%d untracked files left out (more than %d; commit them, or list build output in "
                             ".gitignore)" % (len(untracked), UNTRACKED_DIFF_FILES))
                cp = git(path, "add", "-u", "--", *spec, env=env)
            else:
                cp = git(path, "add", "-A", "--", *spec, env=env)
        if cp.returncode == 0:
            cp = git(path, "diff", "--cached", "--no-color", base, "--", *spec, env=env)
    finally:
        for p in (index, index + ".lock"):
            try:
                os.unlink(p)
            except OSError:
                pass
        shutil.rmtree(objdir, ignore_errors=True)
    if exclude:
        notes.append("%d path%s already uncommitted at spawn left out (%s)" % (
            len(exclude), "" if len(exclude) == 1 else "s", ", ".join(list(exclude)[:5]) +
            (", ..." if len(exclude) > 5 else "")))
    return cp, "; ".join(notes) or None


# Ignored entries a worktree warning does not mention: caches and OS litter
# that no task needs.
_IGNORED_NOISE = (".DS_Store", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".idea",
                  ".vscode", "Thumbs.db")


def ignored_entries(root):
    """Ignored files and directories of a checkout, as git collapses them
    (`node_modules`, `.env`), minus caches. A new worktree has none of them."""
    cp = git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    found = []
    for entry in names_z(cp):
        entry = entry.rstrip("/")
        if not entry or os.path.basename(entry) in _IGNORED_NOISE or entry.endswith(".pyc"):
            continue
        found.append(entry)
    return sorted(found)


def repo_features(root):
    """What in a repository commonly breaks a commit or a checkout in a new
    worktree: [(key, detail)] for commit hooks, commit signing, submodules and
    Git LFS. Hooks live in the common git directory (or core.hooksPath), so a
    worktree runs the parent's hooks without the parent's node_modules or venv."""
    found = []
    hooks_path = git(root, "config", "--get", "core.hooksPath").stdout.strip()
    hooks = []
    if hooks_path:
        hooks.append("core.hooksPath=%s" % hooks_path)
    else:
        hooks_dir = _git_path(root, "hooks")
        if hooks_dir and os.path.isdir(hooks_dir):
            for entry in sorted(os.listdir(hooks_dir)):
                p = os.path.join(hooks_dir, entry)
                if not entry.endswith(".sample") and os.path.isfile(p) and os.access(p, os.X_OK):
                    hooks.append(entry)
    for marker in (".husky", ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml", ".lefthook.yml"):
        if os.path.exists(os.path.join(root, marker)):
            hooks.append(marker)
    if hooks:
        found.append(("hooks", "commit hooks (%s)" % ", ".join(hooks[:5])))
    if git(root, "config", "--type=bool", "--get", "commit.gpgsign").stdout.strip() == "true":
        found.append(("signing", "commit.gpgsign=true"))
    if os.path.isfile(os.path.join(root, ".gitmodules")):
        found.append(("submodules", "submodules (.gitmodules)"))
    try:
        with open(os.path.join(root, ".gitattributes"), encoding="utf-8", errors="replace") as fh:
            if re.search(r"filter=lfs\b", fh.read()):
                found.append(("lfs", "Git LFS (filter=lfs in .gitattributes)"))
    except OSError:
        pass
    return found


def tree_fingerprint(root, cap=2000):
    """{path: [size, mtime_ns]} of a working tree's dirty paths (None for a
    deleted one): enough to tell later which of them changed."""
    fp = {}
    for rel in dirty_files(root)[:cap]:
        try:
            st = os.lstat(os.path.join(root, rel))
            fp[rel] = [st.st_size, st.st_mtime_ns]
        except OSError:
            fp[rel] = None
    return fp


def changed_since(root, before):
    """Paths of `root` whose dirty state differs from fingerprint `before`:
    newly dirty, changed again, or no longer dirty."""
    if not isinstance(before, dict):
        return []
    now = tree_fingerprint(root)
    return sorted(p for p in set(now) | set(before) if now.get(p, "clean") != before.get(p, "clean"))


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
    dirty = is_dirty(path)
    block = {"branch": wt.get("branch"), "base_commit": base, "head_commit": head, "commits": commits,
             "files_changed": files_changed, "insertions": insertions, "deletions": deletions,
             "dirty": dirty, "changed_files": files}
    if dirty:
        left = dirty_files(path)
        features = dict(meta.get("repo_features") or [])
        why = ""
        if features.get("hooks") or features.get("signing"):
            why = (" The repository has %s: a commit that failed there is the usual cause; read the log for the "
                   "hook or signing error." % " and ".join(v for k, v in sorted(features.items())
                                                           if k in ("hooks", "signing")))
        warnings.append("the worktree has uncommitted changes (%s%s), so collect refuses it until they are "
                        "committed.%s Inspect with agent-session diff %s." % (
                            ", ".join(left[:5]), ", ..." if len(left) > 5 else "", why, meta.get("name")))
    root = wt.get("root")
    if root and os.path.isdir(root) and isinstance(meta.get("parent_at_start"), dict):
        moved = changed_since(root, meta["parent_at_start"])
        if moved:
            warnings.append("the parent tree %s changed during the run: %s%s. That is you, another writer, or this "
                            "child writing by absolute path outside its worktree; check before collecting" % (
                                root, ", ".join(moved[:5]), ", ..." if len(moved) > 5 else ""))
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
