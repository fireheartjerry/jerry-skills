"""prefer: blind A/B preference, the primary human metric.

`prefer build --a SPEC --b SPEC --seeds DIR` writes a self-contained page (no network: a Content-Security-Policy of
default-src 'none', everything inline) with one pair per seed, sides randomized per pair and pairs shuffled, and a
separate key file that maps each pair back to its arm. `prefer score ANSWERS` unblinds the exported answers, reports
wins, both and neither with an exact two-sided sign test, and records the result in the eval ledger as `kind: prefer`.

Arm specs:
  none                 no model: the skill's own no-model path
  <home>               this skill's SKILL.md and select, with that data home's model
  <skill dir>+<home>   that skill's SKILL.md and its own CLI's select (an older version), with that home's model
  dir:<path>           reuse expansions already written: <path>/<seed id>.md, .txt or .json ("reply", "prompt" or "text")

Seeds: <dir>/*.json with "seed" (and optionally "id", "context": [earlier messages], "time"), or *.txt / *.md whose
whole text is the seed. Generated expansions are cached under <out>/expansions/<arm>/, so a rebuild spends nothing for
pairs that exist. A home arm whose training sessions contain a seed is flagged as contaminated in the key and in the
score: the comparison is only blind to the model if the model never saw the seed."""
import hashlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

from . import budget as _budget
from . import engine as _engine
from .config import Paths, load_config, read_json, read_jsonl, skill_dir, write_json
from .corpus import normalize, now_iso
from .stats import rate_block, sign_test

PAGE = Path(__file__).resolve().parent / "prefer_page.html"
EXPAND_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reply"],
                 "properties": {"reply": {"type": "string", "minLength": 20}}}
PICKS = ("A", "B", "both", "neither")


class PreferError(Exception):
    pass


# ---------------------------------------------------------------- arms and seeds

def _is_home(p):
    p = Path(p)
    return p.is_dir() and ((p / "model").is_dir() or (p / "config.json").exists())


def parse_arm(spec):
    """{"kind": none|home|skill_home|dir, "spec", "label", ...} for one --a / --b value."""
    spec = (spec or "").strip()
    if not spec:
        raise PreferError("empty arm spec")
    if spec == "none":
        return {"kind": "none", "spec": spec, "label": "no model"}
    if spec.startswith("dir:"):
        d = Path(os.path.expanduser(spec[4:]))
        if not d.is_dir():
            raise PreferError("expansions directory %s does not exist" % d)
        return {"kind": "dir", "spec": spec, "dir": str(d), "label": d.name}
    if "+" in spec:
        left, right = spec.rsplit("+", 1)
        sk, home = Path(os.path.expanduser(left)), Path(os.path.expanduser(right))
        if (sk / "SKILL.md").exists() and _is_home(home):
            return {"kind": "skill_home", "spec": spec, "skill": str(sk.resolve()), "home": str(home.resolve()),
                    "label": "%s+%s" % (sk.name, home.name)}
    home = Path(os.path.expanduser(spec))
    if _is_home(home):
        return {"kind": "home", "spec": spec, "home": str(home.resolve()), "label": home.name}
    raise PreferError("arm %r is not `none`, `dir:<path>`, a data home, or <skill dir>+<home>" % spec)


def load_seeds(seeds_dir, limit=None):
    d = Path(os.path.expanduser(str(seeds_dir)))
    if not d.is_dir():
        raise PreferError("seeds directory %s does not exist" % d)
    out = []
    for p in sorted(d.iterdir()):
        if p.suffix == ".json":
            data = read_json(p)
            if not isinstance(data, dict) or not str(data.get("seed") or "").strip():
                continue
            ctx = data.get("context") or []
            ctx = [str(c) for c in ctx if str(c).strip()] if isinstance(ctx, list) else []
            out.append({"id": _safe_id(data.get("id") or p.stem), "seed": str(data["seed"]).strip(), "context": ctx,
                        "time": data.get("time")})
        elif p.suffix in (".txt", ".md"):
            text = p.read_text(encoding="utf-8").strip()
            if text:
                out.append({"id": _safe_id(p.stem), "seed": text, "context": [], "time": None})
    ids = [s["id"] for s in out]
    if len(set(ids)) != len(ids):
        raise PreferError("seed ids repeat in %s" % d)
    if not out:
        raise PreferError("no seeds in %s (*.json with a seed field, or *.txt / *.md)" % d)
    return out[:limit] if limit else out


