"""Grok raw-session ingest: chat_history.jsonl, events.jsonl and prompt_history.jsonl, plus the derived SQLite index.

    discover(root)                                          -> sorted list of known Grok trace Paths
    iter_events(path, source="grok", machine="local", limit_bytes=None) -> generator of canonical event dicts

Layout: sessions/<percent-encoded-cwd>/<session-uuid>/{chat_history,events}.jsonl and
sessions/<percent-encoded-cwd>/prompt_history.jsonl. session_search.sqlite is a derivative index and is delegated to
trace_ingest. Events follow the trace_ingest contract (same ids, scrubbing, limits and closing source_summary).

Grok writes no positive human marker, so a user-role chat row or a prompt_history row is NOT proof of a person: actor is
"unknown" with human_signal "grok.unverified_prompt" unless the row carries an explicit origin.kind of "human" (human)
or an agent/sidechain marker (agent). Thinking never becomes text, system rows and injected wrappers are blanked, tool
calls and results are observations, and events.jsonl contributes only its type and key names, never values. Chat rows
without a timestamp keep time None; nothing is guessed from folder names or mtimes."""
from pathlib import Path
from urllib.parse import unquote

from . import trace_ingest as _ti

CHAT, EVENTS, PROMPTS, SQLITE = "chat_history.jsonl", "events.jsonl", "prompt_history.jsonl", "session_search.sqlite"
KNOWN = (CHAT, EVENTS, PROMPTS, SQLITE)
_SESSION_FILES = (CHAT, EVENTS)
_AGENT_KINDS = _ti._AGENT_ORIGINS | {"sidechain", "subagent", "tool"}
MAX_KEYS = 40


def _is_uuid(name):
    return bool(_ti._UUID_ANY.fullmatch(name or ""))


def discover(root):
    """Sorted known Grok trace files under root. Missing root -> []. Auth, config and other JSON are never listed."""
    root = Path(root)
    if root.is_file():
        return [root] if _wanted(root) else []
    if not root.exists():
        return []
    return sorted({p for p in _ti._rglob(root, lambda n: n in KNOWN) if _wanted(p)})


def _wanted(p):
    if p.name in _SESSION_FILES:
        return _is_uuid(p.parent.name)
    return p.name in (PROMPTS, SQLITE)


def _project(path):
    enc = path.parent.parent.name if path.name in _SESSION_FILES else path.parent.name
    return unquote(enc) or None


def _origin_kind(rec):
    o = rec.get("origin")
    k = o.get("kind") if isinstance(o, dict) else o
    return k.strip().lower() if isinstance(k, str) else None


def _time(rec):
    for key in ("timestamp", "ts"):
        t = _ti._iso(rec.get(key))
        if t:
            return t
    return None


def _user_actor(rec, text):
    kind = _origin_kind(rec)
    if _ti._INJECTED.match(text or ""):
        return "injected", "grok.injected"
    if rec.get("isSidechain") is True or kind in _AGENT_KINDS:
        return "agent", "grok.origin_agent"
    if kind == "human":
        return "human", "grok.origin_human"
    return "unknown", "grok.unverified_prompt"


