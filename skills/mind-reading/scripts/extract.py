"""Your own messages from Claude Code and Codex transcripts, one JSON line each: source, session, project, time, turn,
text. Injected system text, tool results, other sessions' messages and agent-spawned sessions are left out; pasted
material keeps only a short head, since it is not your words.

Writes $MIND_READING_HOME/corpus/messages.jsonl (default ~/.local/share/mind-reading). Reads ~/.claude/projects and
~/.codex/sessions; override with CLAUDE_PROJECTS_DIR / CODEX_SESSIONS_DIR."""
import glob, json, os, re, sys
from pathlib import Path

HOME = Path(os.path.expanduser(os.environ.get("MIND_READING_HOME", "~/.local/share/mind-reading")))
OUT = HOME / "corpus"
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects"))
CODEX_DIR = os.path.expanduser(os.environ.get("CODEX_SESSIONS_DIR", "~/.codex/sessions"))
SKIP_PREFIX = ("<local-command-caveat>", "<command-name>", "<command-message>", "<bash-input>", "<bash-stdout>",
               "<bash-stderr>", "<task-notification>", "<system-reminder>", "Caveat:", "[Request interrupted",
               "This session is being continued", "<cross-session-message", "[SYSTEM NOTIFICATION", "<local-command-stdout>",
               "<environment_context>", "<user_instructions>", "# AGENTS.md instructions", "<INSTRUCTIONS>", "<turn_aborted>",
               "Base directory for this skill", "<user-prompt-submit-hook>", "Tool loaded.", "Stop hook feedback")
MIDTURN = re.compile(r"The user sent a new message while you were working:\s*\n(.*?)(?:\n\nThis is how Claude Code surfaces|\Z)", re.S)


def clean(text: str) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>",
                  lambda m: f"[pasted {len(m.group(1))} chars: {m.group(1).strip()[:200]!r}...]", text, flags=re.S)
    return text.strip()


def keep(text: str) -> bool:
    return bool(text) and not text.lstrip().startswith(SKIP_PREFIX)


def claude_code():
    for path in sorted(glob.glob(os.path.join(CLAUDE_DIR, "*", "*.jsonl"))):
        project, session = Path(path).parent.name, Path(path).stem
        turn = 0
        for line in open(path, errors="replace"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") != "user" or r.get("isMeta") or r.get("userType") not in (None, "external"):
                continue
            content = (r.get("message") or {}).get("content")
            texts = []
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                    elif item.get("type") == "tool_result":       # mid-turn messages surface inside tool results
                        body = item.get("content")
                        body = body if isinstance(body, str) else " ".join(x.get("text", "") for x in (body or []) if isinstance(x, dict))
                        texts += [m.group(1) for m in MIDTURN.finditer(body or "")]
            for text in texts:
                if "The user sent a new message while you were working" in text:
                    found = [m.group(1) for m in MIDTURN.finditer(text)]
                    text = found[0] if found else ""
                text = clean(text)
                if keep(text):
                    turn += 1
                    yield {"source": "claude-code", "session": session, "project": project, "time": r.get("timestamp"),
                           "turn": turn, "text": text}


def codex():
    for path in sorted(glob.glob(os.path.join(CODEX_DIR, "**", "*.jsonl"), recursive=True)):
        with open(path, errors="replace") as fh:
            try:
                meta = json.loads(fh.readline()).get("payload") or {}
            except json.JSONDecodeError:
                continue
            source = meta.get("source")
            if isinstance(source, dict) or meta.get("originator") == "codex_exec":
                continue                                     # spawned by an agent or a script: not your words
            turn, goals = 0, set()
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = r.get("payload") or {}
                if r.get("type") != "response_item" or p.get("type") != "message" or p.get("role") != "user":
                    continue
                for item in p.get("content") or []:
                    text = clean(item.get("text", "")) if isinstance(item, dict) else ""
                    if text.startswith("<codex_internal_context"):      # Codex's goal reminder: your goal, once a session
                        key = re.sub(r"\d+", "", text)[:600]
                        if key in goals:
                            continue
                        goals.add(key)
                        text = "[Codex goal reminder, first time in this session] " + text
                    if keep(text):
                        turn += 1
                        yield {"source": "codex", "session": Path(path).stem, "project": meta.get("cwd"),
                               "time": r.get("timestamp"), "turn": turn, "text": text}


if __name__ == "__main__":
    counts = {}
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "messages.jsonl", "w") as out:
        for row in list(claude_code()) + list(codex()):
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts[row["source"]] = counts.get(row["source"], 0) + 1
    print(counts)
