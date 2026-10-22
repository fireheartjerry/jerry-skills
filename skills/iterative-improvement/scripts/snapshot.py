#!/usr/bin/env python3
"""Snapshot an artifact for an isolated reviewer or scorer, verify it afterwards,
fingerprint the files it came from, and restore it when a round must be reverted.

usage: snapshot.py create      --root ROOT [--path REL ...] (--out DIR | --dry-run)
                               [--exclude GLOB ...] [--include GLOB ...] [--whole-root]
       snapshot.py verify      --snapshot DIR [--manifest FILE]
       snapshot.py seal        --dir DIR
       snapshot.py fingerprint --snapshot DIR (--save FILE | --compare FILE) [--whole-root]
       snapshot.py restore     --snapshot DIR --root ROOT [--only REL ...] [--expect DIR]
                               [--dry-run] [--force-root] [--backup-dir DIR]
       snapshot.py pair        --first DIR --second DIR --out DIR
                               [--first-evidence DIR --second-evidence DIR]

create copies the --path entries under ROOT (the artifact and the files it is checked
against) into DIR and writes DIR.manifest.json next to it with a sha256 per file. It
always leaves out .git and .iterative-improvement, symlinks, and, when ROOT is inside
a git work tree (worktrees and submodules too, where .git is a file), every gitignored
file. It also leaves out, by name or content, anything that looks like a credential:
  .env and .env.* (except .env.example, .env.sample, .env.template, .env.dist),
  .npmrc .pypirc .netrc _netrc .pgpass .git-credentials .htpasswd, credentials and
  credentials.*, secret.* secrets.* *-secret.* *_secret.* *-secrets.* *_secrets.*
  client_secret*.json service-account*.json, *.pem *.key *.p8 *.p12 *.pfx *.jks
  *.keystore *.tfvars *.tfstate, id_rsa id_dsa id_ecdsa id_ed25519 (and their .pub
  and _suffix forms), anything under .ssh/ .aws/ .gnupg/ .docker/, and any file under
  1 MB that contains a PEM private key block;
and the loop's own criteria doc (*-criteria.md at ROOT's top level or in docs/criteria/).
Names match case-insensitively. Every file left out by these rules is printed to
stderr with its reason, with the count of gitignored entries in scope, so nothing
drops silently; --include GLOB (repeatable, matched against the path and the file
name) keeps a file that a name or content rule would drop. It prints the file count
and size. --dry-run prints every file it would copy and creates nothing (no --out
needed): the list the proposal shows the user before anything is sent to a child.

Without --path, create snapshots all of ROOT, and refuses (exit 2) when that is more
than 500 files or 50 MB unless --whole-root says the artifact really is the whole
root. Exit 2 also when ROOT or a --path is missing, DIR is non-empty, or no file
survives the exclusions (an empty snapshot would have the children grade nothing).

verify re-hashes DIR against the manifest (DIR.manifest.json, or --manifest FILE).
Prints "unchanged" and exits 0, or one line per difference (modified:, added:,
removed:) and exits 3.

seal writes DIR.manifest.json for an existing directory without copying anything.
spawn_child.sh uses it on the staged copy after renaming agent instruction files.

fingerprint hashes the content of the files in ROOT that the snapshot covers (the
manifest's root and --path scope, or all of ROOT with --whole-root), including files
the name rules leave out of the snapshot. It reads the file list with git ls-files
(no index lock) or a directory walk outside git, so it sees edits to untracked files
and works in any folder. --save FILE writes the state; --compare FILE prints
"unchanged" and exits 0, or one line per changed:, added: or removed: path and
exits 3. spawn_child.sh takes it before and after every child run.

restore copies back the manifest's files that differ in ROOT and removes files that
appeared under the covered paths since the snapshot. --only REL (repeatable) limits it
to those files or folders: pass the files the loop changed. --expect DIR names the
later snapshot that holds the state the loop left (for example round-N when
reverting to round-N-1); restore then refuses (exit 2), naming them, files whose
content matches neither snapshot, because someone else changed them. Each file is
re-hashed just before it is overwritten, copied to a backup directory first, and
written with a fresh modification time so build tools see the change. Exit 2 when
ROOT is not the manifest's root, unless --force-root. Run with --dry-run first.

pair builds the blind final judge's input: it verifies both snapshots against their
manifests, copies them into DIR/a and DIR/b in random order, seals DIR (so
spawn_child.sh can stage it; the manifest names no ROOT, so no ROOT check runs) and
writes which is which to DIR.mapping beside DIR, never inside it. With
--first-evidence and --second-evidence (each round's evidence.py view, which carries
no manifest and no check ids), it also writes DIR-evidence/ holding every file of
each view prefixed "a-" or "b-" to match, subfolders joined into the name with "-".
Give the judge DIR and DIR-evidence only; read DIR.mapping after the verdict. Exit 2 when a snapshot is missing or DIR is
not empty, 3 when a snapshot no longer matches its manifest.
"""

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys

