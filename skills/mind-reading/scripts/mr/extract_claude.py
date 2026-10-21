"""Claude Code reader: ~/.claude/projects/<project>/<session>.jsonl -> raw records of the user's typed text.

Record-level rules (each has a fixture):
- type == "user": isMeta falsy, userType in (None, "external"), isSidechain falsy, entrypoint not sdk*, and
  origin.kind (when present) not task-notification or peer.
- type == "attachment" with attachment.type == "queued_command" and attachment.origin.kind == "human":
  the mid-turn messages. Text = attachment.prompt (string or text parts).
- Text parts: message.content string, or list items with type == "text". tool_result items are scanned only for
  the legacy mid-turn wrapper, and only when the whole result is exactly that wrapper (prefix first, suffix last), so
  a fetched page or command output quoting the sentence never enters the corpus as the user's words.
Subagent transcripts live under <session>/subagents/ and are never globbed."""
import glob
import json
import os
from pathlib import Path

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


def iter_records(claude_dir):
    """Yield raw records: source, session, record_id, project, time, ordinal, text, origin_hint, meta, path."""
    claude_dir = os.path.expanduser(str(claude_dir))
    for path in sorted(glob.glob(os.path.join(claude_dir, "*", "*.jsonl"))):
        project, session = Path(path).parent.name, Path(path).stem
        ordinal = 0
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
                texts, time = None, r.get("timestamp")
                if rtype == "user":
                    texts = _user_record(r)
                elif rtype == "attachment":
                    got = _attachment_record(r)
                    if got:
                        texts, time = got
                if not texts:
                    continue
                rid = r.get("uuid") or "%s:%d" % (time, ordinal)
                for k, text in enumerate(texts):
                    yield {
                        "source": "claude-code", "session": session, "path": path,
                        "record_id": rid if k == 0 else "%s#%d" % (rid, k),
                        "project": _project_from_cwd(r.get("cwd")) or project,
                        "time": time, "ordinal": ordinal, "text": text, "origin_hint": None, "meta": {},
                    }


def _project_from_cwd(cwd):
    if not cwd or not isinstance(cwd, str):
        return None
    return cwd
