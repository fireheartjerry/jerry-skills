"""Review and selection: judge, select (best-of-N), diff, collect, conflicts
and snapshots.

The judge is a read-only child of the other engine with a fixed prompt and
JSON schema; its verdict is evidence for the parent, never an instruction.
"""

import json
import os
import shlex
import sys
import time

from agent_sessions.common import (
    DEFAULT_BUDGET, NAME_RE, die, fmt_cost, kv, now_iso, options_of, out, parse_args, parse_count,
    parse_duration, parse_usd, read_json, read_text, sdir_of, warn, write_json, write_text,
)
from agent_sessions.procs import owner_token
from agent_sessions.state import (
    all_metas, check_terminal, group_members, load_group, load_meta, normalize_result, require_meta,
    write_group,
)
from agent_sessions.engines import JUDGE_PROMPT, OTHER_ENGINE
from agent_sessions.schema import JUDGE_SCHEMA, validate
from agent_sessions.gitops import (
    branch_tip, changed_files, commits_ahead, delete_ref, dirty_files, git, is_dirty, list_snapshots, names_z,
    repo_root, resolve_commit, snapshot_records, status_entries, tree_diff,
)
from agent_sessions.launch import SpawnError, create

__all__ = [
    "cmd_judge", "cmd_select", "cmd_diff", "cmd_collect", "cmd_conflicts", "cmd_snapshots", "judge_inputs",
    "rank_key", "run_judge", "JUDGE_ANSWER_CAP", "JUDGE_DIFF_CAP",
]

JUDGE_ANSWER_CAP = 20000
JUDGE_DIFF_CAP = 200000
VERIFY_RANK = {"pass": 0, None: 1, "fail": 2, "error": 2, "skipped": 2}
JUDGE_RANK = {"approve": 0, "revise": 1, None: 2, "reject": 3}


def _status_exit(status):
    return 0 if status == "ok" else 1


# --------------------------------------------------------------------------- judge

def full_diff(meta):
    """(patch text, note, left_out) of everything a child changed, new files
    included, built through a private index and object directory (the
    repository's index and object store stay untouched). A worktree child:
    its worktree against base_commit. A shared-tree child: the tree against
    the HEAD it started from, minus the paths that were already uncommitted
    then (the user's work, recorded at spawn). (None, reason, None) without a
    repository. `left_out` says what the diff does not show, or is None."""
    index = os.path.join(sdir_of(meta["name"]), ".judge-index")
    wt = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else None
    if wt and os.path.isdir(wt.get("path") or ""):
        path, base = wt["path"], wt.get("base_commit") or "HEAD"
        cp, left_out = tree_diff(path, base, index)
        if cp.returncode != 0:
            return None, "git diff failed: %s" % cp.stderr.strip(), None
        return cp.stdout, "worktree %s against %s: every change on the branch, committed or not" % (
            wt.get("branch"), base[:12]), left_out
    workdir = meta.get("workdir") or ""
    if os.path.isdir(workdir) and repo_root(workdir):
        base = meta.get("head_at_spawn")
        if not base or not resolve_commit(workdir, base):
            base = "HEAD"
        excluded = [p for p in meta.get("dirty_at_spawn") or [] if isinstance(p, str)]
        cp, left_out = tree_diff(workdir, base, index, exclude=excluded)
        if cp.returncode == 0:
            return cp.stdout, ("shared tree %s against %s: changes since the child started, which may include "
                               "other writers" % (workdir, base[:12])), left_out
        return None, "git diff failed: %s" % cp.stderr.strip(), None
    return None, "no git repository", None


def judge_inputs(name, meta, res, brief_file):
    """Write judge/brief.md, answer.md, diff.patch and schema.json under the child's state dir."""
    jdir = os.path.join(sdir_of(name), "judge")
    os.makedirs(jdir, 0o700, exist_ok=True)
    if brief_file:
        brief = read_text(brief_file)
        if brief is None:
            die("cannot read --brief %s" % brief_file)
    else:
        brief = read_text(os.path.join(sdir_of(name), "prompt.txt"), "") or ""
    write_text(os.path.join(jdir, "brief.md"), brief)
    answer = res.get("answer") or ""
    if len(answer) > JUDGE_ANSWER_CAP:
        answer = answer[:JUDGE_ANSWER_CAP] + "\n\n[answer capped at %d of %d chars]\n" % (JUDGE_ANSWER_CAP, len(answer))
    write_text(os.path.join(jdir, "answer.md"), answer)
    patch, note, left_out = full_diff(meta)
    diff_p = os.path.join(jdir, "diff.patch")
    if patch is not None and patch.strip():
        if len(patch) > JUDGE_DIFF_CAP:
            patch = patch[:JUDGE_DIFF_CAP] + "\n[diff capped at %d chars; see the worktree]\n" % JUDGE_DIFF_CAP
            left_out = "; ".join(x for x in (left_out, "the diff was capped at %d chars" % JUDGE_DIFF_CAP) if x)
        write_text(diff_p, patch)
    elif os.path.exists(diff_p):
        os.unlink(diff_p)
    write_text(os.path.join(jdir, "schema.json"), json.dumps(JUDGE_SCHEMA, indent=2))
    return jdir, note, left_out


