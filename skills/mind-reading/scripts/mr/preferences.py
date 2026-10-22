"""Local blind human review. The page never receives the arm key or inferred votes."""
import hashlib
import html
import json
import os
import random
import tempfile
from pathlib import Path

from .config import read_json, write_json


class PreferenceError(ValueError):
    pass


def _safe_path(path):
    """Inspect the lexical path before resolving anything or mutating its tree."""
    path = Path(path).expanduser().absolute()
    # macOS exposes its system temp roots through these fixed aliases. Normalize
    # only those exact OS mappings; any symlink below them is still rejected.
    for alias, target in ((Path("/var"), Path("/private/var")), (Path("/tmp"), Path("/private/tmp"))):
        if alias in path.parents and alias.is_symlink() and alias.resolve() == target:
            path = target / path.relative_to(alias)
    for part in (path,) + tuple(path.parents):
        if part.is_symlink():
            raise PreferenceError("output path contains a symlink: %s" % part.name)
        if part != path and part.exists() and not part.is_dir():
            raise PreferenceError("output parent is not a directory")
    if path.with_name(path.name + ".tmp").is_symlink():
        raise PreferenceError("output temporary path is a symlink")
    normalized = path.resolve()
    if normalized.with_name(normalized.name + ".tmp").is_symlink():
        raise PreferenceError("output temporary path is a symlink")
    return normalized


def _check_output(path, content, replace=False):
    path = _safe_path(path)
    if path.exists():
        if not path.is_file():
            raise PreferenceError("output is not a regular file: %s" % path.name)
        if path.read_bytes() != content and not replace:
            raise PreferenceError("output already contains different content: %s" % path.name)
    return path


