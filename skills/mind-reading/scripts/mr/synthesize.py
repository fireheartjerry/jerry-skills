"""synthesize: consolidate -> profile -> lessons -> light -> examples -> grants -> index -> lint -> manifest. Each
substep resumable. `lessons` writes the profile's `## What works and what doesn't` section deterministically from the
consolidated lessons (lessons.py); no engine call."""
import json
import hashlib
import re
import shutil
from pathlib import Path

from . import engine as _engine
from . import grants as _grants
from . import lessons as _lessons
from . import lint as _lint
from . import select as _select
from .config import read_json, write_json
from .corpus import corpus_hash, load_corpus, now_iso
from .mine import LENSES, latest_mined_run, mined_chunks, mined_files, privacy_line
from .prep import NONTYPED_ORIGINS, PrepRefused, parse_time, training_rows

SUBSTEPS = ["consolidate", "profile", "lessons", "light", "examples", "grants", "index", "lint", "manifest"]
UPGRADE_TAG = "upgrade"
VERIFICATION_VERSION = 2
UPGRADES_MIN, UPGRADES_MAX = 2, 4
LAYERS = ["thinking", "quality_bar", "building", "design_taste", "communication", "standing_rules", "likes",
          "dislikes", "workflow", "context"]
ENDORSED_ORIGINS = set(NONTYPED_ORIGINS) | {"question_answer", "goal_reminder"}
# The order lenses are read in within a chunk: the first lens to report an item owns its wording.
_LENS_ORDER = ["all"] + [l for l in LENSES if l != "all"]
# Two claims (or two defaults) are the same item when their content words overlap at least this much (Jaccard).
MERGE_JACCARD = 0.6
_WORD = re.compile(r"[a-z0-9][a-z0-9'+-]*")
_STOP = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "at", "by", "as", "so", "is", "are", "be", "that",
         "this", "it", "its", "with", "when", "then", "than", "not", "no", "do", "does", "every", "any", "all", "one"}
_NEGATED = re.compile(r"(?i)\b(?:not|no|never|without|avoid|skip|don't|dont|cannot|can't)\b")
PROFILE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["profile_md"],
                  "properties": {"profile_md": {"type": "string", "minLength": 200}}}
LIGHT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["light_md"],
                "properties": {"light_md": {"type": "string", "minLength": 50}}}
EXAMPLES_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["examples_md"],
                   "properties": {"examples_md": {"type": "string", "minLength": 200}}}


class SynthError(Exception):
    pass


UPGRADES_RULE = (
    "Write %d to %d upgrade pairs under `## Upgrades`, from the lessons with `Outcome: bad` above: a seed that asks for the "
    "approach the lesson shows is worse (the user's own words where a seed exists, else a plausible short seed), a full "
    "prompt that takes the lesson's better way for the same goal, `Tags:` that include `upgrade` and the lesson group's id "
    "in lower case (`w1`), and after the blockquote one plain line `Changed: <the literal ask> -> <the better way>, "
    "because <the evidence, with its date> (W1).` Never upgrade toward anything outward-facing, destructive, spending or "
    "permission-changing; such a step stays a question for the user.")
NO_UPGRADES_RULE = "There are no lessons with a bad outcome: write no upgrade pairs and use no `upgrade` tag."


def _content_set(text):
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def jaccard(a, b):
    """Content-word Jaccard of two texts (stop words dropped); 1.0 for two texts with no content words that are equal."""
    sa, sb = _content_set(a), _content_set(b)
    if not sa and not sb:
        return 1.0 if _norm_text(a) == _norm_text(b) else 0.0
    return len(sa & sb) / float(len(sa | sb))


def _norm_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


_STRENGTH_RANK = {"weak": 0, "moderate": 1, "strong": 2}
EVIDENCE_CAP = 2


def _trait_quote_key(e):
    return (e.get("source_id") or e.get("session") or "", _norm_text(e.get("quote")))


def _default_quote_key(e):
    return (e.get("source_id") or e.get("session") or "", _norm_text(e.get("first")), _norm_text(e.get("later")))


def _cap_evidence(evidence, cap=EVIDENCE_CAP):
    """Typed evidence first (a trait's voice is what the user typed), then the rest, in their original order."""
    typed = [e for e in evidence if (e.get("origin") or "typed") == "typed"]
    other = [e for e in evidence if (e.get("origin") or "typed") != "typed"]
    return (typed + other)[:cap]


def _merge_into(base, item, key_fn):
    """Fold `item` into `base`: evidence unioned by quote, count summed without counting shared observations twice
    (a quote both lenses cite is one observation), the stronger strength kept, the lenses that reported it listed."""
    have = {key_fn(e) for e in base["evidence"]}
    item_ev = item.get("evidence") or []
    new_ev = [e for e in item_ev if key_fn(e) not in have]
    if "count" in base or "count" in item:
        # the quotes are a sample of each lens's observations: the share of the second lens's quotes the first lacks
        # estimates the share of its observations that are new (all shared: none new; none shared: all new)
        a, b = int(base.get("count") or 1), int(item.get("count") or 1)
        new_share = len(new_ev) / float(len(item_ev)) if item_ev else 0.0
        base["count"] = max(a, b, int(a + b * new_share + 0.5))
    base["evidence"] = base["evidence"] + new_ev
    if "strength" in base and _STRENGTH_RANK.get(item.get("strength"), -1) > _STRENGTH_RANK.get(base.get("strength"), -1):
        base["strength"] = item["strength"]
    for lens in item.get("lenses") or []:
        if lens not in base["lenses"]:
            base["lenses"].append(lens)


