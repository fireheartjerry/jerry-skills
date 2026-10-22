"""`dev.py bisect`: the first commit on the first-parent history where a command got slower.

The search runs in a throwaway `git worktree` (the user's tree, index and branch are never
touched): each probed commit is checked out there, built with `--build` when given, and the
command timed `--runs` times. The classifier is the midpoint of the good and bad medians;
the two ends must differ by more than twice the minimum effect (the saved A/A floor for this
command, or `--min-effect`), and a probe whose median lands within one minimum effect of the
midpoint is rerun with twice the runs before it is classified. A commit that fails to build
or run is skipped and its nearest untested neighbour tried instead. The worktree is removed
afterwards unless `--keep`.
"""
import math
import os
import shutil
import subprocess
import tempfile
import time

from devkit.common import (CommandFailed, DevError, Parser, StartError, UsageError, fmt_time,
                           note, print_json, repo_root)
from devkit.measure import MAX_TOTAL, median, runs_note, split_dashes, time_many

SCHEMA = "devkit-bisect/1"


def git(root, *args):
    p = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def resolve(root, ref):
    code, out, err = git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    if code != 0 or not out:
        raise UsageError("%s is not a commit in %s (%s)" % (ref, root, err or "unknown ref"))
    return out


def subject(root, sha):
    return git(root, "log", "-1", "--format=%s", sha)[1]


class Probe(object):
    """Checks out and times commits inside the throwaway worktree."""

    def __init__(self, wt, argv, build, runs, warmup, timeout):
        self.wt, self.argv, self.build = wt, argv, build
        self.runs, self.warmup, self.timeout = runs, warmup, timeout

    def measure(self, sha, runs=None):
        """(median or None, why None, walls)."""
        code, _, err = git(self.wt, "checkout", "--detach", "--quiet", "--force", sha)
        if code != 0:
            return None, "checkout failed: %s" % err.splitlines()[-1:], []
        git(self.wt, "clean", "-fdxq")
        cwd = os.getcwd()
        os.chdir(self.wt)
        try:
            if self.build:
                p = subprocess.run(["/bin/sh", "-c", self.build], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, universal_newlines=True)
                if p.returncode != 0:
                    return None, "build failed (exit %d)" % p.returncode, []
            try:
                walls, _ = time_many(self.argv, runs or self.runs, self.warmup,
                                     timeout=self.timeout)
            except (CommandFailed, StartError) as e:
                return None, "command failed: %s" % e.message, []
        finally:
            os.chdir(cwd)
        return median(walls), None, walls


def order_to_try(lo, hi, mid):
    """Untested indices strictly between lo and hi, nearest to mid first."""
    out = [mid]
    for d in range(1, hi - lo):
        for i in (mid - d, mid + d):
            if lo < i < hi:
                out.append(i)
    return out


