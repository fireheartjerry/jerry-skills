"""Builders for synthetic Claude Code and Codex transcript lines (tracker and auto-refresh tests). Every name, path and
message here is invented."""
import json
from pathlib import Path


def claude_user(uuid, time, text, session="s1", **kw):
    d = {"type": "user", "uuid": uuid, "timestamp": time, "userType": "external", "entrypoint": "cli",
         "cwd": "/home/user/Code/webshop", "sessionId": session, "message": {"role": "user", "content": text}}
    d.update(kw)
    return json.dumps(d)


def claude_assistant(time, content, **kw):
    d = {"type": "assistant", "timestamp": time, "message": {"role": "assistant", "content": content}}
    d.update(kw)
    return json.dumps(d)


def claude_text(time, text, **kw):
    return claude_assistant(time, [{"type": "text", "text": text}], **kw)


def claude_skill(time, skill="mind-reading", tool_id="t1", **kw):
    return claude_assistant(time, [{"type": "tool_use", "id": tool_id, "name": "Skill", "input": {"skill": skill}}], **kw)


def claude_tool_result(uuid, time, tool_id, body, is_error=False, **kw):
    d = {"type": "user", "uuid": uuid, "timestamp": time, "userType": "external",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": body,
                                                  "is_error": is_error}]}}
    d.update(kw)
    return json.dumps(d)


def claude_slash(uuid, time, args, name="mind-reading", **kw):
    text = "<command-message>%s</command-message>\n<command-name>/%s</command-name>\n<command-args>%s</command-args>" % (name, name, args)
    return claude_user(uuid, time, text, **kw)


def claude_mention(time, skill="mind-reading"):
    return json.dumps({"type": "attachment", "timestamp": time, "attachment": {"type": "skill_mention", "skillName": skill}})


def claude_listing(time):
    """The harness's skill listing attachment: it names the skill and its phrases but is never an invocation."""
    return json.dumps({"type": "attachment", "timestamp": time, "attachment": {
        "type": "skill_listing", "content": "- mind-reading: Fires on \"read my mind\" or /mind-reading"}})


def codex_meta(cwd="/home/user/Code/webshop", source="cli", originator="codex_cli_rs", sid="c1"):
    return json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "originator": originator, "source": source}})


def codex_user(time, text, mid=None):
    p = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    if mid:
        p["id"] = mid
    return json.dumps({"type": "response_item", "timestamp": time, "payload": p})


def codex_assistant(time, text):
    return json.dumps({"type": "response_item", "timestamp": time, "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}})


def codex_skill(time, name="mind-reading"):
    return codex_user(time, "<skill>\n<name>%s</name>\n<path>~/skills/%s/SKILL.md</path>\n---\nname: %s" % (name, name, name))


def codex_abort(time):
    return json.dumps({"type": "event_msg", "timestamp": time, "payload": {"type": "turn_aborted"}})


def write_claude(root, project, session, lines):
    p = Path(root) / project / (session + ".jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_codex(root, name, lines):
    p = Path(root) / "2026" / "03" / "01" / (name + ".jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