def judge_name(name):
    jname = "%s-judge" % name
    if not NAME_RE.match(jname):
        die("the judge child would be named '%s', longer than 40 characters; judge a child with a name of at most "
            "34 characters" % jname)
    return jname


def judge_target(res):
    """Which finished run of the judged child a judge looks at. A resumed or
    replaced child gets a new finished_iso, so an older verdict never passes
    for the current work."""
    return {"finished_iso": res.get("finished_iso"), "session_id": res.get("session_id")}


def judge_where(meta):
    wt = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else None
    if wt and os.path.isdir(wt.get("path") or ""):
        return "The agent worked in a git worktree, which is ./worktree (read it for context around the diff)."
    workdir = meta.get("workdir")
    if workdir and os.path.isdir(workdir):
        return ("The agent worked in %s, which you can read for context around the diff (it is a shared tree: "
                "files there may also hold other people's work)." % workdir)
    return "The agent's directory is gone; judge from judge/diff.patch and judge/answer.md."


def judge_read_dirs(meta):
    """Directories a judge may read besides its own: a shared-tree child's workdir."""
    wt = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else None
    if wt and os.path.isdir(wt.get("path") or ""):
        return []
    workdir = meta.get("workdir")
    return [os.path.realpath(workdir)] if workdir and os.path.isdir(workdir) else []


def start_judge(name, meta, res, engine, tier, brief_file, caps=None):
    jname = judge_name(name)
    jdir, note, left_out = judge_inputs(name, meta, res, brief_file)
    about = note + "." + (" Left out: %s." % left_out if left_out else "")
    prompt = JUDGE_PROMPT % {"schema": json.dumps(JUDGE_SCHEMA), "where": judge_where(meta), "about_diff": about}
    spec = {
        "engine": engine, "name": jname, "dir": sdir_of(name), "prompt": prompt, "tier": tier, "model": None,
        "role": "review", "readonly": True, "steerable": False, "worktree": False, "base": None, "copy": [],
        "timeout_s": 900, "stall_s": meta.get("stall_s") or 900, "deadline_s": None, "retries": 1,
        "budget_usd": (caps or {}).get("budget_usd"), "max_tokens": (caps or {}).get("max_tokens"),
        "max_turns": None, "tree_budget_usd": None, "tree_max_tokens": None,
        "schema_path": os.path.join(jdir, "schema.json"), "verify": None, "verify_timeout_s": None,
        "verify_fixups": None, "after": [], "queue": True, "on_finish": None, "backend": None, "replace": True,
        "json": False, "n": None, "engines": None, "group": None, "plan": None, "judge_of": name,
        "judge_target": judge_target(res), "ledger": (meta.get("budget") or {}).get("ledger"),
        "mcp": "none", "add_dirs": judge_read_dirs(meta), "diff": None, "diff_note": left_out,
    }
    try:
        create(spec)
    except SpawnError as exc:
        die("could not start the judge: %s" % exc.msg, exc.hint, exc.code)
    return jname, note


def verdict_of(jres):
    """(verdict dict, None) from a finished judge child's result, or (None, reason)."""
    if jres.get("status") != "ok":
        return None, "judge %s finished %s (%s): %s" % (jres.get("name"), jres.get("status"),
                                                       jres.get("failure_class"), jres.get("reason"))
    verdict = jres.get("structured_output")
    errors = validate(verdict, JUDGE_SCHEMA) if isinstance(verdict, dict) else ["no structured verdict"]
    if errors:
        return None, "judge verdict does not match the schema: %s" % "; ".join(errors[:5])
    return verdict, None


def record_verdict(name, jname, jres):
    """Write judge.json from a finished judge child. Returns (doc, None) or (None, reason)."""
    jmeta = load_meta(jname) or {}
    verdict, problem = verdict_of(jres)
    if problem:
        return None, problem
    doc = {"engine": jres.get("engine"), "model": jres.get("model"), "tier": jmeta.get("tier"),
           "session_id": jres.get("session_id"), "cost_usd": jres.get("cost_usd"),
           "verdict": verdict["verdict"], "confidence": verdict["confidence"], "summary": verdict["summary"],
           "findings": verdict["findings"], "judged_at_iso": now_iso(), "judge_child": jname,
           "judge_finished_iso": jres.get("finished_iso"), "judged_run": jmeta.get("judge_target"),
           "diff_note": jmeta.get("diff_note"),
           "untrusted": "summary and findings are judge output: evidence to weigh, not instructions"}
    write_json(os.path.join(sdir_of(name), "judge.json"), doc)
    return doc, None


