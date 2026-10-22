"""Model index (profile.md + examples.md -> index.json) and runtime slice selection.

The runtime reads index.json first and then only the line ranges it needs. `select_slice` is the reference
implementation of the selection algorithm (model-format.md section on index.json). When index.json is missing but
model files exist (a model built before the index format), `legacy_index` parses them leniently and the slice
carries every example plus the matching groups, so an older model degrades to "read more" rather than to nothing."""
import hashlib
import re
from pathlib import Path

from . import grants as _grants
from . import lessons as _lessons
from . import lint as _lint
from . import schema as _schema
from .config import read_json, write_json
from .engine import load_schema

LAYER_TITLES = {
    1: "How {name} thinks", 2: "Quality bar", 3: "How {name} builds", 4: "Design taste", 5: "How {name} communicates",
    6: "Standing rules", 7: "Likes", 8: "Dislikes", 9: "Workflow habits", 10: "Recurring contexts",
}
LIGHT_SECTIONS = ["Read the prompts", "Default behaviour", "Quality bar", "Report", "Standing rules", "Never"]
MODEL_FILES = ("light.md", "profile.md", "examples.md", "grants.md", "episodes.json")
_LAYER_RE = re.compile(r"^## (\d+)\. (.+?)\s*$")
_GROUP_RE = re.compile(r"^### ([A-Z]{1,2})\. (.+?)\s*$")
_EXAMPLE_RE = re.compile(r"^### (\d+)\. (.+?)\s*$")
_VOCAB_RE = re.compile(r"^- \*\*(.+?)\*\*\s*:\s*(.*)$")
_WORD = re.compile(r"[a-z0-9][a-z0-9'+-]*")
_STOP = {"a", "an", "and", "the", "or", "of", "to", "for", "in", "on", "with", "any", "request", "work", "something",
         "do", "it", "his", "her", "their", "that", "this", "is", "are", "be"}

SCOPE_HEADER = (
    "Scope: these rules interpret requests from {name}. If the harness identifies this session as a delegated child "
    "or scheduled pipeline, follow its brief as written. A CLI or SDK invocation alone does not imply delegation. "
    "The harness's permission, sandbox and safety rules beat "
    "every line below.")


class ModelFormatError(ValueError):
    pass


def light_header(name):
    return "# %s: how to read %s's prompts\n\n%s\n" % (name, name, SCOPE_HEADER.format(name=name))


def _lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()


def _range_chars(lines, a, b):
    return sum(len(x) + 1 for x in lines[a - 1:b])


def parse_profile(lines, path="profile.md"):
    """Return dict with reading_notes, layers, groups, lessons, vocabulary (1-based inclusive line ranges) or raise.
    The `## What works and what doesn't` section is optional (a model built before lessons has none)."""
    errors = []
    if not lines or not lines[0].startswith("# "):
        raise ModelFormatError("%s:1: first line must be the `# {name}: the full model` title" % path)
    h2 = [(i + 1, line) for i, line in enumerate(lines) if line.startswith("## ")]
    if not h2:
        raise ModelFormatError("%s:1: no `## ` headings found" % path)
    first_h2 = h2[0][0]
    notes_start = 2 if len(lines) > 1 else 1
    reading_notes = [notes_start, max(notes_start, first_h2 - 1)]
    if not any("Reading notes" in line for line in lines[:first_h2]):
        errors.append("%s:%d: no `Reading notes` block before the first `## ` heading" % (path, first_h2))
    layers, groups, vocab, lessons = [], [], [], []
    unsaid_line = vocab_line = None
    for k, (ln, line) in enumerate(h2):
        end = (h2[k + 1][0] - 1) if k + 1 < len(h2) else len(lines)
        m = _LAYER_RE.match(line)
        if m:
            layers.append({"n": int(m.group(1)), "title": m.group(2), "lines": [ln, end]})
        elif line.strip() == "## Unsaid defaults":
            unsaid_line = ln
            groups = _parse_groups(lines, ln, end, path, errors)
        elif line.strip() == _lessons.HEADING:
            lessons = _lessons.parse_groups(lines, ln, end, path, errors)
        elif line.strip() == "## Vocabulary":
            vocab_line = ln
            for i in range(ln, end):
                vm = _VOCAB_RE.match(lines[i])
                if vm:
                    vocab.append({"phrase": vm.group(1).strip(), "line": i + 1})
        else:
            errors.append("%s:%d: unexpected `## ` heading %r (allowed: numbered layers 1-10, Unsaid defaults, %s, Vocabulary)" % (
                path, ln, line, _lessons.HEADING[3:]))
    have = {l["n"] for l in layers}
    for n in range(1, 11):
        if n not in have:
            errors.append("%s: missing layer heading `## %d. %s`" % (path, n, LAYER_TITLES[n].replace("{name}", "...")))
    if unsaid_line is None:
        errors.append("%s: missing `## Unsaid defaults` heading" % path)
    elif not groups:
        errors.append("%s:%d: `## Unsaid defaults` has no `### A. Title` groups" % (path, unsaid_line))
    if vocab_line is None:
        errors.append("%s: missing `## Vocabulary` heading" % path)
    for i, line in enumerate(lines):
        if len(line) > 600:
            errors.append("%s:%d: line over 600 characters" % (path, i + 1))
    if errors:
        raise ModelFormatError("\n".join(errors))
    standing = next((l for l in layers if l["n"] == 6), None)
    return {"reading_notes": reading_notes, "layers": layers, "groups": groups, "vocabulary": vocab,
            "standing_rules": standing["lines"], "lessons": lessons}


