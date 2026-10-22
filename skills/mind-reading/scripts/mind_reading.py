#!/usr/bin/env python3
"""mind-reading pipeline CLI. Stdlib only; run with `python3 -I`.

  python3 -I scripts/mind_reading.py [--home PATH] <subcommand> [options]

Subcommands: init, extract, prep, mine, synthesize, eval, refresh, status, select, log-decision, correct, lint.
Exit codes: 0 ok, 1 usage, 2 refused or blocked (one-look rule, guard, `status` with BLOCKED items), 3 engine failure
after retries or a budget stop, 4 validation failure (also: no model for `select`; a forbidden rule in a `correct`
line or in light.md for `refresh --always-on`), 5 the data home is not writable and the line could not be spooled
either (`log-decision`, `correct`).
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mr import config as _config  # noqa: E402
from mr.config import Paths, load_config, read_jsonl, resolve_home, save_config, spool_path  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_ENGINE, EXIT_INVALID, EXIT_UNWRITABLE = 0, 1, 2, 3, 4, 5


def _log_factory(quiet, as_json):
    def log(msg):
        if quiet:
            return
        print(msg, file=sys.stderr if as_json else sys.stdout)
    return log


def _emit(args, summary):
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, default=str))


def _engine_kind(args, cfg):
    return getattr(args, "engine", None) or cfg.get("engine") or "claude"


def _fake_dir(args):
    return getattr(args, "fake_dir", None)


def cmd_init(args, paths, cfg, log):
    paths.ensure_tree()
    if args.name:
        cfg["name"] = args.name
    if args.timezone:
        cfg["timezone"] = args.timezone
    if args.engine:
        cfg["engine"] = args.engine
    if args.claude_dir:
        cfg["sources"]["claude_projects_dir"] = args.claude_dir
    if args.codex_dir:
        cfg["sources"]["codex_sessions_dir"] = args.codex_dir
    for spec in args.person or []:
        if "=" not in spec:
            log("--person needs Name=<role-token>")
            return EXIT_USAGE
        name, token = spec.split("=", 1)
        cfg.setdefault("people", {})[name.strip()] = token.strip()
    for stage_model in args.model_for or []:
        if "=" not in stage_model:
            log("--model-for needs stage=model (stages: mine, synthesize, expand, judge)")
            return EXIT_USAGE
        stage, model = stage_model.split("=", 1)
        cfg.setdefault("models", {}).setdefault(cfg["engine"], {})[stage.strip()] = model.strip()
    if not cfg.get("_exists") and not args.name:
        log("init: --name <FirstName> is required on first run")
        return EXIT_USAGE
    p = save_config(paths, cfg, args.config)
    log("init: wrote %s (name %s, engine %s)" % (p, cfg["name"], cfg["engine"]))
    _emit(args, {"config": str(p), "name": cfg["name"], "engine": cfg["engine"], "home": str(paths.home)})
    return EXIT_OK


def cmd_extract(args, paths, cfg, log):
    from mr.corpus import run_extract
    summary = run_extract(paths, cfg, claude_dir=args.claude_dir, codex_dir=args.codex_dir, rescrub=args.rescrub, log=log)
    _emit(args, summary)
    return EXIT_OK


def cmd_prep(args, paths, cfg, log):
    from mr.prep import PrepRefused, run_prep
    try:
        out = run_prep(paths, cfg, cutoff=args.cutoff, force=args.force, keep_split=args.keep_split, no_twins=args.no_twins, log=log,
                       no_test=args.no_test, train_before=args.train_before)
    except PrepRefused as exc:
        log("prep refused: %s" % exc)
        _emit(args, {"error": str(exc)})
        return EXIT_REFUSED
    _emit(args, out["split"])
    return EXIT_OK


def _engine_exit(summary):
    if summary.get("failed") or summary.get("stopped"):
        return EXIT_ENGINE
    return EXIT_OK


def cmd_mine(args, paths, cfg, log):
    from mr.engine import EngineError
    from mr.mine import run_mine
    try:
        summary = run_mine(paths, cfg, _engine_kind(args, cfg), run_id=args.run, force=args.force, collect=args.collect,
                           concurrency=args.concurrency, model=args.model, max_calls=args.max_calls,
                           max_cost_usd=args.max_cost_usd, fake_dir=_fake_dir(args), log=log,
                           lenses=["all"] if args.single_pass else (args.lens or None))
    except EngineError as exc:
        log("mine: %s" % exc)
        _emit(args, {"error": str(exc)})
        return EXIT_REFUSED
    if summary.get("instructions"):
        log(summary["instructions"])
    if summary["failed"]:
        log("mine: %d unit(s) failed; resume with: mind_reading.py mine --run %s" % (len(summary["failed"]), summary["run_id"]))
    _emit(args, summary)
    return _engine_exit(summary)


def cmd_synthesize(args, paths, cfg, log):
    from mr.select import ModelFormatError
    from mr.synthesize import SUBSTEPS, SynthError, run_synthesize
    bad = [s for s in (args.substeps or []) if s not in SUBSTEPS]
    if bad:
        log("synthesize: unknown substep(s) %s; choose from %s" % (bad, " ".join(SUBSTEPS)))
        return EXIT_USAGE
    try:
        summary = run_synthesize(paths, cfg, _engine_kind(args, cfg), substeps=args.substeps or None, mined_run=args.mined_run,
                                 run_id=args.run, force=args.force, collect=args.collect, model=args.model, update=args.update,
                                 fake_dir=_fake_dir(args), log=log, strict_lint=not args.no_strict,
                                 max_calls=args.max_calls, max_cost_usd=args.max_cost_usd)
    except ModelFormatError as exc:
        log("synthesize: model format error:\n%s" % exc)
        _emit(args, {"error": str(exc)})
        return EXIT_INVALID
    except SynthError as exc:
        log("synthesize: %s" % exc)
        _emit(args, {"error": str(exc)})
        return EXIT_ENGINE if "failed" in str(exc) else EXIT_INVALID
    if summary.get("instructions"):
        log(summary["instructions"])
    _emit(args, summary)
    return EXIT_OK


def cmd_eval(args, paths, cfg, log):
    from mr.evaluate import EvalRefused, run_eval
    arms = args.arm or None
    try:
        summary = run_eval(paths, cfg, _engine_kind(args, cfg), dev=args.dev, arms=arms, judge_kind=args.judge_engine,
                           force=args.force, run_id=args.run, collect=args.collect, concurrency=args.concurrency,
                           max_sessions=args.max_sessions, fake_dir=_fake_dir(args), log=log, model_expand=args.model,
                           model_judge=args.judge_model, max_calls=args.max_calls, max_cost_usd=args.max_cost_usd, mode=args.mode)
    except EvalRefused as exc:
        log("eval refused: %s" % exc)
        _emit(args, {"error": str(exc)})
        return EXIT_REFUSED
    if summary.get("instructions"):
        log(summary["instructions"])
    if summary.get("report_path"):
        log("report: %s" % summary["report_path"])
    _emit(args, summary)
    report = summary.get("report") or {}
    failed = (report.get("failed_units") or []) + (report.get("stopped_units") or [])
    return EXIT_ENGINE if failed else EXIT_OK


def cmd_refresh(args, paths, cfg, log):
    from mr.refresh import RefreshError, run_refresh
    from mr.select import ModelFormatError
    do_all = not (args.corrections or args.extend or args.always_on)
    try:
        summary = run_refresh(paths, cfg, _engine_kind(args, cfg), corrections=args.corrections or do_all, extend=args.extend,
                              always_on_flag=args.always_on or do_all, write_to=args.write_to, new_cutoff=args.new_cutoff,
                              run_id=args.run, collect=args.collect, fake_dir=_fake_dir(args), log=log, model=args.model,
                              concurrency=args.concurrency, candidate_home=args.candidate_home,
                              max_calls=args.max_calls, max_cost_usd=args.max_cost_usd, train_before=args.train_before)
    except (RefreshError, ModelFormatError) as exc:
        log("refresh: %s" % exc)
        _emit(args, {"error": str(exc)})
        return EXIT_INVALID
    if (summary.get("corrections") or {}).get("instructions"):
        log(summary["corrections"]["instructions"])
    if "always_on" in summary and not args.write_to:
        print(summary["always_on"])
    _emit(args, summary)
    return EXIT_OK


def cmd_status(args, paths, cfg, log):
    """Exit 2 when anything is BLOCKED, so scripts and CI can detect the state without parsing the text."""
    from mr.status import estimate, format_text, gather
    drain_spool(paths, log)
    st = gather(paths, cfg)
    est = estimate(paths, cfg, st) if args.estimate else None
    if args.json:
        print(json.dumps({"status": st, "estimate": est}, ensure_ascii=False, default=str))
    else:
        print(format_text(st, est), end="")
    return EXIT_REFUSED if st["blocked"] else EXIT_OK


def _read_seed(args, log):
    """The seed from --seed, or from --seed-file (a path, or `-` for stdin). Returns None on a usage error."""
    if args.seed is not None and args.seed_file:
        log("pass --seed or --seed-file, not both")
        return None
    if args.seed_file:
        text = sys.stdin.read() if args.seed_file == "-" else Path(args.seed_file).read_text(encoding="utf-8")
        return text.strip("\n")
    if args.seed is None:
        log("select needs --seed <text> or --seed-file <path|->")
        return None
    return args.seed


def cmd_select(args, paths, cfg, log):
    """The slice for a seed. Exit 4 with "no model" only when neither index.json nor any model file exists; a model
    built before the index format is read through the legacy fallback (every example, matching groups)."""
    from mr.select import ModelFormatError, select_slice
    from mr.intent_context import ContextFormatError, normalize
    context = None
    context_file = getattr(args, "context_file", None)
    if context_file:
        if context_file == "-" and args.seed_file == "-":
            log("seed and intent context cannot both read stdin")
            return EXIT_USAGE
        try:
            if context_file == "-":
                raw_context = sys.stdin.read(65537)
            else:
                with open(context_file, encoding="utf-8") as fh:
                    raw_context = fh.read(65537)
            if len(raw_context) > 65536:
                raise ContextFormatError("intent context file exceeds 65536 characters")
            def unique_context(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ContextFormatError("duplicate intent-context key: %s" % key)
                    result[key] = value
                return result
            context = normalize(json.loads(raw_context, object_pairs_hook=unique_context))
        except (OSError, ValueError) as exc:
            log("select: invalid intent context: %s" % exc)
            return EXIT_USAGE
    seed = _read_seed(args, log)
    if seed is None:
        return EXIT_USAGE
    drain_spool(paths, log)
    if not paths.index.exists() and not paths.profile.exists() and not paths.examples.exists():
        log("select: no model at %s (index.json missing): expand from project memory and the conversation, then offer to build it (references/build.md)" % paths.model)
        return EXIT_INVALID
    try:
        text, decision = select_slice(paths.model, seed, cfg["select"], kind=args.kind, no_light=args.no_light, mode=args.mode,
                                      project=args.project, context=context)
    except (ModelFormatError, FileNotFoundError, ContextFormatError) as exc:
        log("select: %s" % exc)
        return EXIT_INVALID
    if args.json:
        print(json.dumps({"slice": text, "decision": decision}, ensure_ascii=False))
    else:
        print(text, end="")
    return EXIT_OK


def _stdin_json(log):
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        log("stdin is not a JSON object: %s" % exc)
        return None
    if not isinstance(data, dict):
        log("stdin must hold one JSON object")
        return None
    return data


SPOOL_TARGETS = ("decisions.jsonl", "corrections.md")


def _append_line(path, line, log, what):
    """Append one line, creating the parent. When the home is not writable (Codex's workspace-write sandbox, a
    read-only mount) the line is spooled under `spool_path()` and applied by the next run that can write the home
    (`select`, `status`, `log-decision`, `correct` all drain it). Exit 5 only when the spool fails too."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError as exc:
        sp = spool_path()
        try:
            if not sp.parent.exists():
                sp.parent.mkdir(parents=True, mode=0o700)
            with open(sp, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"target": path.name, "line": line.rstrip("\n"), "time": _dt.datetime.now().isoformat(timespec="seconds")}) + "\n")
            os.chmod(sp, 0o600)
        except OSError as exc2:
            log("%s not recorded: %s is not writable (%s) and the spool %s is not either (%s). Codex: run with --add-dir %s, "
                "or point MIND_READING_SPOOL at a writable directory." % (what, path, exc.strerror or exc, sp, exc2.strerror or exc2, path.parent.parent))
            return EXIT_UNWRITABLE
        log("%s spooled to %s (%s is not writable: %s); the next run with a writable data home applies it." % (
            what, sp, path, exc.strerror or exc))
        return EXIT_OK
    return EXIT_OK