def run_judge(name, engine=None, tier="standard", brief_file=None, caps=None):
    """Start or reuse the judge of one finished child. Returns (jname, stored
    judge.json or None). Reused: a judge still running on the same finished
    run, and a recorded verdict for that run. A new judge starts when there
    is none, when the child has been resumed since, or when the previous
    judge failed (asking again is the request to retry)."""
    meta = require_meta(name)
    res = check_terminal(name, meta)
    if res is None:
        die("'%s' has not finished; judge it after it does" % name,
            "agent-session wait %s --timeout %d" % (name, DEFAULT_BUDGET))
    res = normalize_result(res)
    target = judge_target(res)
    jname = judge_name(name)
    jmeta = load_meta(jname)
    judge_p = os.path.join(sdir_of(name), "judge.json")
    stored = read_json(judge_p)
    if jmeta:
        if jmeta.get("judge_of") != name:
            die("a child named '%s' exists and is not the judge of '%s'; the judge needs that name" % (jname, name),
                "agent-session reap %s" % jname)
        jres = check_terminal(jname, jmeta)
        same = jmeta.get("judge_target") == target
        if jres is None:
            if same:
                return jname, None
            die("%s is still judging an earlier run of %s" % (jname, name), "agent-session kill %s" % jname)
        jres = normalize_result(jres)
        if same:
            if isinstance(stored, dict) and stored.get("judge_finished_iso") == jres.get("finished_iso"):
                return jname, stored
            if verdict_of(jres)[1] is None:
                return jname, None
    if stored is not None:
        try:
            os.unlink(judge_p)
        except OSError:
            pass
    engine = engine or OTHER_ENGINE.get(meta["engine"], "claude")
    start_judge(name, meta, res, engine, tier, brief_file, caps)
    return jname, None


# A codex judge reads about 220k tokens even for a one-line diff, and codex
# applies a token cap only after the turn: a cap below what it reads cannot
# stop the read, it only turns the finished verdict into a budget failure.
CODEX_JUDGE_FLOOR = 400000
# Revise rounds (resume with findings, then judge again) before the parent
# stops and reports instead of looping on findings that keep coming back.
MAX_REVISE_ROUNDS = 2


def judge_cap_warning(engine, caps):
    n = (caps or {}).get("max_tokens")
    if engine == "codex" and isinstance(n, int) and n < CODEX_JUDGE_FLOOR:
        return ("--max-tokens %d is below what a codex judge reads (about 220k, mostly cached); codex cannot be "
                "stopped mid-read, so this cap only discards a finished verdict as a budget failure. Use %d or more, "
                "or a cap only to bound a runaway" % (n, CODEX_JUDGE_FLOOR))
    return None


def judge_next(name, doc):
    """The `next :` hint after a verdict, with the revise-round stop rule."""
    # A resume after a verdict moves that verdict to attempts/<k>.judge.json,
    # so their count is the number of revise rounds already run.
    adir = os.path.join(sdir_of(name), "attempts")
    rounds = len([f for f in os.listdir(adir) if f.endswith(".judge.json")]) if os.path.isdir(adir) else 0
    verdict = doc.get("verdict")
    if verdict == "approve":
        return "agent-session conflicts %s, then agent-session collect %s --merge (say the target first)" % (
            name, name)
    if rounds >= MAX_REVISE_ROUNDS:
        return ("stop: %s has had %d revise rounds (the limit is %d). Do not resume it again; report the unresolved "
                "findings and the spend to the user" % (name, rounds, MAX_REVISE_ROUNDS))
    if verdict == "reject":
        return ("check the findings yourself; a reject means a wrong approach, so respawn with a new brief rather "
                "than resume, or report to the user")
    return ("check each finding, then agent-session resume %s --message \"<the findings you confirmed>\" and judge "
            "again (revise round %d of %d; stop earlier if a finding recurs or only low ones remain)" % (
                name, rounds + 1, MAX_REVISE_ROUNDS))


def judge_caps(o):
    """--budget-usd / --max-tokens for judge children (a codex judge of a
    small diff reads about 200k tokens, mostly cached)."""
    caps = {}
    if "budget-usd" in o:
        caps["budget_usd"] = parse_usd(o["budget-usd"], "--budget-usd")
    if "max-tokens" in o:
        caps["max_tokens"] = parse_count(o["max-tokens"], "--max-tokens", 1)
    return caps