def _parse_groups(lines, start, end, path, errors, lenient=False):
    groups = []
    heads = [(i + 1, _GROUP_RE.match(lines[i])) for i in range(start, end) if lines[i].startswith("### ")]
    for k, (ln, m) in enumerate(heads):
        gend = (heads[k + 1][0] - 1) if k + 1 < len(heads) else end
        if not m:
            errors.append("%s:%d: group heading must look like `### A. Title`" % (path, ln))
            continue
        when = None
        for i in range(ln, gend):
            if lines[i].startswith("When:"):
                when = [w.strip().lower() for w in lines[i][5:].split(",") if w.strip()]
                break
        if when is None:
            if not lenient:
                errors.append("%s:%d: group %s has no `When:` line" % (path, ln, m.group(1)))
                continue
            when = _keywords_from_title(m.group(2))
        if not lenient and not 3 <= len(when) <= 12:
            errors.append("%s:%d: group %s `When:` needs 3 to 12 keywords, has %d" % (path, ln, m.group(1), len(when)))
        groups.append({"id": m.group(1), "title": m.group(2), "when": when, "lines": [ln, gend],
                       "chars": _range_chars(lines, ln, gend)})
    return groups


def _keywords_from_title(title):
    """Legacy groups have no When: line; quoted phrases and the title's content words stand in for it."""
    quoted = [q.strip().lower() for q in re.findall(r"\"([^\"]+)\"", title) if q.strip()]
    rest = re.sub(r"\"[^\"]+\"", " ", title)
    words = [w for w in _WORD.findall(rest.lower()) if w not in _STOP and len(w) > 2]
    out = []
    for w in quoted + words:
        if w not in out:
            out.append(w)
    return out


def parse_examples(lines, path="examples.md"):
    errors = []
    if not lines or not lines[0].startswith("# "):
        raise ModelFormatError("%s:1: first line must be the `# Seed to full prompt: examples` title" % path)
    heads = [(i + 1, _EXAMPLE_RE.match(line)) for i, line in enumerate(lines) if line.startswith("### ")]
    examples = []
    expected = 1
    for k, (ln, m) in enumerate(heads):
        nxt = heads[k + 1][0] - 1 if k + 1 < len(heads) else len(lines)
        end = nxt
        for i in range(ln, nxt):
            if lines[i].startswith("## "):
                end = i
                break
        if not m:
            errors.append("%s:%d: example heading must look like `### 1. seed`" % (path, ln))
            continue
        n = int(m.group(1))
        if n != expected:
            errors.append("%s:%d: example numbered %d, expected %d (numbering 1..N without gaps)" % (path, ln, n, expected))
        expected = n + 1
        body = lines[ln:end]
        if not any(b.startswith("Situation:") for b in body):
            errors.append("%s:%d: example %d has no `Situation:` line" % (path, ln, n))
        tags_line = next((b for b in body if b.startswith("Tags:")), None)
        if tags_line is None:
            errors.append("%s:%d: example %d has no `Tags:` line" % (path, ln, n))
            tags = []
        else:
            tags = [t.strip().lower() for t in tags_line[5:].split(",") if t.strip()]
            if not 1 <= len(tags) <= 4:
                errors.append("%s:%d: example %d needs 1 to 4 tags" % (path, ln, n))
        if not any(b.startswith(">") for b in body):
            errors.append("%s:%d: example %d has no blockquoted full prompt" % (path, ln, n))
        examples.append({"n": n, "seed": m.group(2), "tags": tags, "lines": [ln, end], "chars": _range_chars(lines, ln, end)})
    if errors:
        raise ModelFormatError("\n".join(errors))
    return examples


