#!/usr/bin/env python3
"""Snapshot an artifact for an isolated reviewer or scorer, verify it afterwards,
and restore it when a round must be reverted.

usage: snapshot.py create  --root ROOT [--path REL ...] --out DIR [--exclude GLOB ...]
       snapshot.py verify  --snapshot DIR [--manifest FILE]
       snapshot.py seal    --dir DIR
       snapshot.py restore --snapshot DIR --root ROOT [--dry-run] [--force-root]
                           [--backup-dir DIR]

create copies ROOT (or only the --path entries under it) into DIR, always leaving
out .git, .iterative-improvement, any *-criteria.md, any *-rounds.md, symlinks,
secret-shaped files (.env, .env.*, *.pem, id_*) and, when ROOT is inside a git work
tree (including worktrees and submodules, where .git is a file), every gitignored
file. It writes DIR.manifest.json next to DIR with a sha256 per file. Exit 2 when
ROOT is missing or DIR is non-empty.

verify re-hashes DIR against the manifest (DIR.manifest.json, or --manifest FILE).
Prints "unchanged" and exits 0, or one line per difference (modified:, added:,
removed:) and exits 3.

seal writes DIR.manifest.json for an existing directory without copying anything.
spawn_child.sh uses it on the staged copy after renaming agent instruction files.

restore copies the manifest's files from DIR back into ROOT and removes files that
appeared under the covered paths since the snapshot. Every file it overwrites or
removes is first copied to a backup directory (default: <LOOP>/restore-backup/<utc
stamp>/ when DIR sits in <LOOP>/snapshots/, otherwise DIR.restore-backup-<stamp>/),
and that path is printed. Exit 2 when ROOT is not the manifest's root, unless
--force-root. Run with --dry-run first.
"""

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys

ALWAYS_EXCLUDED_DIRS = (".git", ".iterative-improvement")
ALWAYS_EXCLUDED_GLOBS = ("*-criteria.md", "*-rounds.md")
SECRET_GLOBS = (".env", ".env.*", "*.pem", "id_*")


def die(message, code=2):
    sys.stderr.write("snapshot: %s\n" % message)
    sys.exit(code)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def to_posix(rel):
    return rel.replace(os.sep, "/")


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")


def excluded(rel, extra_globs):
    parts = rel.split("/")
    for part in parts:
        if part in ALWAYS_EXCLUDED_DIRS:
            return True
    base = parts[-1]
    for glob in ALWAYS_EXCLUDED_GLOBS + SECRET_GLOBS:
        if fnmatch.fnmatch(base, glob):
            return True
    for glob in extra_globs:
        if fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(base, glob):
            return True
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            if fnmatch.fnmatch(prefix, glob) or fnmatch.fnmatch(parts[index - 1], glob):
                return True
    return False


def under_paths(rel, paths):
    if not paths:
        return True
    for covered in paths:
        if rel == covered or rel.startswith(covered + "/"):
            return True
    return False


def inside_git_work_tree(root):
    """True when git says ROOT is inside a work tree (covers worktrees and submodules)."""
    if shutil.which("git") is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == b"true"


def git_file_list(root):
    """Return the relative paths git knows about under ROOT, or None when git is unusable."""
    if not inside_git_work_tree(root):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        sys.stderr.write("snapshot: git ls-files failed, walking the tree instead\n")
        return None
    entries = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    return sorted(set(e for e in entries if e))


