"""refresh: fold corrections.md into the model, extend the corpus, render the always-on block."""
import hashlib
import json
import re
import shutil
from pathlib import Path

from . import engine as _engine
from . import lint as _lint
from . import select as _select
from .config import expand, read_json, save_config, write_json
from .corpus import now_iso
from .mine import privacy_line

BEGIN_RE = re.compile(r"<!-- BEGIN mind-reading\b[^>]*-->")
END_MARK = "<!-- END mind-reading -->"
REFRESH_SCHEMA = {"type": "object", "additionalProperties": False,
                  "required": ["profile_md", "light_md", "examples_md", "notes"],
                  "properties": {"profile_md": {"type": "string", "minLength": 200}, "light_md": {"type": "string", "minLength": 50},
                                 "examples_md": {"type": "string", "minLength": 200},
                                 "notes": {"type": "array", "items": {"type": "string", "maxLength": 300}}}}
_FIELD = re.compile(r"^\s*(date|seed|wrong|meant|scope|kind)\s*:\s*(.*?)\s*$")


class RefreshError(Exception):
    pass


def line_hash(line):
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def parse_corrections(text):
    """(entries, unparseable) where entries are dicts with date, seed, wrong, meant, scope, kind, raw, hash, line."""
    entries, bad = [], []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6 or not re.match(r"^\d{4}-\d{2}-\d{2}", parts[0]):
            bad.append({"line": i, "text": raw})
            continue
        entry = {"date": parts[0][:10], "raw": raw, "hash": line_hash(line), "line": i}
        ok = True
        for p in parts[1:]:
            m = _FIELD.match(p)
            if not m:
                ok = False
                break
            entry[m.group(1)] = m.group(2).strip().strip('"')
        if not ok or not all(k in entry for k in ("seed", "wrong", "meant", "scope", "kind")) or \
                entry["kind"] not in ("fix", "drop", "add") or not (entry["scope"] == "global" or entry["scope"].startswith("project:")):
            bad.append({"line": i, "text": raw})
            continue
        entries.append(entry)
    return entries, bad


def unfolded(entries, folded_through):
    if not folded_through:
        return entries
    for k, e in enumerate(entries):
        if e["hash"] == folded_through:
            return entries[k + 1:]
    return entries


