"""mine: one engine call per chunk per lens, each output validated against the lens's slice of mined.schema.json and
written to mined/<run_id>/<chunk>-<lens>.json.

A single prompt asked for everything at once returned a thin profile (about 16 traits and 7 unsaid defaults per
chunk on a real corpus). Each lens rereads the whole chunk with one question, so the miner spends its output budget
on one kind of evidence at a time; `synthesize consolidate` groups the lens files by chunk (parse_mined_name) and
merges the items two lenses report in one chunk, unioning evidence without counting a shared quote twice.
`--single-pass` keeps the old one-call prompt (lens `all`) for comparison. Mining costs about six times the tokens of
the single pass; `status --estimate` accounts for the lens count.

Endorsed evidence: drafted, pasted, answered and goal-reminder text is what the user accepted, not what the user
typed. With `mine.endorsed_evidence` on (the default) the miner sees it, cut to its head, and quotes it only with its
origin set. `synthesize consolidate` enforces the rest deterministically: a trait whose evidence is all endorsed is
capped at weak and marked `endorsed` (the profile tags it [endorsed]), and a seed expansion or vocabulary phrase that
never occurs in typed corpus text is dropped, so endorsed text is never evidence of voice or vocabulary. With the flag
off the non-typed messages are removed from the chunk before the engine sees it."""
import copy
import hashlib
import json
import re
from pathlib import Path

from . import engine as _engine
from . import schema as _schema
from .config import read_json, write_json
from .prep import NONTYPED_ORIGINS, chunk_files

# Lens name -> the top-level fields of mined.schema.json it produces. `all` is the single-pass prompt.
LENSES = {
    "core": ["traits", "seed_expansions", "preference_changes", "session_stats"],
    "defaults": ["unsaid_defaults"],
    "reactions": ["reactions", "preference_changes"],
    "followthrough": ["traits", "unsaid_defaults"],
    "vocabulary": ["vocabulary"],
    "grants": ["grants"],
    "all": ["traits", "unsaid_defaults", "seed_expansions", "reactions", "vocabulary", "preference_changes", "grants", "session_stats"],
}
DEFAULT_LENSES = ["core", "defaults", "reactions", "followthrough", "vocabulary", "grants"]
_NAME_RE = re.compile(r"^(chunk-\d+)(?:-([a-z]+))?$")
_LENS_HEAD = re.compile(r"^## ([a-z]+)\s*$")


def chunk_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def mined_files(paths, run_id):
    return sorted((paths.mined / run_id).glob("chunk-*.json"))


def parse_mined_name(stem):
    """`chunk-00` -> ('chunk-00', 'all'); `chunk-00-defaults` -> ('chunk-00', 'defaults'); None when not a mined file."""
    m = _NAME_RE.match(stem)
    if not m:
        return None
    return m.group(1), m.group(2) or "all"


def mined_chunks(paths, run_id):
    """{chunk id: {lens: path}} for one run."""
    out = {}
    for f in mined_files(paths, run_id):
        parsed = parse_mined_name(f.stem)
        if parsed:
            out.setdefault(parsed[0], {})[parsed[1]] = f
    return out


def latest_mined_run(paths):
    runs = sorted(p.name for p in paths.mined.glob("*") if p.is_dir() and any(p.glob("chunk-*.json")))
    return runs[-1] if runs else None


def lens_schema(full, lens):
    """The slice of mined.schema.json a lens must return: its fields, all required, nothing else."""
    fields = LENSES[lens]
    out = {k: v for k, v in full.items() if k not in ("properties", "required")}
    out["properties"] = {k: copy.deepcopy(full["properties"][k]) for k in fields}
    out["required"] = list(fields)
    out["additionalProperties"] = False
    return out


def lens_texts(prompts_dir=None):
    """{lens: instruction text} from references/prompts/mine-lenses.md (one `## <lens>` section each)."""
    text = _engine.load_prompt("mine-lenses", prompts_dir)
    out, cur = {}, None
    for line in text.splitlines():
        m = _LENS_HEAD.match(line)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip() + "\n" for k, v in out.items()}


def resolve_lenses(cfg, lenses=None):
    chosen = list(lenses) if lenses else list((cfg.get("mine") or {}).get("lenses") or DEFAULT_LENSES)
    bad = [l for l in chosen if l not in LENSES]
    if bad:
        raise _engine.EngineError("unknown lens(es) %s; choose from %s" % (bad, " ".join(LENSES)))
    if "all" in chosen and len(chosen) > 1:
        raise _engine.EngineError("lens `all` (single pass) cannot be combined with other lenses")
    return chosen


def endorsed_on(cfg):
    return bool((cfg.get("mine") or {}).get("endorsed_evidence", True))


def strip_nontyped(chunk_json):
    """The chunk with every non-typed message removed (endorsed evidence off)."""
    out = []
    for line in chunk_json.splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        b["messages"] = [m for m in b.get("messages", []) if m.get("origin", "typed") not in NONTYPED_ORIGINS and m.get("origin", "typed") != "question_answer"]
        out.append(json.dumps(b, ensure_ascii=False))
    return "\n".join(out) + "\n"


