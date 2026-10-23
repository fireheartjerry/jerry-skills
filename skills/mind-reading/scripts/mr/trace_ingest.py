"""Source-first trace ingest: streaming, provenance-rich observations from Claude Code, Codex, Kimi and Grok sessions.

    discover(root, source)                                   -> sorted list of transcript/session Paths
    iter_events(path, source, machine="local", limit_bytes=None) -> generator of event dicts

Sources: claude, codex, kimi, grok, memory. Every event carries id, source, machine, session, project, time, ordinal,
role, actor, text, text_chars, text_truncated, source_ref, parent_session, human_signal, content_kinds, action and
evidence_key (path- and session-free hash of the original evidence; None without a valid time).
`actor` is "human" only on strong provenance for that source (see the per-source parsers); everything the source does
not mark as a person stays "unknown", "agent" or "injected". Thinking/reasoning, system/developer instructions and
injected instruction blocks never become text. Tool inputs and results are observations: `action.success` is set only
from an explicit status field or exit code, never from words in the output. Visible text and action summaries pass
through scrub.scrub before they leave this module. `project` is the raw cwd (the caller maps it).

Every file ends with one summary event (action.kind == "source_summary") whose summary is compact JSON with line,
malformed, duplicate and ignored-record counts plus decoder errors. Bad rows are counted and skipped, the source file
is only ever read. limit_bytes freezes a prefix (of the decompressed stream for .zst) so append-only files stay
consistent; a line cut by the limit is dropped, not reported as malformed. Python 3.8+ stdlib; .zst needs the
optional zstandard module or the zstd CLI, and a missing decoder is recorded as a diagnostic."""
import contextlib
import datetime as _dt
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path

from . import scrub as _scrub

SOURCES = ("claude", "codex", "kimi", "grok", "memory")
MAX_TEXT = 16000
MAX_SUMMARY = 2000
ACTION_CLIP = 600
MAX_LINE = 32 * 1024 * 1024
PRESCRUB_CAP = 200000
MAX_CALLS = 50000

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_JSONL = re.compile(r"^" + _UUID + r"\.jsonl$")
_UUID_ANY = re.compile(_UUID)
_INJECTED = re.compile(
    r"^\s*(?:<(?:system-reminder|command-name|command-message|command-args|local-command-stdout|local-command-stderr"
    r"|local-command-caveat|task-notification|user-prompt-submit-hook|environment_context|INSTRUCTIONS"
    r"|permissions instructions|skills_instructions|plugins|collaboration_mode|codex_internal_context"
    r"|subagent_notification|turn_aborted|user_shell_command|available-deferred-tools)\b"
    r"|# AGENTS\.md instructions|Base directory for this skill:|\[Request interrupted|Caveat: The messages below)",
    re.I)
_EXIT = re.compile(r"(?i)\b(?:process exited with code|exit(?:ed)? code|exit_code|exitcode)\W{0,3}(-?\d+)")
_ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d:?\d\d)?$")
_AGENT_ORIGINS = {"task-notification", "system", "hook", "agent", "coordinator", "scheduled", "system_trigger"}
_DELEGATED_THREADS = {"subagent", "automation", "agent_created_thread", "agent-created", "agent_created"}


class TraceError(ValueError):
    pass


# ---------------------------------------------------------------- discovery

def _scan(path):
    try:
        with os.scandir(str(path)) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _rglob(root, pattern_ok):
    out = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        for name in filenames:
            if pattern_ok(name):
                out.append(Path(dirpath) / name)
    return out


def discover(root, source):
    """Sorted transcript/session files under root for one source. Missing root -> []."""
    if source not in SOURCES:
        raise TraceError("unknown source %r (expected one of %s)" % (source, ", ".join(SOURCES)))
    root = Path(root)
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    found = []
    if source == "claude":
        # <project>/<uuid>.jsonl and <project>/<uuid>/subagents/agent-*.jsonl; root may itself be one project dir
        for level in (root,) + tuple(Path(e.path) for e in _scan(root) if e.is_dir(follow_symlinks=False)):
            for e in _scan(level):
                if e.is_file() and _UUID_JSONL.match(e.name):
                    found.append(Path(e.path))
                elif e.is_dir(follow_symlinks=False) and _UUID_ANY.fullmatch(e.name):
                    sub = Path(e.path) / "subagents"
                    found.extend(Path(x.path) for x in _scan(sub)
                                 if x.is_file() and x.name.startswith("agent-") and x.name.endswith(".jsonl"))
    elif source == "codex":
        found = _rglob(root, lambda n: n.startswith("rollout-") and (n.endswith(".jsonl") or n.endswith(".jsonl.zst")))
    elif source == "kimi":
        found = [p for p in _rglob(root, lambda n: n == "wire.jsonl") if p.parent.parent.name == "agents"]
    elif source == "grok":
        found = _rglob(root, lambda n: n == "session_search.sqlite")
    else:
        found = _rglob(root, lambda n: n.endswith(".md"))
    return sorted(set(found))