def fold_corrections(paths, cfg, kind, run_id=None, collect=False, fake_dir=None, log=print, backoff=None, model=None, strict_lint=True):
    text = paths.corrections.read_text(encoding="utf-8") if paths.corrections.exists() else ""
    entries, bad = parse_corrections(text)
    manifest = read_json(paths.model_json, {}) or {}
    pending = unfolded(entries, manifest.get("corrections_folded_through"))
    summary = {"corrections_total": len(entries), "unparseable": bad, "pending": len(pending), "applied": 0, "project_notes": 0}
    if not pending:
        log("refresh: no unfolded corrections")
        return summary
    global_lines = [e for e in pending if e["scope"] == "global"]
    project_lines = [e for e in pending if e["scope"].startswith("project:")]
    notes = manifest.get("project_notes", [])
    for e in project_lines:
        notes.append({"project": e["scope"].split(":", 1)[1], "date": e["date"], "seed": e["seed"], "meant": e["meant"], "kind": e["kind"]})
    summary["project_notes"] = len(project_lines)
    folded_drops = list(manifest.get("folded_drops", []))
    if global_lines:
        for n in ("light.md", "profile.md", "examples.md"):
            if not (paths.model / n).exists():
                raise RefreshError("model/%s missing: cannot fold corrections" % n)
        eng = _engine.Engine(paths, cfg, kind, "refresh", run_id=run_id, model=_engine.stage_model(cfg, kind, "synthesize", model),
                             concurrency=1, fake_dir=fake_dir, log=log, backoff=backoff)
        mapping = {"name": cfg["name"], "people_tokens": _engine.people_tokens(cfg), "privacy": privacy_line(cfg),
                   "date": now_iso()[5:10], "corrections_text": "\n".join(e["raw"] for e in global_lines),
                   "profile_md": paths.profile.read_text(encoding="utf-8"), "light_md": paths.light.read_text(encoding="utf-8"),
                   "examples_md": paths.examples.read_text(encoding="utf-8"), "forbidden_rules": _lint.FORBIDDEN_RULES_TEXT}

        def check(data):
            try:
                _select.parse_profile(data["profile_md"].splitlines())
                _select.parse_examples(data["examples_md"].splitlines())
            except _select.ModelFormatError as exc:
                return str(exc)
            errs = _select.check_light(_select.ensure_light_header(data["light_md"], cfg["name"]), cfg["name"], cfg["synth"]["light_max_lines"])
            return "\n".join(errs) if errs else None
        unit = _engine.Unit("corrections", _engine.render(_engine.load_prompt("refresh"), mapping), REFRESH_SCHEMA,
                            eng.run_dir / "refresh-corrections.json", check=check)
        res = eng.run([unit], force=True, collect=collect)
        if res["pending"]:
            summary["instructions"] = res["instructions"]
            summary["run_id"] = eng.run_id
            return summary
        if res["failed"]:
            raise RefreshError("refresh call failed: %s" % res["errors"].get("corrections"))
        if res["stopped"]:
            raise RefreshError("refresh call stopped: %s" % res.get("budget_note", "budget reached"))
        data = read_json(unit.out_path)
        from .synthesize import archive_previous
        from . import lessons as _lessons
        old_section = _lessons.section_of(paths.profile.read_text(encoding="utf-8"))
        new_profile = _nl(data["profile_md"])
        if old_section and not _lessons.section_of(new_profile):
            # the engine dropped the whole lessons section: keep the one that was there (a `drop` correction removes
            # single lessons, never the section)
            drops = [e["meant"].lower() for e in global_lines if e["kind"] == "drop" and e.get("meant")]
            old_section = [l for l in old_section if not (l.startswith("- ") and any(d in l.lower() for d in drops))]
            new_profile = _lessons.insert_section(new_profile, old_section)
            log("refresh: kept the lessons section the engine left out")
        archive_previous(paths, log)
        paths.profile.write_text(new_profile, encoding="utf-8")
        paths.light.write_text(_select.ensure_light_header(data["light_md"], cfg["name"]), encoding="utf-8")
        paths.examples.write_text(_nl(data["examples_md"]), encoding="utf-8")
        summary["notes"] = data.get("notes", [])
        summary["applied"] = len(global_lines)
        folded_drops += [e["meant"] for e in global_lines if e["kind"] == "drop"]
    from .synthesize import write_manifest
    _select.build_index(paths.model)
    hits = _lint.lint_model(paths, cfg)
    if hits["forbidden"] and strict_lint:
        raise RefreshError("model lint found forbidden rules after refresh:\n" + "\n".join(hits["forbidden"]))
    write_manifest(paths, cfg, engine=kind if global_lines else None, folded_through=pending[-1]["hash"],
                   corrections_folded=manifest.get("corrections_folded", 0) + len(pending),
                   extra={"project_notes": notes, "folded_drops": folded_drops})
    log("refresh: folded %d correction(s) (%d applied to the model, %d project notes)" % (len(pending), len(global_lines), len(project_lines)))
    return summary


def _nl(t):
    return t if t.endswith("\n") else t + "\n"


def _demote(light_text):
    out = []
    for line in light_text.splitlines():
        if line.startswith("## "):
            out.append("#" + line)
        elif line.startswith("# "):
            out.append("#" + line)
        else:
            out.append(line)
    return "\n".join(out).strip("\n")


def render_block(light_text, model_hash, name, date=None, home_label="$MIND_READING_HOME"):
    date = date or now_iso()[:10]
    footer = ("Precedence: the harness's rules, then a repo's AGENTS.md or CLAUDE.md and project memory, then %s's newest "
              "words, then this block. Full expansion on demand: the mind-reading skill (\"read my mind: ...\")." % name)
    return ("<!-- BEGIN mind-reading (generated %s from %s/model/light.md, model %s; delete this block to remove) -->\n"
            "%s\n\n%s\n%s\n" % (date, home_label, model_hash, _demote(light_text), footer, END_MARK))


def current_block(paths, cfg):
    if not paths.light.exists():
        raise RefreshError("model/light.md missing")
    manifest = read_json(paths.model_json, {}) or {}
    return render_block(paths.light.read_text(encoding="utf-8"), manifest.get("model_hash") or _select.files_hash(paths.model), cfg["name"])