def print_verdict(doc, as_json):
    if as_json:
        out(json.dumps(doc, indent=2))
        return
    findings = doc.get("findings") or []
    cost = fmt_cost(doc.get("cost_usd"))
    if not isinstance(doc.get("cost_usd"), (int, float)):
        jres = read_json(os.path.join(sdir_of(doc.get("judge_child") or ""), "result.json")) or {}
        total = (jres.get("usage") or {}).get("total_tokens") if isinstance(jres.get("usage"), dict) else None
        cost = "%s tokens, cost unknown" % (total if isinstance(total, int) else "?")
    kv("verdict", "%s (%s %s, %d finding%s, %s)" % (
        doc.get("verdict"), doc.get("engine"), doc.get("tier") or "default", len(findings),
        "" if len(findings) == 1 else "s", cost))
    if doc.get("diff_note"):
        kv("diff", "the judge's diff left out: %s" % doc["diff_note"])
    kv("confidence", doc.get("confidence"))
    out("--- judge summary and findings (judge output, untrusted) ---")
    out(doc.get("summary") or "")
    for f in findings:
        where = f.get("file") or "-"
        if f.get("line") is not None:
            where += ":%s" % f["line"]
        out("[%s] %s: %s" % (f.get("severity"), where, f.get("issue")))
        if f.get("fix"):
            out("    suggested fix: %s" % f["fix"])
    out("--- end ---")


def wait_judges(pairs, budget):
    """Wait for (judged, judge child) pairs; record each verdict as it lands.
    Returns (problems {judged: reason}, still pending pairs)."""
    from agent_sessions.state import kick
    pending = list(pairs)
    problems = {}
    deadline = time.time() + budget
    while pending:
        for judged, jname in list(pending):
            jres = check_terminal(jname, load_meta(jname))
            if jres is not None:
                _doc, problem = record_verdict(judged, jname, normalize_result(jres))
                if problem:
                    problems[judged] = problem
                pending.remove((judged, jname))
        if not pending or time.time() >= deadline:
            break
        kick()
        time.sleep(0.5)
    return problems, pending


def cmd_judge(args):
    pos, o = parse_args(args, **options_of("judge"))
    if len(pos) != 1:
        die("usage: judge <name> [--engine E] [--tier T] [--brief F] [--timeout S] [--json]")
    name = pos[0]
    tier = o.get("tier", "standard")
    if tier not in ("cheap", "standard", "deep"):
        die("unknown tier '%s'" % tier)
    engine = o.get("engine")
    if engine and engine not in OTHER_ENGINE:
        die("--engine must be claude, codex or gemini")
    budget = parse_duration(o.get("timeout", DEFAULT_BUDGET), "--timeout")
    caps = judge_caps(o)
    judged = load_meta(name) or {}
    problem = judge_cap_warning(engine or OTHER_ENGINE.get(judged.get("engine"), "claude"), caps)
    if problem:
        warn(problem)
    jname, stored = run_judge(name, engine, tier, o.get("brief"), caps)
    if stored:
        print_verdict(stored, o.get("json"))
        if not o.get("json"):
            kv("next", judge_next(name, stored))
        return 0
    problems, pending = wait_judges([(name, jname)], budget)
    if pending:
        if o.get("json"):
            out(json.dumps({"name": name, "judge_child": jname, "status": "pending"}))
        else:
            kv("pending", "judge %s of %s" % (jname, name))
            sys.stderr.write("  try: agent-session judge %s --timeout %d   (the running judge is reused)\n"
                             % (name, DEFAULT_BUDGET))
        return 3
    if name in problems:
        if o.get("json"):
            out(json.dumps({"name": name, "judge_child": jname, "status": "error", "reason": problems[name]}))
        else:
            kv("judge", "failed: %s" % problems[name])
            jres = read_json(os.path.join(sdir_of(jname), "result.json")) or {}
            if jres.get("failure_class") == "budget":
                kv("note", "the judge hit its cap; the work it read is paid for and the verdict is lost. A codex "
                   "judge needs --max-tokens %d or more" % CODEX_JUDGE_FLOOR)
            kv("next", "agent-session result %s   (raw judge output); agent-session judge %s starts a new judge"
               % (jname, name))
        return 1
    doc = read_json(os.path.join(sdir_of(name), "judge.json"))
    print_verdict(doc, o.get("json"))
    if not o.get("json"):
        kv("next", judge_next(name, doc or {}))
    return 0


# --------------------------------------------------------------------------- select