def check_light(text, name, max_lines=60):
    """Errors for light.md: header, scope paragraph, sections, line count."""
    errors = []
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        errors.append("light.md:1: missing title line")
    if "Scope:" not in text:
        errors.append("light.md: missing the fixed Scope paragraph")
    for sec in LIGHT_SECTIONS:
        if ("## " + sec) not in lines:
            errors.append("light.md: missing section `## %s`" % sec)
    if len(lines) > max_lines:
        errors.append("light.md: %d lines, maximum %d" % (len(lines), max_lines))
    return errors


def ensure_light_header(body, name):
    """Prepend the fixed header when the model omitted it."""
    if body.lstrip().startswith("# ") and "Scope:" in body:
        return body if body.endswith("\n") else body + "\n"
    stripped = body.strip("\n")
    if stripped.startswith("# "):
        stripped = "\n".join(stripped.splitlines()[1:]).strip("\n")
    return light_header(name) + "\n" + stripped + "\n"


def files_hash(model_dir):
    """SHA256 of MODEL_FILES in order, with absent optional files contributing no bytes."""
    h = hashlib.sha256()
    for name in MODEL_FILES:
        p = Path(model_dir) / name
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()[:16]


def build_index(model_dir, built=None, write=True):
    """Parse profile.md and examples.md into index.json. Raises ModelFormatError with line numbers."""
    model_dir = Path(model_dir)
    profile = _lines(model_dir / "profile.md")
    examples = _lines(model_dir / "examples.md")
    pf = parse_profile(profile)
    ex = parse_examples(examples)
    light_p = model_dir / "light.md"
    corr_p = model_dir / "corrections.md"
    grants_p = model_dir / "grants.md"
    idx = {
        "version": 2, "model_hash": files_hash(model_dir), "built": built or _now(),
        "light": {"path": "light.md", "chars": len(light_p.read_text(encoding="utf-8")) if light_p.exists() else 0},
        "standing_rules": {"path": "profile.md", "lines": pf["standing_rules"],
                           "chars": _range_chars(profile, *pf["standing_rules"])},
        "reading_notes": {"path": "profile.md", "lines": pf["reading_notes"]},
        "groups": pf["groups"], "layers": pf["layers"], "vocabulary": pf["vocabulary"], "examples": ex,
        "corrections": {"path": "corrections.md",
                        "lines": len(corr_p.read_text(encoding="utf-8").splitlines()) if corr_p.exists() else 0},
    }
    if pf["lessons"]:
        idx["lessons"] = pf["lessons"]
    warnings = []
    if grants_p.exists():
        gs, gerrs = _grants.parse_grants(grants_p.read_text(encoding="utf-8"))
        idx["grants"] = {"path": "grants.md", "count": len(gs), "active": sum(1 for g in gs if g.get("active"))}
        warnings += ["grants.md: " + e for e in gerrs]
    errs = _schema.validate(idx, load_schema("index"))
    if errs:
        raise ModelFormatError("index.json failed its schema: " + "; ".join(errs[:5]))
    when_vocab = {w for g in pf["groups"] + pf["lessons"] for w in g["when"]} | {g["id"].lower() for g in pf["groups"] + pf["lessons"]}
    if pf["lessons"]:
        when_vocab.add("upgrade")
    for e in ex:
        for t in e["tags"]:
            if t not in when_vocab:
                warnings.append("examples.md:%d: tag %r matches no group id or When: keyword" % (e["lines"][0], t))
    if write:
        write_json(model_dir / "index.json", idx)
    return idx, warnings