def walk_file_list(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else to_posix(rel_dir)
        kept = []
        for name in sorted(dirnames):
            if name in ALWAYS_EXCLUDED_DIRS:
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                rel = name if not rel_dir else rel_dir + "/" + name
                sys.stderr.write("skipped symlink: %s\n" % rel)
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = name if not rel_dir else rel_dir + "/" + name
            found.append(rel)
    return found


def file_list(root, paths, extra_globs, report_symlinks=True):
    listed = git_file_list(root)
    if listed is None:
        listed = walk_file_list(root)
    result = []
    for rel in listed:
        rel = to_posix(rel)
        if excluded(rel, extra_globs) or not under_paths(rel, paths):
            continue
        full = os.path.join(root, *rel.split("/"))
        if os.path.islink(full):
            if report_symlinks:
                sys.stderr.write("skipped symlink: %s\n" % rel)
            continue
        if not os.path.isfile(full):
            continue
        result.append(rel)
    return sorted(set(result))


def manifest_path_for(snapshot_dir):
    snapshot_dir = os.path.abspath(snapshot_dir.rstrip(os.sep) or snapshot_dir)
    parent, name = os.path.split(snapshot_dir)
    return os.path.join(parent, name + ".manifest.json")


def load_manifest(path):
    if not os.path.isfile(path):
        die("manifest not found: %s" % path)
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except ValueError as error:
            die("manifest is not valid JSON: %s (%s)" % (path, error))


def normalise_paths(paths):
    cleaned = []
    for entry in paths or []:
        entry = to_posix(entry).strip("/")
        if entry and entry != ".":
            cleaned.append(entry)
    return cleaned


def snapshot_contents(snapshot_dir):
    found = {}
    for dirpath, dirnames, filenames in os.walk(snapshot_dir):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, snapshot_dir)
        rel_dir = "" if rel_dir == "." else to_posix(rel_dir)
        for name in sorted(filenames):
            rel = name if not rel_dir else rel_dir + "/" + name
            found[rel] = os.path.join(dirpath, name)
    return found


