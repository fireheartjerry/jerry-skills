"""Bounded, preceding-only invocation context for intent retrieval.

The host supplies the current goal and observed conversation. This module does
not infer goals from regexes, certify caller facts, or create permissions.
"""
import hashlib
import json


class ContextFormatError(ValueError):
    pass


_STRINGS = {"goal": 2000, "phase": 80, "project": 1000}
_LISTS = {"recent_user_turns": (8, 2000), "verified_facts": (8, 500)}


def normalize(data):
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ContextFormatError("intent context must be an object")
    if any(not isinstance(key, str) for key in data):
        raise ContextFormatError("intent-context field names must be strings")
    unknown = set(data) - set(_STRINGS) - set(_LISTS) - {"version"}
    if unknown:
        raise ContextFormatError("unknown intent-context fields: %s; supply preceding context only" % ", ".join(sorted(unknown)))
    if "version" in data and (type(data["version"]) is not int or data["version"] != 1):
        raise ContextFormatError("intent context version must be 1")
    out = {}
    for key, maximum in _STRINGS.items():
        value = data.get(key, "")
        if not isinstance(value, str) or len(value) > maximum:
            raise ContextFormatError("%s must be a string of at most %d characters" % (key, maximum))
        if value.strip():
            out[key] = value.strip()
    for key, (count, maximum) in _LISTS.items():
        value = data.get(key, [])
        if not isinstance(value, list) or len(value) > count or any(not isinstance(v, str) or len(v) > maximum for v in value):
            raise ContextFormatError("%s must contain at most %d strings of at most %d characters" % (key, count, maximum))
        clean = [v.strip() for v in value if v.strip()]
        if clean:
            out[key] = clean
    if len(json.dumps(out, ensure_ascii=False)) > 16000:
        raise ContextFormatError("intent context exceeds 16000 characters; pass a compact preceding-only frame")
    return out


def retrieval_text(context):
    frame = normalize(context)
    parts = [frame.get("goal", ""), *frame.get("recent_user_turns", []),
             *frame.get("verified_facts", []), frame.get("phase", "")]
    return "\n".join(p for p in parts if p)


def fingerprint(context):
    return hashlib.sha256(json.dumps(normalize(context), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def render(context):
    frame = normalize(context)
    if not frame:
        return ""
    return ("## Intent context from the current invocation\n"
            "Caller-supplied current context, not historical evidence or additional authorization. "
            "Use the newest request to update the goal; verify changing facts where they live.\n"
            + json.dumps(frame, ensure_ascii=False, indent=1) + "\n")