def _strip_date(block):
    return BEGIN_RE.sub("<!-- BEGIN mind-reading -->", block).strip()


def extract_block(text):
    m = BEGIN_RE.search(text)
    if not m:
        return None
    end = text.find(END_MARK, m.start())
    if end < 0:
        return None
    return text[m.start():end + len(END_MARK)]


def write_block(target, block, log=print):
    target = expand(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    if target.exists():
        backup = target.with_name(target.name + ".bak-" + now_iso()[:10])
        shutil.copy2(target, backup)
        log("backup: %s" % backup)
    existing = extract_block(old)
    if existing:
        new = old.replace(existing, block.rstrip("\n"))
    else:
        new = (old.rstrip("\n") + "\n\n" if old.strip() else "") + block
    target.write_text(new if new.endswith("\n") else new + "\n", encoding="utf-8")
    return target


def always_on(paths, cfg, write_to=None, log=print):
    """Render the always-on block and write it to each target. light.md is hand-editable and the block is loaded by
    every session, so a forbidden rule in it is refused here (exit 4 through the CLI) before anything is rendered."""
    if not paths.light.exists():
        raise RefreshError("model/light.md missing")
    hits = _lint.forbidden_hits(paths.light.read_text(encoding="utf-8"), "light.md")
    if hits:
        raise RefreshError("light.md carries forbidden rules; the always-on block was not rendered or written:\n" + "\n".join(hits))
    block = current_block(paths, cfg)
    targets = []
    for t in write_to or []:
        write_block(t, block, log)
        s = str(t)
        if s not in cfg.get("always_on_targets", []):
            cfg.setdefault("always_on_targets", []).append(s)
        targets.append(s)
    if write_to:
        save_config(paths, cfg)
        log("always-on block written to %s" % ", ".join(targets))
    return block


def drift(paths, cfg):
    """[(target, status)] with status in current, drifted, missing, no-file."""
    out = []
    try:
        block = _strip_date(current_block(paths, cfg))
    except RefreshError:
        return [(t, "no-model") for t in cfg.get("always_on_targets", [])]
    for t in cfg.get("always_on_targets", []):
        p = expand(t)
        if not p.exists():
            out.append((t, "no-file"))
            continue
        found = extract_block(p.read_text(encoding="utf-8"))
        if not found:
            out.append((t, "missing"))
        elif _strip_date(found) == block:
            out.append((t, "current"))
        else:
            out.append((t, "drifted"))
    return out


def run_refresh(paths, cfg, kind, corrections=True, extend=False, always_on_flag=False, write_to=None, new_cutoff=None,
                run_id=None, collect=False, fake_dir=None, log=print, backoff=None, model=None, concurrency=4):
    summary = {}
    if extend:
        from .corpus import run_extract
        from .mine import latest_mined_run, run_mine
        from .prep import run_prep
        from .synthesize import run_synthesize
        log("refresh --extend: reading transcripts from claude=%s codex=%s (config.sources)" % (
            cfg["sources"]["claude_projects_dir"], cfg["sources"]["codex_sessions_dir"]))
        summary["extract"] = run_extract(paths, cfg, log=log)
        # keep_split freezes test_ids and dev_ids exactly; --new-cutoff mints a fresh held-out set instead
        summary["prep"] = run_prep(paths, cfg, cutoff=new_cutoff or "auto", keep_split=not new_cutoff, force=False, log=log)["split"]["split_id"]
        prev = latest_mined_run(paths)
        summary["mine"] = run_mine(paths, cfg, kind, run_id=run_id, concurrency=concurrency, fake_dir=fake_dir, reuse_from=prev, log=log, backoff=backoff)
        summary["synthesize"] = run_synthesize(paths, cfg, kind, update=True, fake_dir=fake_dir, log=log, backoff=backoff, model=model)
    if corrections:
        summary["corrections"] = fold_corrections(paths, cfg, kind, run_id=run_id, collect=collect, fake_dir=fake_dir, log=log, backoff=backoff, model=model)
    if always_on_flag:
        summary["always_on"] = always_on(paths, cfg, write_to, log)
    return summary


__all__ = ["parse_corrections", "fold_corrections", "render_block", "write_block", "always_on", "drift", "run_refresh",
           "extract_block", "RefreshError", "json"]
