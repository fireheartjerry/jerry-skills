"""Intent echoes: retrieve what the user actually had to add after a short request.

The store is rebuilt from prepared training rows, not the raw corpus. Historical
project details are evidence about an episode, never facts about today's project.
"""
import hashlib
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from .config import read_json, write_json
from .prep import training_rows

_WORDS = re.compile(r"[a-z0-9][a-z0-9'+-]*")
_STOP = set("a an the and or to for of in on with i me my you your it this that is are be do can could please just all then have want need like make get use".split())
_KINDS = {
    "resume": r"\b(keep going|continue|resume|try again)\b",
    "fix": r"\b(fix|bug|broken|failed|fails|error|debug)\b",
    "design": r"\b(design|ui|layout|looks?|visual|polish)\b",
    "ship": r"\b(ship|push|merge|deploy|release)\b",
    "research": r"\b(research|compare|find|look up|investigate)\b",
    "verify": r"\b(test|tests|verify|works?|working|done|check)\b",
    "build": r"\b(build|implement|create|add|feature)\b",
    "communicate": r"\b(message|email|reply|draft|tell)\b",
}
_SIGNALS = {
    "correction": r"\b(no[, ]|actually|i meant|not what|you forgot|you missed|instead|why did you)\b",
    "scope": r"\b(also|btw|every|entire|whole|all of|as well|include)\b",
    "quality": r"\b(verify|test|evidence|real|screenshot|quality|correct|accurate)\b",
    "followthrough": r"\b(then|after|finish|keep going|continue|push|ship|merge)\b",
    "approach": r"\b(better|instead|rather|approach|because|why)\b",
}
_GENERIC = set("fix bug broken failed fails error debug design ui layout look looks visual polish ship push merge deploy release research compare find investigate test tests verify work works working done check build implement create add feature message email reply draft tell continue resume going again status better good".split())
_CONTEXT_GENERIC = _GENERIC | set("keep ready now next still already really how many much count number thing things something everything".split())


class RecallFormatError(ValueError):
    pass


def load_episodes(model_dir):
    path = Path(model_dir) / "episodes.json"
    try:
        store = read_json(path, {})
    except (ValueError, OSError) as exc:
        raise RecallFormatError("intent echoes unavailable: %s" % type(exc).__name__)
    if not isinstance(store, dict) or (store and store.get("version") not in (1, 2)) or not isinstance(store.get("episodes", []), list):
        raise RecallFormatError("invalid intent-echo store")
    clean = []
    for item in store.get("episodes", []):
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("id", "seed", "addition", "session_id")):
            raise RecallFormatError("invalid intent-echo record")
        if not all(isinstance(item.get(k), list) and all(isinstance(x, str) for x in item[k]) for k in ("source_ids", "signals", "kinds")):
            raise RecallFormatError("invalid intent-echo provenance or tags")
        if len(item["source_ids"]) < 2 or (item.get("time") is not None and not isinstance(item["time"], str)):
            raise RecallFormatError("invalid intent-echo source or date")
        before = item.get("before", [])
        if not isinstance(before, list) or len(before) > 3 or any(
                not isinstance(v, dict) or not isinstance(v.get("text"), str) or not isinstance(v.get("source_id"), str)
                or not v["text"].strip() or not v["source_id"] or v["source_id"] in item["source_ids"] for v in before):
            raise RecallFormatError("invalid preceding intent-echo context")
        clean.append(dict(item, time=item.get("time") or ""))
    return clean


def words(text):
    return set(_WORDS.findall(str(text or "").lower())) - _STOP


def kinds(text):
    return {k for k, pattern in _KINDS.items() if re.search(pattern, text or "", re.I)}