def _safe_id(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))[:60] or "seed"


def arm_meta(arm):
    """Model hash and skill version of an arm, for the key and the ledger."""
    meta = {k: arm.get(k) for k in ("kind", "spec", "label")}
    home = arm.get("home")
    if home:
        m = read_json(Path(home) / "model" / "MODEL.json", {}) or {}
        meta["model_hash"] = m.get("model_hash")
    sk = Path(arm["skill"]) if arm.get("skill") else (skill_dir() if arm["kind"] in ("home", "none") else None)
    if sk and (sk / "SKILL.md").exists():
        m = re.search(r"version:\s*([0-9][\w.]*)", (sk / "SKILL.md").read_text(encoding="utf-8")[:2000])
        meta["skill_version"] = m.group(1) if m else None
    return meta


def contaminated(arm, seeds):
    """Ids of seeds whose text occurs in a training session of the arm's home (derived/sessions.jsonl, split train)."""
    home = arm.get("home")
    if not home:
        return []
    rows = read_jsonl(Path(home) / "derived" / "sessions.jsonl")
    texts = [normalize(m.get("text", "")) for r in rows if r.get("split") == "train" for m in r.get("messages") or []]
    out = []
    for s in seeds:
        ns = normalize(s["seed"])
        if len(ns) < 12:
            continue
        if any(ns == t or ns in t for t in texts):
            out.append(s["id"])
    return out


def _skill_body(skill):
    text = (Path(skill) / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            text = text[end + 4:]
    return text.strip()


def _slice(arm, seed, cfg):
    """The runtime slice for a seed under an arm, or None when the arm has no model."""
    if arm["kind"] == "home":
        from .select import ModelFormatError, select_slice
        model = Path(arm["home"]) / "model"
        if not (model / "index.json").exists() and not (model / "profile.md").exists():
            return None
        hcfg = load_config(Paths(arm["home"]))
        try:
            text, _ = select_slice(model, seed, hcfg["select"])
        except (ModelFormatError, FileNotFoundError):
            return None
        return text
    if arm["kind"] == "skill_home":
        cli = Path(arm["skill"]) / "scripts" / "mind_reading.py"
        if not cli.exists():
            raise PreferError("%s has no scripts/mind_reading.py" % arm["skill"])
        env = dict(os.environ)
        env.pop("MIND_READING_HOME", None)
        proc = subprocess.run([sys.executable, "-I", str(cli), "--home", arm["home"], "select", "--seed-file", "-"],
                              input=seed, capture_output=True, text=True, timeout=120, env=env)
        return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None
    return None


def _context_block(seed_doc):
    ctx = seed_doc.get("context") or []
    if not ctx:
        return ""
    return ("Earlier messages in this session, oldest first (the agent's replies are not shown):\n\n<earlier_messages>\n%s\n"
            "</earlier_messages>\n" % json.dumps(ctx, ensure_ascii=False, indent=0))


def _read_given(d, sid):
    for ext in (".md", ".txt", ".json"):
        p = Path(d) / (sid + ext)
        if not p.exists():
            continue
        if ext == ".json":
            data = read_json(p) or {}
            for k in ("reply", "prompt", "text", "expansion"):
                if isinstance(data.get(k), str) and data[k].strip():
                    return data[k].strip()
            return None
        return p.read_text(encoding="utf-8").strip() or None
    return None


def expansions(paths, cfg, arm, letter, seeds, out_dir, kind, model=None, max_cost_usd=None, max_calls=None,
               fake_dir=None, log=print, backoff=None, concurrency=2, collect=False):
    """{seed id: reply text} for one arm; generated expansions land in <out_dir>/expansions/<letter>/<id>.json."""
    if arm["kind"] == "dir":
        got = {s["id"]: _read_given(arm["dir"], s["id"]) for s in seeds}
        return {k: v for k, v in got.items() if v}, {"calls": 0, "cost_usd": 0.0}
    from .mine import privacy_line
    skill = arm.get("skill") or str(skill_dir())
    body = _skill_body(skill)
    name = cfg["name"]
    model = _engine.stage_model(cfg, kind, "expand", model)
    run_id = "prefer-%s-%s" % (re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(out_dir).name)[:40], letter)
    eng = _engine.Engine(paths, cfg, kind, "prefer", run_id=run_id, model=model, concurrency=concurrency, max_calls=max_calls,
                         max_cost_usd=max_cost_usd, fake_dir=fake_dir, log=log, backoff=backoff,
                         cost_estimate=_budget.estimator(cfg, model, "prefer", paths.runs),
                         resume="prefer build (same arguments and --out %s)" % out_dir)
    units = []
    for s in seeds:
        sl = _slice(arm, s["seed"], cfg)
        if sl:
            slice_block = "The skill's select command returned this slice of %s's model for the message:\n\n<slice>\n%s\n</slice>" % (name, sl.strip())
        else:
            slice_block = ("There is no model for %s: follow the skill's no-model path, but write only the reply, as if %s had "
                           "already said yes to acting." % (name, name))
        mapping = {"name": name, "privacy": privacy_line(cfg), "skill_md": body, "slice_block": slice_block,
                   "context_block": _context_block(s), "time": s.get("time") or "an unknown time", "seed": s["seed"]}
        units.append(_engine.Unit("%s:%s" % (letter, s["id"]), _engine.render(_engine.load_prompt("prefer-expand"), mapping),
                                  EXPAND_SCHEMA, Path(out_dir) / "expansions" / letter / (s["id"] + ".json")))
    res = eng.run(units, force=False, collect=collect)
    out = {}
    for s, u in zip(seeds, units):
        data = read_json(u.out_path) if u.out_path.exists() else None
        if isinstance(data, dict) and data.get("reply"):
            out[s["id"]] = data["reply"].strip()
    if res.get("budget_note"):
        log("prefer: " + res["budget_note"])
    if res.get("instructions"):
        log(res["instructions"])
    return out, res