def legacy_index(model_dir):
    """A best-effort index for a model without index.json: lenient headings, groups without `When:` lines keyed by
    their titles, examples without `Tags:` keyed by their seeds. Returns None when there is no profile or examples."""
    model_dir = Path(model_dir)
    pp, ep = model_dir / "profile.md", model_dir / "examples.md"
    if not pp.exists() and not ep.exists():
        return None
    profile = _lines(pp) if pp.exists() else []
    examples = _lines(ep) if ep.exists() else []
    idx = {"version": 2, "legacy": True, "model_hash": files_hash(model_dir), "built": _now(),
           "light": {"path": "light.md", "chars": 0}, "groups": [], "layers": [], "vocabulary": [], "examples": [],
           "standing_rules": None, "reading_notes": None, "corrections": {"path": "corrections.md", "lines": 0}}
    if profile:
        h2 = [(i + 1, line) for i, line in enumerate(profile) if line.startswith("## ")]
        first_h2 = h2[0][0] if h2 else len(profile) + 1
        if first_h2 > 2:
            idx["reading_notes"] = {"path": "profile.md", "lines": [2, first_h2 - 1]}
        for k, (ln, line) in enumerate(h2):
            end = (h2[k + 1][0] - 1) if k + 1 < len(h2) else len(profile)
            low = line.lower()
            m = _LAYER_RE.match(line)
            if m:
                idx["layers"].append({"n": int(m.group(1)), "title": m.group(2), "lines": [ln, end]})
            if "standing rules" in low:
                idx["standing_rules"] = {"path": "profile.md", "lines": [ln, end], "chars": _range_chars(profile, ln, end)}
            elif "unsaid" in low or "defaults" in low:
                idx["groups"] = _parse_groups(profile, ln, end, "profile.md", [], lenient=True)
            elif "vocabulary" in low or "glossary" in low:
                for i in range(ln, end):
                    vm = _VOCAB_RE.match(profile[i])
                    if vm:
                        idx["vocabulary"].append({"phrase": vm.group(1).strip(), "line": i + 1})
    if examples:
        heads = [(i + 1, _EXAMPLE_RE.match(line)) for i, line in enumerate(examples) if line.startswith("### ")]
        for k, (ln, m) in enumerate(heads):
            if not m:
                continue
            nxt = heads[k + 1][0] - 1 if k + 1 < len(heads) else len(examples)
            end = nxt
            for i in range(ln, nxt):
                if examples[i].startswith("## "):
                    end = i
                    break
            body = examples[ln:end]
            tags_line = next((b for b in body if b.startswith("Tags:")), None)
            tags = [t.strip().lower() for t in tags_line[5:].split(",") if t.strip()] if tags_line else []
            situation = next((b[len("Situation:"):] for b in body if b.startswith("Situation:")), "")
            idx["examples"].append({"n": int(m.group(1)), "seed": m.group(2), "tags": tags, "lines": [ln, end],
                                    "chars": _range_chars(examples, ln, end), "situation": situation})
    return idx


def _now():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _words(text):
    return set(_WORD.findall((text or "").lower()))


