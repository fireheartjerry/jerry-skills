"""Agent context: a short, labelled slice of what the agent said and how its turn ended, stored beside each user
message so the lessons lens can see outcomes (what the agent did, whether it failed), not only what the user typed.

It is never the user's voice. The corpus keeps it in its own field (`agent_context`, origin "agent_context"), never in
`text`, so dedup, twins, leaks, seeds, grants, vocabulary and every typed-text check ignore it. It is untrusted data:
an agent reply can quote a web page or a file that carries injected instructions, so the pipeline lets it inform a
lesson as supporting context and nothing else (synthesize.verify_lessons drops any item whose only support is agent
context).

Readers track one turn at a time: the head of the agent's last text reply since the previous user message, and the
markers below for how that turn ended. Both reset when a kept user message receives them."""
import json
import re

MARKERS = ("tool_error", "command_failed", "interrupted", "reverted")
ORIGIN = "agent_context"
DEFAULT_CHARS = 400
# a revert in the agent's own commands: the work was undone. `git restore --staged` alone only unstages and is not one
REVERT_RE = re.compile(r"\bgit\s+(?:revert\b|reset\s+--hard\b|checkout\s+--\s|restore\b(?![^\n;&|]*(?:--staged|\s-S\b))"
                       r"|restore\b(?=[^\n;&|]*(?:--worktree|\s-W\b)))")
INTERRUPT_PREFIXES = ("[Request interrupted", "<turn_aborted>")
REJECTED_TOOL = "The user doesn't want to proceed with this tool use"
# Codex's newer `exec` tool runs a script of exec_command calls; its output reports each command's exit_code
SHELL_TOOLS = {"bash", "shell", "exec", "exec_command", "local_shell", "run_shell_command", "container.exec", "shell_command"}
# the header a shell tool puts before the command's own output: `Exit code: N` (Codex shell), `Exit code N` (Claude
# Bash), `Process exited with code N` (exec_command), each at the start of a line before any `Output:` line
_HEADER_EXIT_RE = re.compile(r"^\s*(?:exit code|process exited with code)\s*[:=]?\s*(-?\d+)\b", re.I | re.M)
# the exec tool's script output: one `"exit_code": N` per command it ran
_SCRIPT_EXIT_RE = re.compile(r"\"?\bexit_code\b\"?\s*[:=]\s*(-?\d+)")
_SCRIPT_HEAD = re.compile(r"^\s*(?:script completed|script failed)\b", re.I)
_OUTPUT_LINE = re.compile(r"^Output:\s*$", re.M)
_ERROR_HEAD = re.compile(r"^\s*(?:error\b|failed\b|script failed\b|apply_patch verification failed)", re.I)
_WS = re.compile(r"\s+")


def head(text, limit):
    """The first `limit` characters of `text` with whitespace folded, cut at a word boundary, ellipsis when cut."""
    t = _WS.sub(" ", text or "").strip()
    if limit <= 0 or not t:
        return ""
    if len(t) <= limit:
        return t
    cut = t[:limit]
    sp = cut.rfind(" ")
    if sp >= limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:") + " ..."


def is_shell(name):
    return (name or "").strip().lower() in SHELL_TOOLS


def output_text(output):
    """A tool output as text: a string as is, a list of content items joined, anything else as JSON."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(x.get("text", "") or "" for x in output if isinstance(x, dict))
    if isinstance(output, dict):
        return output.get("content") if isinstance(output.get("content"), str) else json.dumps(output)
    return ""


def exit_code(output):
    """Exit code reported by a command's output, else None: JSON metadata first, then the tool's leading header (an
    `Exit code: N` or `Process exited with code N` line before the `Output:` line, the first one wins), and for the exec
    tool's script output the last `exit_code` it printed (the command the turn ended on). Exit-code text inside the
    command's own output is never read."""
    if not isinstance(output, str):
        return None
    s = output.strip()
    if s.startswith("{"):
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else data
            code = meta.get("exit_code")
            if isinstance(code, int) and not isinstance(code, bool):
                return code
            out = data.get("output")
            if isinstance(out, str):
                s = out
    if _SCRIPT_HEAD.match(s):
        found = _SCRIPT_EXIT_RE.findall(s)  # a script of commands: the one the turn ended on
        return int(found[-1]) if found else None
    # the tool's own header, never the command's output: text a command prints ("npm ERR! process exited with code
    # 1", "subtask: exit code 0") must not override it
    out_at = _OUTPUT_LINE.search(s)
    header = s[:out_at.start()] if out_at else (s.splitlines() or [""])[0]
    m = _HEADER_EXIT_RE.search(header)
    if m:
        return int(m.group(1))
    if not out_at and not _HEADER_EXIT_RE.search(s):
        found = _SCRIPT_EXIT_RE.findall(s)  # bare script output without its header
        if found:
            return int(found[-1])
    return None


def output_failed(output):
    """(failed, by_exit_code) for a tool output (string, content-item list or dict)."""
    output = output_text(output)
    if _ERROR_HEAD.match(output) and output.lstrip().lower().startswith("script failed"):
        return True, False
    code = exit_code(output)
    if code is not None:
        return code != 0, True
    if isinstance(output, str) and _ERROR_HEAD.match(output):
        return True, False
    return False, False


class Turn:
    """State of the agent turn since the last user message that received a context."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.text = None
        self.last_error = None  # marker of the last tool result when it failed, None when it succeeded
        self.flags = set()

    def assistant_text(self, text):
        if isinstance(text, str) and text.strip():
            self.text = text

    def command(self, command_text):
        """A shell command the agent ran (callers pass shell tools only: a patch that edits docs mentioning `git
        restore` is not a revert)."""
        if isinstance(command_text, str) and REVERT_RE.search(command_text):
            self.flags.add("reverted")

    def tool_result(self, failed, shell):
        self.last_error = ("command_failed" if shell else "tool_error") if failed else None

    def interrupted(self):
        self.flags.add("interrupted")

    def take(self, limit):
        """The context for the next kept user message ({"head", "markers"} or None), then reset."""
        markers = set(self.flags)
        if self.last_error:
            markers.add(self.last_error)
        h = head(self.text, limit) if self.text else ""
        self.reset()
        if not h and not markers:
            return None
        return {"head": h, "markers": [m for m in MARKERS if m in markers]}


def is_interrupt(text):
    return isinstance(text, str) and text.lstrip().startswith(INTERRUPT_PREFIXES)
