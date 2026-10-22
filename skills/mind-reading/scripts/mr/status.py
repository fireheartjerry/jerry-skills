"""status: pipeline state, counts, drift, calibration, guards. Read-only."""
import json
import os
import re
import stat
from pathlib import Path

from . import refresh as _refresh
from .config import expand, read_json, read_jsonl
from .corpus import load_corpus, sources_gone
from .mine import DEFAULT_LENSES, context_lenses, latest_mined_run, mined_chunks, resolve_lenses
from .prep import chunk_files

UNKNOWN_TAG_BLOCK_FRACTION = 0.01


def _dir_mode_ok(p):
    try:
        return stat.S_IMODE(os.stat(p).st_mode) == 0o700
    except OSError:
        return False


_RULE = re.compile(r"^(Read|Edit|Write|Grep|Glob)\((.*)\)$")


def guard_rule_status(rule, home):
    """How one permissions.deny entry relates to <home>/eval: 'ok', 'broken' (a path form Claude Code does not read as
    absolute: a single leading slash is relative to the settings file) or None (unrelated).

    Accepted forms: Read(~/<path under home>/eval/**) and Read(//<absolute path>/eval/**)."""
    m = _RULE.match(rule.strip()) if isinstance(rule, str) else None
    if not m or m.group(1) != "Read":
        return None
    pat = m.group(2).strip()
    body = re.sub(r"(/\*\*/\*|/\*\*|/\*)$", "", pat)
    candidates = {str(Path(home) / "eval"), str(Path(home).resolve() / "eval")}
    home_dir = str(Path.home())
    tildes = {"~" + c[len(home_dir):] for c in candidates if c.startswith(home_dir + "/")}
    if body.startswith("//") and body[1:] in candidates:
        return "ok"
    if body in tildes:
        return "ok"
    if body in candidates:
        return "broken"
    return None


def eval_guard_present(settings_path, home):
    """(status, note): status True when a valid Read deny covers <home>/eval, False otherwise, None when the settings
    file is absent. The note explains a rule that names the path in a form Claude Code does not treat as absolute."""
    p = expand(settings_path)
    if not p.exists():
        return None, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, "settings file is not valid JSON"
    deny = ((data.get("permissions") or {}).get("deny") or [])
    statuses = [(d, guard_rule_status(d, home)) for d in deny]
    if any(st == "ok" for _, st in statuses):
        return True, None
    broken = [d for d, st in statuses if st == "broken"]
    if broken:
        return False, ("deny rule %r names the path in a form Claude Code does not read as absolute (a single leading slash "
                       "is relative to the settings file); use Read(~/...) or Read(//...)" % broken[0])
    return False, None


def calibration(paths):
    rows = read_jsonl(paths.decisions)
    if not rows:
        return {"invocations": 0}
    n = len(rows)
    s = sum(int(r.get("s") or 0) for r in rows)
    l = sum(int(r.get("l") or 0) for r in rows)
    g = sum(int(r.get("g") or 0) for r in rows)
    held = sum(1 for r in rows if r.get("held"))
    corr = 0
    if paths.corrections.exists():
        corr = len(_refresh.parse_corrections(paths.corrections.read_text(encoding="utf-8"))[0])
    modes = {}
    for r in rows:
        modes[r.get("mode", "?")] = modes.get(r.get("mode", "?"), 0) + 1
    upgrades = 0
    for r in rows:
        try:
            upgrades += int(r.get("upgrades") or 0)
        except (TypeError, ValueError):
            continue
    return {"invocations": n, "mean_s": round(s / n, 2), "mean_l": round(l / n, 2), "mean_g": round(g / n, 2),
            "held_rate": round(held / n, 3), "corrections_per_100": round(100.0 * corr / n, 1), "modes": modes,
            "mean_chars": round(sum(int(r.get("chars") or 0) for r in rows) / n),
            "upgrades_per_100": round(100.0 * upgrades / n, 1), "lessons_in_slice_rate": round(sum(1 for r in rows if r.get("lessons")) / n, 3)}


