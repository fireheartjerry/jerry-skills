"""Weekly self-refresh for the trace candidate that feeds the packet's experience layer.

Each local ISO week: a new private study from the current sources, one sealed look at the newest conversation
families, a refit on everything observed, and an atomic swap of `<root>/current`. The swap happens only when the
fresh look shows the learned part is not significantly worse than its best baseline; otherwise, and on any
failure, the previous candidate stays in place. New corrections, re-sends and go-aheads reach the experience
layer through the swapped candidate with no review step, because its summary is rebuilt whenever the candidate
changes. Planning writes files only; installing the schedule is a separate user action.
"""
import datetime as dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import read_json, skill_dir
from .corpus import now_iso
from .preferences import _check_output, _json_bytes, _private_bytes, _private_json, _safe_path
from .schedule import ScheduleError, _lock, subscription_environment

SOURCES = ("claude", "codex", "kimi", "grok", "memory")
FINAL = ("swapped", "kept_previous")
MAX_ATTEMPTS = 3


def _write_state(path, data):
    """Atomic 0600 JSON for this runner's own state files."""
    path = _safe_path(path)
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, default=str)
        fh.write("\n")
    os.replace(str(tmp), str(path))


def _int(value, low, high, name):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ScheduleError("%s must be an integer from %d to %d" % (name, low, high))
    return value


def plan(out_dir, python, sources, code_roots, encoder, bundles=(), seed_cache=None, machine="local",
         weekday=6, hour=3, minute=30, timezone="UTC", keep_studies=1):
    """Write the spec, a standalone runner and a launchd plist. Nothing is loaded or run."""
    for value, low, high, name in ((weekday, 0, 6, "weekday"), (hour, 0, 23, "hour"), (minute, 0, 59, "minute"),
                                   (keep_studies, 1, 8, "keep_studies")):
        _int(value, low, high, name)
    clean_sources = []
    for item in sources:
        kind, sep, path = str(item).partition("=")
        if not sep or kind not in SOURCES or not path:
            raise ScheduleError("source must be kind=PATH with kind in %s" % ", ".join(SOURCES))
        clean_sources.append({"source": kind, "path": str(Path(path).expanduser().absolute())})
    if not clean_sources:
        raise ScheduleError("at least one source is required")
    clean_bundles = []
    for item in bundles:
        path, sep, digest = str(item).partition("=")
        if not sep or len(digest) != 64:
            raise ScheduleError("bundle must be PATH=PARSER_SHA256")
        clean_bundles.append({"file": str(Path(path).expanduser().absolute()), "parser_sha256": digest})
    for key in ("path", "model", "revision"):
        if not isinstance((encoder or {}).get(key), str) or not encoder[key].strip():
            raise ScheduleError("encoder needs path, model and revision")
    out = _safe_path(out_dir)
    spec = {"schema_version": 1, "kind": "trace-weekly", "root": str(out),
            "builder": str(skill_dir() / "scripts" / "intent_build.py"), "python": str(Path(python).absolute()),
            "sources": clean_sources, "bundles": clean_bundles,
            "code_roots": [str(Path(r).expanduser().absolute()) for r in code_roots],
            "encoder": {"path": str(Path(encoder["path"]).expanduser().absolute()), "model": encoder["model"],
                        "revision": encoder["revision"]},
            "seed_cache": str(Path(seed_cache).expanduser().absolute()) if seed_cache else None,
            "machine": machine, "keep_studies": keep_studies, "weekday": weekday, "hour": hour, "minute": minute,
            "timezone": timezone, "installed": False,
            "swap": "after one sealed look; the learned part must not be significantly worse than its best baseline"}
    spec_path, runner = out / "trace-weekly.json", out / "trace-weekly.py"
    _safe_path(spec_path)
    if spec_path.exists() and read_json(spec_path) != spec:
        raise ScheduleError("a different trace refresh plan already exists here; choose another directory")
    runner_data = ("#!/usr/bin/env python3\nimport json, sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                   "from mr.trace_weekly import run\nresult = run(Path(__file__).with_name('trace-weekly.json'))\n"
                   "print(json.dumps(result, sort_keys=True))\nsys.exit(1 if result.get('status') == 'failed' else 0)\n"
                   % str(skill_dir() / "scripts")).encode("utf-8")
    label = "local.mind-reading.trace-weekly-" + __import__("hashlib").sha256(str(out).encode()).hexdigest()[:12]
    plist = out / (label + ".plist")
    plist_data = plistlib.dumps({
        "Label": label, "ProgramArguments": [sys.executable, "-I", str(runner)],
        "StartCalendarInterval": {"Weekday": (weekday + 1) % 7, "Hour": hour, "Minute": minute},
        "RunAtLoad": False, "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
        "StandardOutPath": str(out / "launchd.log"), "StandardErrorPath": str(out / "launchd-error.log")})
    for target, content in ((spec_path, _json_bytes(spec)), (runner, runner_data), (plist, plist_data)):
        _check_output(target, content)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_json(spec_path, spec)
    _private_bytes(runner, runner_data)
    os.chmod(runner, 0o700)
    _private_bytes(plist, plist_data)
    return {"plan": str(spec_path), "runner": str(runner), "launchd_plist": str(plist), "label": label,
            "current": str(out / "current"), "installed": False, "timezone": timezone,
            "note": "launchd uses the Mac's local timezone. Point config `candidate` at the current link to feed the "
                    "experience layer."}