def _stem(w):
    """Light stemming so legacy title keywords match their inflections: messages/message, deploys/deploy."""
    w = w.lower()
    if len(w) > 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith("es") and w[-3:-2] in ("s", "x", "z") or w.endswith(("ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _keyword_in(w, seed_lower, seed_stems, stemmed):
    """A `when` keyword is present when it appears as a phrase; a legacy title keyword also matches by stem."""
    if _phrase_in(w, seed_lower):
        return True
    if not stemmed or " " in w:
        return False
    ws = _stem(w)
    return any(ws == s or (min(len(ws), len(s)) >= 4 and (ws.startswith(s) or s.startswith(ws))) for s in seed_stems)


def _phrase_in(phrase, seed_lower):
    for variant in phrase.split("/"):
        v = variant.strip().lower()
        if v and re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(v), seed_lower):
            return True
    return False


def _kind_keyword_in(keyword, kind_words):
    """A decoded kind can name the informative word in a multiword trigger."""
    return bool((_words(keyword) - _STOP) & (kind_words - _STOP))


def runtime_grants(text, tokens=None):
    """The grants.md text the runtime may see: the file's preamble plus only the blocks that are active, parse with
    zero errors and carry no forbidden instruction outside their Quote line. Revoked, invalid and malformed blocks are
    dropped, so an invalid grant never reaches the slice. Returns (text, [ids]); text is "" when nothing qualifies."""
    gs, _ = _grants.parse_grants(text or "", tokens=tokens)
    keep = []
    for g in gs:
        if not g.get("active") or g.get("errors"):
            continue
        if _lint.forbidden_hits("\n".join(g["raw"]), quote_lines_exempt=True):
            continue
        keep.append(g)
    if not keep:
        return "", []
    lines = (text or "").splitlines()
    first = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    preamble = "\n".join(lines[:first]).strip()
    blocks = ["\n".join(g["raw"]).strip() for g in keep]
    return (preamble + "\n\n" if preamble else "") + "\n\n".join(blocks) + "\n", [g["id"] for g in keep]


def index_is_stale(model_dir):
    """True when index.json exists and its model_hash no longer matches the model files."""
    model_dir = Path(model_dir)
    idx = read_json(model_dir / "index.json")
    return bool(idx) and not idx.get("legacy") and idx.get("model_hash") != files_hash(model_dir)


def select_slice(model_dir, seed, cfg_select, kind=None, no_light=False, mode="execute", index=None, project=None, context=None):
    """Return (markdown slice, decision dict). Implements the algorithm in model-format.md.

    Order of the slice: corrections, standing grants, light, reading notes, standing rules, groups (best first),
    vocabulary present in the seed, examples in ascending relevance so the best match sits last, next to the seed."""
    model_dir = Path(model_dir)
    idx = index or read_json(model_dir / "index.json")
    legacy = False
    stale = False
    if idx and not idx.get("legacy") and idx.get("model_hash") != files_hash(model_dir):
        # profile.md, examples.md, light.md or grants.md changed after `synthesize index`: the stored line ranges may
        # point at the wrong lines, so the index is rebuilt in memory (never written) and the header says so
        stale = True
        try:
            idx, _ = build_index(model_dir, write=False)
        except (ModelFormatError, FileNotFoundError):
            idx = legacy_index(model_dir)
    if not idx:
        idx = legacy_index(model_dir)
        legacy = idx is not None
    elif idx.get("legacy"):
        legacy = True
    if not idx:
        raise ModelFormatError("index.json missing: run `synthesize index`")
    from . import intent_context as _intent
    frame = _intent.normalize(context)
    if project and frame.get("project") and project != frame["project"]:
        raise _intent.ContextFormatError("--project and intent-context project disagree")
    project = project or frame.get("project")
    context_text = _intent.retrieval_text(frame)
    context_lower = context_text.lower()
    context_words = _words(context_text)
    context_stems = {_stem(w) for w in context_words}
    initiative = mode in ("initiative", "plan")
    seed_lower = (seed or "").lower()
    seed_words = _words(seed)
    seed_stems = {_stem(w) for w in seed_words}
    kind_words = _words(kind)
    kind_id = (kind or "").strip().casefold()
    profile = _lines(model_dir / "profile.md") if (model_dir / "profile.md").exists() else []
    examples = _lines(model_dir / "examples.md") if (model_dir / "examples.md").exists() else []

    scored = []
    for g in idx["groups"]:
        s = sum(1 for w in g["when"] if _keyword_in(w, seed_lower, seed_stems, legacy)) + 0.5 * sum(1 for w in g["when"] if _kind_keyword_in(w, kind_words))
        if kind_id == g["id"].casefold():
            s += 0.5
        scored.append((s, g))
    matching = [g for s, g in sorted(scored, key=lambda t: (-t[0], t[1]["id"])) if s > 0]
    context_routed = False
    if not matching and context_text:
        contextual = [(sum(1 for w in g["when"] if _keyword_in(w, context_lower, context_stems, legacy)), g) for g in idx["groups"]]
        matching = [g for s, g in sorted(contextual, key=lambda t: (-t[0], t[1]["id"])) if s > 0]
        context_routed = bool(matching)
    escalated = (not initiative and not context_routed) and len(matching) > cfg_select["max_groups"]  # current seed spanning kinds reads more
    wide = initiative or escalated
    budget = cfg_select["initiative_budget_chars"] if wide or legacy else cfg_select["budget_chars"]
    max_groups = cfg_select.get("initiative_max_groups", 6) if wide else cfg_select["max_groups"]
    max_examples = cfg_select.get("initiative_max_examples", 10) if wide else cfg_select["max_examples"]
    chosen = matching[:max_groups]
    if not chosen and idx["groups"]:
        chosen = [idx["groups"][0]]
    # lessons: the What-works groups whose When: words the seed (or the kind) contains; none when nothing matches
    max_lessons = cfg_select.get("initiative_max_lessons", 3) if wide else cfg_select.get("max_lessons", 2)
    ls_scored = []
    for lg in idx.get("lessons") or []:
        sc = sum(1 for w in lg["when"] if _keyword_in(w, seed_lower, seed_stems, False)) + 0.5 * sum(1 for w in lg["when"] if _kind_keyword_in(w, kind_words))
        if kind_id == lg["id"].casefold():
            sc += 0.5
        ls_scored.append((sc, lg))
    chosen_lessons = [lg for sc, lg in sorted(ls_scored, key=lambda t: (-t[0], t[1]["id"])) if sc > 0][:max_lessons]
    if not chosen_lessons and context_text:
        contextual_lessons = [(sum(1 for w in lg["when"] if _keyword_in(w, context_lower, context_stems, False)), lg)
                              for lg in idx.get("lessons") or []]
        chosen_lessons = [lg for sc, lg in sorted(contextual_lessons, key=lambda t: (-t[0], t[1]["id"])) if sc > 0][:max_lessons]
    chosen_ids = {g["id"].lower() for g in chosen + chosen_lessons}
    chosen_when = {w for g in chosen + chosen_lessons for w in g["when"]}
    ex_scored = []
    for e in idx["examples"]:
        tag_score = sum(1 for t in e["tags"] if t in chosen_ids or t in chosen_when)
        overlap = len((_words(e["seed"]) | _words(e.get("situation"))) & (seed_words | kind_words))
        if context_text:
            overlap += 0.5 * len((_words(e["seed"]) | _words(e.get("situation"))) & context_words)
        when_hits = sum(1 for w in chosen_when if _phrase_in(w, e["seed"].lower()))
        ex_scored.append((tag_score + when_hits, overlap, e))
    ranked = sorted(ex_scored, key=lambda t: (-t[0], -t[1], t[2]["n"]))
    if legacy:
        ex_chosen = [e for _, _, e in ranked]  # an old model has no tags to trust: read every example, best last
    else:
        ex_chosen = [e for ts, ov, e in ranked if ts or ov][:max_examples]
    vocab = [v for v in idx["vocabulary"] if _phrase_in(v["phrase"], seed_lower)]

    parts = []
    corr_p = model_dir / "corrections.md"
    corr = corr_p.read_text(encoding="utf-8") if corr_p.exists() else ""
    if corr.strip():
        # Project corrections cannot cross into another checkout. Global and
        # unparsed legacy lines remain visible so an old correction is not lost.
        scoped = []
        for line in corr.splitlines():
            scope = re.search(r"\|\s*scope:\s*project:([^|]+)", line)
            if scope and (not project or scope.group(1).strip() != project):
                continue
            scoped.append(line)
        if any(line.strip() for line in scoped):
            parts.append(("corrections", "## Corrections (later lines win)\n" + "\n".join(scoped).strip() + "\n", True))
    grants_p = model_dir / "grants.md"
    grant_ids = []
    if grants_p.exists():
        gtext, grant_ids = runtime_grants(grants_p.read_text(encoding="utf-8"))
        if grant_ids:
            parts.append(("grants", gtext, True))
    if not no_light:
        lp = model_dir / "light.md"
        if lp.exists():
            parts.append(("light", lp.read_text(encoding="utf-8").strip() + "\n", True))
    rn = idx.get("reading_notes")
    if rn and profile:
        parts.append(("reading_notes", "\n".join(profile[rn["lines"][0] - 1:rn["lines"][1]]).strip() + "\n", True))
    sr = idx.get("standing_rules")
    if sr and profile:
        parts.append(("standing_rules", "\n".join(profile[sr["lines"][0] - 1:sr["lines"][1]]).strip() + "\n", True))
    if frame:
        parts.append(("intent_context", _intent.render(frame), True))
    for k, g in enumerate(chosen):
        a, b = g["lines"]
        parts.append(("group:" + g["id"], "\n".join(profile[a - 1:b]).strip() + "\n", k == 0))
    for k, lg in enumerate(chosen_lessons):
        a, b = lg["lines"]
        head = (_lessons.HEADING + " (lessons for this kind of request)\n") if k == 0 else ""
        parts.append(("lessons:" + lg["id"], head + "\n".join(profile[a - 1:b]).strip() + "\n", k == 0))
    if vocab:
        vl = ["## Vocabulary (present in the seed)"]
        for v in vocab:
            vl.append(profile[v["line"] - 1])
        parts.append(("vocabulary", "\n".join(vl) + "\n", True))
    from . import recall as _recall
    recall_warning = None
    try:
        echoes = _recall.retrieve(model_dir, seed, kind=kind, project=project, limit=cfg_select.get("max_echoes", 3), context_text=context_text)
    except _recall.RecallFormatError as exc:
        echoes = []
        recall_warning = str(exc)
    for echo in echoes:
        parts.append(("echo:" + echo["id"], _recall.render([echo]), False))
    for e in reversed(ex_chosen):  # ascending relevance: the best match is last
        a, b = e["lines"]
        parts.append(("example:%d" % e["n"], "\n".join(examples[a - 1:b]).strip() + "\n", False))

    def total():
        return sum(len(t) for _, t, _ in parts)

    # trim: the least relevant example first (they sit in ascending order), then groups beyond the first, then lesson
    # groups beyond the first; never corrections, grants, standing rules or vocabulary
    while total() > budget:
        drop = next((i for i in range(len(parts)) if parts[i][0].startswith("example:")), None)
        if drop is None:
            drop = next((i for i in range(len(parts) - 1, -1, -1) if parts[i][0].startswith("group:") and not parts[i][2]), None)
        if drop is None:
            drop = next((i for i in range(len(parts) - 1, -1, -1) if parts[i][0].startswith("lessons:") and not parts[i][2]), None)
        if drop is None:
            drop = next((i for i in range(len(parts) - 1, -1, -1) if parts[i][0].startswith("echo:")), None)
        if drop is None:
            break
        parts.pop(drop)
    kept_groups = [p[0].split(":", 1)[1] for p in parts if p[0].startswith("group:")]
    kept_lessons = [p[0].split(":", 1)[1] for p in parts if p[0].startswith("lessons:")]
    kept_examples = [int(p[0].split(":", 1)[1]) for p in parts if p[0].startswith("example:")]
    kept_echoes = [p[0].split(":", 1)[1] for p in parts if p[0].startswith("echo:")]
    body = "\n".join(t for _, t, _ in parts)
    notes = []
    if legacy:
        if len(kept_examples) == len(idx["examples"]):
            notes.append("legacy model, no index: every example loaded")
        else:
            notes.append("legacy model, no index: %d of %d examples loaded, the budget trimmed the rest" % (len(kept_examples), len(idx["examples"])))
    if stale:
        notes.append("index.json stale, rebuilt in memory: run `synthesize index`")
    header = "<!-- mind-reading slice%s: groups %s; %sexamples %s; %d chars -->\n" % (
        (" (" + "; ".join(notes) + ")") if notes else "",
        ",".join(kept_groups) or "-", ("lessons %s; " % ",".join(kept_lessons)) if kept_lessons else "",
        ",".join(str(n) for n in kept_examples) or "-", len(body))
    decision = {"groups": kept_groups, "examples": kept_examples, "lessons": kept_lessons, "vocabulary": [v["phrase"] for v in vocab],
                "grants": grant_ids, "chars": len(body) + len(header), "light": not no_light, "mode": mode,
                "legacy": legacy, "escalated": escalated, "stale_index": stale}
    decision.update({"echoes": kept_echoes, "project": project,
                     "recall_warning": recall_warning,
                     "matched_keywords": {g["id"]: [w for w in g["when"] if _keyword_in(w, seed_lower, seed_stems, legacy)
                                                               or _kind_keyword_in(w, kind_words)] for g in chosen if g["id"] in kept_groups},
                     "fallback": not bool(matching), "budget_chars": budget,
                     "over_budget": len(body) + len(header) > budget})
    if frame:
        decision.update(context_fields=sorted(frame), context_hash=_intent.fingerprint(frame), context_routed=context_routed,
                        context_keywords={g["id"]: [w for w in g["when"] if _keyword_in(w, context_lower, context_stems, legacy)]
                                          for g in chosen if g["id"] in kept_groups})
    return header + body, decision