# ---------------------------------------------------------------- helpers

def _s(x):
    return x if isinstance(x, str) else ""


def _sha(*parts):
    return hashlib.sha1("\0".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()


def _norm_hash(text):
    return _sha(" ".join(text.split()))


def _iso(v):
    """UTC ISO string with millisecond precision from ISO text, epoch seconds or epoch milliseconds; else None."""
    try:
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, (int, float)):
            secs = v / 1000.0 if abs(v) >= 1e11 else float(v)
            d = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=secs)
        elif isinstance(v, str):
            m = _ISO.match(v.strip())
            if not m:
                return None
            y, mo, da, h, mi, se, frac, tz = m.groups()
            d = _dt.datetime(int(y), int(mo), int(da), int(h), int(mi), int(se),
                             int((frac or "0")[:6].ljust(6, "0")), tzinfo=_dt.timezone.utc)
            if tz and tz != "Z":
                sign = 1 if tz[0] == "+" else -1
                digits = tz[1:].replace(":", "")
                d -= sign * _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        else:
            return None
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)
    except (ValueError, OverflowError, OSError):
        return None


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                out.append(b)
            elif isinstance(b, str):
                out.append({"type": "text", "text": b})
    return out


def _flat_text(content):
    """Text of a str / list-of-blocks payload (tool outputs included); non-text blocks contribute nothing."""
    return "\n".join(_s(b.get("text")) for b in _blocks(content) if b.get("type") in (None, "text", "input_text", "output_text"))


def _clip(text, n=ACTION_CLIP):
    if not text:
        return ""
    return _scrub.scrub(text[:n * 4])[0][:n]