def _load(spec_path):
    spec = read_json(spec_path)
    if not isinstance(spec, dict) or spec.get("kind") != "trace-weekly" or spec.get("schema_version") != 1:
        raise ScheduleError("not a trace refresh plan")
    for key in ("root", "builder", "python"):
        if not isinstance(spec.get(key), str) or not spec[key]:
            raise ScheduleError("trace refresh plan is missing %s" % key)
    if not spec.get("sources"):
        raise ScheduleError("trace refresh plan has no sources")
    return spec


def steps(spec, study, candidate):
    """The builder commands for one week, in order."""
    base = [spec["python"], "-I", spec["builder"], "--home", str(study)]
    ingest = base + ["ingest", "--machine", spec.get("machine") or "local"]
    for src in spec["sources"]:
        if Path(src["path"]).exists():
            ingest += ["--source", "%s=%s" % (src["source"], src["path"])]
    out = [("ingest", ingest)]
    for bundle in spec.get("bundles") or []:
        if Path(bundle["file"]).is_file():
            out.append(("import-bundle", base + ["import-bundle", "--file", bundle["file"],
                                                 "--parser-sha256", bundle["parser_sha256"]]))
    roots = [r for r in spec.get("code_roots") or [] if Path(r).is_dir()]
    if roots:
        git = base + ["git"]
        for r in roots:
            git += ["--root", r]
        out.append(("git", git))
    enc = spec["encoder"]
    out += [("prepare", base + ["prepare"]),
            ("encode", base + ["encode", "--encoder-path", enc["path"], "--model", enc["model"],
                               "--revision", enc["revision"], "--device", "auto"]),
            ("train", base + ["train"]), ("seal", base + ["seal"]), ("evaluate", base + ["evaluate"]),
            ("refit", base + ["refit", "--output", str(candidate)])]
    return out


def gate(study):
    """Swap only when the fresh one-look result is not significantly worse than the best baseline."""
    res = read_json(Path(study) / "research" / "test-results.json")
    boot = (res or {}).get("bootstrap") or {}
    mrr = boot.get("mrr") or {}
    if not isinstance(mrr.get("ci_high"), (int, float)) or not isinstance(mrr.get("mean_diff"), (int, float)):
        return {"pass": False, "reason": "no fresh one-look result"}
    ok = bool(boot.get("primary_is_baseline")) or mrr["ci_high"] >= 0
    return {"pass": ok, "best_baseline": res.get("best_baseline"), "families": mrr.get("n_families"),
            "learned_minus_baseline_mrr": round(mrr["mean_diff"], 4),
            "ci": [round(mrr.get("ci_low", float("nan")), 4), round(mrr["ci_high"], 4)],
            "reason": "learned part not worse than its best baseline on the newest week" if ok else
                      "learned part significantly worse than its best baseline on the newest week"}


def _swap(link, target):
    link = Path(link)
    if link.exists() and not link.is_symlink():
        raise ScheduleError("current candidate path is a real directory, not a link")
    previous = os.path.realpath(link) if link.is_symlink() else None
    tmp = link.with_name(link.name + ".%d.tmp" % os.getpid())
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link))
    return previous


