"""Text-level filtering shared by both readers: tag stripping, skip prefixes, origin classification.

Every entry in SKIP_PREFIXES has a fixture and a test. Text that starts with an unknown tag is kept and the
tag is counted so new harness noise is noticed instead of silently mined."""
import re

SKIP_PREFIXES = (
    "<local-command-caveat>", "<command-name>", "<command-message>", "<bash-input>", "<bash-stdout>",
    "<bash-stderr>", "<task-notification>", "<system-reminder>", "Caveat:", "[Request interrupted",
    "This session is being continued", "<cross-session-message", "[SYSTEM NOTIFICATION", "<local-command-stdout>",
    "<environment_context>", "<user_instructions>", "# AGENTS.md instructions", "<INSTRUCTIONS>", "<turn_aborted>",
    "Base directory for this skill", "<user-prompt-submit-hook>", "Tool loaded.", "Stop hook feedback", "<skill>",
    "<artifact-content-authored-by-others", "<artifact-view-context", "<subagent_notification>",
    "<side-sessions-event>", "<ci-monitor-event>", "[Artifact comment sent to Claude]", "<realtime_delegation>",
    "<external_codex_apps_open_page>", "<in-app-browser-context", "<create-pr-command>", "<queued_command>",
)

LEGACY_MARKER = "The user sent a new message while you were working"
# Exact-wrapper check: the harness prefix opens the block and the harness suffix closes it. A page, file or command
# output that merely contains the sentence somewhere does not become the user's words (a transcript-mined model is a
# memory-poisoning target; the research asks for the prefix and suffix at block boundaries).
MIDTURN = re.compile(
    r"\A\s*The user sent a new message while you were working:[ \t]*\n(.*?)\n[ \t]*\n[ \t]*This is how Claude Code surfaces[^\n]*\s*\Z", re.S)

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_IMAGE_BLOCK = re.compile(r"<image\b[^>]*>.*?</image>", re.S)
_IMAGE_STUB = re.compile(r"<image\b[^>]*/?>")
_PASTE = re.compile(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", re.S)
_TAG = re.compile(r"\s*<([A-Za-z_][\w-]*)")
_KNOWN_TAGS = {p.lstrip("<").rstrip(">").split()[0] for p in SKIP_PREFIXES if p.startswith("<")}
_KNOWN_TAGS |= {"pasted_content", "image", "codex_internal_context", "send_user_message_question_reply", "system-reminder"}

PASTE_HEAD = 200


def clean(text):
    """Strip harness markup. Returns (text, info) where info['pasted_chars'] counts collapsed paste bytes."""
    if not isinstance(text, str):
        return "", {"pasted_chars": 0, "original_chars": 0}
    original = len(text)
    text = _REMINDER.sub("", text)
    text = _IMAGE_BLOCK.sub("", text)
    text = _IMAGE_STUB.sub("", text)
    pasted = {"n": 0}

    def _collapse(m):
        body = m.group(1)
        pasted["n"] += len(body)
        head = body.strip()[:PASTE_HEAD]
        return "[pasted %d chars: %s]" % (len(body), head)

    text = _PASTE.sub(_collapse, text)
    return text.strip(), {"pasted_chars": pasted["n"], "original_chars": original}


def keep(text):
    """False for empty text and for text that starts with a skip prefix."""
    if not text:
        return False
    return not text.lstrip().startswith(SKIP_PREFIXES)


def leading_tag(text):
    """The tag name when the text starts with `<tag`, else None."""
    m = _TAG.match(text or "")
    return m.group(1) if m else None


def unknown_tag(text):
    """A leading tag that is neither a skip prefix nor otherwise handled, else None."""
    tag = leading_tag(text)
    if tag is None or tag in _KNOWN_TAGS:
        return None
    return tag


def legacy_midturn(text):
    """The one message wrapped in the legacy 'new message while you were working' format, when the whole block is
    exactly that wrapper (prefix first, suffix last). Anything else, including the sentence quoted inside other
    text, a wrapper with no suffix, or a wrapped message that itself contains the prefix, yields nothing."""
    if not text or LEGACY_MARKER not in text:
        return []
    m = MIDTURN.match(text)
    if not m:
        return []
    body = m.group(1).strip()
    if not body or LEGACY_MARKER in body:
        return []
    return [body]


_LIST_LINE = re.compile(r"^\s*(?:[-*]\s|#|\d+[.)]\s)")
_TYPO = re.compile(r"\.\s+[a-z]")
# Openers of briefs written by an agent or a script for another agent: second-person imperatives and role framing.
_BRIEF_OPENER = re.compile(
    r"^\s*(?:#+\s*)?(?:you are|you're|your (?:task|job|role|mission) is|read the (?:full )?brief|read the full|"
    r"do not (?:edit|touch|modify)|read-only survey|read only survey|context:|goal:|objective:|mandate:|task:|brief:)", re.I)
_BACKTICK_PATH = re.compile(r"`[^`\s]*[/.][^`\s]*`")

DRAFTED_MIN_CHARS = 1200
DRAFTED_MIN_LIST_LINES = 3
BRIEF_MIN_CHARS = 500
BRIEF_MIN_PATHS = 2


def is_drafted(text):
    """Model-drafted mandate heuristic. Two routes, both needing unusually clean prose (few lowercase letters after a
    full stop): the long structured form (DRAFTED_MIN_CHARS and DRAFTED_MIN_LIST_LINES list lines), or the shorter
    agent-brief form (BRIEF_MIN_CHARS with a second-person opener or BRIEF_MIN_PATHS backticked paths)."""
    n = len(text)
    if n < BRIEF_MIN_CHARS:
        return False
    typos = len(_TYPO.findall(text))
    clean = typos * 400 < n
    if not clean:
        return False
    list_lines = sum(1 for line in text.splitlines() if _LIST_LINE.match(line))
    if n >= DRAFTED_MIN_CHARS and list_lines >= DRAFTED_MIN_LIST_LINES:
        return True
    if _BRIEF_OPENER.match(text):
        return True
    return len(_BACKTICK_PATH.findall(text)) >= BRIEF_MIN_PATHS


def origin(text, info=None, hint=None):
    """One of typed, pasted, drafted, goal_reminder, question_answer."""
    if hint in ("goal_reminder", "question_answer"):
        return hint
    info = info or {}
    pasted = info.get("pasted_chars", 0)
    original = info.get("original_chars", 0) or len(text)
    if pasted and pasted * 2 > original:
        return "pasted"
    if is_drafted(text):
        return "drafted"
    return "typed"