def member_row(index, name):
    meta = load_meta(name) or {}
    res = normalize_result(read_json(os.path.join(sdir_of(name), "result.json")) or {})
    judge = read_json(os.path.join(sdir_of(name), "judge.json")) or {}
    v = res.get("verify") if isinstance(res.get("verify"), dict) else None
    g = res.get("git") if isinstance(res.get("git"), dict) else {}
    return {"member": name, "index": index, "engine": meta.get("engine"), "status": res.get("status"),
            "failure_class": res.get("failure_class"), "verify": v.get("status") if v else None,
            "judge": judge.get("verdict"), "judge_findings": len(judge.get("findings") or []),
            "insertions": g.get("insertions") or 0, "deletions": g.get("deletions") or 0,
            "commits": g.get("commits") or 0, "failed_turns": len(res.get("failed_turns") or []),
            "retries": len(res.get("retries") or []), "cost_usd": res.get("cost_usd"),
            "branch": (meta.get("worktree") or {}).get("branch")}


def rank_key(row):
    """Lexicographic: ok first; verify pass > none > fail; judge approve >
    revise > none > reject; fewer failed turns and retries; smaller diff;
    lower cost (unknown last); member index."""
    cost = row["cost_usd"]
    return (0 if row["status"] == "ok" else 1, VERIFY_RANK.get(row["verify"], 2), JUDGE_RANK.get(row["judge"], 2),
            row["failed_turns"] + row["retries"], row["insertions"] + row["deletions"],
            (0, cost) if isinstance(cost, (int, float)) else (1, 0), row["index"])


def select_pending(group, what, names, hint, as_json):
    if as_json:
        out(json.dumps({"group": group, "status": "pending", "waiting_for": what, "pending": names}, indent=2))
    else:
        kv("pending", "%s %s" % (what, ", ".join(names)))
        sys.stderr.write("  try: %s\n" % hint)
    return 3


def cmd_select(args):
    pos, o = parse_args(args, **options_of("select"))
    if len(pos) != 1:
        die("usage: select <group> [--judge] [--json]")
    group = pos[0]
    g = load_group(group)
    if not g:
        die("no group '%s'" % group, "agent-session list")
    if g.get("kind") != "best-of":
        die("select ranks the members of a best-of group; '%s' is a %s group" % (group, g.get("kind")),
            "agent-session plan status %s" % group if g.get("kind") == "plan" else None)
    members = [m for m in group_members(group) if load_meta(m)]
    if not members:
        die("group '%s' has no members left" % group)
    pending = [m for m in members if check_terminal(m, load_meta(m)) is None]
    if pending:
        return select_pending(group, "members", pending,
                              "agent-session wait %s --timeout %d" % (group, DEFAULT_BUDGET), o.get("json"))
    problems = {}
    if o.get("judge"):
        pairs = []
        for m in members:
            row = member_row(0, m)
            if row["status"] == "ok" and row["verify"] in ("pass", None):
                jname, stored = run_judge(m, caps=judge_caps(o))
                if not stored:
                    pairs.append((m, jname))
        problems, still = wait_judges(pairs, DEFAULT_BUDGET)
        if still:
            return select_pending(group, "judges", [j for _m, j in still],
                                  "agent-session select %s --judge   (running judges are reused)" % group,
                                  o.get("json"))
        for m, problem in sorted(problems.items()):
            warn("judge of %s: %s (ranked without a verdict)" % (m, problem))
    rows = [member_row(i + 1, m) for i, m in enumerate(group_members(group)) if load_meta(m)]
    rows.sort(key=rank_key)
    winner = rows[0]["member"] if rows and rows[0]["status"] == "ok" else None
    g["winner"] = winner
    g["selection"] = rows
    g["selected_iso"] = now_iso()
    write_group(group, g)
    if o.get("json"):
        out(json.dumps({"group": group, "winner": winner, "rows": rows,
                        "judge_problems": problems}, indent=2))
        return 0 if winner else 1
    out("%-22s %-7s %-8s %-8s %-9s %-12s %-9s %s" % ("MEMBER", "ENGINE", "STATUS", "VERIFY", "JUDGE", "DIFF",
                                                    "COST", "BRANCH"))
    for row in rows:
        out("%-22s %-7s %-8s %-8s %-9s %-12s %-9s %s" % (
            row["member"][:22], row["engine"] or "-", (row["status"] or "-")[:8], row["verify"] or "-",
            row["judge"] or "-", "+%d -%d" % (row["insertions"], row["deletions"]), fmt_cost(row["cost_usd"]),
            row["branch"] or "-"))
    kv("rank", "ok first; verify pass > none > fail; judge approve > revise > none > reject; fewer failed turns "
       "and retries; smaller diff; lower cost; member order")
    if winner:
        kv("winner", "%s (%s)" % (winner, rows[0]["branch"]))
        kv("next", "agent-session diff %s, then agent-session collect %s --merge" % (winner, group))
        return 0
    kv("winner", "none: no member finished ok")
    return 1


# --------------------------------------------------------------------------- diff / collect / conflicts