def _private_bytes(path, content, replace=False):
    path = _check_output(path, content, replace)
    if path.exists() and path.read_bytes() == content:
        os.chmod(path, 0o600)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".mr-output-", delete=False) as fh:
            temporary = fh.name
            os.chmod(temporary, 0o600)
            fh.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(data):
    return (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _private_json(path, data, replace=False):
    path = _safe_path(path)
    content = _json_bytes(data)
    if replace and path.exists() and path.read_bytes() != content:
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PreferenceError("existing output is not a generated measurement artifact") from exc
        review = (isinstance(previous, dict) and isinstance(data, dict)
                  and previous.get("review_id") == data.get("review_id") and bool(data.get("review_id"))
                  and previous.get("evidence") == data.get("evidence") and bool(data.get("evidence")))
        schedule = (isinstance(previous, dict) and isinstance(data, dict)
                    and previous.get("promotion") == data.get("promotion") == "never automatic"
                    and isinstance(previous.get("status"), str) and isinstance(data.get("status"), str))
        if not (review or schedule):
            raise PreferenceError("existing output is not the same generated measurement artifact")
    _private_bytes(path, content, replace=replace)


def _digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pairs_from_results(baseline_results, candidate_results, baseline_arm="full", candidate_arm="full", seeds_dir=None):
    """Join frozen expansion artifacts. Missing either arm is a gap, never a loss or a generated answer."""
    roots = [Path(baseline_results), Path(candidate_results)]
    arms = [baseline_arm, candidate_arm]
    maps = []
    for root, arm in zip(roots, arms):
        suffix = "-" + arm + ".json"
        maps.append({p.name[:-len(suffix)]: p for p in (root / "expansions").glob("*" + suffix)})
    if set(maps[0]) != set(maps[1]):
        raise PreferenceError("expansion IDs differ; baseline-only %s; candidate-only %s" % (
            sorted(set(maps[0]) - set(maps[1])), sorted(set(maps[1]) - set(maps[0]))))
    inferred = [root.parent.parent / ("seeds-context" if root.name.endswith("-context") else "seeds") for root in roots]
    seeds = Path(seeds_dir) if seeds_dir else inferred[0]
    pairs = []
    for sid in sorted(maps[0]):
        sd = read_json(seeds / (sid + ".json"))
        if not isinstance(sd, dict) or not isinstance(sd.get("seed"), str):
            raise PreferenceError("missing seed artifact for %s; pass --seeds-dir" % sid)
        candidate_seed = inferred[1] / (sid + ".json")
        # Explicit shared seeds support legacy/custom expansion directories. When
        # the candidate has its own seed artifact it must agree even with an override.
        candidate_doc = read_json(candidate_seed) if candidate_seed.exists() else sd if seeds_dir else None
        if not isinstance(candidate_doc, dict) or not isinstance(candidate_doc.get("seed"), str):
            raise PreferenceError("missing candidate seed artifact for %s" % sid)
        identity = lambda doc: {k: doc.get(k, [] if k == "context" else None)
                                for k in ("seed", "context", "source", "session", "project", "time", "position")}
        baseline_seed = inferred[0] / (sid + ".json")
        baseline_doc = read_json(baseline_seed) if baseline_seed.exists() else sd
        if (not isinstance(baseline_doc, dict) or identity(sd) != identity(baseline_doc)
                or identity(sd) != identity(candidate_doc)):
            raise PreferenceError("seed/context/source identity differs for %s; compare the same frozen requests" % sid)
        expansions = [read_json(m[sid]) for m in maps]
        if any(not isinstance(value, dict) for value in expansions):
            raise PreferenceError("expansion artifact is not an object for %s" % sid)
        prompts = [value.get("prompt") for value in expansions]
        pairs.append({"id": sid, "seed": sd["seed"], "context": sd.get("context", []),
                      "baseline": prompts[0], "candidate": prompts[1]})
    return pairs


def _page(items, review_id, title, choices, storage_prefix):
    """Self contained file:// page. DOM textContent prevents transcript text becoming executable HTML."""
    payload = json.dumps({"review_id": review_id, "items": items}, ensure_ascii=False).replace("<", "\\u003c")
    buttons = json.dumps(choices)
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title><style>
body{font:17px/1.55 system-ui,sans-serif;color:#1b1b1b;background:#faf9f6;max-width:1140px;margin:36px auto;padding:0 20px}
h1{font-size:29px}h2{font-size:19px}button{font:inherit;padding:10px 16px;margin:4px;border:1px solid #555;border-radius:3px;background:#fff;cursor:pointer}
button[aria-pressed=true]{background:#202d40;color:white}button:focus-visible,textarea:focus-visible{outline:3px solid #c66b20;outline-offset:3px}
.columns{display:grid;grid-template-columns:1fr 1fr;gap:24px}.candidate{border-top:2px solid #777;padding:12px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.context{white-space:pre-wrap;color:#444;max-height:220px;overflow:auto}textarea{width:100%%;box-sizing:border-box;font:inherit;min-height:68px}
.bar{display:flex;justify-content:space-between;align-items:center;gap:10px;position:sticky;top:0;background:#faf9f6;border-bottom:1px solid #aaa;padding:8px 0}
@media(max-width:720px){.columns{grid-template-columns:1fr}body{margin:16px auto}.bar{flex-wrap:wrap}}</style>
<h1>%(title)s</h1><p id="intro"></p><div class="bar"><span id="progress" aria-live="polite"></span><div><button id="prev">Previous</button><button id="next">Next</button><button id="export">Download answers</button></div></div>
<main><h2 id="case"></h2><details><summary>Earlier context</summary><div class="context" id="context"></div></details><p id="seed"></p><div class="columns" id="columns"></div><div id="choices" role="group" aria-label="Your verdict"></div><label for="note">Optional note</label><textarea id="note"></textarea></main>
<script id="data" type="application/json">%(payload)s</script><script>
'use strict';const data=JSON.parse(document.getElementById('data').textContent),choices=%(choices)s;
const storage=%(storage)s+data.review_id;let answers={},index=0;
try{const saved=JSON.parse(localStorage.getItem(storage)||'{}');if(saved&&typeof saved==='object')answers=saved;}catch(e){}
const el=id=>document.getElementById(id);
function save(){try{localStorage.setItem(storage,JSON.stringify(answers));}catch(e){}}
function render(){const item=data.items[index],answer=answers[item.id]||{};
el('case').textContent='Case '+(index+1)+' of '+data.items.length;el('seed').textContent=item.seed;
el('context').textContent=typeof item.context==='string'?item.context:JSON.stringify(item.context||[],null,2);
el('columns').replaceChildren();for(const [name,text] of Object.entries(item.options)){const box=document.createElement('section'),h=document.createElement('h2'),p=document.createElement('div');h.textContent=name;p.className='candidate';p.textContent=text;box.append(h,p);el('columns').append(box);}
el('choices').replaceChildren();for(const [value,label] of choices){const b=document.createElement('button');b.textContent=label;b.setAttribute('aria-pressed',answer.pick===value?'true':'false');b.onclick=()=>{answers[item.id]={pick:value,note:el('note').value};save();render();};el('choices').append(b);}
el('note').value=answer.note||'';el('progress').textContent=Object.keys(answers).filter(k=>data.items.some(x=>x.id===k)&&answers[k].pick).length+' / '+data.items.length+' reviewed';el('prev').disabled=index===0;el('next').disabled=index===data.items.length-1;}
el('note').oninput=()=>{const id=data.items[index].id;if(answers[id]){answers[id].note=el('note').value;save();}};
el('prev').onclick=()=>{if(index>0){index--;render();}};el('next').onclick=()=>{if(index<data.items.length-1){index++;render();}};
el('export').onclick=()=>{const blob=new Blob([JSON.stringify({review_id:data.review_id,answers:answers},null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='mind-reading-answers-'+data.review_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);};
el('intro').textContent=choices[0][0]==='A'?'Choose the brief you would want the agent to follow. A and B are randomly assigned. Ties and neither are valid. No choice is selected for you. About ten minutes for 24 cases; progress is saved only in this browser. Download answers when ready.':'Read the displayed brief and the next user message. Mark whether the user corrected that brief. Unrelated and uncertain cases stay outside the measured denominator. No choice is selected for you. Download answers when ready.';
render();</script></html>""" % {"title": html.escape(title), "payload": payload, "choices": buttons, "storage": json.dumps(storage_prefix)}


def build_review(pairs, out_dir, seed=0, limit=24):
    if not isinstance(pairs, list) or not pairs:
        raise PreferenceError("pairs must be a nonempty list")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise PreferenceError("limit must be a positive integer")
    clean, ids = [], set()
    for p in pairs:
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p[k].strip() for k in ("id", "seed", "baseline", "candidate")):
            raise PreferenceError("each pair needs nonempty id, seed, baseline and candidate strings")
        if p["id"] in ids:
            raise PreferenceError("duplicate pair id %s" % p["id"])
        ids.add(p["id"])
        clean.append({k: p.get(k, []) for k in ("id", "seed", "context", "baseline", "candidate")})
    clean.sort(key=lambda p: p["id"])
    rng = random.Random(str(seed))
    rng.shuffle(clean)
    clean = clean[:limit]
    review_id = _digest({"pairs": clean, "seed": seed})[:20]
    items, key = [], {}
    for p in clean:
        sides = ["baseline", "candidate"]
        random.Random("%s:%s" % (review_id, p["id"])).shuffle(sides)
        opaque_id = _digest([review_id, p["id"]])[:16]
        items.append({"id": opaque_id, "seed": p["seed"], "context": p["context"],
                      "options": {side: p[arm] for side, arm in zip(("A", "B"), sides)}})
        key[opaque_id] = {"pair_id": p["id"], "A": sides[0], "B": sides[1], "content_hash": _digest(p)}
    out = _safe_path(out_dir)
    key_path = out / "private" / "preference-key.json"
    page = out / "review.html"
    if key_path.exists() and (read_json(key_path) or {}).get("review_id") != review_id:
        raise PreferenceError("review directory already belongs to a different frozen comparison")
    key_data = {"schema_version": 1, "kind": "preference", "review_id": review_id, "pairs": key}
    page_data = _page(items, review_id, "Which brief would you want?", [("A", "A"), ("B", "B"), ("tie", "Both equally"), ("neither", "Neither")], "mr-preferences-").encode("utf-8")
    # Preflight every artifact before creating a directory or writing the first one.
    _check_output(key_path, _json_bytes(key_data))
    _check_output(page, page_data)
    _private_json(key_path, key_data)
    _private_bytes(page, page_data)
    return {"page": str(page), "key": str(key_path), "review_id": review_id, "count": len(items), "reviewed": 0,
            "human_verdict": "unreviewed", "estimated_review_minutes": 10 if len(items) == 24 else round(len(items) * 10 / 24, 1)}


def read_answers(key_path, answers_path, allowed):
    def unique(items):
        out = {}
        for k, v in items:
            if k in out:
                raise PreferenceError("duplicate JSON key %s" % k)
            out[k] = v
        return out
    key = read_json(key_path)
    try:
        envelope = json.loads(Path(answers_path).read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreferenceError("answers are not readable valid JSON") from exc
    if not isinstance(key, dict) or key.get("schema_version") != 1 or not isinstance(key.get("review_id"), str) or \
            len(key["review_id"]) != 20 or not isinstance(key.get("pairs"), dict) or not key["pairs"]:
        raise PreferenceError("invalid private review key")
    if not isinstance(envelope, dict) or envelope.get("review_id") != key.get("review_id"):
        raise PreferenceError("answers do not match the frozen review_id")
    answers = envelope.get("answers")
    if not isinstance(answers, dict) or set(answers) - set(key["pairs"]):
        raise PreferenceError("answers contain unknown case IDs or are not an object")
    for sid, v in answers.items():
        if not isinstance(v, dict) or v.get("pick") not in allowed or not isinstance(v.get("note", ""), str):
            raise PreferenceError("invalid human answer for %s" % sid)
    return key, answers


def import_answers(key_path, answers_path, out_path=None):
    key, answers = read_answers(key_path, answers_path, {"A", "B", "tie", "neither"})
    if key.get("kind") != "preference":
        raise PreferenceError("key is not for a preference review")
    if any(not isinstance(v, dict) or {v.get("A"), v.get("B")} != {"baseline", "candidate"} or not isinstance(v.get("pair_id"), str)
           for v in key["pairs"].values()):
        raise PreferenceError("invalid A/B mapping in private review key")
    counts = {"baseline": 0, "candidate": 0, "tie": 0, "neither": 0}
    labels = {}
    for sid, v in answers.items():
        pick = v["pick"]
        outcome = key["pairs"][sid][pick] if pick in ("A", "B") else pick
        counts[outcome] += 1
        labels[key["pairs"][sid]["pair_id"]] = {"outcome": outcome, "note": v.get("note", "")}
    total, reviewed = len(key["pairs"]), len(answers)
    completed = total == reviewed
    verdict = "incomplete"
    if completed:
        verdict = ("candidate_preferred" if counts["candidate"] > counts["baseline"] else
                   "baseline_preferred" if counts["baseline"] > counts["candidate"] else "tied")
    report = {"schema_version": 1, "review_id": key["review_id"], "evidence": "imported human choices",
              "total": total, "reviewed": reviewed, "missing": total - reviewed, "completed": completed,
              "counts": counts, "verdict": verdict, "positive_human_verdict": verdict == "candidate_preferred",
              "labels": labels, "promotion": "never automatic"}
    if out_path:
        _private_json(out_path, report, replace=True)
    return report