def merge_items(items, kind):
    """Merge one chunk's traits (kind "trait") or unsaid defaults (kind "default") reported by several lenses.

    Two traits are the same item when layer and scope match and their claims overlap at MERGE_JACCARD or more; two
    defaults when scope matches, their kinds agree (a missing kind matches any) and when + default overlap at
    MERGE_JACCARD or more. Order is kept: the first lens to report an item (lens order in mine.LENSES) owns its
    wording."""
    out = []
    key_fn = _trait_quote_key if kind == "trait" else _default_quote_key
    for it in items:
        it = dict(it)
        it["evidence"] = list(it.get("evidence") or [])
        it["lenses"] = list(it.get("lenses") or [])
        target = None
        for o in out:
            if o.get("scope") != it.get("scope"):
                continue
            if kind == "trait":
                if o.get("layer") != it.get("layer"):
                    continue
                if bool(_NEGATED.search(o.get("claim") or "")) != bool(_NEGATED.search(it.get("claim") or "")):
                    continue
                score = jaccard(o.get("claim"), it.get("claim"))
            else:
                if o.get("kind") and it.get("kind") and o["kind"] != it["kind"]:
                    continue
                if bool(_NEGATED.search(o.get("default") or "")) != bool(_NEGATED.search(it.get("default") or "")):
                    continue
                score = jaccard("%s %s" % (o.get("when"), o.get("default")), "%s %s" % (it.get("when"), it.get("default")))
            if score >= MERGE_JACCARD:
                target = o
                break
        if target is None:
            out.append(it)
        else:
            _merge_into(target, it, key_fn)
            if not target.get("kind") and it.get("kind"):
                target["kind"] = it["kind"]
    for o in out:
        o["support_evidence"] = list(o["evidence"])
        o["evidence"] = _cap_evidence(o["evidence"])
    return out


def _typed_index(paths, rows=None):
    """(normalized typed text joined by newlines, True when the corpus had typed records). Seeds and vocabulary must
    come from what the user typed, never from drafted, pasted, answered or goal-reminder text, and never from the
    agent context stored beside a message (it is not in `text`)."""
    rows = training_rows(paths) if rows is None else rows
    typed = [_norm_text(r.get("text")) for r in rows if (r.get("origin") or "typed") == "typed" and r.get("text")]
    return "\n".join(typed), bool(typed)


def _phrase_typed(phrase, blob):
    """True when any ` / `-separated variant of `phrase` occurs in the typed text as a whole word or words."""
    for variant in (phrase or "").split("/"):
        v = _norm_text(variant)
        if not v:
            continue
        i = blob.find(v)
        while i >= 0:
            before = blob[i - 1] if i > 0 else " "
            after = blob[i + len(v)] if i + len(v) < len(blob) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return True
            i = blob.find(v, i + 1)
    return False


_STATED = re.compile(r"(?i)(?:^|[.!?\n;:]\s*)(?:please\s+)?(?:always|never|from now on|remember|do not|don't)\b|"
                     r"\b(?:non[- ]negotiable|standing rule)\b")
_HEDGE = re.compile(r"(?i)\b(?:maybe|probably|usually|sometimes|i think|i prefer|i'd rather|perhaps)\b")
_CORRECTION = re.compile(r"(?i)(?:\b(?:i meant|not that|i said)\b|^(?:critical\s+)?correction\b|"
                         r"^no[,!:]\s*(?!(?:problem|worries|thanks|doubt|issue)\b))")
_WORKFLOW = re.compile(r"(?i)\b(?:git|commit|push|merge|deploy|send|message|email|workflow|approval|ask)\b")


def verify_evidence(evidence, rows, quote_key="quote", typed_only=False):
    """Resolve a quote in its cited training session, replacing miner metadata.

    A matching phrase in another session, pasted text, agent context or a future
    corpus record cannot rescue it. Optional source/key constraints disambiguate
    citations; abbreviated legacy rollout ids are accepted only when unambiguous.
    """
    quote = evidence.get(quote_key) or ""
    cited = evidence.get("session")
    if not quote.strip() or not cited:
        return None
    candidates = [r for r in rows if (not evidence.get("source") or r.get("source") == evidence["source"])
               and (not evidence.get("key") or r.get("key") == evidence["key"])
               and (not evidence.get("source_id") or r.get("source_id") == evidence["source_id"])]
    exact = [r for r in candidates if r.get("session") == cited]
    candidates = exact or [r for r in candidates if _lessons.same_session(r.get("session"), cited)]
    if len({(r.get("source"), r.get("session")) for r in candidates}) != 1:
        return None
    matches = [r for r in candidates if (not typed_only or r.get("origin", "typed") == "typed")
               and quote in (r.get("text") or "")]
    if not matches:
        return None
    matches.sort(key=lambda r: (0 if r.get("time") == evidence.get("time") else 1,
                                r.get("time") or "", r.get("ordinal", 0), r["key"]))
    row = matches[0]
    return {"quote": quote, "time": row.get("time"), "session": row["session"],
            "origin": row.get("origin", "typed"), "source": row.get("source"), "key": row["key"],
            "source_id": row.get("source_id"), "project": row.get("project"), "ordinal": row.get("ordinal", 0)}


def verify_default(evidence, rows):
    first = verify_evidence(_pair_ref(evidence, "first"), rows, "first")
    later = verify_evidence(_pair_ref(evidence, "later", legacy_later=True), rows, "later", typed_only=True)
    if (first is None or later is None or (first["source"], first["session"]) != (later["source"], later["session"])
            or first["ordinal"] >= later["ordinal"]):
        return None
    return dict(later, first=first["quote"], later=later["quote"], first_source_id=first["source_id"],
                first_time=first["time"], first_origin=first["origin"])


def _pair_ref(evidence, prefix, legacy_later=False):
    """A shared session/source names the pair; legacy key/source_id name its later turn."""
    ref = {k: v for k, v in evidence.items() if k not in ("key", "source_id", "time")}
    if legacy_later:
        ref.update({k: evidence[k] for k in ("key", "source_id", "time") if k in evidence})
    for key in ("source", "session", "key", "source_id", "time"):
        if prefix + "_" + key in evidence:
            ref[key] = evidence[prefix + "_" + key]
    return ref


def verify_change(change, rows):
    earlier = verify_evidence(_pair_ref(change, "earlier"), rows, "earlier", typed_only=True)
    later = verify_evidence(_pair_ref(change, "later", legacy_later=True), rows, "later", typed_only=True)
    if earlier is None or later is None:
        return None
    same = (earlier["source"], earlier["session"]) == (later["source"], later["session"])
    if same:
        if earlier["ordinal"] >= later["ordinal"]:
            return None
    else:
        # Independent sessions need explicit citations and reliable chronological
        # dates; their unrelated per-session ordinals establish no ordering.
        first_time, last_time = parse_time(earlier["time"]), parse_time(later["time"])
        if not change.get("earlier_session") or not change.get("later_session") or not first_time or not last_time or first_time >= last_time:
            return None
    return dict(change, earlier_time=earlier["time"], later_time=later["time"], session=later["session"],
                earlier_session=earlier["session"], later_session=later["session"],
                earlier_source_id=earlier["source_id"], later_source_id=later["source_id"],
                source_ids=[earlier["source_id"], later["source_id"]])