def build_page(pairs, build_id, title):
    data = json.dumps({"build_id": build_id, "pairs": pairs}, ensure_ascii=False)
    # <, > and & only occur inside JSON strings, where \\u escapes are valid: nothing in the data can close the script
    # element or open a comment, and JSON.parse returns the original text
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = PAGE.read_text(encoding="utf-8")
    safe_title = re.sub(r"[<>&\"]", "", title)[:120] or "Blind preference"
    return page.replace("__TITLE__", safe_title).replace("__DATA__", data)


def run_build(paths, cfg, a_spec, b_spec, seeds_dir, out=None, n=None, rng_seed=None, kind=None, model=None,
              max_cost_usd=None, max_calls=None, title=None, fake_dir=None, log=print, backoff=None, collect=False):
    pcfg = cfg.get("prefer") or {}
    kind = kind or cfg.get("engine") or "claude"
    arms = {"a": parse_arm(a_spec), "b": parse_arm(b_spec)}
    seeds = load_seeds(seeds_dir, n or pcfg.get("max_pairs"))
    if rng_seed is None:
        rng_seed = int.from_bytes(os.urandom(4), "big")
    out_dir = Path(os.path.expanduser(str(out))) if out else paths.prefer / now_iso().replace(":", "").replace("-", "")[:15]
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out_dir, 0o700)
    except OSError:
        pass
    budget = _budget.Budget(max_cost_usd if max_cost_usd is not None else pcfg.get("max_cost_usd", 2.0),
                            max_calls if max_calls is not None else pcfg.get("max_calls", 120))
    texts, spend, pending = {}, {}, []
    for letter in ("a", "b"):
        texts[letter], res = expansions(paths, cfg, arms[letter], letter, seeds, out_dir, kind, model=model,
                                        max_cost_usd=budget.remaining_cost(), max_calls=budget.remaining_calls(),
                                        fake_dir=fake_dir, log=log, backoff=backoff, collect=collect)
        budget.charge(res)
        spend[letter] = {"calls": res.get("calls", 0), "cost_usd": res.get("cost_usd", 0.0)}
        if res.get("pending"):
            pending.append(res.get("instructions"))
    if pending:
        return {"pending": True, "instructions": "\n".join(i for i in pending if i), "out": str(out_dir),
                "collect": "rerun the same prefer build command with --out %s --collect" % out_dir}
    rng = random.Random(rng_seed)
    usable = [s for s in seeds if texts["a"].get(s["id"]) and texts["b"].get(s["id"])]
    missing = [s["id"] for s in seeds if s not in usable]
    rng.shuffle(usable)
    pairs, key_pairs = [], {}
    for k, s in enumerate(usable, 1):
        pid = "p%02d" % k
        a_left = rng.random() < 0.5
        left, right = ("a", "b") if a_left else ("b", "a")
        pairs.append({"id": pid, "context": s["context"][-3:], "seed": s["seed"], "A": texts[left][s["id"]],
                      "B": texts[right][s["id"]]})
        key_pairs[pid] = {"A": left, "B": right, "seed_id": s["id"]}
    if not pairs:
        raise PreferError("no pair has both expansions (missing: %s); check the arms, the seeds and the budget" % ", ".join(missing[:10]))
    build_id = hashlib.sha256(json.dumps(key_pairs, sort_keys=True).encode() + str(rng_seed).encode()).hexdigest()[:12]
    html = build_page(pairs, build_id, title or "Blind preference")
    page = out_dir / "prefer.html"
    page.write_text(html, encoding="utf-8")
    key = {"version": 1, "build_id": build_id, "created": now_iso(), "rng_seed": rng_seed, "seeds_dir": str(seeds_dir),
           "arms": {k: arm_meta(v) for k, v in arms.items()}, "pairs": key_pairs, "missing": missing,
           "contaminated": {k: contaminated(v, seeds) for k, v in arms.items()},
           "page_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(), "spend": spend, "budget": budget.as_dict()}
    key_path = out_dir / "key.json"
    write_json(key_path, key)
    for p in (page, key_path):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    for letter, ids in key["contaminated"].items():
        if ids:
            log("prefer: warning: arm %s (%s) was trained on %d of the seeds; its comparison is not blind to the model"
                % (letter, arms[letter]["label"], len(ids)))
    log("prefer: %d pair(s) in %s; key in %s (do not open the key before answering)%s" % (
        len(pairs), page, key_path, ("; %d seed(s) without both expansions" % len(missing)) if missing else ""))
    return {"page": str(page), "key": str(key_path), "build_id": build_id, "pairs": len(pairs), "missing": missing,
            "contaminated": key["contaminated"], "spend": spend, "budget": budget.as_dict()}


# ---------------------------------------------------------------- scoring

def load_key(path):
    """The key, normalized: {"build_id", "arms": {id: meta}, "pairs": {pid: {"A": arm, "B": arm}}}. Also reads the
    earlier one-off format, a bare mapping {pid: {"A": label, "B": label}} whose labels are the arm ids."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise PreferError("key %s is not a JSON object" % path)
    if "pairs" in data and isinstance(data["pairs"], dict):
        return data
    pairs = {k: v for k, v in data.items() if isinstance(v, dict) and "A" in v and "B" in v}
    if not pairs:
        raise PreferError("key %s holds no pairs" % path)
    labels = sorted({v["A"] for v in pairs.values()} | {v["B"] for v in pairs.values()})
    return {"version": 0, "build_id": None, "arms": {l: {"label": l} for l in labels}, "pairs": pairs, "contaminated": {}}


def load_answers(path):
    data = read_json(path)
    if not isinstance(data, dict):
        raise PreferError("answers %s are not a JSON object" % path)
    if isinstance(data.get("answers"), dict):
        return data.get("build_id"), data["answers"]
    return None, {k: v for k, v in data.items() if isinstance(v, dict)}


def score(key, answers):
    """Unblinded counts and the sign test. Pure."""
    arm_ids = list(key["arms"].keys())
    if len(arm_ids) != 2:
        raise PreferError("a key needs exactly two arms, found %s" % arm_ids)
    wins = {a: 0 for a in arm_ids}
    wrong = {a: 0 for a in arm_ids}
    both = neither = unknown = 0
    per_seed = []
    for pid, ans in answers.items():
        m = key["pairs"].get(pid)
        pick = (ans or {}).get("pick")
        if m is None or pick not in PICKS:
            unknown += 1
            continue
        for side in (ans.get("wrong") or []):
            if side in ("A", "B"):
                wrong[m[side]] += 1
        if pick in ("A", "B"):
            wins[m[pick]] += 1
            per_seed.append({"pair": pid, "seed_id": m.get("seed_id"), "winner": m[pick]})
        elif pick == "both":
            both += 1
            per_seed.append({"pair": pid, "seed_id": m.get("seed_id"), "winner": "both"})
        else:
            neither += 1
            per_seed.append({"pair": pid, "seed_id": m.get("seed_id"), "winner": "neither"})
    a, b = arm_ids
    answered = wins[a] + wins[b] + both + neither
    return {"arms": key["arms"], "pairs": len(key["pairs"]), "answered": answered, "unanswered": len(key["pairs"]) - answered,
            "unknown": unknown, "wins": wins, "both": both, "neither": neither, "wrong": wrong,
            "p_sign": round(sign_test(wins[a], wins[b]), 4), "share": {a: rate_block(wins[a], wins[a] + wins[b])},
            "at_least_as_good": {a: rate_block(wins[a] + both, answered - neither) if answered - neither else rate_block(0, 0),
                                 b: rate_block(wins[b] + both, answered - neither) if answered - neither else rate_block(0, 0)},
            "per_pair": per_seed}


def run_score(paths, answers_path, key_path=None, ledger=True, log=print):
    answers_path = Path(os.path.expanduser(str(answers_path)))
    build_id, answers = load_answers(answers_path)
    if key_path is None:
        key_path = answers_path.parent / "key.json"
    key_path = Path(os.path.expanduser(str(key_path)))
    if not key_path.exists():
        raise PreferError("no key at %s (pass --key)" % key_path)
    key = load_key(key_path)
    if build_id and key.get("build_id") and build_id != key["build_id"]:
        raise PreferError("the answers are for build %s but the key is for build %s" % (build_id, key["build_id"]))
    res = score(key, answers)
    res["build_id"] = key.get("build_id") or build_id
    res["contaminated"] = {k: len(v) for k, v in (key.get("contaminated") or {}).items()}
    a, b = list(key["arms"].keys())
    log("prefer: %s (%s) vs %s (%s): %s wins %d, %s wins %d, both %d, neither %d of %d answered; sign test p=%.3f" % (
        a, key["arms"][a].get("label"), b, key["arms"][b].get("label"), a, res["wins"][a], b, res["wins"][b], res["both"],
        res["neither"], res["answered"], res["p_sign"]))
    if any(res["contaminated"].values()):
        log("prefer: warning: contaminated seeds per arm %s (the model saw them in training)" % res["contaminated"])
    if ledger and paths is not None:
        entry = {"kind": "prefer", "primary": True, "date": now_iso(), "build_id": res["build_id"],
                 "arms": {k: {kk: v.get(kk) for kk in ("label", "spec", "model_hash", "skill_version")} for k, v in key["arms"].items()},
                 "pairs": res["pairs"], "answered": res["answered"], "wins": res["wins"], "both": res["both"],
                 "neither": res["neither"], "wrong": res["wrong"], "p_sign": res["p_sign"], "share": res["share"],
                 "contaminated": res["contaminated"], "key_path": str(key_path)}
        led = read_json(paths.ledger, []) or []
        led = [e for e in led if not (e.get("kind") == "prefer" and entry["build_id"] and e.get("build_id") == entry["build_id"])]
        led.append(entry)
        paths.eval.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(paths.eval, 0o700)
        except OSError:
            pass
        write_json(paths.ledger, led)
        res["ledger"] = str(paths.ledger)
    return res


def latest_prefer(ledger):
    rows = [e for e in (ledger or []) if e.get("kind") == "prefer"]
    return rows[-1] if rows else None


def format_status_line(entry):
    if not entry:
        return "human preference (primary): none recorded yet (prefer build, then prefer score)"
    ids = list(entry["arms"].keys())
    a, b = ids[0], ids[1] if len(ids) > 1 else ids[0]
    la, lb = entry["arms"][a].get("label") or a, entry["arms"][b].get("label") or b
    return "human preference (primary, %s): %s %d, %s %d, both %d, neither %d of %d; sign test p=%.3f" % (
        (entry.get("date") or "")[:10], la, entry["wins"].get(a, 0), lb, entry["wins"].get(b, 0), entry.get("both", 0),
        entry.get("neither", 0), entry.get("answered", 0), entry.get("p_sign", 1.0))
