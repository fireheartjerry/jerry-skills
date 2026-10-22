"""A minimal JSON Schema validator and the built-in schemas.

Supported keywords: type (string or list, integer vs number, null), properties,
required, additionalProperties (boolean or schema), enum, const, items (one
schema), minItems, maxItems, minimum, maximum, minLength, maxLength, pattern,
anyOf, oneOf, allOf, not, and $ref to #/definitions/... or #/$defs/....
Anything else is ignored; `unknown_keywords()` lists what was ignored so the
caller can warn once. Errors are strings of the form `path: message`.

The validator exists because the engines can report success without conforming
output; the runner validates locally and asks for one fixup. It is not a full
draft-7 implementation and does not claim to be.
"""

import re

from agent_sessions.common import FAILURE_CLASSES

__all__ = [
    "validate", "unknown_keywords", "check_schema", "strict_schema_problems", "SUPPORTED_KEYWORDS",
    "RESULT_SCHEMA", "EVENT_SCHEMA", "JUDGE_SCHEMA", "JUDGE_RECORD_SCHEMA", "EVENT_KINDS", "SCHEMAS",
]

SUPPORTED_KEYWORDS = frozenset((
    "type", "properties", "required", "additionalProperties", "enum", "const", "items",
    "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "pattern",
    "anyOf", "oneOf", "allOf", "not", "$ref",
))
# Carry no validation meaning; never reported as unknown.
ANNOTATION_KEYWORDS = frozenset((
    "title", "description", "default", "examples", "$schema", "$id", "$comment",
    "deprecated", "readOnly", "writeOnly", "definitions", "$defs",
))
MAX_DEPTH = 64


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_type(value, name):
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    return False


def _resolve(ref, root):
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None, "unsupported $ref %r (only #/definitions/... and #/$defs/...)" % (ref,)
    parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")]
    if parts[0] not in ("definitions", "$defs"):
        return None, "unsupported $ref %r (only #/definitions/... and #/$defs/...)" % (ref,)
    node = root
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None, "unresolved $ref %r" % (ref,)
    if not isinstance(node, dict):
        return None, "$ref %r does not point at a schema object" % (ref,)
    return node, None


def _json(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _check(value, schema, root, path, errors, depth):
    if depth > MAX_DEPTH:
        errors.append("%s: schema nesting deeper than %d (recursive $ref?)" % (path, MAX_DEPTH))
        return
    if schema is True:
        return
    if schema is False:
        errors.append("%s: no value is allowed here" % path)
        return
    if not isinstance(schema, dict):
        errors.append("%s: schema is not an object" % path)
        return
    if "$ref" in schema:
        target, err = _resolve(schema["$ref"], root)
        if err:
            errors.append("%s: %s" % (path, err))
        else:
            _check(value, target, root, path, errors, depth + 1)
    if "type" in schema:
        types = schema["type"]
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            errors.append("%s: schema 'type' must be a string or a list of strings" % path)
        elif not any(_is_type(value, t) for t in types):
            errors.append("%s: expected %s, got %s" % (path, " or ".join(types), _type_name(value)))
    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list) or not any(_equal(value, a) for a in allowed):
            errors.append("%s: %s is not one of %s" % (path, _json(value), _json(allowed)))
    if "const" in schema and not _equal(value, schema["const"]):
        errors.append("%s: expected the constant %s, got %s" % (path, _json(schema["const"]), _json(value)))
    if isinstance(value, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for key, sub in props.items():
                if key in value:
                    _check(value[key], sub, root, "%s.%s" % (path, key), errors, depth + 1)
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append("%s: missing required property %r" % (path, key))
        extra = schema.get("additionalProperties", True)
        if extra is not True:
            known = props if isinstance(props, dict) else {}
            for key in value:
                if key in known:
                    continue
                if extra is False:
                    errors.append("%s.%s: additional property not allowed" % (path, key))
                else:
                    _check(value[key], extra, root, "%s.%s" % (path, key), errors, depth + 1)
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, (dict, bool)):
            for i, item in enumerate(value):
                _check(item, items, root, "%s[%d]" % (path, i), errors, depth + 1)
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append("%s: expected at least %d items, got %d" % (path, schema["minItems"], len(value)))
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append("%s: expected at most %d items, got %d" % (path, schema["maxItems"], len(value)))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append("%s: %s is below the minimum %s" % (path, value, schema["minimum"]))
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append("%s: %s is above the maximum %s" % (path, value, schema["maximum"]))
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append("%s: shorter than %d characters" % (path, schema["minLength"]))
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append("%s: longer than %d characters" % (path, schema["maxLength"]))
        if isinstance(schema.get("pattern"), str):
            try:
                if not re.search(schema["pattern"], value):
                    errors.append("%s: does not match pattern %s" % (path, schema["pattern"]))
            except re.error as exc:
                errors.append("%s: invalid pattern %r in schema (%s)" % (path, schema["pattern"], exc))
    if isinstance(schema.get("allOf"), list):
        for i, sub in enumerate(schema["allOf"]):
            branch = []
            _check(value, sub, root, path, branch, depth + 1)
            if branch:
                errors.append("%s: does not match allOf[%d]: %s" % (path, i, "; ".join(branch[:3])))
    if isinstance(schema.get("anyOf"), list):
        if not any(_matches(value, sub, root, depth) for sub in schema["anyOf"]):
            errors.append("%s: does not match any schema in anyOf" % path)
    if isinstance(schema.get("oneOf"), list):
        hits = sum(1 for sub in schema["oneOf"] if _matches(value, sub, root, depth))
        if hits != 1:
            errors.append("%s: matches %d schemas in oneOf, expected exactly one" % (path, hits))
    if "not" in schema and _matches(value, schema["not"], root, depth):
        errors.append("%s: must not match the schema in 'not'" % path)