ALWAYS_EXCLUDED_DIRS = (".git", ".iterative-improvement")
CRITERIA_DOC = re.compile(r"^(?:docs/criteria/)?[^/]+-criteria\.md$", re.I)
ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template", ".env.dist")
SECRET_NAMES = (
    ".env", ".env.*", ".npmrc", ".pypirc", ".netrc", "_netrc", ".pgpass", ".git-credentials", ".htpasswd",
    "credentials", "credentials.*", "secret.*", "secrets.*", "*-secret.*", "*_secret.*", "*-secrets.*",
    "*_secrets.*", "client_secret*.json", "service-account*.json", "*.pem", "*.key", "*.p8", "*.p12", "*.pfx",
    "*.jks", "*.keystore", "*.tfvars", "*.tfstate",
)
SSH_KEY_TYPES = ("rsa", "dsa", "ecdsa", "ed25519")
SECRET_NAMES += tuple(p % k for k in SSH_KEY_TYPES for p in ("id_%s", "id_%s.*", "id_%s_*"))
SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".docker")
PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
CONTENT_SCAN_LIMIT = 1 << 20
WHOLE_ROOT_FILES = 500
WHOLE_ROOT_BYTES = 50 * 1024 * 1024


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


def matches(rel, globs):
    base = rel.split("/")[-1]
    return any(fnmatch.fnmatchcase(rel.lower(), g.lower()) or fnmatch.fnmatchcase(base.lower(), g.lower())
               for g in globs)


def extra_excluded(rel, extra_globs):
    """True when an --exclude glob matches the path, its file name or a parent folder."""
    parts = rel.split("/")
    for glob in extra_globs:
        if fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(parts[-1], glob):
            return True
        for index in range(1, len(parts)):
            if fnmatch.fnmatch("/".join(parts[:index]), glob) or fnmatch.fnmatch(parts[index - 1], glob):
                return True
    return False


def secret_reason(rel, full):
    """Why REL looks like a credential, or None."""
    parts = rel.split("/")
    for part in parts[:-1]:
        if part.lower() in SECRET_DIRS:
            return "secret-shaped (under %s/)" % part
    base = parts[-1].lower()
    if base not in ENV_TEMPLATES:
        for glob in SECRET_NAMES:
            if fnmatch.fnmatchcase(base, glob):
                return "secret-shaped (name matches %s)" % glob
    try:
        if os.path.getsize(full) <= CONTENT_SCAN_LIMIT:
            with open(full, "rb") as handle:
                if PRIVATE_KEY.search(handle.read()):
                    return "secret-shaped (contains a private key block)"
    except OSError:
        pass
    return None


def under_paths(rel, paths):
    if not paths:
        return True
    for covered in paths:
        if rel == covered or rel.startswith(covered + "/"):
            return True
    return False


def git_env():
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run_git(root, *args):
    if shutil.which("git") is None:
        return None
    try:
        proc = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=git_env(), check=False)
    except OSError:
        return None
    return proc


