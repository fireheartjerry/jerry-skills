"""Codex reader: ~/.codex/sessions/**/*.jsonl rollouts -> raw records of the user's typed text.

File-level rule: read the first line as session_meta. Skip the file when payload.source is a dict (subagent spawn),
payload.source == "exec", payload.originator == "codex_exec", or payload.thread_source exists and is not "user".
Record-level: type == "response_item", payload.type == "message", payload.role == "user", content items of type
input_text (input_image dropped). Goal reminders are kept once per session; question replies are unwrapped to
the answer text."""
import glob
import hashlib
import json
import os
import re
from pathlib import Path

GOAL_OPEN = re.compile(r'^\s*<codex_internal_context\s+source="(?:user_)?goal"')
QUESTION_OPEN = "<send_user_message_question_reply>"
QUESTION_CLOSE = "</send_user_message_question_reply>"
GOAL_MARK = "[goal reminder, kept once] "


def _skip_file(meta):
    src = meta.get("source")
    if isinstance(src, dict) or src == "exec":
        return True
    if meta.get("originator") == "codex_exec":
        return True
    ts = meta.get("thread_source")
    if ts is not None and ts != "user":
        return True
    return False


def _goal_key(text):
    return re.sub(r"\d+", "", text)[:600]


def _unwrap_question(text):
    """Answer strings joined by newlines, and the question text for meta. None when unparseable."""
    body = text.strip()
    if body.startswith(QUESTION_OPEN):
        body = body[len(QUESTION_OPEN):]
    if body.rstrip().endswith(QUESTION_CLOSE):
        body = body.rstrip()[:-len(QUESTION_CLOSE)]
    try:
        items = json.loads(body.strip())
    except json.JSONDecodeError:
        return None
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None
    answers, questions = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        a = it.get("answer")
        if isinstance(a, list):
            a = "\n".join(str(x) for x in a)
        if isinstance(a, str) and a.strip():
            answers.append(a.strip())
        q = it.get("question")
        if isinstance(q, str) and q.strip():
            questions.append(q.strip())
    if not answers:
        return None
    return "\n".join(answers), " | ".join(questions)[:200]


def iter_records(codex_dir):
    codex_dir = os.path.expanduser(str(codex_dir))
    for path in sorted(glob.glob(os.path.join(codex_dir, "**", "*.jsonl"), recursive=True)):
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            try:
                head = json.loads(first)
            except json.JSONDecodeError:
                continue
            if not isinstance(head, dict):
                continue
            meta = head.get("payload") if head.get("type") == "session_meta" else None
            if not isinstance(meta, dict):
                meta = head.get("payload") or {}
            if _skip_file(meta):
                continue
            session = Path(path).stem
            project = meta.get("cwd")
            goals_seen = set()
            ordinal = 1
            for line in fh:
                ordinal += 1
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(r, dict) or r.get("type") != "response_item":
                    continue
                p = r.get("payload") or {}
                if p.get("type") != "message" or p.get("role") != "user":
                    continue
                time = r.get("timestamp")
                base_id = p.get("id")
                k = 0
                for item in p.get("content") or []:
                    if not isinstance(item, dict) or item.get("type") != "input_text":
                        continue
                    text = item.get("text", "") or ""
                    hint, extra_meta = None, {}
                    if GOAL_OPEN.match(text):
                        key = _goal_key(text)
                        if key in goals_seen:
                            continue
                        goals_seen.add(key)
                        hint = "goal_reminder"
                        text = GOAL_MARK + text.strip()
                    elif text.lstrip().startswith(QUESTION_OPEN):
                        got = _unwrap_question(text)
                        if not got:
                            continue
                        text, question = got
                        hint = "question_answer"
                        extra_meta = {"question": question}
                    # No payload id: key on time plus a text digest, not the line ordinal, so a rollout rewritten in
                    # place (lines inserted or removed) does not re-append every record under new keys.
                    bid = base_id or "%s:%s" % (time, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12])
                    rid = bid if k == 0 else "%s#%d" % (bid, k)
                    k += 1
                    yield {
                        "source": "codex", "session": session, "path": path, "record_id": rid,
                        "project": project, "time": time, "ordinal": ordinal, "text": text,
                        "origin_hint": hint, "meta": extra_meta,
                    }
