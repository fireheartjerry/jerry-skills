"""Freeze historical prefix inputs separately from later, reveal-only user turns.

This is retrospective replay, never evidence that a forecast was registered
before the real event. Prospective claims use the foresight journal instead.
"""
from collections import defaultdict
import datetime as dt
import hashlib
import json

from .filters import authored_text
from .preferences import PreferenceError, _check_output, _json_bytes, _private_json, _safe_path


class ReplayError(ValueError):
    pass


def _time(value):
    if not isinstance(value, str):
        raise ReplayError("replay times require explicit timezone-aware timestamps")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayError("invalid replay timestamp") from exc
    if parsed.tzinfo is None:
        raise ReplayError("replay times require timezones")
    return parsed.astimezone(dt.timezone.utc)


def _digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def freeze_cases(rows, out_dir, after, excluded_sessions=(), limit=24, per_session=2, excluded_seeds=()):
    if type(limit) is not int or limit < 1 or type(per_session) is not int or per_session < 1:
        raise ReplayError("replay limits must be positive integers")
    boundary = _time(after)
    if isinstance(excluded_sessions, str):
        raise ReplayError("excluded sessions must be a collection, not one string")
    excluded = set(excluded_sessions)
    excluded_seeds = set(excluded_seeds)
    if any(not isinstance(v, str) for v in excluded):
        raise ReplayError("excluded sessions must be source:session strings")
    sessions, identities, reserved_turns = defaultdict(list), {}, set()
    def long_turn(text):
        return " ".join(text.casefold().split()) if len(text) >= 200 and len(text.split()) >= 30 else None
    for row in rows:
        if not isinstance(row, dict) or row.get("origin") != "typed":
            continue
        if not all(isinstance(row.get(k), str) and row[k] for k in ("source", "session", "key", "text")):
            continue
        sid = row["source"] + ":" + row["session"]
        identity = sid + ":" + row["key"]
        body = authored_text(row["text"]).strip()
        if not body:
            continue
        if sid in excluded:
            fingerprint = long_turn(body)
            if fingerprint:
                reserved_turns.add(fingerprint)
            continue
        canonical = {"source_ref": identity, "text": body, "time": row.get("time"),
                     "ordinal": row.get("ordinal"), "project": row.get("project") if isinstance(row.get("project"), str) else ""}
        if identity in identities:
            if identities[identity] != canonical:
                raise ReplayError("conflicting transcript source identity: %s" % identity)
            continue
        identities[identity] = dict(canonical)
        try:
            canonical["parsed_time"] = _time(canonical["time"])
        except ReplayError:
            continue
        if type(canonical["ordinal"]) is not int:
            continue
        sessions[sid].append(canonical)
    candidates, mirrored = defaultdict(list), []
    for sid in sorted(sessions):
        if any(long_turn(r["text"]) in reserved_turns for r in sessions[sid]):
            mirrored.append(sid)
            continue
        messages = sorted(sessions[sid], key=lambda r: (r["ordinal"], r["parsed_time"], r["source_ref"]))
        for i, seed in enumerate(messages[:-1]):
            if (seed["source_ref"] in excluded_seeds or seed["parsed_time"] < boundary
                    or not 2 <= len(seed["text"].split()) <= 80 or len(seed["text"]) > 1000):
                continue
            prefix = [r for r in messages[max(0, i - 6):i] if r["parsed_time"] <= seed["parsed_time"]]
            later = [r for r in messages[i + 1:i + 4] if r["parsed_time"] > seed["parsed_time"] and len(r["text"].split()) >= 4]
            if not prefix or not later:
                continue
            identity = _digest([seed["source_ref"], seed["text"]])[:16]
            inputs = {"id": identity, "seed": seed["text"], "source_ref": seed["source_ref"],
                      "source_time": seed["time"], "session_id": sid, "project": seed["project"],
                      "context": [r["text"][:1200] for r in prefix],
                      "context_refs": [r["source_ref"] for r in prefix],
                      "context_truncated": any(len(r["text"]) > 1200 for r in prefix)}
            inputs["input_hash"] = _digest(inputs)
            reveal = {"id": identity, "input_hash": inputs["input_hash"], "later_user_turns": [
                {"text": r["text"][:2000], "source_ref": r["source_ref"], "time": r["time"],
                 "truncated": len(r["text"]) > 2000} for r in later]}
            assert not set(inputs["context_refs"] + [inputs["source_ref"]]) & {r["source_ref"] for r in reveal["later_user_turns"]}
            candidates[sid].append((inputs, reveal))
    chosen = []
    order = sorted(candidates, key=lambda sid: _digest(sid))
    for position in range(per_session):
        for sid in order:
            if position < len(candidates[sid]) and len(chosen) < limit:
                chosen.append(candidates[sid][position])
    inputs = {"version": 1, "cases": [p[0] for p in chosen]}
    reveals = {"version": 1, "evidence": "actual later typed words; not proof every later request was already intended",
               "cases": [p[1] for p in chosen]}
    manifest = {"version": 1, "evidence": "retrospective replay, not prospective registration", "after": after,
                "count": len(chosen), "requested": limit, "sessions": len({p[0]["session_id"] for p in chosen}),
                "eligible_candidates": sum(len(v) for v in candidates.values()), "excluded_sessions": sorted(excluded),
                "mirrored_excluded_sessions": mirrored,
                "excluded_seeds": sorted(excluded_seeds),
                "inputs_sha256": hashlib.sha256(_json_bytes(inputs)).hexdigest(),
                "reveals_sha256": hashlib.sha256(_json_bytes(reveals)).hexdigest(),
                "later_turns_used_as_input": False}
    try:
        out = _safe_path(out_dir)
        payloads = [(out / "inputs.json", inputs), (out / "reveals.json", reveals), (out / "manifest.json", manifest)]
        for path, data in payloads:
            _check_output(path, _json_bytes(data))
        for path, data in payloads:
            _private_json(path, data)
    except PreferenceError as exc:
        raise ReplayError(str(exc)) from exc
    return dict(manifest, inputs=str(out / "inputs.json"), reveals=str(out / "reveals.json"), manifest=str(out / "manifest.json"))
