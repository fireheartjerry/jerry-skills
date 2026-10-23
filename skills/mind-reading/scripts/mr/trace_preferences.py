"""A separate utility-evidence channel: human words, never invented personality.

An imperative can be a one-task constraint. Surface preference wording is only a
retrieval cue; its quoted situation remains attached and the host decides what
transfers. Repeated words from one copied conversation count as one family.
"""
import hashlib
import math
import re

CUE = re.compile(r"\b(?:i\s+(?:want|prefer|hate|like|need)|my\s+(?:usual|preference|goal)|always|never|must|don't|don’t|do not|make sure|should)\b", re.I)
FIRST_PERSON = re.compile(r"\b(?:i\s+(?:prefer|hate|like)|my\s+(?:usual|preference))\b", re.I)
SEGMENT = re.compile(r"[^\n.!?]+(?:[.!?]+|$)")
TOKEN = re.compile(r"\w+", re.UNICODE)


def extract_quotes(cases):
    quotes = {}
    for case in cases:
        if case.get("origin") != "human" or case.get("weight") != 1:
            raise ValueError("preference evidence requires verified human training cases")
        if case.get("partition") not in (None, "train"):
            raise ValueError("preference evidence cannot read evaluation cases")
        fields = [(case["input"]["seed"], (case.get("source_refs") or {}).get("seed") if isinstance(case.get("source_refs"), dict) else None),
                  (case["target"]["text"], case["target"].get("source_ref"))]
        for text, ref in fields:
            for segment in SEGMENT.finditer(text):
                cue = CUE.search(segment.group())
                if not cue:
                    continue
                raw = segment.group().strip()
                if len(raw) < 12:
                    continue
                start = max(0, cue.start()-160)
                quote = raw[start:start+800]
                key = " ".join(quote.casefold().split())
                item = quotes.setdefault(key, {"id": hashlib.sha256(key.encode()).hexdigest()[:20], "quote": quote,
                                               "bounded_excerpt": quote != raw, "kind": "surface_preference_or_constraint",
                                               "turn_excerpt": text[max(0, segment.start()-120):segment.start()+1000],
                                               "voice": "verified human turn; embedded quotations and pasted material are not automatically attributed to its author",
                                               "first_person_preference_wording": bool(FIRST_PERSON.search(quote)),
                                               "observations": [], "families": [], "projects": []})
                if case["family"] not in item["families"]:
                    item["families"].append(case["family"])
                if case.get("project") not in item["projects"]:
                    item["projects"].append(case.get("project"))
                if len(item["observations"]) < 5 and not any(o["source_ref"] == ref for o in item["observations"]):
                    item["observations"].append({"case_id": case["id"], "family": case["family"], "source_ref": ref,
                                                 "past_seed": case["input"]["seed"][:600],
                                                 "past_context": [t[:600] for t in case["input"].get("prior_user_turns", [])[-1:]]})
    for item in quotes.values():
        item["support_families"] = len(item["families"])
        item["scope"] = "observed contexts only; no automatic global rule"
    return sorted(quotes.values(), key=lambda q: q["id"])


def select_quotes(seed, context, cases, ranked=None, project=None, limit=4):
    context = context or {}
    case_scores = {cases[i]["id"]: float(s) for i, s in (ranked or [])}
    eligible = [c for c in cases if (not context.get("session") or c.get("session") != context["session"])
                and (not context.get("family") or c.get("family") != context["family"])]
    quotes = extract_quotes(eligible)
    if not quotes:
        return []
    turns = context.get("prior_user_turns", [])
    turns = turns if isinstance(turns, (list, tuple)) else []
    text = seed + " " + str(context.get("goal", "")) + " " + " ".join(t for t in turns[-3:] if isinstance(t, str))
    query = set(TOKEN.findall(text.casefold()))
    def score(q):
        words = set(TOKEN.findall(q["quote"].casefold()))
        overlap = len(query & words) / max(1, len(query | words))
        nearby = max((case_scores.get(o["case_id"], 0.0) for o in q["observations"]), default=0.0)
        recurrence = min(0.1, 0.025*math.log1p(q["support_families"])) if q["first_person_preference_wording"] else 0
        return overlap + 0.3*max(0, nearby) + recurrence
    ordered = sorted(((score(q), q) for q in quotes), key=lambda p: (-p[0], project not in p[1]["projects"], p[1]["id"]))
    out, family_counts = [], {}
    for value, q in ordered:
        if value <= 0:
            continue
        family = q["observations"][0]["family"]
        if family_counts.get(family, 0) >= 2:
            continue
        family_counts[family] = family_counts.get(family, 0)+1
        out.append(dict(q, relevance_score=value,
                        interpretation="Exact excerpt from a verified human turn with its situation. Relevance is not authority, endorsement of every clause, or a probability; verify the voice of embedded material."))
        if len(out) >= limit:
            break
    return out