def seed_sources(seed, rows):
    """A historical seed is a complete user turn, with every supplied citation honored."""
    text = _norm_text(seed.get("seed"))
    if not text:
        return []
    candidates = [r for r in rows if r.get("origin", "typed") == "typed"
                  and (not seed.get("source") or r.get("source") == seed["source"])
                  and (not seed.get("key") or r.get("key") == seed["key"])
                  and (not seed.get("source_id") or r.get("source_id") == seed["source_id"])]
    if seed.get("session"):
        exact = [r for r in candidates if r.get("session") == seed["session"]]
        candidates = exact or [r for r in candidates if _lessons.same_session(r.get("session"), seed["session"])]
        if len({(r.get("source"), r.get("session")) for r in candidates}) != 1:
            return []
    return sorted({r["source_id"] for r in candidates if text == _norm_text(r.get("text"))})


def _support(text, evidence, sensitive=False):
    """Confidence follows verified independent sessions, never lens/chunk counts."""
    unique = {e["source_id"]: e for e in evidence if e.get("source_id")}
    ev = sorted(unique.values(), key=lambda e: (e.get("time") or "", e["source_id"]))
    typed = [e for e in ev if e.get("origin", "typed") == "typed"]
    sessions = sorted({"%s:%s" % (e.get("source"), e["session"]) for e in typed})
    projects = sorted({e["project"] for e in typed if e.get("project")})
    tentative = [e["quote"] for e in typed if _HEDGE.search(e.get("quote") or "")]
    stated = any(_STATED.search(e.get("quote") or "") and not _HEDGE.search(e["quote"]) for e in typed)
    corrected = any(_CORRECTION.search(e.get("quote") or "") and not _HEDGE.search(e["quote"]) for e in typed)
    sufficient = not sensitive or (len(sessions) >= 3 and len(projects) >= 2)
    confidence = ("strong" if stated or corrected or (len(sessions) >= 3 and sufficient) else
                  "moderate" if len(sessions) >= 2 and sufficient else "weak")
    return {"confidence": confidence, "evidence_class": "correction" if corrected else "stated_rule" if stated else
            "inferred" if typed else "endorsed", "session_count": len(sessions), "project_count": len(projects),
            "sessions": sessions, "projects": projects, "source_ids": [e["source_id"] for e in ev],
            "hedges": tentative, "evidence": ev}


def attach_support(consolidated, rows, split):
    """Attach auditable metadata and writeable sidecar data without changing model headings."""
    groups = {}
    entries = []
    for kind, items in (("trait", [t for ts in consolidated["traits"].values() for t in ts]),
                        ("default", consolidated["unsaid_defaults"]), ("lesson", consolidated["lessons"])):
        for item in items:
            text = item.get("claim") or item.get("default") or item.get("better_alternative") or ""
            identity = kind + "\n" + item.get("scope", "person") + "\n" + _norm_text(text)
            eid = kind[0].upper() + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
            item["id"] = eid
            evidence = item.get("support_evidence") or item.get("evidence") or []
            if kind == "lesson":
                evidence = [verify_evidence(e, rows, typed_only=True) for e in evidence if e.get("role", "user") == "user"]
                evidence = [e for e in evidence if e]
            group = groups.setdefault(eid, {"id": eid, "kind": kind, "text": text, "evidence": [], "items": [],
                                            "sensitive": item.get("layer") in ("workflow", "standing_rules", "communication")
                                            or bool(_WORKFLOW.search(text))})
            group["evidence"].extend(evidence)
            group["items"].append(item)
    for eid in sorted(groups):
        group = groups[eid]
        support = _support(group["text"], group["evidence"], group["sensitive"])
        for item in group["items"]:
            item["support"] = {k: v for k, v in support.items() if k != "evidence"}
            item["strength"] = support["confidence"]
            item["count"] = support["session_count"] or len(support["source_ids"])
        entries.append(dict(support, id=eid, kind=group["kind"], text=group["text"]))
    return {"version": 1, "training": {"split_id": split["split_id"], "session_ids": sorted(split["train_ids"]),
                                       "message_count": len(rows), "content_hash": training_hash(rows)}, "items": entries}


def training_hash(rows):
    """Fingerprint prepared content, including context, so stale caches cannot cross a boundary."""
    records = [{k: r.get(k) for k in ("source_id", "text", "time", "origin", "project", "agent_context")} for r in rows]
    records.sort(key=lambda r: r.get("source_id") or "")
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def profile_minimum(cfg, consolidated):
    """An explicit numeric target wins; automatic size follows verified content."""
    requested = cfg["synth"].get("profile_min_lines")
    if requested is not None:
        return int(requested)
    claims = [t for values in consolidated.get("traits", {}).values() for t in values]
    claims += consolidated.get("unsaid_defaults") or []
    unique = {t.get("id") or t.get("claim") or t.get("default") for t in claims}
    entries = len(unique) + len(consolidated.get("vocabulary") or [])
    return min(60, 24 + 2 * entries)


