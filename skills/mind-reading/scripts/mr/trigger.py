"""Reference implementation of the trigger rule in SKILL.md, so the description can be tested against a case set.

The skill fires when an invocation phrase appears in the message as an instruction and something follows it as the
seed. It does not fire on the bare words "mind reading", on a quoted or reported phrase ("he said read my mind"), on
an invocation with no seed, or on a message whose subject is this skill by name or its own model files (those are
ordinary requests, and build or refresh requests go to references/build.md). "the skill", "the model" or "rebuild the
model" without the skill's name are ordinary seed words."""
import re

INVOCATION_PHRASES = ["read my mind", "mind-read this", "mind read this", "/mind-reading", "$mind-reading"]
_INVOKE = re.compile(r"(?i)(?<![\w/$-])(read my mind|mind[- ]read this|/mind-reading|\$mind-reading)(?![\w-])")
_REPORTING = re.compile(r"(?i)\b(said|says|saying|told|tells|telling|wrote|writes|typed|phrase|words?|called|quote[sd]?|like)\b[\s,:]*[\"'`]?$")
# Meta: the seed is about this skill. Only when it names the skill ("mind-reading", "mind reading") next to one of its
# parts, or names a file that exists only in this skill's model. "the skill", "the model", "rebuild the model" and
# "index.json" alone are ordinary seed words: the user may maintain skills, models and routes of their own.
_SKILL_NAME = re.compile(r"(?i)\bmind[- ]reading\b")
_META_TERMS = re.compile(
    r"(?i)\b(skill|model(?: files?)?|tags?|description|block|pipeline|eval|slice|index|prompts?|trigger|rebuild|refresh|"
    r"corrections?|grants?|SKILL\.md|profile\.md|examples\.md|light\.md|build\.md|model-format\.md|index\.json)\b")
_OWN_FILES = re.compile(
    r"(?i)\b(grants|corrections)\.md\b|\b(the|this|my) (always-on block|confidence tags|reading tags)\b|\[[SLG]\] tags?\b")


def is_meta(seed):
    """True when the seed is about this skill or its model files rather than about the user's own work."""
    seed = seed or ""
    if _OWN_FILES.search(seed):
        return True
    return bool(_SKILL_NAME.search(seed) and _META_TERMS.search(seed))


_QUOTE_CHARS = "\"'`“”‘’"


def _inside_quotes(message, start, end):
    """True when the phrase sits between a pair of quote characters on its line."""
    line_start = message.rfind("\n", 0, start) + 1
    line_end = message.find("\n", end)
    line_end = len(message) if line_end < 0 else line_end
    before = message[line_start:start]
    after = message[end:line_end]
    for q in _QUOTE_CHARS:
        if q in before and q in after:
            return True
    if ("“" in before and "”" in after) or ("‘" in before and "’" in after):
        return True
    return False


def classify(message):
    """{"fire": bool, "seed": str, "reason": str}."""
    text = message or ""
    matches = list(_INVOKE.finditer(text))
    if not matches:
        return {"fire": False, "seed": "", "reason": "no invocation phrase"}
    live = []
    for m in matches:
        before = text[:m.start()].rstrip()
        if _inside_quotes(text, m.start(), m.end()):
            continue
        if _REPORTING.search(before[-40:]):
            continue
        live.append(m)
    if not live:
        return {"fire": False, "seed": "", "reason": "quoted or reported phrase"}
    m = live[0]
    seed = (text[:m.start()] + " " + text[m.end():]).strip()
    seed = re.sub(r"^[\s:,.;!-]+", "", seed).strip()
    seed = re.sub(r"[ \t]{2,}", " ", seed)
    if not seed:
        return {"fire": False, "seed": "", "reason": "no seed after the phrase"}
    if is_meta(seed):
        return {"fire": False, "seed": seed, "reason": "the message is about the skill or its model"}
    return {"fire": True, "seed": seed, "reason": "invocation phrase with a seed"}