def write_manifest(path, manifest):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def cmd_create(args):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        die("root not found: %s" % args.root)
    out = os.path.abspath(args.out)
    if os.path.exists(out) and os.listdir(out):
        die("snapshot dir already exists and is not empty (refusing to merge): %s" % args.out)
    paths = normalise_paths(args.path)
    for covered in paths:
        if not os.path.exists(os.path.join(root, *covered.split("/"))):
            die("path not found under root: %s" % covered)
    files = file_list(root, paths, args.exclude or [])
    os.makedirs(out, exist_ok=True)
    manifest = {
        "root": root,
        "created": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "paths": paths,
        "excludes": list(args.exclude or []),
        "files": {},
    }
    for rel in files:
        src = os.path.join(root, *rel.split("/"))
        dst = os.path.join(out, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        manifest["files"][rel] = {"sha256": sha256_of(dst), "size": os.path.getsize(dst)}
    write_manifest(manifest_path_for(out), manifest)
    print("snapshot: %d files -> %s" % (len(files), out))
    return 0


def cmd_seal(args):
    directory = os.path.abspath(args.dir)
    if not os.path.isdir(directory):
        die("dir not found: %s" % args.dir)
    manifest = {
        "root": directory,
        "created": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "paths": [],
        "excludes": [],
        "sealed": True,
        "files": {},
    }
    for rel, full in sorted(snapshot_contents(directory).items()):
        if os.path.islink(full):
            die("sealed directory contains a symlink: %s" % rel)
        manifest["files"][rel] = {"sha256": sha256_of(full), "size": os.path.getsize(full)}
    write_manifest(manifest_path_for(directory), manifest)
    print("sealed: %d files -> %s" % (len(manifest["files"]), manifest_path_for(directory)))
    return 0


def cmd_verify(args):
    snapshot_dir = os.path.abspath(args.snapshot)
    if not os.path.isdir(snapshot_dir):
        die("snapshot dir not found: %s" % args.snapshot)
    manifest = load_manifest(os.path.abspath(args.manifest) if args.manifest else manifest_path_for(snapshot_dir))
    expected = manifest.get("files", {})
    actual = snapshot_contents(snapshot_dir)
    problems = []
    for rel in sorted(expected):
        if rel not in actual:
            problems.append("removed: %s" % rel)
            continue
        full = actual[rel]
        if os.path.islink(full) or sha256_of(full) != expected[rel]["sha256"]:
            problems.append("modified: %s" % rel)
    for rel in sorted(actual):
        if rel not in expected:
            problems.append("added: %s" % rel)
    if not problems:
        print("unchanged")
        return 0
    for line in problems:
        print(line)
    return 3


def default_backup_dir(snapshot_dir):
    parent = os.path.dirname(snapshot_dir)
    stamp = utc_stamp()
    if os.path.basename(parent) == "snapshots":
        return os.path.join(os.path.dirname(parent), "restore-backup", stamp)
    return snapshot_dir + ".restore-backup-" + stamp


def cmd_restore(args):
    snapshot_dir = os.path.abspath(args.snapshot)
    if not os.path.isdir(snapshot_dir):
        die("snapshot dir not found: %s" % args.snapshot)
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        die("root not found: %s" % args.root)
    manifest = load_manifest(manifest_path_for(snapshot_dir))
    recorded_root = manifest.get("root")
    if recorded_root and os.path.realpath(recorded_root) != os.path.realpath(root) and not args.force_root:
        die("root %s differs from the manifest's root %s (pass --force-root to override)" % (root, recorded_root))
    paths = normalise_paths(manifest.get("paths", []))
    excludes = manifest.get("excludes", [])
    expected = manifest.get("files", {})
    current = file_list(root, paths, excludes, report_symlinks=False)
    to_copy = sorted(expected)
    to_remove = [rel for rel in current if rel not in expected]
    for rel in to_copy:
        src = os.path.join(snapshot_dir, *rel.split("/"))
        if not os.path.isfile(src):
            die("snapshot is missing a manifest file: %s" % rel)
    changed = []
    for rel in to_copy:
        dst = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(dst) or sha256_of(dst) != expected[rel]["sha256"]:
            changed.append(rel)
    backup_dir = os.path.abspath(args.backup_dir) if args.backup_dir else default_backup_dir(snapshot_dir)
    to_backup = sorted(rel for rel in changed if os.path.isfile(os.path.join(root, *rel.split("/")))) + to_remove
    if args.dry_run:
        for rel in to_copy:
            print("would copy: %s%s" % (rel, "" if rel in changed else " (unchanged)"))
        for rel in to_remove:
            print("would remove: %s" % rel)
        print("dry run: %d to copy (%d differ), %d to remove; backup of %d files would go to %s"
              % (len(to_copy), len(changed), len(to_remove), len(to_backup), backup_dir))
        return 0
    for rel in to_backup:
        src = os.path.join(root, *rel.split("/"))
        dst = os.path.join(backup_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    for rel in to_copy:
        src = os.path.join(snapshot_dir, *rel.split("/"))
        dst = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    for rel in to_remove:
        os.remove(os.path.join(root, *rel.split("/")))
    print("restored: %d copied, %d removed" % (len(to_copy), len(to_remove)))
    if to_backup:
        print("backup: %d files -> %s" % (len(to_backup), backup_dir))
    else:
        print("backup: nothing differed, no backup written")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="snapshot.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    create = sub.add_parser("create", help="copy the artifact into a snapshot dir and write its manifest")
    create.add_argument("--root", required=True, help="artifact root (repo root, or the folder holding the artifact)")
    create.add_argument("--path", action="append", default=[], help="relative file or directory to include (repeatable)")
    create.add_argument("--out", required=True, help="snapshot directory to create; must not exist or must be empty")
    create.add_argument("--exclude", action="append", default=[], help="extra glob to exclude (repeatable)")
    create.set_defaults(func=cmd_create)

    verify = sub.add_parser("verify", help="re-hash a snapshot against its manifest")
    verify.add_argument("--snapshot", required=True, help="snapshot directory")
    verify.add_argument("--manifest", default=None, help="manifest to compare against (default: <snapshot>.manifest.json)")
    verify.set_defaults(func=cmd_verify)

    seal = sub.add_parser("seal", help="write a manifest for an existing directory without copying")
    seal.add_argument("--dir", required=True, help="directory to seal; writes <dir>.manifest.json")
    seal.set_defaults(func=cmd_seal)

    restore = sub.add_parser("restore", help="copy a snapshot back over the artifact root, backing up what it replaces")
    restore.add_argument("--snapshot", required=True, help="snapshot directory")
    restore.add_argument("--root", required=True, help="artifact root to restore into")
    restore.add_argument("--dry-run", action="store_true", help="print the plan without changing anything")
    restore.add_argument("--force-root", action="store_true", help="allow a root that differs from the manifest's root")
    restore.add_argument("--backup-dir", default=None, help="where to copy replaced and removed files (default: described in --help)")
    restore.set_defaults(func=cmd_restore)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