def search(commits, classify):
    """Binary search for the first bad index. commits[-1] is known bad, the good end sits at
    index -1. classify(i) returns True (bad), False (good) or None (skipped). Returns
    (lo, hi, skipped): hi is the first bad index when hi - lo == 1, else the range left."""
    lo, hi, skipped = -1, len(commits) - 1, set()
    while hi - lo > 1:
        verdict = None
        for i in order_to_try(lo, hi, (lo + hi) // 2):
            if i in skipped:
                continue
            verdict = classify(i)
            if verdict is None:
                skipped.add(i)
                continue
            if verdict:
                hi = i
            else:
                lo = i
            break
        if verdict is None:
            break                           # every commit left in the range was skipped
    return lo, hi, sorted(skipped)


def cmd_bisect(args):
    p = Parser(prog="dev.py bisect",
               description="First commit where a command got slower: binary search over "
                           "good..bad in a throwaway git worktree, repeated runs per commit. "
                           "Usage: dev.py bisect --good REF --bad REF [options] -- CMD")
    p.add_argument("--good", required=True, help="a commit where the command was fast")
    p.add_argument("--bad", required=True, help="a later commit where it is slow")
    p.add_argument("--runs", type=int, default=5, help="timed runs per commit (default 5)")
    p.add_argument("--warmup", type=int, default=1, help="untimed runs per commit (default 1)")
    p.add_argument("--build", default=None, metavar="CMD",
                   help="shell command run in the worktree before timing each commit; a "
                        "failure skips that commit")
    p.add_argument("--min-effect", type=float, default=None, metavar="X",
                   help="smallest ratio change that counts (default: the saved A/A floor for "
                        "this command, else 0.05)")
    p.add_argument("--timeout", type=float, default=None, help="seconds per run")
    p.add_argument("--max-total", type=float, default=MAX_TOTAL, metavar="S",
                   help="stop when the two ends project the search past S seconds (default "
                        "%d; 0 disables)" % MAX_TOTAL)
    p.add_argument("--keep", action="store_true", help="keep the worktree and print its path")
    p.add_argument("--repo", default=None, help="repository root (default: git top level)")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    opts, argv = split_dashes(args)
    a = p.parse_args(opts)
    if not argv:
        raise UsageError("give the command after --, for example: dev.py bisect --good v1.2 "
                         "--bad HEAD -- python3 bench.py 40000")
    if a.runs < 3 or a.warmup < 0:
        raise UsageError("--runs must be >= 3 (a median of fewer is one noisy run) and "
                         "--warmup >= 0")
    if a.max_total < 0:
        raise UsageError("--max-total must be >= 0 (0 disables the cap)")
    root, root_note = repo_root(a.repo)
    if root_note:
        note(root_note)
    if git(root, "rev-parse", "--git-dir")[0] != 0:
        raise UsageError("%s is not a git repository" % root)
    good, bad = resolve(root, a.good), resolve(root, a.bad)
    if git(root, "merge-base", "--is-ancestor", good, bad)[0] != 0:
        raise UsageError("--good %s is not an ancestor of --bad %s" % (a.good, a.bad))
    listed = git(root, "rev-list", "--reverse", "--first-parent", "%s..%s" % (good, bad))[1]
    commits = [c for c in listed.splitlines() if c]
    if not commits:
        raise UsageError("no commits between --good and --bad")
    from devkit import compare
    min_effect, source = compare.resolve_min_effect(a.min_effect, root, [argv])
    probes = int(math.ceil(math.log(len(commits), 2))) if len(commits) > 1 else 0
    runs_note("bisect", "`%s`" % " ".join(argv),
              (a.runs + a.warmup) * (2 + probes))
    tmp = tempfile.mkdtemp(prefix="devkit-bisect-")
    wt = os.path.join(tmp, "wt")
    code, _, err = git(root, "worktree", "add", "--detach", "--quiet", wt, good)
    if code != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DevError("git worktree add failed: %s" % err)
    t0 = time.perf_counter()
    rows = []
    try:
        probe = Probe(wt, argv, a.build, a.runs, a.warmup, a.timeout)
        ends = {}
        for name, sha in (("good", good), ("bad", bad)):
            m, why, walls = probe.measure(sha)
            if m is None:
                raise CommandFailed("cannot time the %s end %s: %s" % (name, sha[:10], why))
            ends[name] = m
            rows.append({"commit": sha, "median": m, "runs": len(walls), "verdict": name,
                         "subject": subject(root, sha)})
        m_good, m_bad = ends["good"], ends["bad"]
        if m_bad <= m_good * (1 + 2 * min_effect):
            raise CommandFailed(
                "the bad end is not slower by more than twice the minimum effect: median %s "
                "at %s against %s at %s (%s); nothing to bisect, or raise --runs"
                % (fmt_time(m_bad), a.bad, fmt_time(m_good), a.good, source))
        spent = time.perf_counter() - t0
        projected = spent / 2.0 * probes
        if a.max_total and spent + projected > a.max_total:
            raise CommandFailed(
                "about %d more commits at about %s each would take %s, past --max-total %s; "
                "pass a larger --max-total, fewer --runs, or a narrower --good..--bad"
                % (probes, fmt_time(spent / 2.0), fmt_time(projected), fmt_time(a.max_total)))
        mid = (m_good + m_bad) / 2.0
        band = min_effect * m_good

        def classify(i):
            sha = commits[i]
            if i == len(commits) - 1:
                return True
            m, why, walls = probe.measure(sha)
            row = {"commit": sha, "subject": subject(root, sha)}
            if m is not None and abs(m - mid) < band:
                m2, why2, more = probe.measure(sha, 2 * a.runs)
                if m2 is not None:
                    walls = walls + more
                    m = median(walls)
                else:
                    m, why = None, why2
            if m is None:
                row.update(median=None, runs=0, verdict="skip", why=why)
                rows.append(row)
                return None
            row.update(median=m, runs=len(walls), verdict="bad" if m > mid else "good")
            rows.append(row)
            return m > mid

        lo, hi, skipped = search(commits, classify)
    finally:
        if a.keep:
            note("kept the worktree at %s; remove it with git worktree remove --force %s"
                 % (wt, wt))
        else:
            git(root, "worktree", "remove", "--force", wt)
            shutil.rmtree(tmp, ignore_errors=True)
            git(root, "worktree", "prune")
    found = hi - lo == 1
    first = commits[hi]
    before = commits[lo] if lo >= 0 else good
    by_sha = dict((r["commit"], r) for r in rows if r.get("median") is not None)
    result = {"schema": SCHEMA, "command": argv, "good": good, "bad": bad,
              "commits": len(commits), "min_effect": min_effect, "min_effect_source": source,
              "threshold": mid, "found": found, "first_bad": first if found else None,
              "range": None if found else [before, first], "skipped": [commits[i] for i in skipped],
              "probes": rows, "elapsed": time.perf_counter() - t0}
    if a.json:
        print_json(result)
        return 0 if found else 1
    print("%-10s %-10s %-5s %-7s %s" % ("commit", "median", "runs", "verdict", "subject"))
    for r in rows:
        print("%-10s %-10s %-5d %-7s %s" % (r["commit"][:10],
                                            fmt_time(r["median"]) if r["median"] else "n/a",
                                            r["runs"], r["verdict"], r["subject"][:60]))
    print("threshold %s, the midpoint of good %s and bad %s; %s" % (
        fmt_time(mid), fmt_time(m_good), fmt_time(m_bad), source))
    if found:
        after_m = by_sha.get(first, {}).get("median", m_bad)
        before_m = by_sha.get(before, {}).get("median", m_good)
        print("first slower commit: %s %s: median %s against %s at %s" % (
            first[:10], subject(root, first), fmt_time(after_m), fmt_time(before_m),
            before[:10]))
        print("Confirm it with dev.py bench on the two commits (worktrees, interleaved runs) "
              "before naming it in a report.")
        return 0
    print("not resolved: every commit between %s and %s failed to build or run; the "
          "slowdown is in that range" % (before[:10], first[:10]))
    return 1