def _dumps(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""


def _success_from(obj):
    """Explicit success from a status object: is_error / error / exit code. None when nothing explicit."""
    if not isinstance(obj, dict):
        return None
    for key in ("is_error", "isError"):
        if isinstance(obj.get(key), bool):
            return not obj[key]
    if obj.get("error"):
        return False
    for key in ("exit_code", "exitCode", "returncode"):
        v = obj.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v == 0
    meta = obj.get("metadata")
    return _success_from(meta) if isinstance(meta, dict) else None


def _success_from_text(text):
    """Explicit exit code at the head of a tool output ("Process exited with code 1", JSON exit_code)."""
    head = text[:300].lstrip()
    if head.startswith("{"):
        try:
            got = _success_from(json.loads(text))
            if got is not None:
                return got
        except (ValueError, RecursionError):
            pass
    m = _EXIT.search(head)
    return int(m.group(1)) == 0 if m else None


def _action(kind, name, call_id, success, summary):
    return {"kind": kind, "name": name, "call_id": call_id, "success": success, "summary": (summary or "")[:MAX_SUMMARY]}


# ---------------------------------------------------------------- run state and stream reading

class _Run:
    def __init__(self, path, source, machine, limit_bytes):
        self.path = path
        self.source = source
        self.machine = machine
        self.limit = limit_bytes
        self.pathhash = _sha(os.path.abspath(str(path)))[:12]
        self.ordinal = 0
        self.lineno = 0
        self.bytes = 0
        self.malformed = 0
        self.oversize = 0
        self.duplicates = 0
        self.truncated_tail = False
        self.early_stop = False
        self.compressed = path.name.endswith(".zst")
        self.events = Counter()
        self.ignored = Counter()
        self.errors = []
        self.seen = set()
        self.calls = {}
        self.session_hint = None
        self.project_hint = None
        self.unanchored = 0

    def error(self, msg):
        if msg not in self.errors and len(self.errors) < 10:
            self.errors.append(msg)

    def ignore(self, what):
        self.ignored[str(what)[:60]] += 1

    def ev(self, *, native, role, actor, text, session, project, time, signal, kinds, action=None, parent=None,
           blank=False):
        """One-element list holding the event, or [] when the native id was already seen in this file."""
        if native is not None:
            if native in self.seen:
                self.duplicates += 1
                return []
            self.seen.add(native)
        raw = text or ""
        chars = len(raw)
        shown = ""
        if raw and not blank:
            shown = _scrub.scrub(raw[:PRESCRUB_CAP])[0][:MAX_TEXT]
        if self.session_hint is None and session:
            self.session_hint = session
        if self.project_hint is None and project:
            self.project_hint = project
        norm = _norm_hash(raw[:PRESCRUB_CAP])
        if native is not None:
            # Native item ids can repeat in separate sessions and copied fork
            # prefixes. Preserve each session's trajectory; the family builder
            # handles mirrored history before any evaluation partition is made.
            ident = _sha(self.source, session, native)[:24]
            ref = "%s:%s" % (self.pathhash, native)
        else:
            # Path-free: exact transcript copies under different paths share ids, genuine repeats differ by
            # output ordinal and time.
            ident = _sha(self.source, session, self.ordinal, role, actor, norm, time,
                         action["kind"] if action else "", action.get("call_id") if action else "")[:24]
            ref = "%s:L%d" % (self.pathhash, self.lineno)
        # Original-evidence key across copies and forks (no path, no session); hash only, needs a real time.
        evidence = _sha("evidence", self.source, native if native is not None else "", role, norm, time)[:24] \
            if time else None
        out = {
            "id": ident, "source": self.source, "machine": self.machine, "session": session,
            "project": project if isinstance(project, str) else None, "time": time, "ordinal": self.ordinal,
            "role": role, "actor": actor, "text": shown, "text_chars": chars,
            "text_truncated": bool(raw) and not blank and chars > MAX_TEXT, "source_ref": ref,
            "parent_session": parent, "human_signal": signal, "content_kinds": list(kinds), "action": action,
            "evidence_key": evidence,
        }
        self.ordinal += 1
        self.events["%s/%s" % (role, actor)] += 1
        return [out]

    def remember_call(self, call_id, name):
        if call_id is not None and len(self.calls) < MAX_CALLS:
            self.calls[call_id] = name

    def summary_event(self):
        info = {"v": 1, "source": self.source, "compressed": self.compressed, "lines": self.lineno,
                "bytes": self.bytes, "malformed": self.malformed, "oversize_lines": self.oversize,
                "duplicates": self.duplicates, "unanchored_user_events": self.unanchored, "limit_bytes": self.limit, "truncated_tail": self.truncated_tail,
                "events": dict(self.events), "ignored": dict(self.ignored.most_common(20)), "errors": self.errors}
        clean = not (self.malformed or self.oversize or self.errors)
        self.lineno = 0
        out = self.ev(native="summary", role="meta", actor="unknown", text="", session=self.session_hint,
                      project=self.project_hint, time=None, signal="diagnostic", kinds=["source_summary"],
                      action=_action("source_summary", self.source, None, clean, _dumps(info)))
        out[0]["id"] = _sha(self.source, self.pathhash, "summary")[:24]
        return out[0]


class _StrictZstdReader(io.RawIOBase):
    """Incremental frames with explicit EOF validation (stream_reader can accept truncation)."""
    def __init__(self, handle, module):
        super().__init__()
        self.handle, self.module = handle, module
        self.decoder = module.ZstdDecompressor().decompressobj()
        self.pending, self.offset, self.tail, self.started = b"", 0, b"", False

    def readable(self):
        return True

    def readinto(self, buffer):
        if not len(buffer):
            return 0
        while self.offset == len(self.pending):
            chunk = self.tail or self.handle.read(8192)
            self.tail = b""
            if not chunk:
                if not self.started or not self.decoder.eof:
                    raise EOFError("incomplete zstd frame")
                return 0
            if self.decoder.eof:
                self.decoder = self.module.ZstdDecompressor().decompressobj()
            self.started = True
            self.pending = self.decoder.decompress(chunk)
            self.offset = 0
            if self.decoder.eof:
                self.tail = self.decoder.unused_data
        n = min(len(buffer), len(self.pending)-self.offset)
        buffer[:n] = self.pending[self.offset:self.offset+n]
        self.offset += n
        return n


@contextlib.contextmanager
def _zst_stream(run):
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        with open(str(run.path), "rb") as fh:
            with io.BufferedReader(_StrictZstdReader(fh, zstandard)) as reader:
                yield reader
        return
    exe = shutil.which("zstd")
    if not exe:
        run.error("zstd_unavailable")
        yield None
        return
    proc = subprocess.Popen([exe, "-q", "-dc", "--", str(run.path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield proc.stdout
    finally:
        if run.early_stop:
            proc.kill()
        proc.stdout.close()
        rc = proc.wait()
        if rc != 0 and not run.early_stop:
            run.error("decompress_failed:zstd_exit_%d" % rc)


def _lines(run):
    """Raw lines (bytes) of the source prefix, decompressing .zst on the fly. Never loads the file whole."""
    opener = _zst_stream(run) if run.compressed else None
    fh = None
    try:
        if opener is not None:
            stream = opener.__enter__()
        else:
            fh = open(str(run.path), "rb")
            stream = fh
        try:
            if stream is None:
                return
            while True:
                line = stream.readline(MAX_LINE)
                if not line:
                    return
                big = len(line) >= MAX_LINE and not line.endswith(b"\n")
                size = len(line)
                if big:  # drain the rest of the oversize line
                    while True:
                        rest = stream.readline(MAX_LINE)
                        size += len(rest)
                        if not rest or rest.endswith(b"\n"):
                            break
                if run.limit is not None and run.bytes + size > run.limit:
                    run.truncated_tail = True
                    run.early_stop = True
                    return
                run.bytes += size
                if big:
                    run.oversize += 1
                    run.lineno += 1
                    continue
                yield line
        except Exception as exc:  # decoder errors surface on read; keep the rows already yielded
            if opener is not None:
                run.error("decompress_failed:%s" % type(exc).__name__)
            else:
                run.error("read_failed:%s" % type(exc).__name__)
        finally:
            if opener is not None:
                opener.__exit__(None, None, None)
    finally:
        if fh is not None:
            fh.close()


def _rows(run):
    """(lineno, dict) for each parseable JSON object line; malformed lines are counted and skipped."""
    for raw in _lines(run):
        run.lineno += 1
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            run.malformed += 1
            continue
        if not isinstance(obj, dict):
            run.malformed += 1
            continue
        yield run.lineno, obj


# ---------------------------------------------------------------- claude

def _claude_user_actor(rec, text):
    if rec.get("isMeta") is True or _INJECTED.match(text or ""):
        return "injected", "claude.injected"
    origin = rec.get("origin")
    okind = origin.get("kind") if isinstance(origin, dict) else (origin if isinstance(origin, str) else None)
    tor = rec.get("turnOrigin") if isinstance(rec.get("turnOrigin"), str) else None
    if okind == "human":
        return "human", "claude.origin.human"
    if tor == "human":
        return "human", "claude.turnOrigin.human"
    if rec.get("sourceToolAssistantUUID") or rec.get("toolUseResult") is not None:
        return "agent", "claude.tool_related"
    if okind in _AGENT_ORIGINS:
        return "agent", "claude.origin." + okind
    if okind or tor:
        # SDK / other origin without a human marker: neither human nor robot is established
        return "unknown", "claude.unknown_origin:%s/%s" % (okind or "null", tor or "null")
    return "unknown", "claude.legacy_no_origin"


def _parse_claude(run):
    path = run.path
    path_sub = path.parent.name == "subagents"
    path_session = path.parent.parent.name if path_sub else path.stem
    for lineno, rec in _rows(run):
        rtype = rec.get("type")
        msg = rec.get("message")
        if rtype not in ("user", "assistant") or not isinstance(msg, dict):
            run.ignore("type:%s" % rtype)
            continue
        sid = _s(rec.get("sessionId")) or path_session
        sub = path_sub or rec.get("isSidechain") is True or bool(rec.get("agentId"))
        agent_id = _s(rec.get("agentId")) or (path.stem if path_sub else "")
        session = "%s/%s" % (sid, agent_id or path.stem) if sub else sid
        common = dict(session=session, project=rec.get("cwd"), time=_iso(rec.get("timestamp")),
                      parent=sid if sub else None)
        uid = _s(rec.get("uuid")) or None
        blocks = _blocks(msg.get("content"))
        kinds = []
        for b in blocks:
            k = _s(b.get("type")) or "unknown"
            if k not in kinds:
                kinds.append(k)
        if rtype == "assistant":
            texts = []
            for b in blocks:
                bt = b.get("type")
                if bt == "text" and isinstance(b.get("text"), str):
                    texts.append(b["text"])
                elif bt in ("thinking", "redacted_thinking"):
                    run.ignore("thinking_block")
                elif bt == "tool_use":
                    tid = _s(b.get("id")) or None
                    name = _s(b.get("name"))
                    run.remember_call(tid, name)
                    yield from run.ev(native="%s:%s" % (uid, tid) if uid and tid else None, role="tool",
                                      actor="agent", text="", signal="claude.tool_use", kinds=["tool_use"],
                                      action=_action("tool_call", name, tid, None, _clip(_dumps(b.get("input")))),
                                      **common)
            text = "\n".join(texts)
            if text.strip():
                yield from run.ev(native=uid, role="assistant", actor="agent", text=text,
                                  signal="claude.sidechain" if sub else "claude.assistant",
                                  kinds=[k for k in kinds if k not in ("thinking", "redacted_thinking")], **common)
            continue
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            for b in results:
                tid = _s(b.get("tool_use_id")) or None
                body = _flat_text(b.get("content"))
                success = _success_from(b)
                if success is None:
                    success = _success_from(rec.get("toolUseResult"))
                yield from run.ev(native="%s:%s" % (uid, tid) if uid and tid else None, role="tool", actor="agent",
                                  text="", signal="claude.tool_result", kinds=["tool_result"],
                                  action=_action("tool_result", run.calls.get(tid, ""), tid, success, _clip(body)),
                                  **common)
            if any(b.get("type") == "text" for b in blocks):
                run.ignore("user_text_with_tool_result")
            continue
        text = "\n".join(_s(b.get("text")) for b in blocks if b.get("type") == "text")
        if sub:
            actor, signal = "agent", "claude.sidechain"
        else:
            actor, signal = _claude_user_actor(rec, text)
        if actor == "injected":
            yield from run.ev(native=uid, role="meta", actor="injected", text=text, blank=True, signal=signal,
                              kinds=kinds or ["injected"], **common)
        else:
            yield from run.ev(native=uid, role="user", actor=actor, text=text, signal=signal, kinds=kinds, **common)


# ---------------------------------------------------------------- codex

def _codex_class(meta):
    """(user-text actor, signal, parent session id) from session_meta provenance."""
    src = meta.get("source")
    ts = meta.get("thread_source")
    spawn = None
    if isinstance(src, dict):
        sub = src.get("subagent")
        spawn = (sub.get("thread_spawn") if isinstance(sub, dict) else None) or src.get("thread_spawn")
    parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    if isinstance(src, dict) and ("subagent" in src or "thread_spawn" in src) or ts in _DELEGATED_THREADS:
        return "agent", "codex.delegated:%s" % (ts or "source.subagent"), parent
    if ts == "user":
        return "human", "codex.thread_source.user", None
    if ts is None and src == "vscode":
        return "unknown", "codex.legacy_vscode_unknown", None
    if meta.get("originator") == "codex_exec" or src == "exec":
        return "unknown", "codex.exec_unknown", None
    return "unknown", "codex.unknown_source", None


def _parse_codex(run):
    fname = _UUID_ANY.findall(run.path.name)
    session = fname[-1] if fname else run.path.stem
    project = None
    actor0, signal0, parent = "unknown", "codex.no_session_meta", None
    have_meta = False
    pending = Counter()
    pending_info = {}
    emitted = Counter()
    for lineno, rec in _rows(run):
        rtype = rec.get("type")
        p = rec.get("payload")
        ts = _iso(rec.get("timestamp"))
        if not isinstance(p, dict):
            run.ignore("type:%s" % rtype)
            continue

        def common():
            return dict(session=session, project=project, time=ts, parent=parent)

        if rtype == "session_meta":
            if have_meta:
                run.ignore("session_meta_repeat")
                continue
            have_meta = True
            session = _s(p.get("id")) or _s(p.get("session_id")) or session
            project = p.get("cwd") if isinstance(p.get("cwd"), str) else project
            actor0, signal0, parent = _codex_class(p)
            src = p.get("source")
            info = {"thread_source": p.get("thread_source"),
                    "source": "dict:" + ",".join(sorted(src)) if isinstance(src, dict) else src,
                    "originator": p.get("originator"), "user_text_actor": actor0, "signal": signal0}
            yield from run.ev(native="session_meta:%s" % session, role="meta", actor="unknown", text="",
                              signal=signal0, kinds=["session_meta"],
                              action=_action("session_meta", "codex", None, None, _dumps(info)),
                              **dict(common(), time=_iso(p.get("timestamp")) or ts))
            continue
        if rtype == "turn_context":
            if project is None and isinstance(p.get("cwd"), str):
                project = p["cwd"]
            run.ignore("turn_context")
            continue
        if rtype == "event_msg":
            item = p.get("item")
            if p.get("type") == "item_completed" and isinstance(item, dict) and item.get("type") == "UserMessage":
                text = "\n".join(_s(b.get("text")) for b in _blocks(item.get("content")))
                h = _norm_hash(text)
                if emitted[h] > 0:
                    emitted[h] -= 1
                    run.duplicates += 1
                else:
                    pending[h] += 1
                    pending_info.setdefault(h, []).append((text, ts))
            else:
                run.ignore("event_msg:%s" % p.get("type"))
            continue
        if rtype != "response_item":
            run.ignore("type:%s" % rtype)
            continue
        pt = p.get("type")
        role = p.get("role")
        mid = _s(p.get("id")) or None
        if pt == "message" and role == "user":
            md = p.get("internal_chat_message_metadata_passthrough")
            kinds = md.get("content_item_kinds") if isinstance(md, dict) else None
            kinds = [str(k) for k in kinds] if isinstance(kinds, list) else []
            items = _blocks(p.get("content"))
            aligned = len(kinds) == len(items)
            human, injected, other_kinds = [], [], []
            for i, b in enumerate(items):
                if b.get("type") not in ("input_text", "text"):
                    other_kinds.append(_s(b.get("type")) or "unknown")
                    continue
                txt = _s(b.get("text"))
                k = kinds[i] if aligned else None
                if k == "user.text" or (k is None and "user.text" in kinds and not _INJECTED.match(txt)):
                    human.append(txt)
                elif k is None and not kinds and not _INJECTED.match(txt):
                    human.append(txt)
                else:
                    injected.append(txt)
            misaligned = not aligned and "user.text" in kinds
            anchored = aligned and "user.text" in kinds
            if injected:
                yield from run.ev(native="%s:injected" % mid if mid else None, role="meta", actor="injected",
                                  text="\n".join(injected), blank=True, signal="codex.injected",
                                  kinds=[k for k in kinds if k != "user.text"] or ["injected"], **common())
            if human or (other_kinds and not injected):
                text = "\n".join(human)
                if anchored:
                    actor, signal = actor0, signal0 + ("+user.text" if actor0 == "human" else "")
                else:
                    actor = "agent" if actor0 == "agent" else "unknown"
                    signal = signal0 if actor0 == "agent" else (
                        "codex.kinds_misaligned" if misaligned else "codex.no_user_text_kind")
                h = _norm_hash(text)
                if pending[h] > 0:
                    pending[h] -= 1
                    pending_info[h].pop(0)
                    run.duplicates += 1
                else:
                    emitted[h] += 1
                yield from run.ev(native=mid, role="user", actor=actor, text=text, signal=signal,
                                  kinds=(["user.text"] if human else []) + other_kinds, **common())
        elif pt == "message" and role == "assistant":
            text = "\n".join(_s(b.get("text")) for b in _blocks(p.get("content")) if b.get("type") == "output_text")
            if text.strip():
                yield from run.ev(native=mid, role="assistant", actor="agent", text=text, signal="codex.assistant",
                                  kinds=["output_text"], **common())
        elif pt == "agent_message":
            text = _flat_text(p.get("content"))
            if text.strip():
                yield from run.ev(native=mid, role="assistant", actor="agent", text=text,
                                  signal="codex.agent_message", kinds=["agent_message"], **common())
        elif pt in ("custom_tool_call", "function_call"):
            cid = _s(p.get("call_id")) or None
            name = (_s(p.get("namespace")) + "." if p.get("namespace") else "") + _s(p.get("name"))
            run.remember_call(cid, name)
            raw_in = p.get("input") if pt == "custom_tool_call" else p.get("arguments")
            summary = _clip(raw_in if isinstance(raw_in, str) else _dumps(raw_in))
            yield from run.ev(native="call:%s" % cid if cid else None, role="tool", actor="agent", text="",
                              signal="codex.tool_call", kinds=["tool_call"],
                              action=_action("tool_call", name, cid, None, summary), **common())
        elif pt in ("custom_tool_call_output", "function_call_output"):
            cid = _s(p.get("call_id")) or None
            out = p.get("output")
            body = out if isinstance(out, str) else _flat_text(out)
            success = _success_from_text(body)
            if success is None and _s(p.get("status")) in ("failed", "error"):
                success = False
            yield from run.ev(native="result:%s" % cid if cid else None, role="tool", actor="agent", text="",
                              signal="codex.tool_result", kinds=["tool_result"],
                              action=_action("tool_result", run.calls.get(cid, ""), cid, success, _clip(body)),
                              **common())
        else:
            # reasoning, developer/system messages, configuration and tool listings are never text
            run.ignore("response_item:%s%s" % (pt, "/" + role if role else ""))
    for h, count in pending.items():
        for k in range(count):
            text, ts = pending_info[h][k]
            if not text.strip():
                continue
            # An unanchored event_msg has no response_item position: keep it as blank metadata (never a user turn)
            # so a late EOF emit cannot reorder a fake human turn behind later assistant turns.
            run.unanchored += 1
            yield from run.ev(native="%s:evmsg:%s:%d" % (session, h[:12], k), role="meta", actor="unknown",
                              text=text, blank=True, signal="codex.event_msg_only_unanchored",
                              kinds=["unanchored_user_event"], session=session, project=project, time=ts,
                              parent=parent)


# ---------------------------------------------------------------- kimi

def _read_json_file(run, path, cap=2 * 1024 * 1024):
    try:
        if path.stat().st_size > cap:
            run.error("state_json_too_large")
            return None
        with open(str(path), "rb") as fh:
            obj = json.loads(fh.read().decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else None
    except OSError:
        return None
    except (ValueError, RecursionError):
        run.error("state_json_malformed")
        return None


def _parse_kimi(run):
    parts = run.path.parents
    agent = run.path.parent.name
    sess_dir = parts[2].name if len(parts) > 2 else run.path.stem
    state = _read_json_file(run, parts[2] / "state.json") if len(parts) > 2 else None
    agents = state.get("agents") if isinstance(state, dict) and isinstance(state.get("agents"), dict) else {}
    info = agents.get(agent) if isinstance(agents.get(agent), dict) else {}
    sub = agent != "main" or info.get("type") == "sub"
    session = "%s/%s" % (sess_dir, agent) if sub else sess_dir
    parent = sess_dir if sub else None
    project = state.get("cwd") if isinstance(state, dict) and isinstance(state.get("cwd"), str) else None
    for lineno, rec in _rows(run):
        rtype = rec.get("type")
        ts = _iso(rec.get("time"))
        common = dict(session=session, project=project, time=ts, parent=parent)
        if rtype == "turn.prompt":
            origin = rec.get("origin")
            okind = origin.get("kind") if isinstance(origin, dict) else None
            items = _blocks(rec.get("input"))
            text = "\n".join(_s(b.get("text")) for b in items if b.get("type") == "text")
            kinds = sorted({_s(b.get("type")) or "unknown" for b in items}) or ["text"]
            if sub:
                actor, signal = "agent", "kimi.subagent"
            elif okind == "user":
                actor, signal = "human", "kimi.prompt.origin.user"
            elif okind in _AGENT_ORIGINS:
                actor, signal = "agent", "kimi.%s" % okind
            else:
                actor, signal = "unknown", "kimi.origin:%s" % okind
            pid = _s(rec.get("promptId")) or None
            yield from run.ev(native="prompt:%s" % pid if pid else None, role="user", actor=actor, text=text,
                              signal=signal, kinds=kinds, **common)
        elif rtype == "context.append_message":
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            if msg.get("role") != "user":
                run.ignore("append_message:%s" % msg.get("role"))
                continue
            origin = msg.get("origin")
            okind = origin.get("kind") if isinstance(origin, dict) else None
            if okind in ("user", "system_trigger"):
                run.duplicates += 1  # copy of turn.prompt, not a second turn
                continue
            yield from run.ev(native=None, role="meta", actor="injected", text=_flat_text(msg.get("content")),
                              blank=True, signal="kimi.%s" % okind, kinds=["injected"], **common)
        elif rtype == "context.append_loop_event":
            ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
            et = ev.get("type")
            if et == "content.part":
                part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                if part.get("type") == "text" and _s(part.get("text")).strip():
                    yield from run.ev(native=None, role="assistant", actor="agent", text=part["text"],
                                      signal="kimi.assistant", kinds=["text"], **common)
                else:
                    run.ignore("content.part:%s" % part.get("type"))
            elif et == "tool.call":
                cid = _s(ev.get("toolCallId")) or None
                name = _s(ev.get("name"))
                run.remember_call(cid, name)
                yield from run.ev(native="call:%s" % cid if cid else None, role="tool", actor="agent", text="",
                                  signal="kimi.tool_call", kinds=["tool_call"],
                                  action=_action("tool_call", name, cid, None, _clip(_dumps(ev.get("args")))),
                                  **common)
            elif et == "tool.result":
                cid = _s(ev.get("toolCallId")) or None
                result = ev.get("result")
                body = _dumps(result) if not isinstance(result, dict) else _s(result.get("output")) or _dumps(result)
                yield from run.ev(native="result:%s" % cid if cid else None, role="tool", actor="agent", text="",
                                  signal="kimi.tool_result", kinds=["tool_result"],
                                  action=_action("tool_result", run.calls.get(cid, ""), cid, _success_from(result),
                                                 _clip(body)), **common)
            else:
                run.ignore("loop:%s" % et)
        else:
            run.ignore("type:%s" % rtype)


# ---------------------------------------------------------------- grok

def _parse_grok(run):
    uri = Path(os.path.abspath(str(run.path))).as_uri() + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        run.error("sqlite_open_failed:%s" % type(exc).__name__)
        return
    try:
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" in tables:
            for k, v in conn.execute("SELECT key, value FROM meta LIMIT 20"):
                if k in ("schema_version", "version"):
                    run.ignore("meta:%s=%s" % (k, v))
        if "session_docs" not in tables:
            run.error("unsupported_schema:no_session_docs")
            return
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_docs)")}
        want = ["session_id", "cwd", "updated_at", "title", "content", "content_hash"]
        missing = [c for c in want if c not in cols]
        if missing:
            run.error("unsupported_schema:missing_%s" % ",".join(missing))
        sel = ", ".join(c if c in cols else "NULL" for c in want)
        for n, (sid, cwd, upd, title, content, chash) in enumerate(conn.execute("SELECT %s FROM session_docs" % sel), 1):
            run.lineno = n
            if content:
                run.error("unsupported_content_format")
            native = "doc:%s:%s" % (sid, chash or n)
            summary = _dumps({"title_chars": len(title) if isinstance(title, str) else 0,
                              "content_hash": chash, "has_content": bool(content)})
            yield from run.ev(native=native, role="meta", actor="unknown", text=_s(content), blank=True,
                              session=_s(sid) or None, project=cwd if isinstance(cwd, str) else None,
                              time=_iso(upd), signal="grok.derived_document", kinds=["derived_document"],
                              action=_action("derived_document", "session_docs", None, None, summary))
    except sqlite3.Error as exc:
        run.error("sqlite_read_failed:%s" % type(exc).__name__)
    finally:
        conn.close()


# ---------------------------------------------------------------- memory

def _parse_memory(run):
    head, total = [], 0
    run.lineno = 0
    try:
        with open(str(run.path), "rb") as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                if run.limit is not None and run.bytes + len(chunk) > run.limit:
                    chunk = chunk[:max(run.limit - run.bytes, 0)]
                    run.truncated_tail = True
                run.bytes += len(chunk)
                text = chunk.decode("utf-8", "replace")
                total += len(text)
                if sum(len(h) for h in head) < PRESCRUB_CAP:
                    head.append(text)
                if run.truncated_tail:
                    break
    except OSError as exc:
        run.error("read_failed:%s" % type(exc).__name__)
        return
    body = "".join(head)
    hdr = {}
    for line in body.splitlines()[:25]:
        m = re.match(r"^\s*(thread_id|updated_at|cwd|rollout_path)\s*:\s*(.+?)\s*$", line)
        if m:
            hdr[m.group(1)] = m.group(2)
    run.lineno = 1
    out = run.ev(native=None, role="meta", actor="injected", text=body[:PRESCRUB_CAP], signal="memory.derivative",
                 kinds=["derived_memory"],
                 session=hdr.get("thread_id") or "document:" + _sha(os.path.abspath(str(run.path))),
                 project=hdr.get("cwd"),
                 time=_iso(hdr.get("updated_at")))
    for e in out:
        e["text_chars"] = total
        e["text_truncated"] = total > MAX_TEXT
    yield from out


_PARSERS = {"claude": _parse_claude, "codex": _parse_codex, "kimi": _parse_kimi, "grok": _parse_grok,
            "memory": _parse_memory}


def iter_events(path, source, machine="local", limit_bytes=None):
    """Stream event dicts for one source file, ending with a source_summary diagnostics event."""
    if source not in SOURCES:
        raise TraceError("unknown source %r (expected one of %s)" % (source, ", ".join(SOURCES)))
    p = Path(path)
    if not p.is_file():
        raise TraceError("not a file: %s" % p.name)
    if limit_bytes is not None and (isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int) or limit_bytes < 0):
        raise TraceError("limit_bytes must be a non-negative integer")
    return _stream(_Run(p, source, str(machine), limit_bytes))


def _stream(run):
    try:
        for e in _PARSERS[run.source](run):
            yield e
    except Exception as exc:  # a parser bug or odd row must not hide the rows already read
        run.error("parser_failed:%s" % type(exc).__name__)
    yield run.summary_event()