def _prune(studies, candidates, keep, current, previous):
    removed = []
    for old in sorted(p for p in studies.iterdir() if p.is_dir())[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(str(old))
    protect = {os.path.realpath(current)} | ({previous} if previous else set())
    for old in sorted(p for p in candidates.iterdir() if p.is_dir()):
        if os.path.realpath(old) not in protect:
            shutil.rmtree(old, ignore_errors=True)
            removed.append(str(old))
    return removed


def _seed_cache(spec, studies, study):
    """Reuse earlier embeddings: the cache key includes the encoder identity, so only new texts are encoded."""
    target = study / "embedding-cache.sqlite"
    if target.exists():
        return None
    earlier = [p / "embedding-cache.sqlite" for p in sorted(studies.iterdir()) if p != study]
    sources = [p for p in reversed(earlier) if p.is_file()]
    if spec.get("seed_cache") and Path(spec["seed_cache"]).is_file():
        sources.append(Path(spec["seed_cache"]))
    if sources:
        study.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sources[0], target)
        return str(sources[0])
    return None


def run(spec_path, today=None, run_command=None):
    spec = _load(spec_path)
    root = _safe_path(spec["root"])
    studies, candidates, state_dir = root / "studies", root / "candidates", root / "state"
    if today is None:
        try:
            from zoneinfo import ZoneInfo
            today = dt.datetime.now(ZoneInfo(spec.get("timezone") or "UTC")).date()
        except Exception as exc:
            raise ScheduleError("cannot resolve the plan's timezone") from exc
    year, week, _ = today.isocalendar()
    slot = "%04d-W%02d" % (year, week)
    study, candidate = studies / slot, candidates / slot
    slot_file, latest, log_path = state_dir / (slot + ".json"), state_dir / "latest.json", state_dir / (slot + ".log")
    for p in (studies, candidates, state_dir):
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = _lock(state_dir / "running.json")
    try:
        previous_state = read_json(slot_file) if slot_file.exists() else None
        attempts = (previous_state or {}).get("attempts", 0)
        if previous_state and previous_state.get("status") in FINAL:
            return dict(previous_state, status="already_ran", recorded_status=previous_state["status"])
        if attempts >= MAX_ATTEMPTS:
            return {"status": "failed", "slot": slot, "reason": "attempt limit reached this week", "attempts": attempts}
        for leftover in (study, candidate):  # an interrupted or failed attempt is rebuilt from scratch
            if leftover.exists() and os.path.realpath(leftover) != os.path.realpath(root / "current"):
                shutil.rmtree(leftover)
        result = {"status": "running", "slot": slot, "started": now_iso(), "study": str(study),
                  "candidate": str(candidate), "attempts": attempts + 1}
        _write_state(slot_file, result)
        _write_state(latest, result)
        env = subscription_environment()
        runner = run_command or subprocess.run
        with open(log_path, "a", encoding="utf-8") as log:
            os.chmod(log_path, 0o600)
            for name, argv in steps(spec, study, candidate):
                if name == "encode":
                    result["cache_from"] = _seed_cache(spec, studies, study)
                if name == "refit":
                    result["fresh_look"] = gate(study)
                    if not result["fresh_look"]["pass"]:
                        result["status"] = "kept_previous"
                        break
                log.write("\n== %s %s\n" % (name, now_iso()))
                log.flush()
                done = runner(argv, stdout=log, stderr=subprocess.STDOUT, text=True, check=False, env=env)
                if done.returncode != 0:
                    result.update(status="failed", failed_step=name, exit_code=done.returncode)
                    break
            else:
                if not (candidate / "cases.jsonl").is_file() or not (candidate / "manifest.json").is_file():
                    result.update(status="failed", failed_step="refit", reason="refit produced no candidate")
                else:
                    previous = _swap(root / "current", candidate)
                    result.update(status="swapped", previous=previous)
                    result["pruned"] = _prune(studies, candidates, spec.get("keep_studies", 1), candidate, previous)
        result["finished"] = now_iso()
        _write_state(slot_file, result)
        _write_state(latest, result)
        return result
    except Exception as exc:
        _write_state(latest, {"status": "failed", "slot": slot, "finished": now_iso(),
                              "error_type": type(exc).__name__, "error": str(exc)[:300]})
        raise
    finally:
        handle.close()
