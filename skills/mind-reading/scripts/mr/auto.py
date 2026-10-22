"""refresh --auto: the unattended, capped, low-effort refresh; refresh --rollback: one command back.

One run, in order, under a lock (auto/lock) and one spend cap (auto.max_cost_usd, default 3 USD, and auto.max_calls):

1. extract: new transcript records are appended (the corpus is append-only).
2. track: the correction tracker (no engine).
3. gate on the live model: when its correction rate lies above its parent's 95% interval (auto.min_invocations under
   each), the run builds nothing; with auto.rollback_on_regression (default) it rolls back to the parent.
4. prep with pinned chunks (incremental.py): existing chunks keep their bytes; only new material gets new chunks, once it
   reaches auto.min_new_chars. A held-out split stays frozen; a no-test split advances train_before to now minus
   auto.settle_hours (auto.advance_train_before).
5. mine only the units whose chunk is new or changed, with the auto models (Sonnet-class by default), reserving
   auto.synth_reserve_usd for synthesis. A budget stop ends the run as `incomplete`; the next run resumes the rest.
6. stage: the live model files are copied to auto/staging/model; synthesize --update (the previous profile as prior) and
   the fold of reviewed corrections (corrections.md lines; `# pending` drafts are comments and never folded) run there.
7. lint gate: no forbidden rule, and no lint finding the live model does not already have.
8. swap: only when no live model file changed during the run. The live files are archived (model/archive/<hash>/),
   the staged ones moved in (MODEL.json last), the swap recorded in model/history.jsonl, and archives the auto refresh
   made beyond auto.keep_versions are pruned.

Every run writes auto/last.json and appends to auto/runs.jsonl; the CLI also appends its log to logs/refresh-auto.log.
Outcomes: swapped, nothing-new, deferred (new material below min_new_chars), incomplete (budget or engine stop; resumes),
held (a gate refused the swap), rolled-back, locked, error."""
import datetime as _dt
import json
import os
import shutil
import time
from pathlib import Path

from . import budget as _budget
from . import engine as _engine
from .config import Paths, read_json, read_jsonl, write_json
from .corpus import now_iso

MODEL_FILES = ("light.md", "profile.md", "examples.md", "grants.md", "index.json")
CORE_FILES = ("light.md", "profile.md", "examples.md")
LOCK_STALE_SECONDS = 6 * 3600


class AutoError(Exception):
    pass


class AutoLocked(AutoError):
    pass


# ---------------------------------------------------------------- lock

class Lock:
    """auto/lock holds {"pid", "started"}. A lock whose process is gone or that is older than six hours is taken over."""

    def __init__(self, paths):
        self.path = paths.auto / "lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        if self.path.exists():
            info = read_json(self.path, {}) or {}
            pid, started = info.get("pid"), info.get("epoch") or 0
            if _alive(pid) and time.time() - float(started) < LOCK_STALE_SECONDS:
                raise AutoLocked("another refresh --auto is running (pid %s since %s)" % (pid, info.get("started")))
        write_json(self.path, {"pid": os.getpid(), "started": now_iso(), "epoch": time.time()})
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------- staging and swap

def staged_paths(paths, root):
    """A Paths whose model files live under root/model; corpus, chunks, mined runs and engine runs stay the live ones."""
    sp = Paths(paths.home)
    m = Path(root) / "model"
    sp.model, sp.model_json, sp.light, sp.profile = m, m / "MODEL.json", m / "light.md", m / "profile.md"
    sp.examples, sp.index, sp.corrections, sp.grants = m / "examples.md", m / "index.json", m / "corrections.md", m / "grants.md"
    sp.decisions, sp.archive, sp.history = m / "decisions.jsonl", m / "archive", m / "history.jsonl"
    return sp


def fingerprints(paths):
    """(size, mtime_ns) of every live model file a swap would replace, plus MODEL.json."""
    out = {}
    for n in MODEL_FILES + ("MODEL.json",):
        p = paths.model / n
        try:
            st = os.stat(p)
            out[n] = [st.st_size, st.st_mtime_ns]
        except OSError:
            out[n] = None
    return out