ENDORSED_RULES = {
    True: ("Messages whose `origin` is drafted, pasted, question_answer or goal_reminder are text {name} accepted, pasted or "
           "answered, not text {name} typed. They are weak evidence of what {name} endorses (scope, standards, decisions): quote "
           "them only with that origin set on the evidence, so the profile marks them [endorsed] and caps them at weak. They are "
           "never evidence of {name}'s voice: draw no vocabulary, no communication-style trait and no seed from them."),
    False: ("Only typed messages are present in this chunk; drafted, pasted and answered text was removed before you saw it."),
}


def build_units(paths, cfg, run_id, schema, prompt_tpl=None, lenses=None):
    prompt_tpl = prompt_tpl or _engine.load_prompt("mine")
    lenses = resolve_lenses(cfg, lenses)
    texts = lens_texts()
    endorsed = endorsed_on(cfg)
    units = []
    for cpath in chunk_files(paths):
        cid = cpath.stem
        chunk_json = cpath.read_text(encoding="utf-8")
        if not endorsed:
            chunk_json = strip_nontyped(chunk_json)
        for lens in lenses:
            sch = schema if lens == "all" else lens_schema(schema, lens)
            mapping = {
                "name": cfg["name"], "chunk_json": chunk_json, "chunk_id": cid, "lens": lens,
                "lens_focus": _engine.render(texts[lens], {"name": cfg["name"], "people_tokens": _engine.people_tokens(cfg)}),
                "endorsed_rule": ENDORSED_RULES[endorsed].format(name=cfg["name"]),
                "people_tokens": _engine.people_tokens(cfg),
                "privacy": privacy_line(cfg),
                "schema_summary": "\n".join(_schema.summary(sch)),
            }
            uid = cid if lens == "all" else "%s:%s" % (cid, lens)
            out_name = (cid if lens == "all" else "%s-%s" % (cid, lens)) + ".json"
            units.append(_engine.Unit(uid, _engine.render(prompt_tpl, mapping), sch, paths.mined / run_id / out_name))
    return units


def privacy_line(cfg):
    return ("{name}'s own private data, used only to build a local model of how {name} works. Copy no secrets, "
            "credentials, phone numbers, addresses or other people's names into your output; use role tokens "
            "{people_tokens}; do not use the network.").format(name=cfg["name"], people_tokens=_engine.people_tokens(cfg))


def reuse_previous(paths, run_id, previous_run, units, log=print):
    """Copy valid outputs from a previous run for chunks whose content hash is unchanged (refresh --extend)."""
    if not previous_run:
        return 0
    reused = 0
    for u in units:
        prev = paths.mined / previous_run / u.out_path.name
        if not prev.exists():
            continue
        data = read_json(prev)
        if not isinstance(data, dict):
            continue
        meta = data.get("_meta") or {}
        cid = parse_mined_name(u.out_path.stem)[0]
        if meta.get("chunk_hash") == chunk_hash(paths.chunks / (cid + ".jsonl")):
            u.out_path.parent.mkdir(parents=True, exist_ok=True)
            u.out_path.write_bytes(prev.read_bytes())
            reused += 1
    if reused:
        log("mine: reused %d unchanged unit(s) from run %s" % (reused, previous_run))
    return reused


def run_mine(paths, cfg, kind, run_id=None, force=False, collect=False, concurrency=4, model=None, max_calls=None,
             max_cost_usd=None, fake_dir=None, reuse_from=None, log=print, backoff=None, lenses=None):
    schema = _engine.load_schema("mined")
    if run_id in (None, "latest") and collect:
        run_id = _engine.latest_run_id(paths, "mine")
        if not run_id:
            raise _engine.EngineError("no mine run to collect")
    if run_id == "latest":
        run_id = _engine.latest_run_id(paths, "mine") or None
    eng = _engine.Engine(paths, cfg, kind, "mine", run_id=run_id, model=_engine.stage_model(cfg, kind, "mine", model),
                         concurrency=concurrency, max_calls=max_calls, max_cost_usd=max_cost_usd, fake_dir=fake_dir,
                         log=log, backoff=backoff)
    units = build_units(paths, cfg, eng.run_id, schema, lenses=lenses)
    if not units:
        raise _engine.EngineError("no chunks in derived/chunks: run prep first")
    if reuse_from and not force:
        reuse_previous(paths, eng.run_id, reuse_from, units, log)
    summary = eng.run(units, force=force, collect=collect)
    # stamp chunk hashes into _meta for refresh --extend
    for u in units:
        if u.out_path.exists():
            data = read_json(u.out_path)
            if isinstance(data, dict):
                cid, lens = parse_mined_name(u.out_path.stem)
                meta = data.setdefault("_meta", {})
                meta["chunk_hash"] = chunk_hash(paths.chunks / (cid + ".jsonl"))
                meta["lens"] = lens
                write_json(u.out_path, data)
    chunk_ids = {parse_mined_name(u.out_path.stem)[0] for u in units}
    summary["chunks"] = len(chunk_ids)
    summary["lenses"] = resolve_lenses(cfg, lenses)
    summary["endorsed_evidence"] = endorsed_on(cfg)
    log("mine: run %s, %d chunk(s) x %d lens(es), ok %d, skipped %d, failed %d, pending %d, stopped %d" % (
        eng.run_id, len(chunk_ids), len(summary["lenses"]), len(summary["ok"]), len(summary["skipped"]), len(summary["failed"]),
        len(summary["pending"]), len(summary["stopped"])))
    if summary.get("budget_note"):
        log("mine: " + summary["budget_note"])
    return summary