def _matches(value, schema, root, depth):
    branch = []
    _check(value, schema, root, "$", branch, depth + 1)
    return not branch


def _equal(a, b):
    """JSON equality: 1 == 1.0 but True != 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) != type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def validate(instance, schema, root=None):
    """Return a list of 'path: message' strings; empty means valid."""
    errors = []
    _check(instance, schema, schema if root is None else root, "$", errors, 0)
    seen = set()
    unique = []
    for err in errors:
        if err not in seen:
            seen.add(err)
            unique.append(err)
    return unique


def unknown_keywords(schema):
    """Sorted list of keywords the validator ignores, collected once over the
    whole schema (definitions included)."""
    found = set()

    def walk(node, depth):
        if depth > MAX_DEPTH or not isinstance(node, dict):
            return
        for key, sub in node.items():
            if key in ("properties", "definitions", "$defs"):
                if isinstance(sub, dict):
                    for child in sub.values():
                        walk(child, depth + 1)
                continue
            if key in ("items", "additionalProperties", "not"):
                walk(sub, depth + 1)
                continue
            if key in ("anyOf", "oneOf", "allOf"):
                if isinstance(sub, list):
                    for child in sub:
                        walk(child, depth + 1)
                continue
            if key not in SUPPORTED_KEYWORDS and key not in ANNOTATION_KEYWORDS:
                found.add(key)

    walk(schema, 0)
    return sorted(found)


def check_schema(schema):
    """(error, unknown_keywords) for a user-supplied schema: error is set when
    the schema cannot be used at all (not a JSON object)."""
    if not isinstance(schema, dict):
        return "schema must be a JSON object, got %s" % _type_name(schema), []
    return None, unknown_keywords(schema)


def strict_schema_problems(schema):
    """What keeps a schema from the strict form OpenAI structured output (and
    so `codex exec --output-schema`) requires: every object sets
    "additionalProperties": false and lists every property in "required"
    (an optional field is written as nullable instead). [] when it complies.
    Paths: `$` is the root, `.name` a property, `[]` the items of an array."""
    problems = []

    def is_object(node):
        kind = node.get("type")
        return kind == "object" or (isinstance(kind, list) and "object" in kind) or "properties" in node

    def walk(node, path, depth):
        if depth > MAX_DEPTH or not isinstance(node, dict):
            return
        if is_object(node):
            if node.get("additionalProperties") is not False:
                problems.append('%s: set "additionalProperties": false' % path)
            props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
            required = node.get("required") if isinstance(node.get("required"), list) else []
            missing = [k for k in props if k not in required]
            if missing:
                problems.append('%s: "required" must list %s' % (path, ", ".join(missing)))
            for key, sub in props.items():
                walk(sub, "%s.%s" % (path, key), depth + 1)
        if "items" in node:
            walk(node["items"], path + "[]", depth + 1)
        for key in ("anyOf", "oneOf", "allOf"):
            if isinstance(node.get(key), list):
                for i, sub in enumerate(node[key]):
                    walk(sub, "%s.%s[%d]" % (path, key, i), depth + 1)
        for key in ("definitions", "$defs"):
            if isinstance(node.get(key), dict):
                for name, sub in node[key].items():
                    walk(sub, "%s.%s.%s" % (path, key, name), depth + 1)

    walk(schema, "$", 0)
    return problems


# --------------------------------------------------------------------------- built-in schemas

def _nullable(kind, **extra):
    d = {"type": [kind, "null"]}
    d.update(extra)
    return d


USAGE_SCHEMA = {
    "type": "object",
    "required": ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"],
    "properties": {
        "input_tokens": {"type": "integer", "minimum": 0},
        "cached_input_tokens": {"type": "integer", "minimum": 0},
        "output_tokens": {"type": "integer", "minimum": 0},
        "reasoning_output_tokens": {"type": "integer", "minimum": 0},
        "total_tokens": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

RESULT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "agent-session result.json (v3; v2 files read back with the new fields as null)",
    "type": "object",
    "required": ["version", "name", "engine", "status", "ended", "turns", "failed_turns",
                 "started_iso", "finished_iso", "duration_s", "answer", "answer_chars", "untrusted"],
    "properties": {
        "version": {"type": "integer", "enum": [2, 3]},
        "name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$"},
        "engine": {"type": "string", "enum": ["claude", "codex", "gemini"]},
        "model": _nullable("string"),
        "tier": _nullable("string", enum=["cheap", "standard", "deep", None]),
        "role": _nullable("string", enum=["implementation", "review", "research", None]),
        "workdir": _nullable("string"),
        "state_dir": _nullable("string"),
        "log": _nullable("string"),
        "events_file": _nullable("string"),
        "status": {"type": "string", "enum": ["ok", "error", "timeout", "killed", "lost"]},
        "ended": {"type": "string", "enum": ["completed", "stopped", "idle", "killed", "lost", "dependency"]},
        "failure_class": {"type": ["string", "null"], "enum": list(FAILURE_CLASSES) + [None]},
        "reason": {"type": "string"},
        "exit_code": _nullable("integer"),
        "turns": {"type": "integer", "minimum": 0},
        "failed_turns": {"type": "array", "items": {
            "type": "object", "required": ["turn", "status"],
            "properties": {"turn": {"type": "integer"}, "status": {"type": "string"}},
            "additionalProperties": False}},
        "interrupted_turns": {"type": "array", "items": {"type": "integer"}},
        "attempts": _nullable("integer", minimum=1),
        "retries": _nullable("array", items={
            "type": "object", "required": ["attempt", "failure_class", "reason"],
            "properties": {"attempt": {"type": "integer"}, "failure_class": {"type": ["string", "null"]},
                           "reason": {"type": ["string", "null"]}, "waited_s": {"type": ["number", "null"]},
                           "finished_iso": {"type": ["string", "null"]}}}),
        "resumes": _nullable("array", items={
            "type": "object", "required": ["at_iso", "from_status"],
            "properties": {"at_iso": {"type": "string"}, "from_status": {"type": ["string", "null"]},
                           "message": {"type": ["string", "null"]}}}),
        "started_iso": _nullable("string"),
        "finished_iso": {"type": "string"},
        "duration_s": {"type": "number", "minimum": 0},
        "answer": _nullable("string"),
        "answer_chars": {"type": "integer", "minimum": 0},
        "answer_file": _nullable("string"),
        "structured_output": {},
        "schema_valid": _nullable("boolean"),
        "schema_file": _nullable("string"),
        "session_id": _nullable("string"),
        "engine_version": _nullable("string"),
        "usage": {"anyOf": [USAGE_SCHEMA, {"type": "null"}]},
        "cost_usd": _nullable("number", minimum=0),
        "cost_source": {"type": ["string", "null"], "enum": ["engine", "estimate", None]},
        "budget": {"anyOf": [{"type": "null"}, {
            "type": "object",
            "properties": {"usd_limit": _nullable("number"), "tokens_limit": _nullable("integer"),
                           "turns_limit": _nullable("integer"), "deadline_iso": _nullable("string"),
                           "ledger": _nullable("string"), "ledger_remaining_usd": _nullable("number")},
            "additionalProperties": False}]},
        "verify": {"anyOf": [{"type": "null"}, {
            "type": "object", "required": ["command", "status", "exit_code", "duration_s", "output_tail",
                                           "output_file", "attempts"],
            "properties": {"command": {"type": "string"},
                           "status": {"type": "string", "enum": ["pass", "fail", "error", "skipped"]},
                           "exit_code": _nullable("integer"), "duration_s": _nullable("number"),
                           "output_tail": _nullable("string"), "output_file": _nullable("string"),
                           "attempts": {"type": "integer", "minimum": 0}, "error": {"type": "string"}},
            "additionalProperties": False}]},
        "git": {"anyOf": [{"type": "null"}, {
            "type": "object", "required": ["branch", "base_commit", "head_commit", "commits", "files_changed",
                                           "insertions", "deletions", "dirty", "changed_files"],
            "properties": {"branch": _nullable("string"), "base_commit": _nullable("string"),
                           "head_commit": _nullable("string"), "commits": {"type": "integer", "minimum": 0},
                           "files_changed": {"type": "integer", "minimum": 0},
                           "insertions": {"type": "integer", "minimum": 0},
                           "deletions": {"type": "integer", "minimum": 0}, "dirty": {"type": "boolean"},
                           "changed_files": {"type": "array", "items": {"type": "string"}, "maxItems": 500}},
            "additionalProperties": False}]},
        "pipeline": {"anyOf": [{"type": "null"}, {
            "type": "object", "required": ["after", "upstream"],
            "properties": {"after": {"type": "array", "minItems": 1,
                                     "items": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$"}},
                           "upstream": {"type": "object", "additionalProperties": {
                               "type": ["string", "null"],
                               "enum": ["ok", "error", "timeout", "killed", "lost", None]}}},
            "additionalProperties": False}]},
        "warnings": _nullable("array", items={"type": "string"}),
        "worktree": {"anyOf": [{"type": "null"}, {"type": "object"}]},
        "stderr_tail": {"type": "string"},
        "judge": {"type": "object"},
        "untrusted": {"type": "string"},
    },
    "additionalProperties": False,
    # Invariants between fields: an ok result has no failure class; a child
    # finalized because an upstream failed is an error of class dependency.
    "allOf": [
        {"anyOf": [{"properties": {"status": {"not": {"const": "ok"}}}},
                   {"properties": {"failure_class": {"type": "null"}}}]},
        {"anyOf": [{"properties": {"ended": {"not": {"const": "dependency"}}}},
                   {"properties": {"status": {"const": "error"}, "failure_class": {"const": "dependency"}},
                    "required": ["failure_class"]}]},
    ],
}

EVENT_KINDS = ("lifecycle", "init", "assistant", "tool_start", "tool_end", "cmd", "edit", "tool_result",
               "api_retry", "denied", "error", "turn_start", "turn_end", "retry", "stall", "budget",
               "verify", "result", "task")

EVENT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "one line of <state>/<name>/events.jsonl",
    "type": "object",
    "required": ["ts", "iso", "turn", "attempt", "type", "text", "data"],
    "properties": {
        "ts": {"type": "number"},
        "iso": {"type": "string"},
        "turn": _nullable("integer", minimum=0),
        "attempt": {"type": "integer", "minimum": 1},
        "type": {"type": "string", "enum": list(EVENT_KINDS)},
        "text": {"type": "string"},
        "data": {"type": ["object", "null"]},
    },
    "additionalProperties": False,
}

JUDGE_SCHEMA = {
    "type": "object",
    "required": ["verdict", "confidence", "summary", "findings"],
    "properties": {
        "verdict": {"enum": ["approve", "revise", "reject"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        # Every key required and no extras at every level: codex passes
        # --output-schema to OpenAI strict structured output, which rejects a
        # schema otherwise (optional fields are expressed as nullable).
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "file", "line", "issue", "fix"],
            "properties": {"severity": {"enum": ["high", "medium", "low"]},
                           "file": {"type": ["string", "null"]},
                           "line": {"type": ["integer", "null"]},
                           "issue": {"type": "string"},
                           "fix": {"type": ["string", "null"]}},
            "additionalProperties": False}},
    },
    "additionalProperties": False,
}

# <state>/<name>/judge.json: the verdict plus who judged which run. `result
# --json` and `status --json` carry it under `judge`.
JUDGE_RECORD_SCHEMA = {
    "type": "object",
    "required": ["engine", "model", "tier", "session_id", "cost_usd", "verdict", "confidence", "summary",
                 "findings", "judged_at_iso", "judge_child", "untrusted"],
    "properties": dict(JUDGE_SCHEMA["properties"], **{
        "engine": {"type": "string", "enum": ["claude", "codex", "gemini"]},
        "model": _nullable("string"), "tier": _nullable("string"), "session_id": _nullable("string"),
        "cost_usd": _nullable("number", minimum=0), "judged_at_iso": {"type": "string"},
        "judge_child": {"type": "string"}, "judge_finished_iso": _nullable("string"),
        "diff_note": _nullable("string"),
        "judged_run": {"type": ["object", "null"]}, "untrusted": {"type": "string"},
    }),
    "additionalProperties": False,
}
RESULT_SCHEMA["properties"]["judge"] = JUDGE_RECORD_SCHEMA

SCHEMAS = {"result": RESULT_SCHEMA, "judge": JUDGE_SCHEMA, "event": EVENT_SCHEMA}
