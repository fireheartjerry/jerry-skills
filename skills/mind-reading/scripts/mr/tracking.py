"""Traceable real-use measurement. Detection suggests cases; only human review supplies labels."""
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from . import filters
from .config import read_jsonl
from .corpus import now_iso
from .preferences import (PreferenceError, _digest, _page, _private_json, read_answers,
                          _safe_path, _check_output, _private_bytes, _json_bytes)
from .scrub import scrub

OUTCOMES = {"corrected", "accepted", "no_correction_observed"}
READING = re.compile(r"(?im)^\s*(?:\*\*)?Reading\s*:")
TAG = re.compile(r"\[(?:S|L|G)(?::[^\]\n]{0,120})?\]")
CORRECTION = re.compile(r"\b(?:no[, ]+i (?:meant|said)|not (?:that|what i|the one)|i said|you (?:missed|forgot|misunderstood)|"
                        r"actually[, ]|instead[, ]|don't (?:do|add|change|touch)|do not (?:do|add|change|touch))", re.I)


class TrackingError(ValueError):
    pass


def new_decision_id():
    return str(uuid.uuid4())


def validate_decision(data):
    if not isinstance(data, dict):
        raise TrackingError("decision must be an object")
    out = dict(data)
    for k in ("s", "l", "g", "chars", "upgrades"):
        v = out.get(k, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise TrackingError("%s must be a nonnegative integer" % k)
        out[k] = v
    decision_id = out.get("decision_id") or new_decision_id()
    if not isinstance(decision_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", decision_id):
        raise TrackingError("invalid decision_id")
    out["decision_id"] = decision_id
    out.setdefault("timestamp", now_iso())
    return out


def _source_ref(value):
    return (isinstance(value, str) and bool(value.strip())) or (
        isinstance(value, dict) and all(value.get(k) is not None and str(value[k]).strip() for k in ("source", "session", "turn")))


def record_outcome(paths, data):
    if not isinstance(data, dict) or data.get("reviewer") != "human":
        raise TrackingError("outcomes require an explicit human reviewer label")
    did = data.get("decision_id")
    ids = {r.get("decision_id") for r in read_jsonl(paths.decisions) if isinstance(r, dict)}
    if not did or did not in ids:
        raise TrackingError("decision_id must refer to a recorded invocation")
    if data.get("outcome") not in OUTCOMES or not _source_ref(data.get("source_ref")):
        raise TrackingError("outcome and source_ref of the observed human follow-up are required")
    row = {"decision_id": did, "outcome": data["outcome"], "source_ref": data["source_ref"], "reviewer": "human",
           "timestamp": now_iso(), "note": str(data.get("note", ""))[:2000]}
    p = _safe_path(paths.home / "telemetry" / "outcomes.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.chmod(p, 0o600)
    return row


def summarize(paths):
    decisions = read_jsonl(paths.decisions)
    linked, legacy, duplicate = {}, 0, 0
    for d in decisions:
        did = d.get("decision_id") if isinstance(d, dict) else None
        if not did:
            legacy += 1
        elif did in linked:
            duplicate += 1
        else:
            linked[did] = d
    valid, invalid, revisions = {}, 0, 0
    for r in read_jsonl(paths.home / "telemetry" / "outcomes.jsonl"):
        if not isinstance(r, dict) or r.get("decision_id") not in linked or r.get("reviewer") != "human" or \
                r.get("outcome") not in OUTCOMES or not _source_ref(r.get("source_ref")):
            invalid += 1
            continue
        revisions += r["decision_id"] in valid
        valid[r["decision_id"]] = r
    counts = {o: sum(r["outcome"] == o for r in valid.values()) for o in sorted(OUTCOMES)}
    reviewed, total = len(valid), len(linked)
    return {"invocations": len(decisions), "linked_invocations": total, "legacy_unlinked": legacy,
            "duplicate_decision_ids": duplicate, "human_reviewed_observed": reviewed, "unreviewed_or_unobserved": total - reviewed,
            "counts": counts, "invalid_outcomes": invalid, "outcome_revisions": revisions,
            "corrections_per_100_observed": round(100 * counts["corrected"] / reviewed, 1) if reviewed else None,
            "denominator": "unique linked invocations with an observed human follow-up explicitly reviewed by a human",
            "limitation": "Unreviewed or missing follow-ups are unknown. Silence is never acceptance."}


def discover_candidates(rows):
    """Normalize explicit displayed briefs with their next human message. Never infer verified correction labels."""
    events, seen = [], set()
    for row in rows:
        brief = row.get("brief", "")
        if not isinstance(brief, str) or not READING.search(brief) or not (TAG.search(brief) or re.search(r"(?m)^\s*>\s*\S", brief)):
            continue
        ref = row.get("source_ref")
        if not _source_ref(ref):
            continue
        identity = {k: ref.get(k) for k in ("source", "session", "turn")} if isinstance(ref, dict) else ref
        eid = _digest({"source_ref": identity, "brief": brief})[:20]
        if eid in seen:
            continue
        seen.add(eid)
        followup = row.get("followup")
        events.append({"id": eid, "seed": row.get("seed", ""), "brief": brief, "source_ref": ref,
                       "followup": followup, "followup_ref": row.get("followup_ref"),
                       "followup_observed": isinstance(followup, str) and bool(followup.strip()),
                       "correction_candidate": bool(isinstance(followup, str) and CORRECTION.search(followup)),
                       "status": "candidate_unreviewed", "verified_correction": None})
    return events


def _text_parts(content, kinds):
    if isinstance(content, str):
        return content
    return "\n".join(x.get("text", "") for x in content or [] if isinstance(x, dict) and x.get("type") in kinds)


def _transcript_messages(path, source):
    """Read only top-level human sessions; reuse the corpus readers' human-origin filters."""
    from .extract_claude import _attachment_record, _user_record
    from .extract_codex import _skip_file, _unwrap_question, QUESTION_OPEN
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            if source == "claude-code":
                if str(r.get("entrypoint", "")).startswith("sdk"):
                    return
                if r.get("isSidechain"):
                    continue
                typ = r.get("type")
                if typ == "assistant":
                    text = _text_parts((r.get("message") or {}).get("content"), {"text"})
                    if text.strip():
                        yield "assistant", text, line_no, r.get("timestamp")
                    continue
                texts = _user_record(r) if typ == "user" else None
                if typ == "attachment":
                    got = _attachment_record(r)
                    texts = got[0] if got else None
                text = "\n".join(texts or [])
            else:
                p = r.get("payload") or {}
                if line_no == 1 and _skip_file(p):
                    return
                if r.get("type") != "response_item" or p.get("type") != "message":
                    continue
                role = p.get("role")
                if role == "assistant":
                    text = _text_parts(p.get("content"), {"output_text", "text"})
                    if text.strip():
                        yield "assistant", text, line_no, r.get("timestamp")
                    continue
                if role != "user":
                    continue
                text = _text_parts(p.get("content"), {"input_text"})
                if text.lstrip().startswith(QUESTION_OPEN):
                    got = _unwrap_question(text)
                    text = got[0] if got else ""
            text, info = filters.clean(text)
            # A long human-supplied follow-up can look drafted. It must still end the observation window;
            # otherwise skipping it would attach a later unrelated message to the displayed brief.
            if filters.keep(text) and not text.lstrip().startswith("<codex_internal_context"):
                yield "user", text, line_no, r.get("timestamp")


def scan_transcripts(cfg):
    """Retrospective candidate audit. A displayed Reading/brief establishes exposure, not invocation words alone."""
    roots = [("claude-code", Path(os.path.expanduser(cfg["sources"]["claude_projects_dir"])), "*/*.jsonl"),
             ("codex", Path(os.path.expanduser(cfg["sources"]["codex_sessions_dir"])), "**/*.jsonl")]
    raw, files, unreadable = [], 0, 0
    for source, root, pattern in roots:
        for path in sorted(root.glob(pattern)):
            if "subagents" in path.parts:
                continue
            files += 1
            previous, agent = None, []
            try:
                for role, text, line, timestamp in _transcript_messages(path, source):
                    ref = {"source": source, "session": path.stem, "turn": line, "path": str(path), "time": timestamp}
                    if role == "assistant":
                        agent.append(text)
                        continue
                    if previous and agent:
                        brief = "\n\n".join(agent)
                        # Store only the displayed reading, not unrelated subsequent tool commentary.
                        at = READING.search(brief)
                        if at:
                            raw.append({"seed": previous["text"], "brief": brief[at.start():], "source_ref": previous["ref"],
                                        "followup": text, "followup_ref": ref})
                    previous, agent = {"text": text, "ref": ref}, []
                if previous and agent:
                    brief = "\n\n".join(agent)
                    at = READING.search(brief)
                    if at:
                        raw.append({"seed": previous["text"], "brief": brief[at.start():], "source_ref": previous["ref"],
                                    "followup": None, "followup_ref": None})
            except OSError:
                unreadable += 1
    events = discover_candidates(raw)
    # Scrub display text after hashing raw evidence. Private source references retain exact file/line provenance.
    for event in events:
        for k in ("seed", "brief", "followup"):
            if event[k]:
                event[k] = scrub(event[k], cfg.get("people"))[0]
    return {"files_scanned": files, "unreadable_files": unreadable, "eligible_displayed_briefs": len(events),
            "observed_followups": sum(e["followup_observed"] for e in events),
            "correction_candidates": sum(e["correction_candidate"] for e in events), "human_reviewed": 0,
            "confirmed_correction_rate": None, "events": events,
            "limitation": "Retrospective detections are candidates requiring human review; they are not a verified correction baseline."}


def build_baseline_review(events, out_dir):
    selected = [e for e in events if e.get("followup_observed")]
    if not selected or len({e["id"] for e in selected}) != len(selected):
        raise TrackingError("baseline review needs unique observed exposure events")
    selected.sort(key=lambda e: e["id"])
    review_id = _digest(selected)[:20]
    key = {e["id"]: {"source_ref": e["source_ref"], "followup_ref": e.get("followup_ref"), "content_hash": _digest(e)} for e in selected}
    items = [{"id": e["id"], "seed": e["seed"], "context": [],
              "options": {"Displayed brief": e["brief"], "Next user message": e["followup"]}} for e in selected]
    out = _safe_path(out_dir)
    key_path, page = out / "private" / "baseline-key.json", out / "baseline.html"
    _safe_path(key_path)
    if key_path.exists() and (json.loads(key_path.read_text()) or {}).get("review_id") != review_id:
        raise TrackingError("baseline directory already belongs to different exposure events")
    key_data = {"schema_version": 1, "kind": "correction_baseline", "review_id": review_id, "pairs": key}
    page_data = _page(items, review_id, "Review actual mind-reading follow-ups", [
        ("corrected", "Corrected the brief"), ("no_correction_observed", "No correction in this follow-up"),
        ("unrelated", "Unrelated follow-up"), ("uncertain", "Uncertain")], "mr-baseline-").encode("utf-8")
    for target, content in ((key_path, _json_bytes(key_data)), (out / "private" / "candidates.json", _json_bytes(events)),
                            (page, page_data)):
        _check_output(target, content)
    _private_json(key_path, key_data)
    _private_json(out / "private" / "candidates.json", events)
    _private_bytes(page, page_data)
    return {"page": str(page), "key": str(key_path), "review_id": review_id, "count": len(items), "human_reviewed": 0}


def import_baseline_answers(key_path, answers_path, out_path=None):
    try:
        key, answers = read_answers(key_path, answers_path, {"corrected", "no_correction_observed", "unrelated", "uncertain"})
    except PreferenceError as exc:
        raise TrackingError(str(exc)) from exc
    if key.get("kind") != "correction_baseline":
        raise TrackingError("key is not a correction baseline review")
    counts = {o: sum(v["pick"] == o for v in answers.values()) for o in ("corrected", "no_correction_observed", "unrelated", "uncertain")}
    den = counts["corrected"] + counts["no_correction_observed"]
    report = {"review_id": key["review_id"], "total_observed": len(key["pairs"]), "human_reviewed": len(answers),
              "missing": len(key["pairs"]) - len(answers), "counts": counts, "eligible_reviewed_denominator": den,
              "corrections_per_100_observed": round(100 * counts["corrected"] / den, 1) if den else None,
              "completed": len(key["pairs"]) == len(answers), "evidence": "human-reviewed retrospective displayed briefs and next user messages",
              "limitation": "Only reviewed eligible observed follow-ups enter the denominator; missing, unrelated and uncertain are separate."}
    if out_path:
        _private_json(out_path, report, replace=True)
    return report