def drain_spool(paths, log):
    """Apply spooled decision and correction lines to the data home. Returns the number applied; leaves the spool in
    place when the home is still not writable."""
    sp = spool_path()
    if not sp.exists():
        return 0
    rows = read_jsonl(sp)
    targets = {"decisions.jsonl": paths.decisions, "corrections.md": paths.corrections}
    applied = 0
    try:
        for r in rows:
            target = targets.get(r.get("target"))
            line = r.get("line")
            if target is None or not isinstance(line, str):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(line.rstrip("\n") + "\n")
            applied += 1
        sp.unlink()
    except OSError:
        return 0
    if applied:
        log("applied %d spooled line(s) from %s" % (applied, sp))
    return applied


def seed_hash(seed):
    return hashlib.sha256((seed or "").encode("utf-8")).hexdigest()[:8]


def cmd_log_decision(args, paths, cfg, log):
    """stdin: {"seed": "...", "mode": "execute", "s": 5, "l": 3, "g": 2, "held": [...], "groups": [...],
    "examples": [...], "grants": [...], "lessons": [...], "upgrades": 0, "chars": 6900}. The seed is hashed and never
    stored."""
    data = _stdin_json(log)
    if data is None:
        return EXIT_USAGE
    drain_spool(paths, log)
    seed = data.pop("seed", None)
    row = {"date": _dt.date.today().isoformat(), "seed_hash": data.pop("seed_hash", None) or seed_hash(seed or ""),
           "mode": data.get("mode", "execute")}
    for k in ("s", "l", "g", "chars"):
        value = data.get(k, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            log("field %r must be a nonnegative integer" % k)
            return EXIT_USAGE
        row[k] = value
    row["held"] = [str(h)[:120] for h in (data.get("held") or []) if str(h).strip()]
    row["groups"] = [str(g) for g in (data.get("groups") or [])]
    row["examples"] = [int(e) for e in (data.get("examples") or []) if str(e).isdigit()]
    row["grants"] = [str(g)[:8] for g in (data.get("grants") or []) if str(g).strip()]  # grant ids applied (G1, G2)
    row["lessons"] = [str(x)[:8] for x in (data.get("lessons") or []) if str(x).strip()]  # lesson groups in the slice (W1)
    row["echoes"] = [str(x)[:32] for x in (data.get("echoes") or []) if str(x).strip()]
    row["upgrades"] = data.get("upgrades", 0)
    if not isinstance(row["upgrades"], int) or isinstance(row["upgrades"], bool) or row["upgrades"] < 0:
        log("field 'upgrades' must be a nonnegative integer")
        return EXIT_USAGE
    if row["mode"] not in ("execute", "discuss", "status", "hold", "plan"):
        log("mode must be one of execute, discuss, status, hold, plan")
        return EXIT_USAGE
    from mr.tracking import TrackingError, validate_decision
    for key in ("decision_id", "timestamp", "source_ref"):
        if key in data:
            row[key] = data[key]
    from mr.select import files_hash
    row["model_hash"] = files_hash(paths.model)
    try:
        row = validate_decision(row)
    except TrackingError as exc:
        log("log-decision: %s" % exc)
        return EXIT_USAGE
    if any(r.get("decision_id") == row["decision_id"] for r in read_jsonl(paths.decisions)):
        log("log-decision: duplicate decision_id")
        return EXIT_USAGE
    code = _append_line(paths.decisions, json.dumps(row, ensure_ascii=False), log, "decision")
    if code == EXIT_OK:
        log("decision recorded in %s" % paths.decisions)
    _emit(args, row)
    return code


def cmd_correct(args, paths, cfg, log):
    """stdin: {"seed": "...", "wrong": "...", "meant": "...", "scope": "global"|"project:<name>", "kind": "fix"|"add"|"drop"}.
    Appends one corrections.md line in the format model-format.md section 6 defines."""
    from mr.refresh import parse_corrections
    data = _stdin_json(log)
    if data is None:
        return EXIT_USAGE
    drain_spool(paths, log)
    seed = str(data.get("seed") or "").replace("\n", " ").strip()[:80]
    wrong = str(data.get("wrong") or "n/a").replace("\n", " ").strip() or "n/a"
    meant = str(data.get("meant") or "").replace("\n", " ").strip()
    scope = str(data.get("scope") or "global").strip()
    kind = str(data.get("kind") or "fix").strip()
    if not seed or not meant:
        log("correct needs at least seed and meant")
        return EXIT_USAGE
    if kind not in ("fix", "add", "drop") or not (scope == "global" or scope.startswith("project:")):
        log("kind must be fix, add or drop; scope must be global or project:<name>")
        return EXIT_USAGE
    if kind != "drop":  # a correction never lifts a held category or grants anything; only grants.md can, by hand
        from mr.grants import grant_phrase, has_grant_phrase
        from mr.lint import forbidden_hits
        bad = forbidden_hits(meant, "meant") + forbidden_hits(wrong, "wrong")
        if has_grant_phrase(meant):
            bad.append("meant: a correction cannot grant (%r); grants are typed by hand into grants.md" % grant_phrase(meant))
        if bad:
            log("correct refused: the correction would tell the agent to defeat the harness or would grant an authorization. "
                "Record the preference instead (\"gets frustrated by repeated permission prompts; expects the agent to batch "
                "questions and work on unblocked items meanwhile\").\n" + "\n".join(bad))
            _emit(args, {"error": "forbidden correction", "hits": bad})
            return EXIT_INVALID
    line = "%s | seed: \"%s\" | wrong: %s | meant: %s | scope: %s | kind: %s" % (
        _dt.date.today().isoformat(), seed.replace("|", "/").replace('"', "'"), wrong.replace("|", "/"),
        meant.replace("|", "/"), scope.replace("|", "/"), kind)
    if data.get("decision_id"):
        import re
        did = data["decision_id"]
        if not isinstance(did, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", did):
            log("correct: invalid decision_id")
            return EXIT_USAGE
        if not any(r.get("decision_id") == did for r in read_jsonl(paths.decisions)):
            log("correct: decision_id must refer to a recorded invocation")
            return EXIT_USAGE
        line += " | decision_id: " + did
    entries, bad = parse_corrections(line)
    if bad or len(entries) != 1:
        log("correction does not parse after formatting; check for stray characters: %s" % line)
        return EXIT_USAGE
    code = _append_line(paths.corrections, line, log, "correction")
    if code == EXIT_OK:
        log("correction recorded in %s (fold it in later with refresh --corrections)" % paths.corrections)
    _emit(args, {"line": line})
    return code


def cmd_lint(args, paths, cfg, log):
    from mr.lint import lint_model, lint_skill
    hits, fatal, warnings = [], False, []
    if args.skill:
        hits = lint_skill(args.skill, deny_file=args.deny_file, warnings=warnings)
        fatal = bool(hits)
    if args.model:
        m = lint_model(paths, cfg)
        hits += m["forbidden"] + m["other"]
        fatal = fatal or bool(m["forbidden"]) or (args.strict and bool(m["other"]))
    if not args.skill and not args.model:
        log("lint: pass --skill PATH and/or --model")
        return EXIT_USAGE
    for h in hits:
        print(h)
    for w in warnings:
        log("lint: warning: " + w)
    if not hits:
        log("lint: clean" + (" (with warnings)" if warnings else ""))
    _emit(args, {"hits": hits, "warnings": warnings})
    return EXIT_INVALID if fatal else EXIT_OK


def cmd_demo(args, paths, cfg, log):
    from mr.demo import DemoRefused, run_demo
    if not args.home:
        log("demo: pass --home <new-scratch-directory> to keep it separate from your personal model")
        return EXIT_REFUSED
    try:
        summary = run_demo(paths, cfg, force=args.force, log=log)
    except DemoRefused as exc:
        log("demo: %s" % exc)
        return EXIT_REFUSED
    _emit(args, summary)
    return EXIT_OK


def cmd_recall(args, paths, cfg, log):
    from mr.prep import PrepRefused
    from mr.recall import RecallFormatError, build_episodes, render, retrieve
    if args.action == "build":
        try:
            summary = build_episodes(paths)
        except PrepRefused as exc:
            log("recall: %s" % exc)
            return EXIT_REFUSED
        log("recall: built %d verified intent echoes" % summary["episodes"])
        _emit(args, summary)
    else:
        seed = _read_seed(args, log)
        if seed is None:
            return EXIT_USAGE
        try:
            found = retrieve(paths.model, seed, kind=args.kind, project=args.project, limit=args.limit)
        except RecallFormatError as exc:
            log("recall: %s" % exc)
            return EXIT_INVALID
        if args.json:
            print(json.dumps({"echoes": found}, ensure_ascii=False))
        else:
            print(render(found) or "No relevant intent echo. Use current context.\n", end="")
    return EXIT_OK


def cmd_preferences(args, paths, cfg, log):
    from mr.preferences import PreferenceError, build_review, import_answers, pairs_from_results
    try:
        if args.action == "build":
            if args.pairs:
                with open(args.pairs, encoding="utf-8") as fh:
                    pairs = json.load(fh)
            elif args.baseline_results and args.candidate_results:
                pairs = pairs_from_results(args.baseline_results, args.candidate_results, args.baseline_arm,
                                           args.candidate_arm, args.seeds_dir)
            else:
                raise PreferenceError("pass --pairs or both --baseline-results and --candidate-results")
            if not args.out:
                raise PreferenceError("build needs --out <private-review-directory>")
            summary = build_review(pairs, args.out, seed=args.seed, limit=args.limit)
        elif args.action == "import":
            if not args.key or not args.answers:
                raise PreferenceError("import needs --key and --answers")
            summary = import_answers(args.key, args.answers, out_path=args.out)
        else:
            raise PreferenceError("choose build or import")
    except (PreferenceError, OSError, ValueError) as exc:
        log("preferences: %s" % exc)
        return EXIT_INVALID
    print(json.dumps(summary, ensure_ascii=False, indent=None if args.json else 2))
    return EXIT_OK


def cmd_tracking(args, paths, cfg, log):
    from mr import tracking
    from mr.preferences import PreferenceError
    try:
        if args.action == "record":
            data = _stdin_json(log)
            if data is None:
                return EXIT_USAGE
            summary = tracking.record_outcome(paths, data)
        elif args.action == "report":
            summary = tracking.summarize(paths)
            if args.out:
                from mr.preferences import _private_json
                _private_json(args.out, summary)
        elif args.action == "discover":
            discovery = tracking.scan_transcripts(cfg)
            events = discovery.pop("events")
            meta = discovery
            summary = dict(meta, candidates=len(events), confirmed_correction_rate=None)
            if args.out:
                summary["review"] = tracking.build_baseline_review(events, args.out) if any(e["followup_observed"] for e in events) else None
                from mr.preferences import _private_json
                _private_json(Path(args.out) / "private" / "discovery.json", {"metadata": meta, "events": events})
        elif args.action == "import":
            if not args.key or not args.answers:
                raise tracking.TrackingError("import needs --key and --answers")
            summary = tracking.import_baseline_answers(args.key, args.answers, out_path=args.out)
        else:
            raise tracking.TrackingError("choose record, report, discover or import")
    except (tracking.TrackingError, PreferenceError, ValueError, OSError) as exc:
        log("tracking: %s" % exc)
        return EXIT_INVALID
    print(json.dumps(summary, ensure_ascii=False, indent=None if args.json else 2))
    return EXIT_OK


def cmd_foresight(args, paths, cfg, log):
    from mr import foresight
    try:
        if args.action == "report":
            summary = foresight.report(paths)
        elif args.action == "replay":
            from mr.replay import freeze_cases
            from mr.config import read_json
            if not args.after or not args.out:
                raise foresight.ForesightError("replay needs --after <UTC timestamp> and --out <private directory>")
            excluded = set((read_json(paths.split, {}) or {}).get("train_ids", []))
            if args.exclude_sessions:
                extra = read_json(args.exclude_sessions)
                if not isinstance(extra, list) or any(not isinstance(s, str) for s in extra):
                    raise foresight.ForesightError("--exclude-sessions must contain a JSON list of source:session strings")
                excluded.update(extra)
            summary = freeze_cases(read_jsonl(paths.messages), args.out, args.after, excluded_sessions=excluded, limit=args.limit)
        else:
            data = _stdin_json(log)
            if data is None:
                return EXIT_USAGE
            summary = (foresight.record if args.action == "record" else foresight.observe)(paths, data)
    except (ValueError, OSError) as exc:
        log("foresight: %s" % exc)
        return EXIT_INVALID
    print(json.dumps(summary, ensure_ascii=False, indent=None if args.json else 2))
    return EXIT_OK


def cmd_schedule(args, paths, cfg, log):
    from mr.schedule import ScheduleError, plan
    try:
        summary = plan(paths, cfg, args.out, weekday=args.weekday, hour=args.hour, minute=args.minute,
                       timezone=args.timezone, max_calls=args.max_calls, engine=args.engine)
    except (ScheduleError, OSError, ValueError) as exc:
        log("schedule: %s" % exc)
        return EXIT_INVALID
    print(json.dumps(summary, ensure_ascii=False, indent=None if args.json else 2))
    return EXIT_OK


def cmd_atlas(args, paths, cfg, log):
    from mr.atlas import build_atlas
    try:
        summary = build_atlas(paths, args.out, force=args.force)
    except (ValueError, OSError) as exc:
        log("atlas: %s" % exc)
        return EXIT_INVALID
    print(json.dumps(summary, ensure_ascii=False, indent=None if args.json else 2))
    return EXIT_OK


def build_parser():
    ap = argparse.ArgumentParser(prog="mind_reading.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    def global_opts(p, suppress=False):
        d = {"default": argparse.SUPPRESS} if suppress else {}
        p.add_argument("--home", help="data home (default $MIND_READING_HOME or %s)" % _config.DEFAULT_HOME, **d)
        p.add_argument("--config", help="config.json path (default <home>/config.json)", **d)
        p.add_argument("--quiet", action="store_true", **d)
        p.add_argument("--json", action="store_true", help="machine-readable summary on stdout, logs to stderr", **d)

    global_opts(ap)
    common = argparse.ArgumentParser(add_help=False)
    global_opts(common, suppress=True)  # the same options are accepted after the subcommand
    sub = ap.add_subparsers(dest="cmd", parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    def engine_opts(p, with_judge=False):
        p.add_argument("--engine", choices=["claude", "codex", "print", "fake"])
        p.add_argument("--model", help="override the config model for this stage")
        p.add_argument("--concurrency", type=int, default=4)
        p.add_argument("--max-calls", type=int)
        p.add_argument("--max-cost-usd", type=float)
        p.add_argument("--run", help="run id to resume, or `latest`")
        p.add_argument("--collect", action="store_true", help="print mode: validate outputs that landed")
        p.add_argument("--force", action="store_true")
        p.add_argument("--fake-dir", help=argparse.SUPPRESS)
        if with_judge:
            p.add_argument("--judge-engine", choices=["claude", "codex", "print", "fake"])
            p.add_argument("--judge-model")

    p = sub.add_parser("init", help="write config.json and create the tree")
    p.add_argument("--name", help="your first name as the model should call you")
    p.add_argument("--timezone")
    p.add_argument("--engine", choices=["claude", "codex", "print", "fake"])
    p.add_argument("--claude-dir")
    p.add_argument("--codex-dir")
    p.add_argument("--person", action="append", help="Full Name=<role-token>, repeatable")
    p.add_argument("--model-for", action="append", help="stage=model, repeatable")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("extract", help="read transcripts, scrub, append to the corpus")
    p.add_argument("--claude-dir")
    p.add_argument("--codex-dir")
    p.add_argument("--rescrub", action="store_true", help="re-apply the current scrub to every stored record")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("prep", help="dedup, twins, split, leaks, chunks, seeds")
    p.add_argument("--cutoff", default="auto", help="ISO date/time or auto")
    p.add_argument("--keep-split", action="store_true", help="reuse the cutoff in derived/split.json")
    p.add_argument("--no-twins", action="store_true")
    p.add_argument("--force", action="store_true", help="archive an eval split that already has results")
    p.add_argument("--no-test", action="store_true", help="hold nothing out: a deployment model (eval refuses the split)")
    p.add_argument("--train-before", help="with --no-test: ISO date/time; only sessions that started before it are train")
    p.set_defaults(func=cmd_prep)

    p = sub.add_parser("mine", help="mine each chunk through the engine, one call per chunk per lens")
    p.add_argument("--lens", action="append", help="restrict to these lenses (repeatable): core defaults reactions followthrough vocabulary grants lessons")
    p.add_argument("--single-pass", action="store_true", help="one call per chunk with the combined prompt instead of the lenses")
    engine_opts(p)
    p.set_defaults(func=cmd_mine)

    p = sub.add_parser("synthesize", help="profile, light, examples, grants, index, lint, manifest")
    p.add_argument("substeps", nargs="*", help="subset of: consolidate profile lessons light examples grants index lint manifest")
    p.add_argument("--mined-run")
    p.add_argument("--update", action="store_true", help="give the profile prompt the previous profile as prior")
    p.add_argument("--no-strict", action="store_true", help="forbidden-rule lint hits warn instead of failing")
    engine_opts(p)
    p.set_defaults(func=cmd_synthesize)

    p = sub.add_parser("eval", help="score the model on the held-out split")
    p.add_argument("--dev", action="store_true", help="score the dev split (repeatable, no ledger)")
    p.add_argument("--arm", action="append", choices=["none", "light", "full", "full-all"])
    p.add_argument("--mode", default="first", choices=["first", "context"],
                   help="first: context-free first-message seeds (default); context: short mid-session seeds with the earlier messages")
    p.add_argument("--max-sessions", type=int)
    engine_opts(p, with_judge=True)
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("refresh", help="fold corrections, extend the corpus, render the always-on block")
    p.add_argument("--corrections", action="store_true")
    p.add_argument("--extend", action="store_true")
    p.add_argument("--always-on", action="store_true")
    p.add_argument("--write-to", action="append", help="file to receive the always-on block (repeatable)")
    p.add_argument("--new-cutoff", help="with --extend: mint a new split instead of keeping the current one")
    p.add_argument("--candidate-home", help="copy into a separate private candidate; never alter the source model")
    p.add_argument("--train-before", help="with --extend: deployment candidate from reliable sessions before this date, with no test split")
    engine_opts(p)
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("status", help="pipeline state, counts, drift, calibration, guards")
    p.add_argument("--estimate", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("select", help="print the runtime slice for a seed (exit 4 when no model exists)")
    p.add_argument("--seed", help="the seed text (shell-quoted; prefer --seed-file - with a quoted heredoc)")
    p.add_argument("--seed-file", help="read the seed from this file, or from stdin with `-`")
    p.add_argument("--kind", help="resolved request kind (group id or words)")
    p.add_argument("--project", help="exact project id for scoped corrections and relevant historical episodes")
    p.add_argument("--no-light", action="store_true", help="omit light.md (the always-on block is already in context)")
    p.add_argument("--context-file", help="bounded JSON intent frame from preceding conversation; '-' reads stdin")
    p.add_argument("--mode", default="execute", choices=["execute", "discuss", "status", "hold", "plan", "initiative"])
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("log-decision", help="append one runtime decision (JSON on stdin; the seed is hashed, never stored)")
    p.set_defaults(func=cmd_log_decision)

    p = sub.add_parser("correct", help="append one correction line (JSON on stdin: seed, wrong, meant, scope, kind)")
    p.set_defaults(func=cmd_correct)

    p = sub.add_parser("demo", help="try a fictional engineer's model offline in a separate home")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("recall", help="build or retrieve source-linked intent echoes")
    p.add_argument("action", nargs="?", choices=["build"])
    p.add_argument("--seed")
    p.add_argument("--seed-file")
    p.add_argument("--kind")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=3)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("preferences", help="build and score a frozen human blind comparison")
    p.add_argument("action", choices=["build", "import"])
    p.add_argument("--pairs")
    p.add_argument("--baseline-results")
    p.add_argument("--candidate-results")
    p.add_argument("--baseline-arm", default="full")
    p.add_argument("--candidate-arm", default="full")
    p.add_argument("--seeds-dir")
    p.add_argument("--out")
    p.add_argument("--limit", type=int, default=24)
    p.add_argument("--seed", default="0")
    p.add_argument("--key")
    p.add_argument("--answers")
    p.set_defaults(func=cmd_preferences)

    p = sub.add_parser("tracking", help="measure linked outcomes or review historical brief follow-ups")
    p.add_argument("action", choices=["record", "report", "discover", "import"])
    p.add_argument("--out")
    p.add_argument("--key")
    p.add_argument("--answers")
    p.set_defaults(func=cmd_tracking)

    p = sub.add_parser("foresight", help="register predictions before later turns and measure explicit human outcomes")
    p.add_argument("action", choices=["record", "observe", "report", "replay"])
    p.add_argument("--out")
    p.add_argument("--after")
    p.add_argument("--exclude-sessions")
    p.add_argument("--limit", type=int, default=24)
    p.set_defaults(func=cmd_foresight)

    p = sub.add_parser("schedule", help="prepare a portable weekly candidate refresh runner")
    p.add_argument("action", choices=["plan"])
    p.add_argument("--out", required=True)
    p.add_argument("--weekday", type=int, default=0)
    p.add_argument("--hour", type=int, default=9)
    p.add_argument("--minute", type=int, default=0)
    p.add_argument("--timezone")
    p.add_argument("--max-calls", type=int, default=12)
    p.add_argument("--engine", choices=["claude", "codex"])
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("atlas", help="explore exact personal evidence and intent echoes in a private interactive page")
    p.add_argument("--out", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_atlas)

    p = sub.add_parser("lint", help="lint the public skill or the built model")
    p.add_argument("--skill", help="skill directory to lint")
    p.add_argument("--deny-file", help="extra forbidden strings, one per line")
    p.add_argument("--model", action="store_true")
    p.add_argument("--strict", action="store_true", help="model lint: names and secrets are fatal too")
    p.set_defaults(func=cmd_lint)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return EXIT_USAGE
    os.umask(0o077)  # everything this CLI writes describes one person: directories 0700, files 0600
    home = resolve_home(args.home)
    paths = Paths(home)
    cfg = load_config(paths, args.config)
    log = _log_factory(args.quiet, args.json)
    if args.cmd not in ("init", "lint", "status", "select", "log-decision", "correct", "demo", "preferences", "tracking") and not cfg.get("_exists"):
        log("no config at %s: run `init --name <Name>` first" % paths.config)
        return EXIT_USAGE
    try:
        return args.func(args, paths, cfg, log)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