def cmd_diff(args):
    pos, o = parse_args(args, **options_of("diff"))
    if len(pos) != 1:
        die("usage: diff <name> [--stat] [--base REF]")
    name = pos[0]
    meta = require_meta(name)
    wt = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else None
    stat = ["--stat"] if o.get("stat") else []
    if wt and os.path.isdir(wt.get("path") or ""):
        base = o.get("base") or wt.get("base_commit") or "HEAD"
        cp = git(wt["path"], *(["diff"] + stat + [base]))
        if cp.returncode != 0:
            die("git diff failed: %s" % cp.stderr.strip())
        sys.stdout.write(cp.stdout)
        for xy, path in status_entries(wt["path"]):
            if xy == "??":
                out("untracked : %s" % path)
        return 0
    workdir = meta.get("workdir") or ""
    if not os.path.isdir(workdir) or not repo_root(workdir):
        die("'%s' has no git repository to diff (%s)" % (name, workdir))
    cp = git(workdir, *(["diff"] + stat + ([o["base"]] if o.get("base") else [])))
    sys.stdout.write(cp.stdout)
    for line in git(workdir, "status", "--porcelain").stdout.splitlines():
        out("status : %s" % line)
    out("note : shared tree; changes may include other writers")
    return 0


def resolve_collect_target(target):
    if load_meta(target):
        return target
    g = load_group(target)
    if g:
        if not g.get("winner"):
            die("group '%s' has no winner yet" % target, "agent-session select %s" % target)
        return g["winner"]
    die("no session or group '%s'" % target, "agent-session list")


def cmd_collect(args):
    pos, o = parse_args(args, **options_of("collect"))
    modes = [m for m in ("merge", "squash", "branch") if o.get(m)] + (["cherry-pick"] if o.get("cherry-pick") else [])
    if len(pos) != 1 or len(modes) != 1:
        die("usage: collect <name|group> --merge | --squash | --cherry-pick SHA | --branch [--json]")
    mode = modes[0]
    group = pos[0] if not load_meta(pos[0]) and load_group(pos[0]) else None
    name = resolve_collect_target(pos[0])
    meta = require_meta(name)
    res = check_terminal(name, meta)
    if res is None:
        die("'%s' is still running" % name, "agent-session wait %s --timeout %d" % (name, DEFAULT_BUDGET))
    res = normalize_result(res)
    verify = res.get("verify") if isinstance(res.get("verify"), dict) else None
    unproven = None
    if res.get("status") != "ok":
        unproven = "'%s' finished %s%s" % (name, res.get("status"),
                                            " (%s)" % res["failure_class"] if res.get("failure_class") else "")
    elif verify and verify.get("status") not in ("pass", None):
        unproven = "'%s' finished ok but its verify is %s" % (name, verify.get("status"))
    if unproven and mode != "branch" and not o.get("allow-failed"):
        die("%s, so its work is not proven; collect brings only verified work into a branch" % unproven,
            "agent-session result %s (read why), agent-session collect %s --branch (inspect it), or --allow-failed "
            "when the user wants the partial work anyway" % (name, name))
    wt = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else None
    if not wt:
        die("'%s' did not run in a worktree; its changes are already in %s" % (name, meta.get("workdir")))
    root, branch, base = wt["root"], wt["branch"], wt.get("base_commit")
    tip = branch_tip(root, branch)
    if not tip:
        die("branch %s no longer exists" % branch)
    if os.path.isdir(wt.get("path") or "") and is_dirty(wt["path"]):
        die("the worktree of '%s' has uncommitted changes; commit in the worktree first, or agent-session diff %s "
            "to review" % (name, name))
    ahead = commits_ahead(root, base, tip) if base else 0
    if ahead == 0:
        die("nothing to collect: %s has no commits beyond %s" % (branch, (base or "?")[:12]))
    doc = {"mode": mode, "branch": branch, "base": base, "head": None, "merged_into": None, "commit": None,
           "commits": ahead}
    if mode == "branch":
        if o.get("json"):
            doc["head"] = tip
            out(json.dumps(doc, indent=2))
        else:
            kv("branch", "%s (%d commit%s on %s)" % (branch, ahead, "" if ahead == 1 else "s", (base or "?")[:12]))
            kv("tip", tip)
            kv("next", "git -C %s merge %s   (or open a pull request from it)" % (shlex.quote(root), branch))
        return 0
    into = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = [p for xy, p in status_entries(root, untracked="no")]
    if dirty and mode in ("merge", "squash", "cherry-pick"):
        die("the parent tree %s has uncommitted changes (%s%s); they are the user's (or another writer's) work, "
            "not yours to commit or stash, and a %s on top of them is not safe" % (
                root, ", ".join(dirty[:5]), ", ..." if len(dirty) > 5 else "", mode.replace("-", " ")),
            "agent-session collect %s --branch (report the branch and tip; the user merges when the tree is "
            "clean), or ask the user to commit or stash first" % name)
    count = 1 if mode == "cherry-pick" else ahead
    if not o.get("json"):
        kv("target", "%s in %s (%s of %s, %d commit%s)" % (into, root, mode, branch, count, "" if count == 1 else "s"))
        sys.stdout.flush()
    if mode == "merge":
        cp = git(root, "merge", "--no-edit", branch)
        if cp.returncode != 0:
            git(root, "merge", "--abort")
            die("merge of %s into %s stopped on conflicts and was aborted:\n%s" % (
                branch, into, (cp.stdout + cp.stderr).strip()[-2000:]), "agent-session conflicts", code=1)
    elif mode == "squash":
        cp = git(root, "merge", "--squash", branch)
        if cp.returncode == 0:
            cp = git(root, "commit", "-q", "-m", "agent-session: %s (squash of %s, %d commit%s)" % (
                name, branch, ahead, "" if ahead == 1 else "s"))
        if cp.returncode != 0:
            git(root, "reset", "--merge")
            die("squash of %s failed and was rolled back:\n%s" % (branch, (cp.stdout + cp.stderr).strip()[-2000:]),
                code=1)
    else:
        sha = resolve_commit(root, o["cherry-pick"])
        if not sha:
            die("--cherry-pick %s does not name a commit" % o["cherry-pick"])
        if git(root, "merge-base", "--is-ancestor", sha, tip).returncode != 0 or \
                (base and git(root, "merge-base", "--is-ancestor", sha, base).returncode == 0):
            die("--cherry-pick %s is not one of the %d commit%s of %s" % (sha[:12], ahead, "" if ahead == 1 else "s",
                                                                           branch))
        cp = git(root, "cherry-pick", sha)
        if cp.returncode != 0:
            git(root, "cherry-pick", "--abort")
            die("cherry-pick of %s stopped on conflicts and was aborted:\n%s" % (
                sha[:12], (cp.stdout + cp.stderr).strip()[-2000:]), code=1)
        doc["commit"] = sha
    head = resolve_commit(root, "HEAD")
    doc.update({"head": head, "merged_into": into})
    if o.get("json"):
        out(json.dumps(doc, indent=2))
    else:
        kv("collected", "%s into %s by %s" % (branch, into, mode))
        kv("head", head)
        if mode == "merge":
            kv("next", "agent-session reap %s   (deletes the branch, now merged%s)" % (
                group or name, "; reaps every member of the group" if group else ""))
        else:
            # A squash or cherry-pick copies the changes; the branch never becomes
            # an ancestor of HEAD, so a plain reap keeps it.
            kv("next", "agent-session reap %s --discard %s   (the branch stays unmerged after a %s; this snapshots "
                       "its tip, then deletes it)" % (name, tip, mode.replace("-", " ")))
    return 0