def consolidate(paths, mined_run, budget_chars=600000, log=print):
    """Merge mined chunks: lens files grouped by chunk, traits and unsaid defaults merged across lenses with evidence
    counts, project facts aside, vocabulary merged, seed expansions pooled.

    Within one chunk the lenses reread the same messages, so a trait the core and followthrough lenses both report is
    one item (merge_items). Items from different chunks stay separate; the profile prompt weighs them by chunk count.
    Seeds and vocabulary are kept only when they occur in typed corpus text, and a trait whose evidence is all
    endorsed (drafted, pasted, answered, goal reminder) is capped at weak and marked `endorsed`. A trait, default,
    reaction or preference-change quote that occurs only in stored agent context (never in a corpus message) is
    dropped whatever lens reported it, and a trait or default left without evidence goes with it: agent context
    reaches the profile prompt only through verified lessons, never as a trait, reaction or rule."""
    by_chunk = mined_chunks(paths, mined_run)
    if not by_chunk:
        raise SynthError("no mined chunks in mined/%s" % mined_run)
    lens_order = {name: i for i, name in enumerate(_LENS_ORDER)}
    traits = {layer: [] for layer in LAYERS}
    project_facts, unsaid, seeds, reactions, changes, grant_candidates = [], [], [], [], [], []
    lesson_items = []
    vocab = {}
    stats = {"chunks": 0, "sessions_read": 0, "messages_read": 0, "time_reliable_sessions": 0}
    times = []
    migration = {}
    try:
        rows = training_rows(paths, report=migration)
    except PrepRefused as exc:
        raise SynthError(str(exc))
    split = read_json(paths.split)
    blob, have_typed = _typed_index(paths, rows)
    idx = _lessons.corpus_index(rows)
    dropped = {"seeds": 0, "vocabulary": 0, "endorsed_capped": 0, "agent_only": 0, "unverified": 0}

    def from_agent(*quotes):
        """True when a quote occurs only in stored agent context: never the user's voice, whatever lens reported it."""
        return any(_lessons.agent_only(q, idx) for q in quotes if q)
    for cid in sorted(by_chunk):
        lens_files = sorted(by_chunk[cid].items(), key=lambda kv: (lens_order.get(kv[0], 99), kv[0]))
        chunk_traits, chunk_unsaid, chunk_stats = [], [], {}
        chunk_reactions, chunk_changes = {}, {}
        read_any = False
        for lens, f in lens_files:
            data = read_json(f)
            if not isinstance(data, dict):
                continue
            read_any = True
            for k in ("sessions_read", "messages_read", "time_reliable_sessions"):
                # every lens read the same chunk: count its sessions once
                chunk_stats[k] = max(chunk_stats.get(k, 0), int((data.get("session_stats") or {}).get(k) or 0))
            for t in data.get("traits") or []:
                t = dict(t)
                t["lenses"] = [lens]
                ev = [e for e in t.get("evidence") or [] if not from_agent(e.get("quote"))]
                if len(ev) < len(t.get("evidence") or []):
                    dropped["agent_only"] += len(t.get("evidence") or []) - len(ev)
                    if not ev:
                        continue
                checked = [verify_evidence(e, rows) for e in ev]
                dropped["unverified"] += sum(e is None for e in checked)
                t["evidence"] = [e for e in checked if e]
                if not t["evidence"]:
                    continue
                chunk_traits.append(t)
            for u in data.get("unsaid_defaults") or []:
                u = dict(u)
                u["lenses"] = [lens]
                ev = [e for e in u.get("evidence") or [] if not from_agent(e.get("first"), e.get("later"))]
                if len(ev) < len(u.get("evidence") or []):
                    dropped["agent_only"] += len(u.get("evidence") or []) - len(ev)
                    if not ev:
                        continue
                checked = [verify_default(e, rows) for e in ev]
                dropped["unverified"] += sum(e is None for e in checked)
                u["evidence"] = [e for e in checked if e]
                if not u["evidence"]:
                    continue
                chunk_unsaid.append(u)
            for s_ in data.get("seed_expansions") or []:
                s_ = dict(s_)
                s_["chunk"] = cid
                source_ids = seed_sources(s_, rows)
                if not source_ids:
                    dropped["seeds"] += 1
                    continue
                s_["source_ids"] = source_ids
                s_["evidence_class"] = "reconstructed_example"
                seeds.append(s_)
            for r in data.get("reactions") or []:
                if from_agent(r.get("quote")):
                    dropped["agent_only"] += 1
                    continue
                verified = verify_evidence(r, rows, typed_only=True)
                if verified is None:
                    dropped["unverified"] += 1
                    continue
                r = dict(r, **verified)
                key = (r.get("kind"), _norm_text(r.get("quote")))
                if key not in chunk_reactions:
                    chunk_reactions[key] = dict(r, chunk=cid)
            for v in data.get("vocabulary") or []:
                key = (v.get("phrase") or "").strip().lower()
                if not key:
                    continue
                if not have_typed or not _phrase_typed(v.get("phrase"), blob):
                    dropped["vocabulary"] += 1
                    continue
                entry = vocab.setdefault(key, {"phrase": v.get("phrase"), "meanings": [], "chunks": []})
                if v.get("meaning") and v["meaning"] not in entry["meanings"]:
                    entry["meanings"].append(v["meaning"])
                if cid not in entry["chunks"]:
                    entry["chunks"].append(cid)
            for c in data.get("preference_changes") or []:
                if from_agent(c.get("earlier"), c.get("later")):
                    dropped["agent_only"] += 1
                    continue
                verified = verify_change(c, rows)
                if verified is None:
                    dropped["unverified"] += 1
                    continue
                c = verified
                key = (_norm_text(c.get("earlier")), _norm_text(c.get("later")))
                if key not in chunk_changes:
                    chunk_changes[key] = dict(c, chunk=cid)
            for g in data.get("grants") or []:
                g = dict(g)
                g["chunk"] = cid
                grant_candidates.append(g)
            for l in data.get("lessons") or []:
                lesson_items.append((cid, lens, l))
        if not read_any:
            continue
        stats["chunks"] += 1
        for k, v in chunk_stats.items():
            stats[k] += v
        for t in merge_items(chunk_traits, "trait"):
            t["chunk"] = cid
            for e in t["evidence"]:
                if e.get("time"):
                    times.append(e["time"])
            if t["evidence"] and all((e.get("origin") or "typed") in ENDORSED_ORIGINS for e in t["evidence"]):
                if t.get("strength") != "weak":
                    dropped["endorsed_capped"] += 1
                t["strength"] = "weak"
                t["endorsed"] = True
            if t.get("scope") == "project":
                project_facts.append(t)
            else:
                traits.setdefault(t.get("layer", "context"), []).append(t)
        for u in merge_items(chunk_unsaid, "default"):
            u["chunk"] = cid
            if u.get("scope") == "project":
                project_facts.append(u)
            else:
                unsaid.append(u)
        reactions += list(chunk_reactions.values())
        changes += list(chunk_changes.values())
    if not stats["chunks"]:
        raise SynthError("no readable mined chunks in mined/%s" % mined_run)
    stats["sessions_read"] = len({(r["source"], r["session"]) for r in rows})
    stats["messages_read"] = len(rows)
    stats["time_reliable_sessions"] = len({(r["source"], r["session"]) for r in rows if r.get("time")})
    lessons, project_lessons, lesson_report = _lessons.consolidate(lesson_items, rows, log=log, idx=idx)
    project_facts += [dict(l, kind="lesson") for l in project_lessons]
    out = {"version": 5, "verification_version": VERIFICATION_VERSION, "chunks": stats["chunks"], "session_stats": stats, "date_range": _date_range(times),
           "traits": traits, "unsaid_defaults": unsaid, "vocabulary": sorted(vocab.values(), key=lambda v: v["phrase"].lower()),
           "preference_changes": changes, "reactions": reactions, "seed_expansions": seeds,
           "project_facts": project_facts, "grant_candidates": grant_candidates, "trimmed": [],
           "lessons": lessons, "lesson_report": lesson_report,
           "provenance_migration": migration,
           "origin_filter": dict(dropped, verified=have_typed)}
    evidence = attach_support(out, rows, split)
    out["training"] = evidence["training"]
    paths.model.mkdir(parents=True, exist_ok=True)
    archive_previous(paths, log)
    write_json(paths.model / "evidence.json", evidence)
    if not have_typed:
        out["trimmed"].append("seed and vocabulary origin not verified: no typed corpus records")
    body = json.dumps(_prompt_body(out), ensure_ascii=False)
    if len(body) > budget_chars:
        for layer in LAYERS:
            out["traits"][layer] = [t for t in out["traits"][layer] if not (t.get("strength") == "weak" and t.get("count", 1) == 1)]
        out["trimmed"].append("dropped weak traits with count 1")
        body = json.dumps(_prompt_body(out), ensure_ascii=False)
    if len(body) > budget_chars:
        for layer in LAYERS:
            for t in out["traits"][layer]:
                t["evidence"] = t["evidence"][:1]
        for u in out["unsaid_defaults"]:
            u["evidence"] = u["evidence"][:1]
        out["trimmed"].append("trimmed evidence to one quote")
    out["prompt_chars"] = len(json.dumps(_prompt_body(out), ensure_ascii=False))
    write_json(paths.mined / mined_run / "consolidated.json", out)
    log("provenance: %d prepared messages, %d legacy harness records excluded, %d old authorship fields reparsed" % (
        migration["input_messages"], migration["harness_removed"], migration["authorship_reparsed"]))
    log("consolidate: %d chunks, %d traits, %d project facts set aside, %d unsaid defaults, %d seeds, %d vocabulary; "
        "dropped %d seed(s) and %d vocabulary phrase(s) absent from typed text, %d agent-only quote(s), capped %d "
        "endorsed-only trait(s) at weak; "
        "lessons: %d mined, %d kept, %d dropped unverified or authorizing, %d single episode(s) dropped" % (
            stats["chunks"], sum(len(v) for v in out["traits"].values()), len(project_facts), len(unsaid), len(seeds), len(vocab),
            dropped["seeds"], dropped["vocabulary"], dropped["agent_only"], dropped["endorsed_capped"], lesson_report["mined"], len(lessons),
            len(lesson_report["dropped"]), lesson_report["single_dropped"]))
    return out


