#!/usr/bin/env python3
"""Validate the skills repository. Standard library only (Python 3.8+).

Checks, per skill directory under skills/:
  - SKILL.md exists (exact case) and starts with YAML frontmatter
  - frontmatter `name` matches the directory name and is a lowercase kebab-case
    identifier of at most 64 characters
  - frontmatter `description` is present, at most 1024 characters (warning above
    600)
  - every relative path referenced from SKILL.md and references/**/*.md exists:
    markdown links and inline-code paths are errors; paths mentioned in prose or
    inside fenced code blocks are warnings (they are often illustrative)
  - SKILL.md is under 500 lines (warning)

Repo-wide, over every tracked or to-be-tracked file (git ls-files -co
--exclude-standard; falls back to walking the tree when git is unavailable):
  - no secret-shaped strings (Anthropic, OpenAI, GitHub, AWS, Slack, Google key
    formats; private key headers)
  - no email addresses outside the allowlist
  - no absolute home paths (/Users/<name>/, /home/<name>/) except placeholders
  - no name listed in the optional, gitignored .validate-denylist
  - every file with a shebang is executable (and executables have a shebang)
  - .claude-plugin/plugin.json and .claude-plugin/marketplace.json parse, agree
    on the plugin name and version, and point at paths that exist

Exit status: 0 when there are no errors (warnings are allowed unless --strict),
1 when there are errors, 2 for a usage or setup problem.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

MAX_DESCRIPTION = 1024
WARN_DESCRIPTION = 600
WARN_SKILL_LINES = 500
MAX_NAME_LENGTH = 64
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

KNOWN_FRONTMATTER_KEYS = {
    "name", "description", "license", "compatibility", "metadata",
    "allowed-tools", "model", "disable-model-invocation", "user-invocable",
    "context", "agent", "hooks", "paths", "argument-hint", "effort", "version",
}

PLUGIN_MANIFEST = os.path.join(".claude-plugin", "plugin.json")
MARKETPLACE_MANIFEST = os.path.join(".claude-plugin", "marketplace.json")
DENYLIST_FILE = ".validate-denylist"
DENYLIST_EXAMPLE = ".validate-denylist.example"

# Full key shapes, not bare prefixes, so documentation of a pattern (for example a
# scrubber's own regex source) does not trip the scan. Each entry: (label, regex).
SECRET_PATTERNS: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    ("Anthropic API key", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style secret key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?!ant-)(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9_])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}")),
    ("GitHub fine-grained token", re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{22,}")),
    ("AWS access key id", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9_-])xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])")),
    ("private key header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![A-Za-z0-9-])")
# Entries: an exact address, an "@domain" suffix, or a ".tld" suffix. The TLDs
# below are reserved for documentation and testing (RFC 2606 / RFC 6761) and can
# never belong to a real person.
DEFAULT_EMAIL_ALLOWLIST = (
    "noreply@anthropic.com",
    "noreply@github.com",
    "@example.com", "@example.org", "@example.net",
    "@users.noreply.github.com",
    ".example", ".invalid", ".test", ".localhost",
)
FORMAT_PLACEHOLDER_LOCAL_RE = re.compile(r"^%[a-z]$")

HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/([^/\s'\"`]+)/")
GENERIC_HOME_NAMES = {
    "you", "user", "username", "yourname", "your-name", "your_name", "name",
    "me", "jdoe", "alice", "bob", "example", "someone", "shared", "runner",
    "ubuntu", "ec2-user", "root", "x", "xxx",
}

REF_PREFIX_RE = re.compile(r"^(?:\.\.?/|(?:references|scripts|tests|assets|templates|fixtures)/)")
PROSE_PATH_RE = re.compile(r"(?<![\w/.`\-])((?:\.\.?/|(?:references|scripts|tests|assets|templates|fixtures)/)[\w./\-]*)")
LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
PLACEHOLDER_CHARS = set("<>$*{}?[]|~")

LEVEL_ORDER = {"ERROR": 0, "WARN": 1}


@dataclass
class Finding:
    level: str
    code: str
    path: str
    message: str
    line: int = 0

    def format(self) -> str:
        where = self.path or "."
        if self.line:
            where = "%s:%d" % (where, self.line)
        return "%-5s %-24s %s: %s" % (self.level, self.code, where, self.message)

    def as_dict(self) -> Dict[str, object]:
        return {"level": self.level, "code": self.code, "path": self.path,
                "line": self.line, "message": self.message}


def error(code: str, path: str, message: str, line: int = 0) -> Finding:
    return Finding("ERROR", code, path, message, line)


def warn(code: str, path: str, message: str, line: int = 0) -> Finding:
    return Finding("WARN", code, path, message, line)


# --------------------------------------------------------------------------- YAML

class FrontmatterError(Exception):
    pass


YamlValue = Union[str, List[str], Dict[str, object]]


def split_frontmatter(text: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return (frontmatter lines, None) or (None, reason)."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None, "missing"
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return lines[1:i], None
    return None, "unterminated"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        body = value[1:-1]
        if value[0] == '"':
            body = body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        else:
            body = body.replace("''", "'")
        return body
    # strip a trailing comment: "value # comment"
    return re.sub(r"\s+#.*$", "", value)


def _fold_block(block: List[str], style: str) -> str:
    nonblank = [l for l in block if l.strip()]
    if not nonblank:
        return ""
    indent = min(_indent(l) for l in nonblank)
    body = [l[indent:] if l.strip() else "" for l in block]
    if style == "|":
        return "\n".join(body).rstrip("\n")
    paragraphs: List[str] = []
    current: List[str] = []
    for l in body:
        if l == "":
            if current:
                paragraphs.append(" ".join(current))
                current = []
            else:
                paragraphs.append("")
        else:
            current.append(l.strip())
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs).strip("\n")


_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.\-]+)\s*:(?:\s+(.*))?$")


def parse_yaml_mapping(lines: List[str], indent: int = 0, offset: int = 0) -> Dict[str, YamlValue]:
    """A deliberately small YAML subset: mappings, block scalars (> and |),
    quoted and plain scalars (including multi-line plain scalars), and simple
    `- item` lists. Enough for skill frontmatter; anything else raises."""
    result: Dict[str, YamlValue] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = _KEY_RE.match(line.rstrip())
        if not m or len(m.group(1)) != indent:
            raise FrontmatterError("frontmatter line %d: expected `key: value` at indent %d, got %r"
                                   % (offset + i + 1, indent, line.strip()))
        key = m.group(2)
        raw = (m.group(3) or "").strip()
        j = i + 1
        block: List[str] = []
        while j < n:
            nxt = lines[j]
            if not nxt.strip() or _indent(nxt) > indent:
                block.append(nxt)
                j += 1
                continue
            break
        while block and not block[-1].strip():
            block.pop()
        if raw[:1] in (">", "|") and re.match(r"^[>|][+-]?\d*$", raw):
            result[key] = _fold_block(block, raw[0])
        elif raw == "":
            if block:
                first = block[0]
                if first.lstrip().startswith("- "):
                    result[key] = [_unquote(it.strip()[2:]) for it in block if it.strip()]
                else:
                    result[key] = parse_yaml_mapping(block, _indent(first), offset + i + 1)
            else:
                result[key] = ""
        else:
            value = raw
            if block:
                for k, cont in enumerate(block):
                    if cont.strip() and _KEY_RE.match(cont.rstrip()) and not raw.startswith(("\"", "'")):
                        raise FrontmatterError("frontmatter line %d: %r is indented under `%s: %s` but looks like a key (bad indentation?)"
                                               % (offset + i + 1 + k + 1, cont.strip(), key, raw))
                value = " ".join([raw] + [l.strip() for l in block if l.strip()])
            result[key] = _unquote(value)
        i = j
    return result


def parse_frontmatter(text: str) -> Dict[str, YamlValue]:
    lines, reason = split_frontmatter(text)
    if lines is None:
        raise FrontmatterError(reason or "missing")
    return parse_yaml_mapping(lines)


# --------------------------------------------------------------------- file list

def list_repo_files(root: str, use_git: bool = True) -> Tuple[List[str], str]:
    """Relative POSIX paths of tracked or to-be-tracked files. Returns
    (paths, source) where source is "git" or "walk"."""
    if use_git:
        try:
            out = subprocess.run(
                ["git", "-C", root, "ls-files", "-z", "-co", "--exclude-standard"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
            ).stdout
            paths = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
            paths = [p for p in paths if os.path.isfile(os.path.join(root, p))]
            return sorted(set(paths)), "git"
        except (OSError, subprocess.CalledProcessError):
            pass
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "__pycache__", "node_modules"))
        for fn in filenames:
            if fn == DENYLIST_FILE or fn.endswith(".pyc") or fn == ".DS_Store":
                continue
            full = os.path.join(dirpath, fn)
            paths.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(paths), "walk"


def read_text(path: str) -> Optional[str]:
    """Text of a file, or None when it looks binary."""
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


# ------------------------------------------------------------------- references

def _iter_markdown_lines(text: str) -> Iterable[Tuple[int, str, bool]]:
    """Yield (line number, line, in_fence)."""
    fence: Optional[str] = None
    for idx, line in enumerate(text.split("\n"), start=1):
        stripped = line.lstrip()
        if fence is None:
            m = re.match(r"^(`{3,}|~{3,})", stripped)
            if m:
                fence = m.group(1)[0] * len(m.group(1))
                yield idx, line, True
                continue
            yield idx, line, False
        else:
            if stripped.startswith(fence):
                fence = None
            yield idx, line, True


def _clean_token(token: str) -> Optional[str]:
    token = token.strip(" \t'\"()[]{}").rstrip(".,;:")
    if ":" in token:
        token = token.split(":", 1)[0]
    if not token or "//" in token:
        return None
    if any(ch in PLACEHOLDER_CHARS for ch in token):
        return None
    if not REF_PREFIX_RE.match(token):
        return None
    return token


def _link_target(target: str) -> Optional[str]:
    if "://" in target or target.startswith(("#", "mailto:", "/", "~")):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target or any(ch in PLACEHOLDER_CHARS for ch in target):
        return None
    if target.startswith("<") and target.endswith(">"):
        return None
    return target


def _resolve(skill_dir: str, md_path: str, token: str) -> bool:
    want_dir = token.endswith("/")
    candidates = [os.path.join(os.path.dirname(md_path), token), os.path.join(skill_dir, token)]
    for cand in candidates:
        if want_dir and os.path.isdir(cand):
            return True
        if not want_dir and os.path.exists(cand):
            return True
    return False


def check_references(root: str, skill_dir: str, md_path: str, text: str) -> List[Finding]:
    rel = os.path.relpath(md_path, root).replace(os.sep, "/")
    findings: List[Finding] = []
    seen = set()

    def report(token: str, line: int, level: str, what: str) -> None:
        key = (token, level)
        if key in seen:
            return
        seen.add(key)
        msg = "%s does not exist: %s" % (what, token)
        findings.append(error("missing-path", rel, msg, line) if level == "ERROR"
                        else warn("missing-path", rel, msg, line))

    for lineno, line, in_fence in _iter_markdown_lines(text):
        if in_fence:
            for m in PROSE_PATH_RE.finditer(line):
                token = _clean_token(m.group(1))
                if token and not _resolve(skill_dir, md_path, token):
                    report(token, lineno, "WARN", "path in fenced code block")
            continue
        for m in LINK_RE.finditer(line):
            target = _link_target(m.group(1))
            if target and not _resolve(skill_dir, md_path, target):
                report(target, lineno, "ERROR", "linked path")
        rest = []
        last = 0
        for m in INLINE_CODE_RE.finditer(line):
            rest.append(line[last:m.start()])
            last = m.end()
            for chunk in re.split(r"[\s\"'`()\[\]{}|;&=,]+", m.group(1)):
                token = _clean_token(chunk)
                if token and not _resolve(skill_dir, md_path, token):
                    report(token, lineno, "ERROR", "inline-code path")
        rest.append(line[last:])
        prose = LINK_RE.sub(" ", "".join(rest))
        for m in PROSE_PATH_RE.finditer(prose):
            token = _clean_token(m.group(1))
            if token and not _resolve(skill_dir, md_path, token):
                report(token, lineno, "WARN", "path in prose")
    return findings


# ------------------------------------------------------------------------ skills

def check_skill(root: str, skill_dir: str) -> List[Finding]:
    findings: List[Finding] = []
    name = os.path.basename(skill_dir)
    rel_dir = os.path.relpath(skill_dir, root).replace(os.sep, "/")
    skill_md = os.path.join(skill_dir, "SKILL.md")
    rel_md = rel_dir + "/SKILL.md"

    if not os.path.isfile(skill_md):
        lookalikes = [e for e in os.listdir(skill_dir) if e.lower() == "skill.md"]
        if lookalikes:
            findings.append(error("skill-md-case", rel_dir, "found %s; the file must be named SKILL.md" % lookalikes[0]))
        else:
            findings.append(error("skill-md-missing", rel_dir, "no SKILL.md"))
        return findings

    text = read_text(skill_md)
    if text is None:
        findings.append(error("skill-md-binary", rel_md, "SKILL.md is not a text file"))
        return findings

    line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if line_count > WARN_SKILL_LINES:
        findings.append(warn("skill-too-long", rel_md, "%d lines (keep SKILL.md under %d; move detail to references/)"
                             % (line_count, WARN_SKILL_LINES)))

    try:
        fm = parse_frontmatter(text)
    except FrontmatterError as exc:
        reason = str(exc)
        if reason == "missing":
            findings.append(error("frontmatter-missing", rel_md, "file must start with a `---` YAML frontmatter block"))
        elif reason == "unterminated":
            findings.append(error("frontmatter-unterminated", rel_md, "frontmatter has no closing `---`"))
        else:
            findings.append(error("frontmatter-invalid", rel_md, reason))
        fm = {}

    if fm:
        fm_name = fm.get("name")
        if not isinstance(fm_name, str) or not fm_name.strip():
            findings.append(error("name-missing", rel_md, "frontmatter has no `name`"))
        else:
            fm_name = fm_name.strip()
            if fm_name != name:
                findings.append(error("name-mismatch", rel_md, "frontmatter name %r does not match directory %r" % (fm_name, name)))
            if not NAME_RE.match(fm_name) or len(fm_name) > MAX_NAME_LENGTH:
                findings.append(error("name-format", rel_md, "name %r must be lowercase letters, digits and single hyphens, at most %d characters"
                                      % (fm_name, MAX_NAME_LENGTH)))
        desc = fm.get("description")
        if not isinstance(desc, str) or not desc.strip():
            findings.append(error("description-missing", rel_md, "frontmatter has no non-empty `description`"))
        else:
            length = len(desc.strip())
            if length > MAX_DESCRIPTION:
                findings.append(error("description-too-long", rel_md, "description is %d characters (max %d)" % (length, MAX_DESCRIPTION)))
            elif length > WARN_DESCRIPTION:
                findings.append(warn("description-long", rel_md, "description is %d characters (warn above %d, max %d)"
                                     % (length, WARN_DESCRIPTION, MAX_DESCRIPTION)))
        for key in fm:
            if key not in KNOWN_FRONTMATTER_KEYS:
                findings.append(warn("unknown-key", rel_md, "unknown frontmatter key %r" % key))

    findings.extend(check_references(root, skill_dir, skill_md, text))
    refs_dir = os.path.join(skill_dir, "references")
    if os.path.isdir(refs_dir):
        for dirpath, dirnames, filenames in os.walk(refs_dir):
            dirnames.sort()
            for fn in sorted(filenames):
                if fn.lower().endswith(".md"):
                    path = os.path.join(dirpath, fn)
                    ref_text = read_text(path)
                    if ref_text is not None:
                        findings.extend(check_references(root, skill_dir, path, ref_text))
    return findings


def iter_skill_dirs(root: str) -> List[str]:
    skills_dir = os.path.join(root, "skills")
    if not os.path.isdir(skills_dir):
        return []
    out = []
    for entry in sorted(os.listdir(skills_dir)):
        full = os.path.join(skills_dir, entry)
        if entry.startswith(".") or not os.path.isdir(full):
            continue
        out.append(full)
    return out


# ------------------------------------------------------------------ content scan

def load_denylist(path: Optional[str]) -> List[str]:
    if not path or not os.path.isfile(path):
        return []
    names = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    return names


def _mask(value: str, keep: int = 1) -> str:
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def email_allowed(email: str, allowlist: Sequence[str]) -> bool:
    lower = email.lower()
    for entry in allowlist:
        entry = entry.lower().strip()
        if not entry:
            continue
        if entry.startswith("@") and lower.endswith(entry):
            return True
        if entry.startswith("*@") and lower.endswith(entry[1:]):
            return True
        if entry.startswith(".") and lower.rsplit("@", 1)[-1].endswith(entry):
            return True
        if lower == entry:
            return True
    return False


def scan_content(root: str, files: Sequence[str], denylist: Sequence[str],
                 email_allowlist: Sequence[str], home_allowlist: Sequence[str] = ()) -> List[Finding]:
    """One finding per (code, file, message); repeats within a file are counted,
    so a fixture that mentions the same path on 200 lines yields one line."""
    findings: List[Finding] = []
    deny_res = [(name, re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.IGNORECASE))
                for name in denylist]
    home_ok = set(GENERIC_HOME_NAMES) | {n.lower() for n in home_allowlist}
    for rel in files:
        full = os.path.join(root, rel)
        try:
            text = read_text(full)
        except OSError as exc:
            findings.append(error("unreadable", rel, "cannot read: %s" % exc))
            continue
        if text is None:
            continue
        hits: Dict[Tuple[str, str], List[int]] = {}

        def hit(code: str, message: str, lineno: int) -> None:
            hits.setdefault((code, message), []).append(lineno)

        for lineno, line in enumerate(text.split("\n"), start=1):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hit("secret", "%s-shaped string (value not shown)" % label, lineno)
            for m in EMAIL_RE.finditer(line):
                email = m.group(0)
                if FORMAT_PLACEHOLDER_LOCAL_RE.match(m.group(1)):
                    continue  # "%s@host.test" is a format template, not an address
                if not email_allowed(email, email_allowlist):
                    hit("email", "email address %s@%s (--allow-email to permit)" % (_mask(m.group(1)), m.group(2)), lineno)
            for m in HOME_PATH_RE.finditer(line):
                user = m.group(1)
                if user.lower() in home_ok or user[:1] in "<$%{":
                    continue
                hit("home-path", "absolute home path %s (use ~ or a placeholder, or --allow-home for a synthetic persona)"
                    % m.group(0).replace(user, _mask(user)), lineno)
            if rel != DENYLIST_EXAMPLE:
                for name, pattern in deny_res:
                    if pattern.search(line):
                        hit("denylist", "denylisted name %s appears here" % _mask(name), lineno)
        for (code, message), lines in hits.items():
            if len(lines) > 1:
                message = "%s [%d occurrences in this file, first shown]" % (message, len(lines))
            findings.append(error(code, rel, message, lines[0]))
    return findings


def check_exec_bits(root: str, files: Sequence[str]) -> List[Finding]:
    findings: List[Finding] = []
    for rel in files:
        full = os.path.join(root, rel)
        try:
            with open(full, "rb") as fh:
                head = fh.read(2)
            mode = os.stat(full).st_mode
        except OSError:
            continue
        executable = bool(mode & 0o111)
        has_shebang = head == b"#!"
        if has_shebang and not executable:
            findings.append(error("not-executable", rel, "has a shebang but is not executable (chmod +x)"))
        elif executable and not has_shebang and not rel.endswith((".py", ".sh")):
            findings.append(warn("exec-no-shebang", rel, "is executable but has no shebang"))
    return findings


# -------------------------------------------------------------------- manifests

def _load_json(root: str, rel: str) -> Tuple[Optional[Dict[str, object]], List[Finding]]:
    full = os.path.join(root, rel)
    if not os.path.isfile(full):
        return None, [error("manifest-missing", rel, "file not found")]
    try:
        with open(full, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, [error("manifest-parse", rel, "invalid JSON: %s" % exc)]
    if not isinstance(data, dict):
        return None, [error("manifest-parse", rel, "top level must be a JSON object")]
    return data, []


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def check_manifests(root: str) -> List[Finding]:
    plugin, findings = _load_json(root, PLUGIN_MANIFEST)
    market, more = _load_json(root, MARKETPLACE_MANIFEST)
    findings.extend(more)
    if plugin is None or market is None:
        return findings

    pname = plugin.get("name")
    pversion = plugin.get("version")
    if not isinstance(pname, str) or not pname:
        findings.append(error("manifest-field", PLUGIN_MANIFEST, "missing `name`"))
    if not isinstance(pversion, str) or not VERSION_RE.match(pversion):
        findings.append(error("manifest-field", PLUGIN_MANIFEST, "`version` must be a semantic version string, got %r" % (pversion,)))
    if not isinstance(plugin.get("description"), str) or not plugin.get("description"):
        findings.append(warn("manifest-field", PLUGIN_MANIFEST, "missing `description`"))
    skills_path = plugin.get("skills")
    if isinstance(skills_path, str):
        if not os.path.isdir(os.path.join(root, skills_path)):
            findings.append(error("manifest-path", PLUGIN_MANIFEST, "`skills` points at %r which is not a directory" % skills_path))
    elif isinstance(skills_path, list):
        for entry in skills_path:
            if not isinstance(entry, str) or not os.path.exists(os.path.join(root, entry)):
                findings.append(error("manifest-path", PLUGIN_MANIFEST, "`skills` entry %r does not exist" % (entry,)))

    plugins = market.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        findings.append(error("manifest-field", MARKETPLACE_MANIFEST, "`plugins` must be a non-empty list"))
        return findings
    if not isinstance(market.get("name"), str) or not market.get("name"):
        findings.append(error("manifest-field", MARKETPLACE_MANIFEST, "missing `name`"))

    matches = [p for p in plugins if isinstance(p, dict) and p.get("name") == pname]
    if isinstance(pname, str) and not matches:
        findings.append(error("manifest-plugin-missing", MARKETPLACE_MANIFEST, "no plugins[] entry named %r" % pname))
    for entry in matches:
        mversion = entry.get("version")
        if mversion != pversion:
            findings.append(error("manifest-version-mismatch", MARKETPLACE_MANIFEST,
                                  "plugins[%r].version %r != plugin.json version %r" % (pname, mversion, pversion)))
        source = entry.get("source")
        if isinstance(source, str) and not source.startswith(("http://", "https://", "git@")):
            if not os.path.isdir(os.path.join(root, source)):
                findings.append(error("manifest-path", MARKETPLACE_MANIFEST, "plugins[%r].source %r is not a directory" % (pname, source)))
        elif not isinstance(source, (str, dict)):
            findings.append(error("manifest-field", MARKETPLACE_MANIFEST, "plugins[%r] has no `source`" % pname))
    metadata = market.get("metadata")
    if isinstance(metadata, dict) and "version" in metadata and metadata.get("version") != pversion:
        findings.append(error("manifest-version-mismatch", MARKETPLACE_MANIFEST,
                              "metadata.version %r != plugin.json version %r" % (metadata.get("version"), pversion)))
    return findings


# ----------------------------------------------------------------------- driver

@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    files_scanned: int = 0
    file_source: str = ""
    denylist_entries: int = 0
    denylist_path: str = ""

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "ERROR"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "WARN"]


def validate_repo(root: str, denylist_path: Optional[str] = None,
                  email_allowlist: Sequence[str] = (), use_git: bool = True,
                  home_allowlist: Sequence[str] = ()) -> Report:
    root = os.path.abspath(root)
    report = Report()
    if denylist_path is None:
        denylist_path = os.path.join(root, DENYLIST_FILE)
    report.denylist_path = denylist_path
    denylist = load_denylist(denylist_path)
    report.denylist_entries = len(denylist)
    allowlist = list(DEFAULT_EMAIL_ALLOWLIST) + list(email_allowlist)

    skill_dirs = iter_skill_dirs(root)
    if not os.path.isdir(os.path.join(root, "skills")):
        report.findings.append(error("skills-dir-missing", "skills", "no skills/ directory"))
    elif not skill_dirs:
        report.findings.append(error("skills-empty", "skills", "skills/ contains no skill directories"))
    for skill_dir in skill_dirs:
        report.skills.append(os.path.basename(skill_dir))
        report.findings.extend(check_skill(root, skill_dir))

    files, source = list_repo_files(root, use_git=use_git)
    report.files_scanned = len(files)
    report.file_source = source
    report.findings.extend(scan_content(root, files, denylist, allowlist, home_allowlist))
    report.findings.extend(check_exec_bits(root, files))
    report.findings.extend(check_manifests(root))

    report.findings.sort(key=lambda f: (LEVEL_ORDER.get(f.level, 9), f.path, f.line, f.code, f.message))
    return report


def default_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the skills repository (stdlib only).")
    parser.add_argument("--root", default=default_root(), help="repository root (default: parent of scripts/)")
    parser.add_argument("--denylist", default=None, help="path to the denylist file (default: <root>/%s)" % DENYLIST_FILE)
    parser.add_argument("--allow-email", action="append", default=[], metavar="EMAIL",
                        help="allow this email address, or a domain suffix such as @example.com (repeatable)")
    parser.add_argument("--allow-home", action="append", default=[], metavar="NAME",
                        help="treat /Users/NAME/ and /home/NAME/ as a placeholder, for a synthetic persona in fixtures (repeatable)")
    parser.add_argument("--no-git", action="store_true", help="walk the tree instead of asking git which files are tracked or untracked-and-not-ignored")
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("validate: root %s is not a directory" % root, file=sys.stderr)
        return 2

    report = validate_repo(root, denylist_path=args.denylist, email_allowlist=args.allow_email,
                           use_git=not args.no_git, home_allowlist=args.allow_home)
    errors, warnings = report.errors, report.warnings
    failed = bool(errors) or (args.strict and bool(warnings))

    if args.json:
        print(json.dumps({
            "root": root, "skills": report.skills, "files_scanned": report.files_scanned,
            "file_source": report.file_source, "denylist_entries": report.denylist_entries,
            "errors": len(errors), "warnings": len(warnings), "ok": not failed,
            "findings": [f.as_dict() for f in report.findings],
        }, indent=2))
        return 1 if failed else 0

    print("validate: %s" % root)
    print("  skills: %d (%s)" % (len(report.skills), ", ".join(report.skills) or "none"))
    print("  files scanned: %d (%s)" % (report.files_scanned, "git ls-files -co --exclude-standard" if report.file_source == "git" else "directory walk"))
    if report.denylist_entries:
        print("  denylist: %d entries from %s" % (report.denylist_entries, os.path.relpath(report.denylist_path, root)))
    else:
        print("  denylist: none (%s not found; copy %s to enable)" % (DENYLIST_FILE, DENYLIST_EXAMPLE))
    if report.findings:
        print()
        for f in report.findings:
            print(f.format())
    print()
    status = "FAIL" if failed else "OK"
    print("%s: %d error%s, %d warning%s" % (status, len(errors), "" if len(errors) == 1 else "s",
                                           len(warnings), "" if len(warnings) == 1 else "s"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