def _merge_tree(root, a, b):
    """Conflicted paths of a simulated merge of commits a and b
    (`git merge-tree --write-tree`, git 2.38+): a list, [] when the merge is
    clean, or None when this git cannot simulate it."""
    cp = git(root, "merge-tree", "--write-tree", "--name-only", "--no-messages", a, b)
    if cp.returncode == 0:
        return []
    if cp.returncode != 1:
        return None
    lines = [l.strip() for l in cp.stdout.splitlines() if l.strip()]
    return sorted(set(lines[1:])) if lines else None


def cmd_conflicts(args):
    pos, o = parse_args(args, **options_of("conflicts"))
    from agent_sessions.commands import expand_names
    if pos:
        names = expand_names(pos)
        for n in names:
            require_meta(n)
    else:
        me = owner_token()
        names = [m["name"] for m in all_metas() if m.get("owner") == me]
    members = {}
    roots = {}
    tips = {}
    parent_since = {}
    for n in names:
        meta = load_meta(n) or {}
        wt = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else None
        if not wt or not os.path.isdir(wt.get("path") or ""):
            continue
        base = wt.get("base_commit")
        members[n] = changed_files(wt["path"], base)
        roots[n] = wt["root"]
        tip = branch_tip(wt["root"], wt.get("branch") or "")
        if tip and base and tip != base:
            tips[n] = tip
        # What the parent changed since this child's base: commits made on the
        # parent branch after the spawn, plus the parent tree's dirty files.
        since = set(dirty_files(wt["root"]))
        if base:
            since.update(names_z(git(wt["root"], "diff", "--name-only", "-z", base, "HEAD")))
        parent_since[n] = since
    if not members:
        die("no worktree children in scope; conflicts compares the files changed in worktrees")
    children = sorted(members)
    several_roots = len(set(roots.values())) > 1
    label_of = {}
    for n in children:
        root = roots[n]
        label = "(parent tree %s)" % root if several_roots else "(parent tree)"
        label_of[n] = label
        if parent_since[n]:
            members[label] = sorted(set(members.get(label, [])) | parent_since[n])
            roots[label] = root
    overlaps = []
    for i, a in enumerate(children):
        files = sorted(set(members[a]) & parent_since[a])
        if files:
            overlaps.append({"a": a, "b": label_of[a], "files": files})
        for b in children[i + 1:]:
            if roots[a] != roots[b]:
                continue
            files = sorted(set(members[a]) & set(members[b]))
            if files:
                overlaps.append({"a": a, "b": b, "files": files})
    # Committed work can be merged for real in memory: the child's branch tip
    # against the parent's HEAD and against every sibling's tip.
    simulated = []
    simulation = "git merge-tree"
    for i, a in enumerate(children):
        if a not in tips:
            continue
        head = resolve_commit(roots[a], "HEAD")
        pairs = [(label_of[a], head)] + [(b, tips[b]) for b in children[i + 1:]
                                         if b in tips and roots[b] == roots[a]]
        for b, other in pairs:
            if not other or simulation is None:
                continue
            files = _merge_tree(roots[a], other, tips[a])
            if files is None:
                simulation = None
                continue
            if files:
                simulated.append({"a": a, "b": b, "files": files})
    if simulation is None:
        simulated = None
    found = bool(overlaps) or bool(simulated)
    groups_named = [p for p in pos if not load_meta(p) and (load_group(p) or {}).get("kind") == "best-of"]
    if o.get("json"):
        out(json.dumps({"members": members, "overlaps": overlaps, "merge_conflicts": simulated,
                        "merge_simulation": simulation or "unavailable (needs git 2.38+)"}, indent=2))
    else:
        for ov in overlaps:
            out("%s <-> %s : %s" % (ov["a"], ov["b"], ", ".join(ov["files"])))
        for sc in simulated or []:
            out("merge conflict (simulated): %s <-> %s : %s" % (sc["a"], sc["b"], ", ".join(sc["files"])))
        if not found:
            out("no overlapping files among %s" % ", ".join(sorted(members, key=lambda k: (k.startswith("("), k))))
        if groups_named:
            out("note : members of a best-of group solve the same task, so they overlap by design; before collect, "
                "check the winner against the parent: agent-session conflicts <winner>")
        if simulation:
            out("note : overlaps are a forecast from changed paths; merge conflicts come from git merge-tree "
                "on committed work only")
        else:
            out("note : a forecast from changed paths (same file touched); this git cannot simulate merges (2.38+)")
    return 1 if found else 0