def _prompt_body(c):
    """What the profile prompt sees: never project_facts, seed_expansions, grant candidates or lessons (grants.md is
    built deterministically from verified quotes and the lessons section deterministically from verified lessons; the
    profile records preferences, never authorizations, and never agent context)."""
    return {k: c[k] for k in ("chunks", "session_stats", "date_range", "traits", "unsaid_defaults", "vocabulary",
                              "preference_changes", "reactions") if k in c}


GRANT_STATE_KEYS = ("grant_tombstones", "grants_added")


def _people_tokens_list(cfg):
    return list((cfg.get("people") or {}).values())


def build_grants(paths, cfg, consolidated, force=False, log=print):
    """grants.md from the mined grant candidates: only typed quotes that carry an explicit, non-negated grant phrase,
    exist verbatim in the corpus and name the action they cover. No engine call. An existing file is merged into (new
    source records appended, the user's edits kept); --force rebuilds it from scratch and clears the tombstones.

    Tombstones (MODEL.json `grant_tombstones`) record every grant the user revoked or deleted, keyed by source record
    and by quote, so neither `synthesize grants` nor `refresh --extend` ever re-adds one. `grants_added` lists what
    the pipeline itself added, which is how a deletion is noticed. Mined `preference_changes` whose later message
    revokes an earlier grant set that grant to revoked. Returns a summary."""
    raw = (consolidated or {}).get("grant_candidates") or []
    candidates, seen = [], set()
    for c in raw:  # the same quote mined from two chunks is one candidate
        key = re.sub(r"\s+", " ", (c.get("quote") or "")).strip().lower()
        if key and key not in seen:
            seen.add(key)
            candidates.append(c)
    try:
        rows = training_rows(paths)
    except PrepRefused as exc:
        raise SynthError(str(exc))
    verified_candidates = []
    for candidate in candidates:
        verified = verify_evidence(candidate, rows, typed_only=True)
        if verified:
            verified_candidates.append(dict(candidate, session=verified["session"], time=verified["time"], origin="typed"))
        else:
            log("grants: dropped candidate absent from its cited training session")
    candidates = verified_candidates
    tokens = _people_tokens_list(cfg)
    manifest = read_json(paths.model_json, {}) or {}
    today = now_iso()[:10]
    tombstones = [] if force else list(manifest.get("grant_tombstones") or [])
    added_before = [] if force else list(manifest.get("grants_added") or [])
    existing = "" if force or not paths.grants.exists() else paths.grants.read_text(encoding="utf-8")
    existing, migrated = _grants.migrate_autonomy(existing)
    if migrated:
        log("grants: rewrote %d legacy autonomy line(s) to the current bounded text" % migrated)
    present, _ = _grants.parse_grants(existing, tokens=tokens)
    present_sources = {g["source"] for g in present if g.get("source")}
    present_quotes = {_grants._norm(g.get("quote")) for g in present if g.get("quote")}
    new_tombs = []
    for a in added_before:  # a grant the pipeline added that is no longer in the file was deleted by hand
        if (a.get("source") and a["source"] in present_sources) or (a.get("quote") and a["quote"] in present_quotes):
            continue
        new_tombs.append({"id": a.get("id", ""), "source": a.get("source") or "", "quote": a.get("quote") or "", "reason": "deleted", "date": today})
        log("grants: %s was deleted by hand; it will not be re-added" % (a.get("id") or a.get("source") or a.get("quote", "")[:40]))
    text, revoked = _grants.revoke_from_changes(existing, (consolidated or {}).get("preference_changes") or [], rows, today)
    for gid, later in revoked:
        log("grants: %s revoked by a later typed message: %r" % (gid, later[:60]))
    for g in _grants.parse_grants(text, tokens=tokens)[0]:
        if g.get("quote") and not g.get("active"):
            new_tombs.append(_grants.tombstone(g, "revoked", today))
    tombstones = _grants.merge_tombstones(tombstones, new_tombs)
    kept, dropped = _grants.verify_quotes(candidates, rows, tokens=tokens)
    for c, why in dropped:
        log("grants: dropped candidate %r: %s" % ((c.get("quote") or "")[:60], why))
    for c in kept:
        if c.get("covers_derived"):
            log("grants: Covers for %r taken from the quote itself: %r" % ((c.get("quote") or "")[:40], c["covers"]))
    text, added, records = _grants.merge_grants(text, kept, cfg["name"], tombstones=tombstones)
    gs, errors = _grants.parse_grants(text, tokens=tokens)
    if gs or paths.grants.exists() or force:
        paths.grants.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    for e in errors:
        log("grants: " + e)
    added_now = [a for a in added_before if (a.get("source") and a["source"] in present_sources) or (a.get("quote") and a["quote"] in present_quotes)]
    for r in records:
        if r not in added_now:
            added_now.append(r)
    if paths.grants.exists() or tombstones or added_now:
        manifest["grant_tombstones"] = tombstones
        manifest["grants_added"] = added_now
        write_json(paths.model_json, manifest)
    valid_active = [g for g in gs if g.get("active") and not g.get("errors")]
    return {"candidates": len(candidates), "kept": len(kept), "dropped": len(dropped), "added": added, "revoked": [r[0] for r in revoked],
            "tombstones": len(tombstones), "total": len(gs), "active": len(valid_active), "errors": errors}