def stage(paths, root):
    """Copy the live model into the staging area; returns (staged paths, fingerprints of the live files)."""
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    sp = staged_paths(paths, root)
    sp.model.mkdir(parents=True, exist_ok=True)
    for n in MODEL_FILES + ("MODEL.json", "corrections.md", "history.jsonl"):
        src = paths.model / n
        if src.exists():
            shutil.copy2(src, sp.model / n)
    return sp, fingerprints(paths)


def _replace(src, dst):
    tmp = dst.with_name(dst.name + ".swap-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def swap(paths, cfg, sp, event="auto", log=print):
    """Archive the live model, move the staged files in (MODEL.json last), record the swap, prune old auto archives."""
    from .synthesize import archive_previous, record_history
    live = read_json(paths.model_json, {}) or {}
    archived = archive_previous(paths, log)
    for n in MODEL_FILES:
        src = sp.model / n
        if src.exists():
            _replace(src, paths.model / n)
    _replace(sp.model_json, paths.model_json)
    new = read_json(paths.model_json, {}) or {}
    row = record_history(paths, new.get("model_hash"), live.get("model_hash"), event,
                         archived=live.get("model_hash") if archived else None)
    prune_versions(paths, int((cfg.get("auto") or {}).get("keep_versions", 5)), log)
    return row


def prune_versions(paths, keep, log=print):
    """Delete archive/<hash>/ directories this module archived, beyond the newest `keep`. Archives made by hand-run
    synthesize or refresh are never touched, nor the live model's own hash."""
    rows = read_jsonl(paths.history)
    archived = [r.get("archived") for r in rows if r.get("event") in ("auto", "auto-rollback", "rollback") and r.get("archived")]
    seen, order = set(), []
    for h in reversed(archived):
        if h not in seen:
            seen.add(h)
            order.append(h)
    live = (read_json(paths.model_json, {}) or {}).get("model_hash")
    removed = []
    for h in order[max(0, keep):]:
        d = paths.archive / h
        if h != live and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(h)
    if removed:
        log("refresh: pruned %d old model version(s) beyond the newest %d" % (len(removed), keep))
    return removed


def versions(paths):
    """Archived versions, newest first: [{"model_hash", "archived", "time"}]."""
    out = []
    if paths.archive.is_dir():
        for d in paths.archive.iterdir():
            if d.is_dir() and all((d / n).exists() for n in CORE_FILES):
                out.append({"model_hash": d.name, "time": _dt.datetime.fromtimestamp(d.stat().st_mtime, _dt.timezone.utc)
                            .replace(microsecond=0).isoformat().replace("+00:00", "Z")})
    return sorted(out, key=lambda r: r["time"], reverse=True)


def previous_version(paths):
    """The model that was live before the current one: from history, else MODEL.json parents."""
    manifest = read_json(paths.model_json, {}) or {}
    cur = manifest.get("model_hash")
    for r in reversed(read_jsonl(paths.history)):
        if r.get("model_hash") == cur and r.get("previous") and r["previous"] != cur:
            return r["previous"]
    parents = manifest.get("parents") or []
    return parents[0] if parents else None


def rollback(paths, cfg, to=None, event="rollback", log=print):
    """Put an archived version back. grants.md stays as it is (revocations and edits survive a rollback); corrections
    folded after that version become pending again, so the next refresh folds them into it. Returns a summary."""
    from .select import build_index
    from .synthesize import archive_previous, write_manifest
    manifest = read_json(paths.model_json, {}) or {}
    cur = manifest.get("model_hash")
    target = to or previous_version(paths)
    if not target:
        raise AutoError("no previous model version recorded (model/history.jsonl, MODEL.json parents)")
    matches = [d for d in (paths.archive.iterdir() if paths.archive.is_dir() else []) if d.is_dir() and d.name.startswith(target)]
    if len(matches) != 1:
        raise AutoError("model version %s is not archived under %s (refresh --rollback --list shows what is)" % (target, paths.archive)
                        if not matches else "model version prefix %s is ambiguous" % target)
    src = matches[0]
    if not all((src / n).exists() for n in CORE_FILES):
        raise AutoError("archive %s is incomplete" % src)
    if src.name == cur:
        raise AutoError("model %s is already live" % cur)
    archive_previous(paths, log)
    for n in CORE_FILES:
        _replace(src / n, paths.model / n)
    build_index(paths.model)
    old = read_json(src / "MODEL.json", {}) or {}
    m = write_manifest(paths, cfg, run_id=old.get("run_id"), mined_run=manifest.get("mined_run"), parents=[cur] if cur else [],
                       corrections_folded=old.get("corrections_folded", 0), folded_through=old.get("corrections_folded_through"),
                       extra={"rolled_back_from": cur, "rolled_back_to": src.name, "_event": event, "_archived": cur})
    log("refresh: rolled back from %s to %s (now live as %s); the version you left is archived for --rollback --to %s" % (
        cur, src.name, m["model_hash"], cur))
    return {"from": cur, "to": src.name, "model_hash": m["model_hash"]}


# ---------------------------------------------------------------- the run

def _run_cost(paths, run_id):
    rows = _engine.read_calls(paths.runs / run_id) if run_id else []
    return {"calls": sum(1 for r in rows if r.get("status") != "pending"), "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 6)}


def _pending_corrections(paths, sp=None):
    from .refresh import parse_corrections, unfolded
    p = (sp or paths).corrections
    manifest = read_json((sp or paths).model_json, {}) or {}
    entries, _ = parse_corrections(p.read_text(encoding="utf-8") if p.exists() else "")
    return unfolded(entries, manifest.get("corrections_folded_through"))


def _lint_gate(paths, sp, cfg):
    from .lint import lint_model
    new = lint_model(sp, cfg)
    if new["forbidden"]:
        return "lint found forbidden rules in the candidate: " + "; ".join(new["forbidden"][:3])
    live = lint_model(paths, cfg)
    fresh = [h for h in new["other"] if h not in live["other"]]
    if fresh:
        return "lint findings the live model does not have: " + "; ".join(fresh[:3])
    return None


def _models(cfg, kind):
    m = ((cfg.get("auto") or {}).get("models") or {}).get(kind) or {}
    return (m.get("mine") or _engine.stage_model(cfg, kind, "mine"), m.get("synthesize") or _engine.stage_model(cfg, kind, "synthesize"))


def run_auto(paths, cfg, kind=None, max_cost_usd=None, max_calls=None, fake_dir=None, log=print, backoff=None, force=False,
             now=None):
    """One unattended refresh. Returns the run record (also written to auto/last.json)."""
    acfg = cfg.get("auto") or {}
    kind = kind or cfg.get("engine") or "claude"
    budget = _budget.Budget(max_cost_usd if max_cost_usd is not None else acfg.get("max_cost_usd", 3.0),
                            max_calls if max_calls is not None else acfg.get("max_calls", 30))
    mine_model, synth_model = _models(cfg, kind)
    rec = {"started": now_iso(), "engine": kind, "models": {"mine": mine_model, "synthesize": synth_model},
           "outcome": None, "reason": None, "steps": {}}
    if kind == "codex" and not float(cfg.get("codex_usd_per_million_tokens") or 0):
        log("refresh --auto: codex reports tokens, not cost; set codex_usd_per_million_tokens in config.json for the "
            "cost cap to bind (until then only auto.max_calls limits the run)")
    try:
        with Lock(paths):
            _run(paths, cfg, kind, acfg, budget, mine_model, synth_model, rec, fake_dir, log, backoff, force, now)
    except AutoLocked as exc:
        rec["outcome"], rec["reason"] = "locked", str(exc)
    except AutoError as exc:
        rec["outcome"], rec["reason"] = "error", str(exc)
    rec["finished"] = now_iso()
    rec["budget"] = budget.as_dict()
    paths.auto.mkdir(parents=True, exist_ok=True)
    write_json(paths.auto / "last.json", rec)
    with open(paths.auto / "runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    staging = paths.auto / "staging"
    if staging.exists() and rec["outcome"] != "held":
        shutil.rmtree(staging, ignore_errors=True)  # a held candidate stays for inspection until the next run
    log("refresh --auto: %s%s (spent $%.4f in %d call(s) of the $%s cap)" % (
        rec["outcome"], (": " + rec["reason"]) if rec.get("reason") else "", budget.spent, budget.calls,
        budget.max_cost if budget.max_cost is not None else "none"))
    return rec


def _finish(rec, outcome, reason=None):
    rec["outcome"], rec["reason"] = outcome, reason
    return rec


def _run(paths, cfg, kind, acfg, budget, mine_model, synth_model, rec, fake_dir, log, backoff, force, now):
    from .corpus import run_extract
    from .mine import latest_mined_run, pending_units, run_mine
    from .prep import PrepRefused, run_prep
    from .select import ModelFormatError
    from .synthesize import SynthError, run_synthesize
    from .refresh import RefreshError, fold_corrections
    from . import track as _track
    manifest = read_json(paths.model_json, {}) or {}
    if not manifest.get("model_hash"):
        raise AutoError("no model yet: build one first (references/build.md); refresh --auto only keeps a model current")
    split = read_json(paths.split)
    if not split:
        raise AutoError("derived/split.json missing: run prep once by hand first")

    ex = run_extract(paths, cfg, log=log)
    rec["steps"]["extract"] = {"added": ex.get("added"), "total": ex.get("total")}
    ts = _track.run_track(paths, cfg, log=log)
    rec["steps"]["track"] = {"invocations": ts["invocations"], "classified": ts["classified"],
                             "correction_rate": ts["correction_rate"], "refinement_rate": ts["refinement_rate"]}

    live, parents = manifest.get("model_hash"), manifest.get("parents") or []
    reg = _track.regression(ts, live, parents[0] if parents else None, int(acfg.get("min_invocations", 10)))
    if reg:
        rec["regression"] = reg
        if acfg.get("rollback_on_regression", True):
            try:
                rec["rollback"] = rollback(paths, cfg, to=reg["parent"], event="auto-rollback", log=log)
            except AutoError as exc:
                return _finish(rec, "held", "correction rate regressed under %s and the rollback failed: %s" % (live, exc))
            return _finish(rec, "rolled-back", "correction rate under %s (%s) lies above %s's 95%% interval" % (
                live, reg["live_rate"]["rate"], reg["parent"]))
        return _finish(rec, "held", "correction rate under %s lies above its parent's 95%% interval" % live)

    kw = {"pin": True, "min_new_chars": 0 if force else int(acfg.get("min_new_chars", 0)),
          "delta_chunk_chars": acfg.get("delta_chunk_chars")}
    try:
        if split.get("no_test"):
            tb = split.get("train_before")
            if acfg.get("advance_train_before", True):
                at = now or _dt.datetime.now(_dt.timezone.utc)
                tb = (at - _dt.timedelta(hours=float(acfg.get("settle_hours", 2)))).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            out = run_prep(paths, cfg, no_test=True, train_before=tb, log=log, **kw)
        else:
            out = run_prep(paths, cfg, keep_split=True, log=log, **kw)
    except PrepRefused as exc:
        raise AutoError("prep: %s" % exc)
    inc = out["report"].get("incremental") or {}
    rec["steps"]["prep"] = {k: inc.get(k) for k in ("new_chunks", "changed_chunks", "new_chars", "deferred_chars",
                                                     "bootstrapped", "bootstrap_unmatched")}
    rec["steps"]["prep"]["split_id"] = out["split"]["split_id"]

    prev = latest_mined_run(paths)
    todo = pending_units(paths, cfg, prev)
    if todo:
        mine_cap = budget.remaining_cost(reserve=float(acfg.get("synth_reserve_usd", 1.0)))
        if mine_cap is not None and mine_cap <= 0:
            return _finish(rec, "incomplete", "%d mine unit(s) wait; the cap leaves nothing after the synthesis reserve" % len(todo))
        ms = run_mine(paths, cfg, kind, model=mine_model, max_cost_usd=mine_cap, max_calls=budget.remaining_calls(),
                      reuse_from=prev, fake_dir=fake_dir, log=log, backoff=backoff, concurrency=int(acfg.get("concurrency", 1)),
                      cost_estimate=_budget.estimator(cfg, mine_model, "mine", paths.runs))
        budget.charge(ms)
        left = len(ms["failed"]) + len(ms["stopped"]) + len(ms["pending"])
        rec["steps"]["mine"] = {"run": ms["run_id"], "units": len(todo), "ok": len(ms["ok"]), "left": left,
                                "cost_usd": ms["cost_usd"], "calls": ms["calls"]}
        if left:
            return _finish(rec, "incomplete", "%d of %d mine unit(s) left (%s); the next run resumes them" % (
                left, len(todo), "budget cap" if ms["stopped"] else "engine failure" if ms["failed"] else "print mode"))

    latest = latest_mined_run(paths)
    need_synth = bool(latest) and latest != manifest.get("mined_run")
    pending_corr = [e for e in _pending_corrections(paths) if e["scope"] == "global"]
    any_pending = _pending_corrections(paths)
    if not need_synth and not any_pending:
        if inc.get("deferred_chars"):
            return _finish(rec, "deferred", "%d characters of new material wait until they reach auto.min_new_chars" % inc["deferred_chars"])
        return _finish(rec, "nothing-new")

    sp, fp = stage(paths, paths.auto / "staging")
    if need_synth:
        rid = _engine.new_run_id(paths)
        try:
            run_synthesize(sp, cfg, kind, update=True, model=synth_model, run_id=rid, max_cost_usd=budget.remaining_cost(),
                           max_calls=budget.remaining_calls(), fake_dir=fake_dir, log=log, backoff=backoff,
                           cost_estimate=_budget.estimator(cfg, synth_model, "synthesize", paths.runs))
        except (SynthError, ModelFormatError) as exc:
            budget.charge(_run_cost(paths, rid))
            stopped = "stopped" in str(exc)
            return _finish(rec, "incomplete" if stopped else "held", "synthesize: %s" % str(exc).splitlines()[0][:300])
        budget.charge(_run_cost(paths, rid))
        rec["steps"]["synthesize"] = {"run": rid, "mined_run": latest, **_run_cost(paths, rid)}
    if any_pending:
        rid = _engine.new_run_id(paths)
        try:
            fs = fold_corrections(sp, cfg, kind, run_id=rid, model=synth_model, fake_dir=fake_dir, log=log, backoff=backoff,
                                  max_cost_usd=budget.remaining_cost(), max_calls=budget.remaining_calls(),
                                  cost_estimate=_budget.estimator(cfg, synth_model, "refresh", paths.runs))
        except (RefreshError, ModelFormatError) as exc:
            budget.charge(_run_cost(paths, rid))
            return _finish(rec, "held", "fold corrections: %s" % str(exc).splitlines()[0][:300])
        budget.charge(_run_cost(paths, rid))
        rec["steps"]["corrections"] = {"folded": fs.get("pending", 0), "applied": fs.get("applied", 0),
                                       "project_notes": fs.get("project_notes", 0), "global_pending": len(pending_corr)}
    why = _lint_gate(paths, sp, cfg)
    if why:
        return _finish(rec, "held", why)
    if fingerprints(paths) != fp:
        return _finish(rec, "held", "a live model file changed during the run (a hand edit?); nothing was swapped")
    row = swap(paths, cfg, sp, event="auto", log=log)
    rec["swap"] = {"from": row.get("previous"), "to": row.get("model_hash")}
    return _finish(rec, "swapped", "model %s replaces %s" % (row.get("model_hash"), row.get("previous")))


def last_run(paths):
    return read_json(paths.auto / "last.json")


def format_status_line(last):
    if not last:
        return "auto refresh: never run (refresh --auto; schedule install runs it weekly)"
    b = last.get("budget") or {}
    return "auto refresh: last run %s: %s%s (spent $%.2f of $%s)" % (
        (last.get("finished") or last.get("started") or "")[:16], last.get("outcome"),
        (": " + last["reason"][:160]) if last.get("reason") else "", float(b.get("spent_usd") or 0),
        b.get("max_cost_usd") if b.get("max_cost_usd") is not None else "-")