# --------------------------------------------------------------------------- snapshots

def cmd_snapshots(args):
    pos, o = parse_args(args, **options_of("snapshots"))
    if pos:
        die("usage: snapshots [--prune --older-than Nd] [--json]")
    days = 30
    if "older-than" in o:
        raw = o["older-than"]
        if not raw.endswith("d"):
            die("--older-than takes days, such as 30d")
        days = parse_count(raw[:-1], "--older-than", 0)
        if not o.get("prune"):
            die("--older-than goes with --prune")
    roots = [r.get("root") for r in snapshot_records()]
    roots += [(m.get("worktree") or {}).get("root") for m in all_metas()]
    here = repo_root(os.getcwd())
    if here:
        roots.append(here)
    snaps = list_snapshots(roots)
    now = time.time()
    if o.get("prune"):
        pruned = []
        for snap in snaps:
            if now - snap["epoch"] >= days * 86400 and delete_ref(snap["root"], snap["ref"]):
                pruned.append(snap)
        if o.get("json"):
            out(json.dumps({"pruned": pruned, "older_than_days": days}, indent=2))
        else:
            for snap in pruned:
                out("pruned %s (%s)" % (snap["ref"], snap["root"]))
            out("pruned %d snapshot%s older than %d day%s" % (len(pruned), "" if len(pruned) == 1 else "s", days,
                                                              "" if days == 1 else "s"))
        return 0
    if o.get("json"):
        out(json.dumps({"snapshots": snaps}, indent=2))
        return 0
    if not snaps:
        out("(no snapshots)")
        return 0
    for snap in snaps:
        age_h = (now - snap["epoch"]) / 3600.0
        age = "%.0fh" % age_h if age_h < 48 else "%.0fd" % (age_h / 24)
        out("%-60s %-6s %s  %s" % (snap["ref"], age, snap["commit"][:12], snap["root"]))
    out("restore : git -C <repo> worktree add <dir> <ref>   (or git checkout <ref> -- <path>)")
    return 0