def gather(paths, cfg):
    st = {"home": str(paths.home), "blocked": [], "warnings": []}
    if not cfg.get("_exists"):
        st["blocked"].append("config.json missing: run init --name <Name>")
    rows = load_corpus(paths)
    by_source = {}
    times = []
    origins = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        origins[r.get("origin", "typed")] = origins.get(r.get("origin", "typed"), 0) + 1
        if r.get("time"):
            times.append(r["time"])
    st["corpus"] = {"records": len(rows), "by_source": by_source, "origins": origins,
                    "date_range": [min(times)[:10], max(times)[:10]] if times else None}
    unknown = read_json(paths.unknown_tags, {}) or {}
    st["unknown_tags"] = dict(sorted(unknown.items(), key=lambda kv: -kv[1])[:10])
    total_unknown = sum(unknown.values())
    if rows and total_unknown > UNKNOWN_TAG_BLOCK_FRACTION * len(rows):
        st["blocked"].append("unknown harness tags in %d records (%.1f%%): add them to filters.SKIP_PREFIXES or confirm they are typed text" % (
            total_unknown, 100.0 * total_unknown / len(rows)))
    st["sources_gone"] = [{"path": p, "records_retained": n} for p, n in sources_gone(paths)]
    report = read_json(paths.scrub_report, {}) or {}
    runs = report.get("runs") or []
    st["scrub"] = runs[-1].get("counts", {}) if runs else {}
    split = read_json(paths.split)
    st["split"] = {k: split.get(k) for k in ("cutoff", "split_id", "counts")} if split else None
    prep_report = read_json(paths.prep_report, {}) or {}
    st["prep"] = {k: prep_report.get(k) for k in ("messages_in", "messages_out", "within_session_duplicates_removed",
                                                  "leak_removed_train", "leak_removed_dev", "leak_exempt_short")} if prep_report else None
    st["twins"] = len(prep_report.get("twins", [])) if prep_report else 0
    st["bulk_stamped"] = len(prep_report.get("bulk_stamped_sessions", [])) if prep_report else 0
    st["chunks"] = len(chunk_files(paths))
    mr = latest_mined_run(paths)
    # a chunk counts as mined when every lens the config asks for has an output (one file per chunk per lens)
    lenses = _lenses(cfg)
    done = mined_chunks(paths, mr) if mr else {}
    st["mined"] = {"run": mr, "done": sum(1 for got in done.values() if all(l in got for l in lenses) or "all" in got),
                   "started": len(done), "total": st["chunks"], "lenses": lenses}
    manifest = read_json(paths.model_json)
    if manifest and not manifest.get("model_hash"):
        manifest = None  # only grant bookkeeping so far (synthesize grants ran before the manifest step): no model yet
    legacy_files = [n for n in ("light.md", "profile.md", "examples.md") if (paths.model / n).exists()]
    if manifest:
        files = {}
        for n in ("light.md", "profile.md", "examples.md", "grants.md", "index.json"):
            p = paths.model / n
            if p.exists():
                size = p.stat().st_size
                files[n] = {"bytes": size, "tokens_est": size // 4}
        st["model"] = {"built": manifest.get("built"), "model_hash": manifest.get("model_hash"), "files": files,
                       "parents": manifest.get("parents", []), "corrections_folded": manifest.get("corrections_folded", 0)}
    elif legacy_files and not paths.index.exists():
        st["model"] = {"built": None, "model_hash": None, "files": {n: {"bytes": (paths.model / n).stat().st_size} for n in legacy_files},
                       "parents": [], "corrections_folded": 0, "legacy": True}
        st["warnings"].append("model predates the index format (no index.json, no MODEL.json): select reads it through the legacy "
                              "fallback (every example, matching groups); rebuild with synthesize for the indexed slice")
    else:
        st["model"] = None
        st["blocked"].append("no model: run prep, mine, synthesize")
    if st["model"] and paths.index.exists():
        from .select import index_is_stale
        if index_is_stale(paths.model):
            st["warnings"].append("index.json is stale (a model file changed after `synthesize index`): select rebuilds it in "
                                  "memory on every call; run `synthesize index manifest`")
    st["lessons"] = {"groups": 0, "lessons": 0}
    idx = read_json(paths.index) if st["model"] and paths.index.exists() else None
    if isinstance(idx, dict):
        st["lessons"] = {"groups": len(idx.get("lessons") or []), "lessons": sum(int(g.get("count") or 0) for g in idx.get("lessons") or [])}
    st["grants"] = {"total": 0, "active": 0, "invalid": 0, "revoked": 0, "errors": []}
    if paths.grants.exists():
        from . import grants as _grants
        gs, gerrs = _grants.parse_grants(paths.grants.read_text(encoding="utf-8"), tokens=list((cfg.get("people") or {}).values()))
        invalid = [g for g in gs if g.get("errors")]
        # active means valid and active: an invalid grant never applies, so it is not counted as active
        st["grants"] = {"total": len(gs), "active": sum(1 for g in gs if g.get("active") and not g.get("errors")),
                        "invalid": len(invalid), "revoked": sum(1 for g in gs if not g.get("active") and not g.get("errors")),
                        "errors": gerrs}
        if gerrs:
            st["blocked"].append("grants.md has %d invalid entr%s (run lint --model); invalid grants never apply" % (
                len(gerrs), "y" if len(gerrs) == 1 else "ies"))
    loose = paths.private()
    if loose:
        st["warnings"].append("not mode 0700: %s (chmod -R go-rwx %s; the next init/extract/prep tightens the tree)" % (", ".join(loose), paths.home))
    from .config import spool_path
    sp = spool_path()
    st["spool_pending"] = len(read_jsonl(sp)) if sp.exists() else 0
    if st["spool_pending"]:
        st["warnings"].append("%d spooled runtime line(s) wait in %s (applied when the data home is writable)" % (st["spool_pending"], sp))
    st["ledger"] = read_json(paths.ledger, []) or []
    # the human metrics: blind preference (primary) and the correction rate in real use
    from . import prefer as _prefer
    st["prefer"] = _prefer.latest_prefer(st["ledger"])
    st["track"] = read_json(paths.track_summary)
    from . import auto as _auto
    st["auto"] = _auto.last_run(paths)
    try:
        from . import schedule as _schedule
        st["schedule"] = _schedule.status(paths, cfg)
    except Exception as exc:  # status is read-only and must not fail on the scheduler
        st["schedule"] = {"error": str(exc)}
    if paths.chunk_pins.exists() and st["chunks"]:
        st["incremental"] = True
    corr_entries, corr_bad = ([], [])
    if paths.corrections.exists():
        corr_entries, corr_bad = _refresh.parse_corrections(paths.corrections.read_text(encoding="utf-8"))
    folded_through = (manifest or {}).get("corrections_folded_through")
    pending = len(_refresh.unfolded(corr_entries, folded_through))
    st["corrections"] = {"total": len(corr_entries), "pending": pending, "unparseable": corr_bad}
    if len(corr_entries) > 200:
        st["warnings"].append("corrections.md has %d lines: run refresh --corrections" % len(corr_entries))
    st["calibration"] = calibration(paths)
    st["drift"] = []
    if cfg.get("always_on_targets") and manifest:
        st["drift"] = _refresh.drift(paths, cfg)
        for t, s in st["drift"]:
            if s != "current":
                st["blocked"].append("always-on block %s in %s: run refresh --always-on --write-to %s" % (s, t, t))
    if paths.eval.exists() and not _dir_mode_ok(paths.eval):
        st["blocked"].append("eval/ is not mode 0700: chmod 700 %s" % paths.eval)
    guard, note = eval_guard_present(cfg.get("claude_settings_path", "~/.claude/settings.json"), paths.home)
    st["eval_guard"], st["eval_guard_note"] = guard, note
    if guard is False:
        st["blocked"].append("no valid permissions.deny guard for %s in %s (see build.md step 4)%s" % (
            paths.eval, cfg.get("claude_settings_path"), (": " + note) if note else ""))
    codex_targets = [t for t in cfg.get("always_on_targets", []) if ".codex" in str(t)]
    if codex_targets and not cfg.get("codex_isolate_home", True):
        st["blocked"].append("codex_isolate_home is false and the always-on block is in %s: codex engine runs would read it, "
                             "contaminating mining and the `none` eval arm" % codex_targets[0])
    return st


CHARS_PER_TOKEN = 4


def _lenses(cfg):
    try:
        return resolve_lenses(cfg)
    except Exception:  # status is read-only and must not fail on a bad config; mine reports the error itself
        return list(DEFAULT_LENSES)


def estimate(paths, cfg, st):
    """Token estimates derived from what the prompts carry: chunk characters divided by CHARS_PER_TOKEN plus the prompt
    text and the model's output. Mining makes one call per chunk per lens (seven by default, one with --single-pass),
    and every lens rereads the whole chunk, so the mine figures scale with the lens count. They are estimates; the
    run's calls.jsonl records what was spent."""
    prep_report = read_json(paths.prep_report, {}) or {}
    chunk_chars = prep_report.get("chunk_chars") or []
    chunks = st["chunks"] or max(1, st["corpus"]["records"] // 400)
    total_chunk_chars = sum(chunk_chars) if chunk_chars else chunks * cfg["prep"]["chunk_chars"]
    user_chars = sum(prep_report.get("chunk_user_chars") or []) or total_chunk_chars
    ctx_chars = max(0, total_chunk_chars - user_chars)
    lenses = _lenses(cfg)
    n_lenses = len(lenses)
    n_ctx = len([l for l in lenses if l in set(context_lenses(cfg))])
    # chunk, prompt and schema summary, per lens; only the agent-context lenses read the agent context
    mine_in = (user_chars // CHARS_PER_TOKEN + chunks * 2500) * n_lenses + (ctx_chars // CHARS_PER_TOKEN) * n_ctx
    mine_out = chunks * 6000 * n_lenses
    synth_budget = cfg["synth"]["budget_chars"] // CHARS_PER_TOKEN
    synth = [synth_budget // 3 + 40000, synth_budget + 80000]  # consolidated JSON plus the three outputs
    split = st.get("split") or {}
    test = (split.get("counts") or {}).get("test_seeds") or 0
    arms = len(cfg["eval"]["arms"])
    sessions = min(test, cfg["eval"]["max_sessions"])
    truth_chars = cfg["prep"]["truth_max_messages"] * cfg["prep"]["truth_max_chars"]
    slice_tokens = cfg["select"]["budget_chars"] // CHARS_PER_TOKEN
    per_session_lo = 3000 + arms * (1500 + 1200) + arms * 3000  # requirements, expansions, judgments: short sessions
    per_session_hi = truth_chars // CHARS_PER_TOKEN + 2000 + arms * (slice_tokens + 1500) + arms * (truth_chars // CHARS_PER_TOKEN + 2500)
    # measured once (Sonnet, a 172k-character chunk): 92k tokens as the CLI reports them, so the upper bound is 2x
    return {"chunks": chunks, "lenses": n_lenses, "mine_calls": chunks * n_lenses, "mine_tokens": [int(mine_in * 0.9 + mine_out), int(mine_in * 2.0 + mine_out * 2)],
            "synthesize_tokens": synth,
            "eval_sessions": sessions, "eval_calls": sessions * (1 + 2 * arms),
            "eval_tokens": [sessions * per_session_lo, sessions * per_session_hi],
            "basis": "chunk and truth characters / %d plus prompts and outputs; estimates, see runs/*/calls.jsonl for actuals" % CHARS_PER_TOKEN,
            "wall_minutes_at_concurrency_4": {"mine": [max(2, chunks * n_lenses // 4), max(5, chunks * n_lenses)], "synthesize": [5, 20], "eval": [max(2, sessions // 2), max(5, sessions * 2)]}}


def format_text(st, est=None):
    out = []
    if st["blocked"]:
        out.append("BLOCKED:")
        out += ["  - " + b for b in st["blocked"]]
    for w in st["warnings"]:
        out.append("warning: " + w)
    c = st["corpus"]
    out.append("corpus: %d records %s, dates %s, origins %s" % (c["records"], c["by_source"], c["date_range"], c["origins"]))
    if st["unknown_tags"]:
        out.append("unknown tags: " + ", ".join("%s=%d" % kv for kv in st["unknown_tags"].items()))
    for g in st["sources_gone"]:
        out.append("source gone: %s (%d records retained)" % (g["path"], g["records_retained"]))
    if st["scrub"]:
        out.append("scrub (last run): " + ", ".join("%s=%d" % kv for kv in sorted(st["scrub"].items())))
    if st["split"]:
        out.append("split %s: cutoff %s, %s" % (st["split"]["split_id"], st["split"]["cutoff"], st["split"]["counts"]))
    if st["prep"]:
        out.append("prep: %s; twins %d, bulk-stamped %d" % (st["prep"], st["twins"], st["bulk_stamped"]))
    out.append("chunks: %d; mined: %d of %d chunk(s) x %d lens(es) (run %s)" % (
        st["chunks"], st["mined"]["done"], st["mined"]["total"], len(st["mined"].get("lenses") or []) or 1, st["mined"]["run"]))
    if st["model"]:
        m = st["model"]
        if m.get("legacy"):
            out.append("model: legacy format (no index.json); files %s" % ", ".join(m["files"]))
        else:
            out.append("model %s built %s, corrections folded %d" % (m["model_hash"], m["built"], m["corrections_folded"]))
            for n, f in m["files"].items():
                out.append("  %s: %d bytes (~%d tokens)" % (n, f["bytes"], f["tokens_est"]))
    if st["model"] and not st["model"].get("legacy"):
        ls = st.get("lessons") or {}
        out.append("lessons: %d in %d group(s)%s" % (ls.get("lessons", 0), ls.get("groups", 0),
                                                     "" if ls.get("lessons") else " (none yet: mine with the lessons lens, then synthesize)"))
    g = st.get("grants") or {}
    out.append("grants: %d total, %d active%s%s" % (g.get("total", 0), g.get("active", 0),
                                                    (", %d revoked" % g["revoked"]) if g.get("revoked") else "",
                                                    (", %d invalid (never applied)" % g["invalid"]) if g.get("invalid") else ""))
    from . import prefer as _prefer
    from . import track as _track
    from . import auto as _auto
    out.append(_prefer.format_status_line(st.get("prefer")))
    out.append(_track.format_status_line(st.get("track")))
    for e in st["ledger"]:
        if e.get("kind") == "prefer":
            continue
        out.append("eval look: model %s split %s on %s arms %s%s (automated judge, secondary to the two human metrics)" % (
            e.get("model_hash"), e.get("split_id"), (e.get("date") or "")[:10], e.get("arms"), " TUNED" if e.get("forced") else ""))
    cr = st["corrections"]
    out.append("corrections: %d total, %d pending, %d unparseable" % (cr["total"], cr["pending"], len(cr["unparseable"])))
    for b in cr["unparseable"]:
        out.append("  unparseable line %d: %s" % (b["line"], b["text"][:80]))
    cal = st["calibration"]
    if cal["invocations"]:
        out.append("calibration: %d invocations, mean S/L/G %s/%s/%s, held rate %s, corrections per 100: %s, upgrades per 100: %s, mean chars %s" % (
            cal["invocations"], cal["mean_s"], cal["mean_l"], cal["mean_g"], cal["held_rate"], cal["corrections_per_100"],
            cal["upgrades_per_100"], cal["mean_chars"]))
    else:
        out.append("calibration: no runtime decisions recorded yet")
    for t, s in st["drift"]:
        out.append("always-on %s: %s" % (t, s))
    out.append(_auto.format_status_line(st.get("auto")))
    sch = st.get("schedule") or {}
    if sch.get("error"):
        out.append("schedule: unknown (%s)" % sch["error"])
    elif sch:
        from . import schedule as _schedule
        out.append(_schedule.format_status_line(sch))
    if st["eval_guard"] is None:
        out.append("eval guard: no Claude settings file found (nothing to check)")
    else:
        out.append("eval guard: %s%s" % ("present" if st["eval_guard"] else "MISSING", (" (" + st["eval_guard_note"] + ")") if st.get("eval_guard_note") else ""))
    if est:
        out.append("estimate: %s" % json.dumps(est))
    return "\n".join(out) + "\n"
