"""Claude Code reader: ~/.claude/projects/<project>/<session>.jsonl -> raw records of the user's typed text.

Record-level rules (each has a fixture):
- type == "user": isMeta falsy, userType in (None, "external"), isSidechain falsy, entrypoint not sdk*, and
  origin.kind (when present) not task-notification or peer.
- type == "attachment" with attachment.type == "queued_command" and attachment.origin.kind == "human":
  the mid-turn messages. Text = attachment.prompt (string or text parts).
- Text parts: message.content string, or list items with type == "text". tool_result items are scanned only for
  the legacy mid-turn wrapper, and only when the whole result is exactly that wrapper (prefix first, suffix last), so
  a fetched page or command output quoting the sentence never enters the corpus as the user's words.
Subagent transcripts live under <session>/subagents/ and are never globbed.

Agent context (agent_context.py): assistant text blocks, tool_use commands and tool_result errors are read only to
build the short context slice that rides beside the next kept user message; none of it is ever yielded as text."""
import glob
import json
import os
from pathlib import Path

from . import agent_context as _ac
from . import filters


def _parts(content):
    """(text parts, legacy-wrapped mid-turn messages) from a message.content value."""
    texts, legacy = [], []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(item.get("text", "") or "")
            elif item.get("type") == "tool_result":
                body = item.get("content")
                if not isinstance(body, str):
                    body = " ".join(x.get("text", "") for x in (body or []) if isinstance(x, dict))
                legacy += filters.legacy_midturn(body or "")
    return texts, legacy


def _origin_kind(obj):
    o = obj.get("origin")
    if isinstance(o, dict):
        return o.get("kind")
    if isinstance(o, str):
        return o
    return None


def _user_record(r):
    if r.get("isMeta") or r.get("isSidechain"):
        return None
    if r.get("userType") not in (None, "external"):
        return None
    ep = r.get("entrypoint") or ""
    if isinstance(ep, str) and ep.startswith("sdk"):
        return None
    if _origin_kind(r) in ("task-notification", "peer"):
        return None
    texts, legacy = _parts((r.get("message") or {}).get("content"))
    out = []
    for t in texts:
        wrapped = filters.legacy_midturn(t)  # only when the whole text is exactly the harness wrapper
        if wrapped:
            out += wrapped
            continue
        out.append(t)  # text that merely mentions the wrapper sentence is the user's own typed text
    out += legacy
    return out


def _attachment_record(r):
    a = r.get("attachment") or {}
    if a.get("type") != "queued_command":
        return None
    if _origin_kind(a) != "human":
        return None
    prompt = a.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(p.get("text", "") for p in prompt if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(prompt, str):
        return None
    return [prompt], a.get("timestamp") or r.get("timestamp")


def _kept(text):
    return filters.keep(filters.clean(text)[0])


def _scan_assistant(r, turn, tool_names):
    """Record the assistant's text and its commands for the agent-context slice."""
    content = (r.get("message") or {}).get("content")
    if isinstance(content, str):
        turn.assistant_text(content)
        return
    for item in content or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            turn.assistant_text(item.get("text"))
        elif item.get("type") == "tool_use":
            name = item.get("name") or ""
            if item.get("id"):
                tool_names[item["id"]] = name
            inp = item.get("input") if isinstance(item.get("input"), dict) else {}
            if _ac.is_shell(name):
                turn.command(inp.get("command"))


def _scan_tool_results(r, turn, tool_names):
    """Failed, rejected or interrupted tool results in a user-typed record (the harness files them as type user)."""
    tur = r.get("toolUseResult")
    if isinstance(tur, dict) and tur.get("interrupted"):
        turn.interrupted()
    content = (r.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        body = item.get("content")
        if not isinstance(body, str):
            body = " ".join(x.get("text", "") for x in (body or []) if isinstance(x, dict))
        if _ac.REJECTED_TOOL in (body or ""):
            turn.interrupted()
            turn.tool_result(False, False)
            continue
        turn.tool_result(bool(item.get("is_error")), _ac.is_shell(tool_names.get(item.get("tool_use_id"))))


def iter_records(claude_dir, context_chars=_ac.DEFAULT_CHARS):
    """Yield raw records: source, session, record_id, project, time, ordinal, text, origin_hint, meta, path, and
    agent_context ({"head", "markers"} or None; always None when context_chars is 0)."""
    claude_dir = os.path.expanduser(str(claude_dir))
    for path in sorted(glob.glob(os.path.join(claude_dir, "*", "*.jsonl"))):
        project, session = Path(path).parent.name, Path(path).stem
        ordinal = 0
        turn, tool_names = _ac.Turn(), {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                ordinal += 1
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(r, dict):
                    continue
                rtype = r.get("type")
                if rtype == "assistant":
                    if not r.get("isSidechain"):
                        _scan_assistant(r, turn, tool_names)
                    continue
                texts, time = None, r.get("timestamp")
                if rtype == "user":
                    if not r.get("isSidechain"):
                        _scan_tool_results(r, turn, tool_names)
                    texts = _user_record(r)
                elif rtype == "attachment":
                    got = _attachment_record(r)
                    if got:
                        texts, time = got
                if not texts:
                    continue
                rid = r.get("uuid") or "%s:%d" % (time, ordinal)
                for k, text in enumerate(texts):
                    ctx = None
                    if context_chars > 0:
                        if _ac.is_interrupt(text):
                            turn.interrupted()
                        elif _kept(text):
                            ctx = turn.take(context_chars)
                    yield {
                        "source": "claude-code", "session": session, "path": path,
                        "record_id": rid if k == 0 else "%s#%d" % (rid, k),
                        "project": _project_from_cwd(r.get("cwd")) or project,
                        "time": time, "ordinal": ordinal, "text": text, "origin_hint": None, "meta": {},
                        "agent_context": ctx,
                    }


def _project_from_cwd(cwd):
    if not cwd or not isinstance(cwd, str):
        return None
    return cwd
