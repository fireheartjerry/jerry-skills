"""Minimal JSON Schema validator (stdlib). Covers the subset the pipeline schemas use:
type (incl. lists), properties, required, additionalProperties, enum, const, minLength, maxLength,
minimum, maximum, items, minItems, maxItems, pattern, anyOf, oneOf, $ref to #/$defs/... .
Errors name the JSON path so a retry prompt can quote them."""
import re

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


class SchemaError(ValueError):
    pass


def _type_ok(value, t):
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPES.get(t)
    return py is not None and isinstance(value, py)


def _resolve(schema, root):
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not ref.startswith("#/"):
        raise SchemaError("only local $ref supported: %s" % ref)
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(value, schema, path="$", root=None):
    """Return a list of error strings (empty when valid)."""
    root = root if root is not None else schema
    schema = _resolve(schema, root)
    errors = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            errors.append("%s: expected type %s, got %s" % (path, "|".join(types), type(value).__name__))
            return errors
    if "const" in schema and value != schema["const"]:
        errors.append("%s: expected const %r" % (path, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: %r not in enum %s" % (path, value, schema["enum"]))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: string shorter than %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("%s: string longer than %d (%d chars)" % (path, schema["maxLength"], len(value)))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append("%s: does not match pattern %s" % (path, schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: %r below minimum %r" % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s: %r above maximum %r" % (path, value, schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("%s: more than %d items" % (path, schema["maxItems"]))
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], "%s[%d]" % (path, i), root)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append("%s: missing required key %r" % (path, key))
        for key, sub in props.items():
            if key in value:
                errors += validate(value[key], sub, "%s.%s" % (path, key), root)
        extra = schema.get("additionalProperties", True)
        if extra is False:
            for key in value:
                if key not in props:
                    errors.append("%s: unexpected property %r" % (path, key))
        elif isinstance(extra, dict):
            for key in value:
                if key not in props:
                    errors += validate(value[key], extra, "%s.%s" % (path, key), root)
    for combo in ("anyOf", "oneOf"):
        if combo in schema:
            results = [validate(value, sub, path, root) for sub in schema[combo]]
            passing = sum(1 for r in results if not r)
            if passing == 0:
                errors.append("%s: matches none of %s (%s)" % (path, combo, "; ".join(results[0][:2])))
            elif combo == "oneOf" and passing > 1:
                errors.append("%s: matches more than one of oneOf" % path)
    return errors


def check(value, schema):
    """Raise SchemaError with all messages when invalid."""
    errs = validate(value, schema)
    if errs:
        raise SchemaError("\n".join(errs))
    return value


def summary(schema, indent=0):
    """A compact human-readable sketch of a schema for prompts."""
    lines = []
    pad = "  " * indent
    schema = _resolve(schema, schema)
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        req = set(schema.get("required", []))
        for k, v in schema.get("properties", {}).items():
            v = _resolve(v, schema)
            desc = _one_line(v)
            lines.append("%s%s%s: %s" % (pad, k, "" if k in req else "?", desc))
            if v.get("type") == "object" or "properties" in v:
                lines += summary(v, indent + 1)
            elif v.get("type") == "array" and isinstance(v.get("items"), dict) and (
                    v["items"].get("type") == "object" or "properties" in v["items"]):
                lines += summary(v["items"], indent + 1)
    return lines


def _one_line(v):
    t = v.get("type", "any")
    if isinstance(t, list):
        t = "|".join(t)
    parts = [t]
    if "enum" in v:
        parts.append("one of " + ", ".join(str(e) for e in v["enum"]))
    if "maxLength" in v:
        parts.append("<=%d chars" % v["maxLength"])
    if "minItems" in v or "maxItems" in v:
        parts.append("%s..%s items" % (v.get("minItems", 0), v.get("maxItems", "n")))
    if t == "array" and isinstance(v.get("items"), dict):
        it = v["items"]
        if it.get("type") not in (None, "object"):
            parts.append("of " + _one_line(it))
    return ", ".join(parts)