def inside_git_work_tree(root):
    """True when git says ROOT is inside a work tree (covers worktrees and submodules)."""
    proc = run_git(root, "rev-parse", "--is-inside-work-tree")
    return proc is not None and proc.returncode == 0 and proc.stdout.strip() == b"true"


def split_z(raw):
    return sorted(set(e for e in raw.decode("utf-8", "surrogateescape").split("\0") if e))


def git_file_list(root):
    """Return the relative paths git knows about under ROOT, or None when git is unusable."""
    if not inside_git_work_tree(root):
        return None
    proc = run_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if proc is None or proc.returncode != 0:
        sys.stderr.write("snapshot: git ls-files failed, walking the tree instead\n")
        return None
    return split_z(proc.stdout)


def git_ignored_entries(root, paths):
    proc = run_git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
    if proc is None or proc.returncode != 0:
        return []
    found = []
    for entry in split_z(proc.stdout):
        rel = to_posix(entry).rstrip("/")
        if rel.split("/")[0] in ALWAYS_EXCLUDED_DIRS:
            continue
        if not paths or under_paths(rel, paths) or any(p.startswith(rel + "/") for p in paths):
            found.append(rel)
    return found


def walk_file_list(root, report=None):
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
                if report is not None:
                    report.append(("symlink", name if not rel_dir else rel_dir + "/" + name))
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            found.append(name if not rel_dir else rel_dir + "/" + name)
    return found


def file_list(root, paths, extra_globs, report_symlinks=True, include_globs=(), skipped=None, name_rules=True):
    """Files under ROOT in scope, after every exclusion. SKIPPED, when a list, collects
    (reason, path) for each file a rule left out. name_rules=False keeps credential-shaped
    files and criteria docs (the fingerprint wants every file a child could touch)."""
    report = [] if skipped is None else skipped
    listed = git_file_list(root)
    if listed is None:
        listed = walk_file_list(root, report)
    result = []
    for rel in listed:
        rel = to_posix(rel)
        parts = rel.split("/")
        if any(part in ALWAYS_EXCLUDED_DIRS for part in parts) or not under_paths(rel, paths):
            continue
        full = os.path.join(root, *parts)
        if os.path.islink(full):
            report.append(("symlink", rel))
            continue
        if not os.path.isfile(full):
            continue
        if extra_excluded(rel, extra_globs):
            continue
        if name_rules and not matches(rel, include_globs):
            reason = "criteria doc" if CRITERIA_DOC.match(rel) else secret_reason(rel, full)
            if reason:
                report.append((reason, rel))
                continue
        result.append(rel)
    if report_symlinks and skipped is None:
        for reason, rel in report:
            if reason == "symlink":
                sys.stderr.write("skipped symlink: %s\n" % rel)
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
        while entry.startswith("./"):
            entry = entry[2:]
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