def lessons_section(paths, consolidated, cfg):
    """The What-works section lines for the consolidated lessons, minus sentences dropped by folded corrections."""
    manifest = read_json(paths.model_json, {}) or {}
    drops = [d for d in manifest.get("folded_drops", []) if isinstance(d, str)]
    return _lessons.render_section((consolidated or {}).get("lessons") or [], cfg["synth"].get("lessons_max", 40), drops)


def write_lessons(paths, cfg, consolidated, log=print):
    """Insert (or replace) the section in profile.md, trimming the weakest lessons until the profile fits its line
    limit. Returns the number of lessons written."""
    text = paths.profile.read_text(encoding="utf-8")
    lessons = list((consolidated or {}).get("lessons") or [])[:cfg["synth"].get("lessons_max", 40)]
    max_lines = cfg["synth"]["profile_max_lines"]
    while True:
        section = lessons_section(paths, {"lessons": lessons}, cfg)
        new = _lessons.insert_section(text, section)
        if len(new.splitlines()) <= max_lines or not lessons:
            break
        lessons = lessons[:-1]
    if new != text:
        paths.profile.write_text(new, encoding="utf-8")
    n = sum(1 for l in section if l.startswith("- "))
    log("lessons: %d lesson(s) in %d group(s) written to profile.md" % (n, sum(1 for l in section if l.startswith("### "))))
    return n


def upgrade_pairs(examples_text):
    """[(n, has Changed line)] for the examples tagged `upgrade`."""
    out, cur = [], None
    for line in examples_text.splitlines():
        m = re.match(r"^### (\d+)\. ", line)
        if m:
            cur = {"n": int(m.group(1)), "upgrade": False, "changed": False}
            out.append(cur)
        elif cur is not None and line.startswith("Tags:"):
            cur["upgrade"] = UPGRADE_TAG in [t.strip().lower() for t in line[5:].split(",")]
        elif cur is not None and line.startswith("Changed:"):
            cur["changed"] = True
    return [(e["n"], e["changed"]) for e in out if e["upgrade"]]


def _date_range(times):
    ts = sorted(t[:10] for t in times if isinstance(t, str) and len(t) >= 10)
    if not ts:
        return "unknown dates"
    return "%s to %s" % (ts[0], ts[-1])


def _profile_excerpt(profile_text):
    """Standing rules layer and the Unsaid defaults section."""
    lines = profile_text.splitlines()
    out, keep = [], False
    for line in lines:
        if line.startswith("## "):
            keep = line.startswith("## 6.") or line.strip() == "## Unsaid defaults"
        if keep:
            out.append(line)
    return "\n".join(out)


def _newer(out_path, *inputs):
    out_path = Path(out_path)
    if not out_path.exists():
        return False
    m = out_path.stat().st_mtime
    return all(not Path(i).exists() or Path(i).stat().st_mtime <= m for i in inputs)


def archive_previous(paths, log=print):
    manifest = read_json(paths.model_json)
    if not manifest or not any((paths.model / n).exists() for n in ("light.md", "profile.md", "examples.md")):
        return None
    old_hash = manifest.get("model_hash") or _select.files_hash(paths.model)
    dest = paths.archive / old_hash
    if dest.exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("light.md", "profile.md", "examples.md", "grants.md", "index.json", "evidence.json", "episodes.json", "preference_memory.json", "MODEL.json"):
        p = paths.model / name
        if p.exists():
            shutil.copy2(p, dest / name)
    log("archived previous model %s to %s" % (old_hash, dest))
    return dest


def write_manifest(paths, cfg, run_id=None, mined_run=None, engine=None, parents=None, corrections_folded=None,
                   folded_through=None, extra=None):
    # Runtime correction folding can update a legacy model without its old corpus.
    # A new build hashes the prepared training subset, never the future raw corpus.
    try:
        rows = training_rows(paths)
    except PrepRefused:
        rows = []
    split = read_json(paths.split, {}) or {}
    prev = read_json(paths.model_json, {}) or {}
    model_hash = _select.files_hash(paths.model)
    parents = parents if parents is not None else ([prev["model_hash"]] if prev.get("model_hash") and prev["model_hash"] != model_hash else prev.get("parents", []))
    manifest = {
        "version": 2, "built": now_iso(), "name_token": cfg["name"], "run_id": run_id or prev.get("run_id"),
        "mined_run": mined_run or prev.get("mined_run"), "split_id": split.get("split_id"),
        "corpus_hash": corpus_hash(rows) if rows else prev.get("corpus_hash"),
        "engine": engine or prev.get("engine") or cfg.get("engine"), "models": cfg.get("models", {}).get(engine or cfg.get("engine"), {}),
        "model_hash": model_hash, "parents": parents,
        "corrections_folded": corrections_folded if corrections_folded is not None else prev.get("corrections_folded", 0),
        "corrections_folded_through": folded_through if folded_through is not None else prev.get("corrections_folded_through"),
        "project_notes": prev.get("project_notes", []), "folded_drops": prev.get("folded_drops", []),
        "grant_tombstones": prev.get("grant_tombstones", []), "grants_added": prev.get("grants_added", []),
        "always_on_targets": cfg.get("always_on_targets", []),
        "files": {n: (paths.model / n).stat().st_size for n in ("light.md", "profile.md", "examples.md", "grants.md", "index.json", "evidence.json", "episodes.json", "preference_memory.json") if (paths.model / n).exists()},
    }
    if extra:
        manifest.update(extra)
    write_json(paths.model_json, manifest)
    return manifest