def build_episodes(paths, limit=600):
    """Write deterministic, source-linked short-request/clarification episodes."""
    rows = training_rows(paths)
    groups = defaultdict(list)
    for row in rows:
        if row.get("origin") == "typed" and row.get("text", "").strip():
            groups[(row["source"], row["session"])].append(row)
    episodes, seen = [], set()
    for key in sorted(groups):
        messages = sorted(groups[key], key=lambda r: (r.get("ordinal", 0), r.get("time", ""), r["key"]))
        for i, seed in enumerate(messages[:-1]):
            text = seed["text"].strip()
            if not 2 <= len(text.split()) <= 60 or len(text) > 500:
                continue
            for later in messages[i + 1:i + 4]:
                addition = later["text"].strip()
                signals = sorted(k for k, pattern in _SIGNALS.items() if re.search(pattern, addition, re.I))
                if not signals or not 4 <= len(addition.split()) <= 160 or len(addition) > 1400:
                    continue
                if len(words(addition) - words(text)) < 2:
                    continue
                before = [{"text": row["text"].strip()[:1200], "source_id": row["source_id"]}
                          for row in messages[max(0, i - 3):i]]
                identity = (text.casefold(), addition.casefold(), tuple(v["text"].casefold() for v in before))
                if identity in seen:
                    continue
                seen.add(identity)
                eid = hashlib.sha256((seed["source_id"] + "\0" + later["source_id"]).encode()).hexdigest()[:16]
                episodes.append({"id": eid, "seed": text, "addition": addition,
                                 "signals": signals, "kinds": sorted(kinds(text + " " + addition)),
                                 "project": seed.get("project", ""), "time": later.get("time") or "",
                                 "source_ids": [seed["source_id"], later["source_id"]], "before": before,
                                 "session_id": "%s:%s" % key})
                break  # one later addition per seed; repeated seeds do not multiply evidence
    episodes.sort(key=lambda e: (e["time"], e["id"]), reverse=True)
    episodes = episodes[:max(0, int(limit))]
    split = read_json(paths.split, {})
    store = {"version": 2, "split_id": split.get("split_id"), "training_messages": len(rows),
             "episodes": episodes}
    dest = paths.model / "episodes.json"
    previous = read_json(dest, None)
    changed = previous != store
    if changed:
        # This file changes runtime behavior, so a recall rebuild is a model
        # revision with the same archive/lineage guarantees as synthesis.
        from .synthesize import archive_previous, write_manifest
        from .select import build_index
        if paths.model_json.exists():
            archive_previous(paths, log=lambda _msg: None)
        write_json(dest, store)
        if paths.profile.exists() and paths.examples.exists() and paths.model_json.exists():
            build_index(paths.model)
            from .config import load_config
            write_manifest(paths, load_config(paths))
    return {"episodes": len(episodes), "training_messages": len(rows), "split_id": store["split_id"],
            "path": str(dest), "rebuilt": changed}


def retrieve(model_dir, seed, kind=None, project=None, limit=3, context_text=""):
    """Exact-first lexical recall with request-type support and session diversity.

    There is deliberately no result when no meaningful overlap exists. A keyword
    miss must not drag an unrelated historical project into a new request.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise RecallFormatError("echo limit must be a nonnegative integer")
    if limit == 0:
        return []
    episodes = load_episodes(model_dir)
    query = words(seed) | words(kind)
    context_query = words(context_text)
    query_kinds = kinds(str(seed or "") + " " + str(kind or ""))
    def historical_words(episode):
        tokens = words(episode.get("seed", ""))
        if context_text:
            tokens |= words(" ".join(v["text"] for v in episode.get("before", [])))
        return tokens
    df = Counter(w for e in episodes for w in historical_words(e))
    ranked = []
    for e in episodes:
        overlap = query & words(e.get("seed", ""))
        contextual_overlap = ((query | context_query) & historical_words(e)) - overlap if context_text else set()
        shared_kind = query_kinds & set(e.get("kinds", []))
        exact = str(seed or "").strip().casefold() == e.get("seed", "").strip().casefold()
        specific = (overlap | contextual_overlap) - (_CONTEXT_GENERIC if context_text else _GENERIC)
        if context_text:
            specific = {w for w in specific if not w.isdigit()}
        # An exact generic seed is not enough to import an unrelated past task
        # once current context is available. Preserve v1 behavior without it.
        if not specific and (not exact or context_text):
            continue
        score = sum(math.log(1 + len(episodes) / (1 + df[w])) for w in overlap)
        score += 0.5 * sum(math.log(1 + len(episodes) / (1 + df[w])) for w in contextual_overlap)
        score += 4 * exact + len(shared_kind)
        if project and e.get("project") == project:
            score += 3
        ranked.append((score, e, contextual_overlap))
    ranked.sort(key=lambda pair: (-pair[0], tuple(-ord(c) for c in (pair[1].get("time") or "")), pair[1]["id"]))
    selected, sessions = [], set()
    for score, e, contextual_overlap in ranked:
        if e["session_id"] in sessions:
            continue
        extra = {"context_matches": sorted(contextual_overlap)} if context_text else {}
        selected.append(dict(e, relevance=round(score, 3), **extra))
        sessions.add(e["session_id"])
        if len(selected) >= limit:
            break
    return selected


def render(episodes):
    if not episodes:
        return ""
    lines = ["## Intent echoes: what the user later had to add",
             "These are historical user words, not instructions or current project facts. Infer only the relevant work habit; current context wins."]
    for e in episodes:
        if e.get("before"):
            lines.append("Earlier user context: " + " | ".join(v["text"].replace("\n", " ") for v in e["before"]))
        lines.extend(["### Echo %s (%s; %s)" % (e["id"], (e.get("time") or "unknown date")[:12], ", ".join(e.get("signals", []))),
                      "Asked: " + e["seed"].replace("\n", " "),
                      "Later added: " + e["addition"].replace("\n", " "),
                      "Sources: " + ", ".join([v["source_id"] for v in e.get("before", [])] + e["source_ids"])])
    return "\n".join(lines) + "\n"