def human_size(size):
    return "%.1f MB" % (size / 1048576.0) if size >= 102400 else "%d KB" % max(1, (size + 1023) // 1024)


def cmd_create(args):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        die("root not found: %s" % args.root)
    if not args.out and not args.dry_run:
        die("create needs --out DIR (or --dry-run to list the files)")
    out = os.path.abspath(args.out) if args.out else None
    if not args.dry_run and os.path.exists(out) and os.listdir(out):
        die("snapshot dir already exists and is not empty (refusing to merge): %s" % args.out)
    paths = normalise_paths(args.path)
    for covered in paths:
        if not os.path.exists(os.path.join(root, *covered.split("/"))):
            die("path not found under root: %s" % covered)
    skipped = []
    files = file_list(root, paths, args.exclude or [], include_globs=args.include or [], skipped=skipped)
    for reason, rel in skipped:
        sys.stderr.write("skipped %s: %s\n" % (reason, rel))
    if inside_git_work_tree(root):
        ignored = git_ignored_entries(root, paths)
        if ignored:
            shown = ", ".join(ignored[:10]) + (", ..." if len(ignored) > 10 else "")
            sys.stderr.write("skipped %d gitignored entr%s in scope: %s\n"
                             % (len(ignored), "y" if len(ignored) == 1 else "ies", shown))
    if not files:
        die("no files to snapshot under %s%s. Likely causes: ROOT sits under a gitignored "
            "path of an enclosing repo (git ls-files lists nothing), every file is ignored, "
            "secret-shaped or excluded, or --path names the wrong place. Check with "
            "'git -C ROOT ls-files --cached --others --exclude-standard'"
            % (args.root, (" (paths: %s)" % ", ".join(paths)) if paths else ""))
    total = sum(os.path.getsize(os.path.join(root, *rel.split("/"))) for rel in files)
    if not paths and not args.whole_root and (len(files) > WHOLE_ROOT_FILES or total > WHOLE_ROOT_BYTES):
        die("refusing a whole-root snapshot of %d files (%s): pass --path for the artifact and each file it is "
            "checked against, or --whole-root if the artifact really is all of ROOT" % (len(files), human_size(total)))
    if args.dry_run:
        for rel in files:
            print("would copy: %s" % rel)
        print("dry run: %d files (%s) from %s%s" % (len(files), human_size(total), root,
                                                    (" (paths: %s)" % ", ".join(paths)) if paths else " (whole root)"))
        return 0
    os.makedirs(out, exist_ok=True)
    manifest = {
        "root": root,
        "created": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "paths": paths,
        "excludes": list(args.exclude or []),
        "includes": list(args.include or []),
        "files": {},
    }
    for rel in files:
        src = os.path.join(root, *rel.split("/"))
        dst = os.path.join(out, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        manifest["files"][rel] = {"sha256": sha256_of(dst), "size": os.path.getsize(dst)}
    write_manifest(manifest_path_for(out), manifest)
    print("snapshot: %d files (%s) -> %s" % (len(files), human_size(total), out))
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


def root_state(manifest, whole_root):
    """{rel: sha256} of the files in the manifest's root that the snapshot covers."""
    root = manifest.get("root") or ""
    if not os.path.isdir(root):
        return None
    paths = [] if whole_root else normalise_paths(manifest.get("paths", []))
    state = {}
    for rel in file_list(root, paths, manifest.get("excludes", []), report_symlinks=False, skipped=[],
                         name_rules=False):
        full = os.path.join(root, *rel.split("/"))
        try:
            state[rel] = sha256_of(full)
        except OSError:
            state[rel] = "unreadable"
    return state


def cmd_fingerprint(args):
    snapshot_dir = os.path.abspath(args.snapshot)
    manifest = load_manifest(manifest_path_for(snapshot_dir))
    state = root_state(manifest, args.whole_root)
    if state is None:
        state = {}
    if args.save:
        with open(args.save, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
        digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        print("fingerprint: %d files, %s" % (len(state), digest))
        return 0
    with open(args.compare, "r", encoding="utf-8") as handle:
        before = json.load(handle)
    problems = []
    for rel in sorted(set(before) | set(state)):
        if rel not in state:
            problems.append("removed: %s" % rel)
        elif rel not in before:
            problems.append("added: %s" % rel)
        elif before[rel] != state[rel]:
            problems.append("changed: %s" % rel)
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


def current_sha(root, rel):
    full = os.path.join(root, *rel.split("/"))
    return sha256_of(full) if os.path.isfile(full) else None


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
    only = normalise_paths(args.only)
    for rel in only:
        if paths and not under_paths(rel, paths):
            die("--only %s is outside the snapshot's paths (%s)" % (rel, ", ".join(paths)))
    excludes = manifest.get("excludes", [])
    expected = manifest.get("files", {})
    current = file_list(root, paths, excludes, report_symlinks=False, include_globs=manifest.get("includes", []),
                        skipped=[])
    in_scope = lambda rel: under_paths(rel, only)
    for rel in expected:
        if in_scope(rel) and not os.path.isfile(os.path.join(snapshot_dir, *rel.split("/"))):
            die("snapshot is missing a manifest file: %s" % rel)
    changed = sorted(rel for rel in expected if in_scope(rel) and current_sha(root, rel) != expected[rel]["sha256"])
    to_remove = sorted(rel for rel in current if in_scope(rel) and rel not in expected)
    if args.expect:
        later = load_manifest(manifest_path_for(os.path.abspath(args.expect))).get("files", {})
        foreign = []
        for rel in changed + to_remove:
            now = current_sha(root, rel)
            left = later.get(rel, {}).get("sha256")
            if now != left:
                foreign.append(rel)
        if foreign:
            for rel in foreign:
                print("not the loop's change: %s (matches neither snapshot)" % rel)
            die("%d file%s changed since the loop's snapshot %s, so someone else edited %s; narrow the restore with "
                "--only to the files the loop changed" % (len(foreign), "" if len(foreign) == 1 else "s",
                                                          args.expect, "it" if len(foreign) == 1 else "them"))
    backup_dir = os.path.abspath(args.backup_dir) if args.backup_dir else default_backup_dir(snapshot_dir)
    if args.dry_run:
        for rel in changed:
            print("would copy: %s" % rel)
        for rel in to_remove:
            print("would remove: %s" % rel)
        print("dry run: %d to copy, %d to remove%s; backups would go to %s"
              % (len(changed), len(to_remove), (" (only: %s)" % ", ".join(only)) if only else "", backup_dir))
        return 0
    copied = removed = backed_up = 0

    def backup(rel):
        src = os.path.join(root, *rel.split("/"))
        dst = os.path.join(backup_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    for rel in changed:
        dst = os.path.join(root, *rel.split("/"))
        now = current_sha(root, rel)
        if now == expected[rel]["sha256"]:
            continue
        if now is not None:
            backup(rel)
            backed_up += 1
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        src = os.path.join(snapshot_dir, *rel.split("/"))
        shutil.copyfile(src, dst)
        shutil.copymode(src, dst)
        copied += 1
    for rel in to_remove:
        full = os.path.join(root, *rel.split("/"))
        if os.path.isfile(full):
            backup(rel)
            backed_up += 1
            os.remove(full)
            removed += 1
    print("restored: %d copied, %d removed" % (copied, removed))
    if backed_up:
        print("backup: %d files -> %s" % (backed_up, backup_dir))
    else:
        print("backup: nothing differed, no backup written")
    return 0


def cmd_pair(args):
    sources = []
    for given in (args.first, args.second):
        directory = os.path.abspath(given)
        if not os.path.isdir(directory):
            die("snapshot dir not found: %s" % given)
        expected = load_manifest(manifest_path_for(directory)).get("files", {})
        actual = snapshot_contents(directory)
        changed = sorted(set(expected) ^ set(actual)) + sorted(
            rel for rel in set(expected) & set(actual) if sha256_of(actual[rel]) != expected[rel]["sha256"])
        if changed:
            die("%s no longer matches its manifest (%s); a pair must show what was scored"
                % (given, ", ".join(changed[:5])), 3)
        sources.append(directory)
    if bool(args.first_evidence) != bool(args.second_evidence):
        die("pass both --first-evidence and --second-evidence, or neither")
    views = {}
    for source, given in zip(sources, (args.first_evidence, args.second_evidence)):
        if given:
            if not os.path.isdir(given):
                die("evidence dir not found: %s" % given)
            if os.path.exists(os.path.join(given, "manifest.md")):
                die("%s holds manifest.md: pass the evidence.py view, not the evidence dir" % given)
            views[source] = os.path.abspath(given)
    out = os.path.abspath(args.out)
    evidence_out = out.rstrip(os.sep) + "-evidence"
    for target in (out, evidence_out):
        if os.path.exists(target) and os.listdir(target):
            die("not empty: %s" % target)
    order = list(sources)
    random.SystemRandom().shuffle(order)
    for label, source in zip(("a", "b"), order):
        shutil.copytree(source, os.path.join(out, label), symlinks=True)
        if source in views:
            os.makedirs(evidence_out, exist_ok=True)
            for rel, full in sorted(snapshot_contents(views[source]).items()):
                shutil.copy2(full, os.path.join(evidence_out, "%s-%s" % (label, rel.replace("/", "-"))))
    with open(out.rstrip(os.sep) + ".mapping", "w", encoding="utf-8") as handle:
        for label, source in zip(("a", "b"), order):
            handle.write("%s: %s\n" % (label, source))
    cmd_seal(argparse.Namespace(dir=out))
    manifest = load_manifest(manifest_path_for(out))
    manifest["root"] = ""
    write_manifest(manifest_path_for(out), manifest)
    print("paired: a/ and b/ in %s; the mapping is in %s.mapping (never show it to the judge)" % (out, out))
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
    create.add_argument("--path", action="append", default=[],
                        help="relative file or directory to include: the artifact and what it is checked against (repeatable)")
    create.add_argument("--out", default=None, help="snapshot directory to create; must not exist or must be empty")
    create.add_argument("--exclude", action="append", default=[], help="extra glob to exclude (repeatable)")
    create.add_argument("--include", action="append", default=[],
                        help="glob to keep although a credential or criteria-doc rule would drop it (repeatable)")
    create.add_argument("--whole-root", action="store_true",
                        help="allow a snapshot of all of ROOT above %d files or 50 MB" % WHOLE_ROOT_FILES)
    create.add_argument("--dry-run", action="store_true", help="list the files it would copy; create nothing")
    create.set_defaults(func=cmd_create)

    verify = sub.add_parser("verify", help="re-hash a snapshot against its manifest")
    verify.add_argument("--snapshot", required=True, help="snapshot directory")
    verify.add_argument("--manifest", default=None, help="manifest to compare against (default: <snapshot>.manifest.json)")
    verify.set_defaults(func=cmd_verify)

    seal = sub.add_parser("seal", help="write a manifest for an existing directory without copying")
    seal.add_argument("--dir", required=True, help="directory to seal; writes <dir>.manifest.json")
    seal.set_defaults(func=cmd_seal)

    fingerprint = sub.add_parser("fingerprint", help="hash the files in ROOT that a snapshot covers")
    fingerprint.add_argument("--snapshot", required=True, help="snapshot directory whose manifest names ROOT and the scope")
    action = fingerprint.add_mutually_exclusive_group(required=True)
    action.add_argument("--save", default=None, help="write the current state to this file")
    action.add_argument("--compare", default=None, help="compare the current state with this saved file")
    fingerprint.add_argument("--whole-root", action="store_true", help="cover all of ROOT, not only the snapshot's paths")
    fingerprint.set_defaults(func=cmd_fingerprint)

    restore = sub.add_parser("restore", help="copy a snapshot back over the artifact root, backing up what it replaces")
    restore.add_argument("--snapshot", required=True, help="snapshot directory")
    restore.add_argument("--root", required=True, help="artifact root to restore into")
    restore.add_argument("--only", action="append", default=[],
                         help="restore only this file or folder (repeatable): the files the loop changed")
    restore.add_argument("--expect", default=None,
                         help="later snapshot holding the state the loop left; refuse files that match neither")
    restore.add_argument("--dry-run", action="store_true", help="print the plan without changing anything")
    restore.add_argument("--force-root", action="store_true", help="allow a root that differs from the manifest's root")
    restore.add_argument("--backup-dir", default=None,
                         help="where to copy replaced and removed files (default: <LOOP>/restore-backup/<UTC stamp>/ "
                              "when the snapshot is under <LOOP>/snapshots/, else <snapshot>.restore-backup-<stamp>/)")
    restore.set_defaults(func=cmd_restore)

    pair = sub.add_parser("pair", help="stage two snapshots as a/ and b/ in random order for the blind judge")
    pair.add_argument("--first", required=True, help="one snapshot, for example LOOP/snapshots/round-0")
    pair.add_argument("--second", required=True, help="the other, for example the last scored round")
    pair.add_argument("--out", required=True, help="directory to create, for example LOOP/snapshots/pair")
    pair.add_argument("--first-evidence", default=None, help="the evidence.py view for --first")
    pair.add_argument("--second-evidence", default=None, help="the evidence.py view for --second")
    pair.set_defaults(func=cmd_pair)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