def run_synthesize(paths, cfg, kind, substeps=None, mined_run=None, run_id=None, force=False, collect=False, model=None,
                   update=False, fake_dir=None, log=print, strict_lint=True, backoff=None, max_calls=None, max_cost_usd=None):
    substeps = substeps or SUBSTEPS
    mined_run = mined_run or latest_mined_run(paths)
    if not mined_run and any(s in substeps for s in ("consolidate", "profile", "examples")):
        raise SynthError("no mined run found: run mine first")
    paths.model.mkdir(parents=True, exist_ok=True)
    summary = {"mined_run": mined_run, "substeps": {}, "warnings": []}
    consolidated_path = paths.mined / mined_run / "consolidated.json" if mined_run else None
    consolidated = None
    needs_engine = any(s in substeps for s in ("profile", "light", "examples"))
    eng = None
    if needs_engine:
        if run_id == "latest":
            run_id = _engine.latest_run_id(paths, "synthesize")
        eng = _engine.Engine(paths, cfg, kind, "synthesize", run_id=run_id, model=_engine.stage_model(cfg, kind, "synthesize", model),
                             concurrency=1, fake_dir=fake_dir, log=log, backoff=backoff,
                             max_calls=max_calls, max_cost_usd=max_cost_usd)
        summary["run_id"] = eng.run_id
    archived = False
    name = cfg["name"]
    people = _engine.people_tokens(cfg)
    base_map = {"name": name, "people_tokens": people, "privacy": privacy_line(cfg)}
    forbidden = _lint.FORBIDDEN_RULES_TEXT

    if "consolidate" in substeps:
        cached = read_json(consolidated_path, {}) or {}
        if (force or cached.get("version") != 5 or cached.get("verification_version") != VERIFICATION_VERSION
                or not _newer(consolidated_path, *mined_files(paths, mined_run))
                or not _newer(consolidated_path, paths.split, paths.sessions)):
            consolidated = consolidate(paths, mined_run, cfg["synth"]["budget_chars"], log)
            summary["substeps"]["consolidate"] = "built"
        else:
            summary["substeps"]["consolidate"] = "skipped (up to date)"
    if consolidated is None and consolidated_path and consolidated_path.exists():
        consolidated = read_json(consolidated_path)
    if consolidated is not None and any(s in substeps for s in ("profile", "lessons", "light", "examples", "grants")):
        try:
            rows = training_rows(paths)
        except PrepRefused as exc:
            raise SynthError(str(exc))
        split = read_json(paths.split)
        boundary = consolidated.get("training") or {}
        if (consolidated.get("version") != 5 or consolidated.get("verification_version") != VERIFICATION_VERSION
                or boundary.get("split_id") != split.get("split_id")
                or boundary.get("content_hash") != training_hash(rows)):
            raise SynthError("consolidated evidence predates the prepared training boundary; run synthesize consolidate")

    def _engine_step(step, prompt_name, mapping, schema, out_file, key, check=None, post=None):
        nonlocal archived
        if not force and _newer(out_file, consolidated_path or out_file) and (step != "light" or _newer(out_file, paths.profile)):
            summary["substeps"][step] = "skipped (up to date)"
            return True
        unit = _engine.Unit(step, _engine.render(_engine.load_prompt(prompt_name), mapping), schema,
                            eng.run_dir / ("synth-%s.json" % step), check=check)
        res = eng.run([unit], force=force, collect=collect)
        if res["pending"]:
            summary["substeps"][step] = "pending (print mode)"
            summary["instructions"] = res.get("instructions")
            return False
        if res["failed"]:
            summary["substeps"][step] = "failed: " + res["errors"].get(step, "")
            raise SynthError("%s failed: %s" % (step, res["errors"].get(step, "")))
        if res["stopped"]:
            summary["substeps"][step] = "stopped (budget)"
            raise SynthError("%s stopped: %s" % (step, res.get("budget_note", "budget reached")))
        text = read_json(unit.out_path)[key]
        if post:
            text = post(text)
        if not archived:
            archive_previous(paths, log)
            archived = True
        Path(out_file).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        summary["substeps"][step] = "built"
        return True

    if "profile" in substeps:
        if consolidated is None:
            raise SynthError("consolidated.json missing: run `synthesize consolidate`")
        prior = ""
        if update and paths.profile.exists():
            prior = ("Prior profile (update, do not rewrite; keep dated changes; add only what the new evidence supports):\n\n"
                     + paths.profile.read_text(encoding="utf-8"))
        if prior:
            prior = _lessons.strip_section(prior)  # the pipeline rewrites the lessons section itself
        # the lessons section is written after the engine (substep `lessons`): leave room for it
        reserve = len(lessons_section(paths, consolidated, cfg))
        minimum = profile_minimum(cfg, consolidated)
        body_max = max(minimum, cfg["synth"]["profile_max_lines"] - reserve)
        mapping = dict(base_map, chunk_count=str(consolidated["chunks"]), date_range=consolidated["date_range"],
                       consolidated_json=json.dumps(_prompt_body(consolidated), ensure_ascii=False, indent=0),
                       prior_block=prior, forbidden_rules=forbidden,
                       synth_min_lines=str(minimum), synth_max_lines=str(body_max),
                       profile_size_rule=("The lower line target is automatic guidance, not a quota: write less when the verified evidence supports less."
                                          if cfg["synth"].get("profile_min_lines") is None else
                                          "The lower line target is explicitly configured; preserve it without adding unsupported claims."))

        def check_profile(data):
            text = _lessons.strip_section(data["profile_md"])
            try:
                _select.parse_profile(text.splitlines())
            except _select.ModelFormatError as exc:
                return str(exc)
            n = len(text.splitlines())
            if n > body_max:
                return "profile has %d lines, maximum %d (the rest is reserved for the lessons section)" % (n, body_max)
            return None
        if not _engine_step("profile", "synthesize-profile", mapping, PROFILE_SCHEMA, paths.profile, "profile_md", check_profile,
                            post=_lessons.strip_section):
            return summary
    if "lessons" in substeps:
        if consolidated is None or not paths.profile.exists():
            raise SynthError("consolidated.json or profile.md missing")
        n = write_lessons(paths, cfg, consolidated, log)
        summary["substeps"]["lessons"] = "%d lesson(s) in profile.md" % n
    if "light" in substeps:
        if not paths.profile.exists():
            raise SynthError("profile.md missing")
        mapping = dict(base_map, profile_md=paths.profile.read_text(encoding="utf-8"), forbidden_rules=forbidden,
                       light_body_max=str(cfg["synth"]["light_max_lines"] - 4))  # the fixed header is title, blank, Scope, blank

        def check_light(data):
            body = _select.ensure_light_header(data["light_md"], name)
            errs = _select.check_light(body, name, cfg["synth"]["light_max_lines"])
            return "\n".join(errs) if errs else None
        if not _engine_step("light", "synthesize-light", mapping, LIGHT_SCHEMA, paths.light, "light_md", check_light,
                            post=lambda t: _select.ensure_light_header(t, name)):
            return summary
    if "examples" in substeps:
        if consolidated is None or not paths.profile.exists():
            raise SynthError("consolidated.json or profile.md missing")
        profile_text = paths.profile.read_text(encoding="utf-8")
        section = _lessons.section_of(profile_text)
        has_upgrade_lesson = any(l.startswith("- ") and "Outcome: bad" in l for l in section)
        bad_lesson_ids, current_lesson = set(), None
        for line in section:
            match = re.match(r"^### (W\d+)\.", line)
            if match:
                current_lesson = match.group(1).lower()
            elif current_lesson and line.startswith("- ") and "Outcome: bad" in line:
                bad_lesson_ids.add(current_lesson)
        mapping = dict(base_map, seed_expansions_json=json.dumps(consolidated.get("seed_expansions", []), ensure_ascii=False, indent=0),
                       profile_excerpt=_profile_excerpt(profile_text), forbidden_rules=forbidden,
                       examples_min=str(cfg["synth"]["examples_min"]), examples_max=str(cfg["synth"]["examples_max"]),
                       lessons_md=_lessons.compact_for_prompt(section) or "(no lessons: write no upgrade pairs)",
                       upgrades_rule=(UPGRADES_RULE % (UPGRADES_MIN, UPGRADES_MAX)) if has_upgrade_lesson else NO_UPGRADES_RULE)

        def check_examples(data):
            try:
                ex = _select.parse_examples(data["examples_md"].splitlines())
            except _select.ModelFormatError as exc:
                return str(exc)
            if not cfg["synth"]["examples_min"] <= len(ex) <= cfg["synth"]["examples_max"]:
                return "%d examples, need %d to %d" % (len(ex), cfg["synth"]["examples_min"], cfg["synth"]["examples_max"])
            ups = upgrade_pairs(data["examples_md"])
            if has_upgrade_lesson and not UPGRADES_MIN <= len(ups) <= UPGRADES_MAX:
                return "%d pair(s) tagged `upgrade`, need %d to %d (the profile has lessons with a bad outcome)" % (
                    len(ups), UPGRADES_MIN, UPGRADES_MAX)
            if not has_upgrade_lesson and ups:
                return "pairs tagged `upgrade` but the profile has no lesson with a bad outcome"
            for example in ex:
                tags = example.get("tags") or []
                if UPGRADE_TAG in tags:
                    cited = {t for t in tags if re.fullmatch(r"w\d+", t)}
                    if not cited or not cited.issubset(bad_lesson_ids):
                        return "upgrade pair references a lesson without a verified bad outcome: %s" % example.get("n")
            missing = [n for n, changed in ups if not changed]
            if missing:
                return "upgrade pair(s) %s lack the `Changed:` line" % ", ".join(str(n) for n in missing)
            hits = _lessons.upgrade_hits(data["examples_md"])
            if hits:
                return "\n".join(hits)
            return None
        if not _engine_step("examples", "synthesize-examples", mapping, EXAMPLES_SCHEMA, paths.examples, "examples_md", check_examples):
            return summary
    if "grants" in substeps:
        if consolidated is None:
            raise SynthError("consolidated.json missing: run `synthesize consolidate`")
        g = build_grants(paths, cfg, consolidated, force=force, log=log)
        summary["substeps"]["grants"] = "%d candidate(s), %d verified, %d added, %d in grants.md (%d valid and active)" % (
            g["candidates"], g["kept"], g["added"], g["total"], g["active"])
        summary["warnings"] += ["grants.md: " + e for e in g["errors"]]
    if "index" in substeps:
        idx, warnings = _select.build_index(paths.model)
        summary["substeps"]["index"] = "built (%d groups, %d examples)" % (len(idx["groups"]), len(idx["examples"]))
        summary["warnings"] += warnings
        for w in warnings:
            log("index: warning: " + w)
    if "lint" in substeps:
        hits = _lint.lint_model(paths, cfg)
        summary["substeps"]["lint"] = "%d forbidden, %d other" % (len(hits["forbidden"]), len(hits["other"]))
        for h in hits["forbidden"] + hits["other"]:
            log("lint: " + h)
        if hits["forbidden"] and strict_lint:
            raise SynthError("model lint found forbidden rules; fix the model or re-run with --no-strict:\n" + "\n".join(hits["forbidden"]))
    if "manifest" in substeps:
        m = write_manifest(paths, cfg, run_id=summary.get("run_id"), mined_run=mined_run, engine=kind if needs_engine else None)
        summary["substeps"]["manifest"] = "written (model %s)" % m["model_hash"]
        summary["model_hash"] = m["model_hash"]
    log("synthesize: " + ", ".join("%s: %s" % kv for kv in summary["substeps"].items()))
    return summary