def _parse_chat(run):
    path = run.path
    folder = path.parent.name if _is_uuid(path.parent.name) else ""
    project = _project(path)
    for _, rec in _ti._rows(run):
        rtype = rec.get("type") if isinstance(rec.get("type"), str) else rec.get("role")
        if rtype not in ("system", "user", "assistant", "tool", "tool_result"):
            run.ignore("type:%s" % rtype)
            continue
        sid_field = _ti._s(rec.get("session_id"))
        session = folder or sid_field or _ti._s(rec.get("id")) or "document:" + run.pathhash
        mid = _ti._s(rec.get("uuid")) or _ti._s(rec.get("message_id"))
        if not mid and sid_field and _is_uuid(_ti._s(rec.get("id"))):
            mid = rec["id"]
        mid = mid or None
        common = dict(session=session, project=project, time=_time(rec), parent=None)
        blocks = _ti._blocks(rec.get("content"))
        kinds = []
        for b in blocks:
            k = _ti._s(b.get("type")) or "unknown"
            if k not in kinds:
                kinds.append(k)
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if rtype == "system":
            yield from run.ev(native=mid, role="meta", actor="injected", text=_ti._flat_text(rec.get("content")),
                              blank=True, signal="grok.system", kinds=kinds or ["system"], **common)
        elif rtype == "assistant":
            texts = []
            for b in blocks:
                bt = b.get("type")
                if bt in (None, "text") and isinstance(b.get("text"), str):
                    texts.append(b["text"])
                elif bt in ("thinking", "redacted_thinking", "reasoning"):
                    run.ignore("thinking_block")
                elif bt == "tool_use":
                    tid = _ti._s(b.get("id")) or None
                    name = _ti._s(b.get("name"))
                    run.remember_call(tid, name)
                    yield from run.ev(native="%s:%s" % (mid, tid) if mid and tid else None, role="tool",
                                      actor="agent", text="", signal="grok.tool_use", kinds=["tool_use"],
                                      action=_ti._action("tool_call", name, tid, None,
                                                         _ti._clip(_ti._dumps(b.get("input")))), **common)
            text = "\n".join(texts)
            if text.strip():
                yield from run.ev(native=mid, role="assistant", actor="agent", text=text, signal="grok.assistant",
                                  kinds=[k for k in kinds if k not in ("thinking", "redacted_thinking", "reasoning")],
                                  **common)
        elif rtype in ("tool", "tool_result") or results:
            if not results:
                tid = _ti._s(rec.get("tool_call_id")) or _ti._s(rec.get("tool_use_id")) or None
                results = [{"tool_use_id": tid, "content": rec.get("content"), **{
                    k: rec[k] for k in ("is_error", "isError", "exit_code", "exitCode", "error") if k in rec}}]
            for b in results:
                tid = _ti._s(b.get("tool_use_id")) or None
                yield from run.ev(native="%s:%s" % (mid, tid) if mid and tid else None, role="tool", actor="agent",
                                  text="", signal="grok.tool_result", kinds=["tool_result"],
                                  action=_ti._action("tool_result", run.calls.get(tid, ""), tid, _ti._success_from(b),
                                                     _ti._clip(_ti._flat_text(b.get("content")))), **common)
            if rtype == "user" and any(b.get("type") == "text" for b in blocks):
                run.ignore("user_text_with_tool_result")
        else:
            text = "\n".join(_ti._s(b.get("text")) for b in blocks if b.get("type") in (None, "text"))
            actor, signal = _user_actor(rec, text)
            if actor == "injected":
                yield from run.ev(native=mid, role="meta", actor=actor, text=text, blank=True, signal=signal,
                                  kinds=kinds or ["injected"], **common)
            else:
                yield from run.ev(native=mid, role="user", actor=actor, text=text, signal=signal, kinds=kinds,
                                  **common)


def _parse_prompts(run):
    project = _project(run.path)
    for _, rec in _ti._rows(run):
        prompt = rec.get("prompt")
        if not isinstance(prompt, str):
            run.ignore("no_prompt")
            continue
        kind = _origin_kind(rec)
        if kind == "human":
            actor, signal = "human", "grok.origin_human"
        elif kind in _AGENT_KINDS:
            actor, signal = "agent", "grok.origin_agent"
        else:
            actor, signal = "unknown", "grok.unverified_prompt"
        yield from run.ev(native=None, role="user", actor=actor, text=prompt, signal=signal,
                          kinds=["bash" if rec.get("is_bash") is True else "text"], parent=None,
                          session=_ti._s(rec.get("session_id")) or "document:" + run.pathhash,
                          project=project, time=_ti._iso(rec.get("timestamp")))


def _parse_events(run):
    folder = run.path.parent.name if _is_uuid(run.path.parent.name) else ""
    project = _project(run.path)
    for _, rec in _ti._rows(run):
        etype = rec.get("type") if isinstance(rec.get("type"), str) else ""
        meta = rec.get("metadata")
        info = {"type": _ti._clip(etype, 80),
                "keys": sorted(_ti._clip(str(k), 60) for k in rec if k != "type")[:MAX_KEYS],
                "metadata_keys": sorted(_ti._clip(str(k), 60) for k in meta)[:MAX_KEYS] if isinstance(meta, dict) else []}
        yield from run.ev(native=None, role="meta", actor="unknown", text="", blank=True, signal="grok.event",
                          kinds=["event"], parent=None, project=project, time=_time(rec),
                          session=folder or _ti._s(rec.get("session_id")) or "document:" + run.pathhash,
                          action=_ti._action("event", info["type"], None, None, _ti._dumps(info)))


_PARSERS = {CHAT: _parse_chat, PROMPTS: _parse_prompts, EVENTS: _parse_events}


def iter_events(path, source="grok", machine="local", limit_bytes=None):
    """Stream canonical events for one Grok trace file, ending with a source_summary diagnostics event."""
    if source != "grok":
        raise _ti.TraceError("source must be 'grok', got %r" % (source,))
    p = Path(path)
    if p.name == SQLITE:
        return _ti.iter_events(p, "grok", machine, limit_bytes)
    if p.name not in _PARSERS:
        raise _ti.TraceError("not a Grok trace file: %s" % p.name)
    if not p.is_file():
        raise _ti.TraceError("not a file: %s" % p.name)
    if limit_bytes is not None and (isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int) or limit_bytes < 0):
        raise _ti.TraceError("limit_bytes must be a non-negative integer")
    return _stream(_ti._Run(p, "grok", str(machine), limit_bytes), _PARSERS[p.name])


def _stream(run, parser):
    try:
        for e in parser(run):
            yield e
    except Exception as exc:  # an odd row must not hide the rows already read
        run.error("parser_failed:%s" % type(exc).__name__)
    yield run.summary_event()
