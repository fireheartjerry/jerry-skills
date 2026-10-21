#!/usr/bin/env python3
"""Structural lint for the competitive-systems skill. Runs from any cwd, no network.

Prints `ok: ...` or `FAIL: ...` per check and exits 1 if any check failed.
"""
import os
import py_compile
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL = os.path.join(ROOT, "SKILL.md")
REFS = os.path.join(ROOT, "references")
SCRIPTS = os.path.join(ROOT, "scripts")
EVALS = os.path.join(ROOT, "evals")

FIELDS = ["**Problem shape:**", "**Precondition test:**", "**Complexity:**", "**Sketch:**",
          "**Oracle:**", "**Traps:**", "**Not when:**"]
GLOSSARY = ["fail-closed", "direct mapping", "sensitive file", "heavy job", "makespan",
            "must-keep set", "oracle", "property test", "precondition", "noise floor",
            "minimum effect", "A/A test", "change point", "Amdahl cap",
            "reference implementation", "differential test", "metamorphic test"]
TEMPLATE_FIELDS = ["Problem", "Gate 1", "Gate 2", "Gate 3", "Noise floor", "Card", "Card facts",
                   "Invariant", "Oracle", "Before", "Class", "Verdict", "Guards", "Kept",
                   "Not verified"]
SKILL_CAP = 10000
# Assembled from fragments so this file never contains the strings it forbids.
BANNED_DESCRIPTION = ["Code" + "forces", "pres" + "tige", "grand" + "master", "world" + "-class",
                      "eli" + "te", "IC" + "PC"]
# The four-digit form of three thousand is banned everywhere (write it with a comma), so the
# description check and the repo-wide grep in the acceptance checklist agree. The private
# tooling's command, environment-variable prefix and history directory are banned too;
# `tools/dev.py` itself is the documented shim name, so only the private subcommand form is.
BANNED_EVERYWHERE = ["tools/dev.py " + "strategy", "inven" + "tion-loop", "/Us" + "ers/",
                     "/ho" + "me/", "9to" + "5", "NINE_" + "TO_FIVE", ".test" + "brain",
                     "TO" + "DO", "T" + "BD", "30" + "00"]
EM_DASH = chr(0x2014)

failures = []


def ok(msg):
    print("ok: " + msg)


def fail(msg):
    failures.append(msg)
    print("FAIL: " + msg)


def check(cond, msg):
    (ok if cond else fail)(msg)
    return cond


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def description_of(text):
    m = re.search(r'^description:\s*"(.*)"\s*$', text, re.M)
    if m:
        return m.group(1)
    m = re.search(r"^description:\s*>-?\s*\n((?:[ \t]+.*\n?)+)", text, re.M)
    if m:
        return " ".join(line.strip() for line in m.group(1).splitlines()).strip()
    m = re.search(r"^description:\s*(.*?)\s*$", text, re.M)
    return m.group(1).strip("'\"") if m else ""


def skill_files(exts):
    out = []
    for dirpath, _, names in os.walk(ROOT):
        if "__pycache__" in dirpath:
            continue
        for n in sorted(names):
            if os.path.splitext(n)[1] in exts:
                out.append(os.path.join(dirpath, n))
    return sorted(out)


def check_frontmatter(text):
    check(text.startswith("---"), "SKILL.md starts with frontmatter")
    check(re.search(r"^name:\s*competitive-systems\s*$", text, re.M) is not None,
          "SKILL.md name is competitive-systems")
    desc = description_of(text)
    check(200 <= len(desc) <= 600, "description length %d is within 200..600" % len(desc))
    for s in BANNED_DESCRIPTION:
        check(s not in desc, "description does not contain %r" % s)
    check("Not for" in desc[:200], "description says 'Not for' within its first 200 characters "
          "(at %d)" % desc.find("Not for"))
    size = os.path.getsize(SKILL)
    check(size <= SKILL_CAP, "SKILL.md size %d bytes <= %d" % (size, SKILL_CAP))
    m = re.search(r"^\s+version:\s*(\S+)\s*$", text, re.M)
    init = read(os.path.join(SCRIPTS, "devkit", "__init__.py"))
    v = re.search(r'VERSION = "([^"]+)"', init)
    check(m is not None and v is not None and m.group(1) == v.group(1),
          "SKILL.md version %s equals devkit VERSION %s" % (m and m.group(1), v and v.group(1)))


def check_paths(text):
    mentioned = re.findall(r"(?:references|scripts|tests)/[A-Za-z0-9_.*-]+", text)
    for rel in sorted(set(mentioned)):
        if "*" in rel:
            continue
        check(os.path.exists(os.path.join(ROOT, rel)), "mentioned path exists: %s" % rel)
    for name in sorted(os.listdir(REFS)):
        if name.startswith("cards-"):
            continue                          # named through references/cards-*.md
        if name.endswith((".md", ".json")):
            check("references/" + name in text, "references/%s is mentioned in SKILL.md" % name)
    check("references/cards-*.md" in text, "SKILL.md points at references/cards-*.md")
    check("scripts/dev.py" in text, "scripts/dev.py is mentioned in SKILL.md")
    tooling = read(os.path.join(REFS, "tooling.md"))
    check("scripts/bench.py" in tooling, "scripts/bench.py is mentioned in references/tooling.md")


def card_sections(text):
    """Split on `## ` headings, ignoring lines inside ``` fences."""
    sections, current, in_fence = [], None, False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            current = [line[3:]]
            sections.append(current)
            continue
        if current is not None:
            current.append(line)
    return ["\n".join(s) for s in sections]  # drop the intro before the first card


def card_headings(path):
    return [sec.splitlines()[0].strip() for sec in card_sections(read(path))]


def check_card(path):
    text = read(path)
    rel = os.path.relpath(path, ROOT)
    sections = card_sections(text)
    check(len(sections) >= 3, "%s has at least 3 cards (%d)" % (rel, len(sections)))
    for sec in sections:
        lines = sec.splitlines()
        title = lines[0].strip() if lines else "?"
        positions = []
        for label in FIELDS:
            idx = [i for i, ln in enumerate(lines) if ln.startswith(label)]
            if len(idx) != 1:
                fail("%s / %s: field %s appears %d times" % (rel, title, label, len(idx)))
                positions = None
                break
            positions.append(idx[0])
        if positions is None:
            continue
        if positions != sorted(positions):
            fail("%s / %s: fields out of order" % (rel, title))
            continue
        empty = []
        for k, label in enumerate(FIELDS):
            start = positions[k]
            end = positions[k + 1] if k + 1 < len(FIELDS) else len(lines)
            body = lines[start][len(label):] + "\n" + "\n".join(lines[start + 1:end])
            if not body.strip():
                empty.append(label)
        if empty:
            fail("%s / %s: empty fields %s" % (rel, title, ", ".join(empty)))
        else:
            ok("%s / %s: seven fields in order" % (rel, title))


def check_banned():
    for path in skill_files({".md", ".py", ".sh", ".json"}):
        text = read(path)
        rel = os.path.relpath(path, ROOT)
        check(EM_DASH not in text, "%s has no em dash" % rel)
        hits = [s for s in BANNED_EVERYWHERE if s in text]
        check(not hits, "%s has no banned strings%s" % (rel, (": " + ", ".join(hits)) if hits else ""))


def check_glossary():
    text = read(os.path.join(REFS, "glossary.md"))
    for term in GLOSSARY:
        pat = r"^(?:- |\*\*)" + re.escape(term)
        check(re.search(pat, text, re.M) is not None, "references/glossary.md defines %r" % term)


def check_template():
    text = read(os.path.join(REFS, "report-template.md"))
    m = re.search(r"^```\n(.*?)^```", text, re.S | re.M)
    if not check(m is not None, "references/report-template.md has a fenced template block"):
        return
    starts = [ln.split(":", 1)[0] for ln in m.group(1).splitlines() if ":" in ln]
    for field in TEMPLATE_FIELDS:
        check(field in starts, "report template has a %r line" % field)
    for field in TEMPLATE_FIELDS:
        check(re.search(r"^- %s: `" % re.escape(field), text, re.M) is not None,
              "report template has a filled example line for %r" % field)


def check_catalog():
    import json
    try:
        with open(os.path.join(REFS, "techniques.json"), encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError) as e:
        fail("references/techniques.json loads: %s" % e)
        return
    ids = [e.get("id") for e in entries]
    check(len(ids) == len(set(ids)), "techniques.json ids are unique")
    for name in sorted(os.listdir(REFS)):
        if not (name.startswith("cards-") and name.endswith(".md")):
            continue
        sections = dict((sec.splitlines()[0].strip(), sec) for sec in card_sections(read(os.path.join(REFS, name))))
        titles = set(e.get("title") for e in entries if e.get("file") == name)
        check(titles == set(sections), "techniques.json titles equal the headings of %s%s" % (
            name, "" if titles == set(sections) else ": %s" % sorted(titles ^ set(sections))))
        for e in entries:
            if e.get("file") != name or e.get("title") not in sections:
                continue
            for fact in e.get("facts", []):
                check(fact in sections[e["title"]], "%s: fact %r appears in its card" % (e["id"], fact))


def check_executables():
    for dirpath, _, names in os.walk(ROOT):
        if "__pycache__" in dirpath:
            continue
        for n in sorted(names):
            path = os.path.join(dirpath, n)
            rel = os.path.relpath(path, ROOT)
            with open(path, "rb") as f:
                shebang = f.read(2) == b"#!"
            executable = os.access(path, os.X_OK)
            if shebang != executable:
                fail("%s: %s" % (rel, "shebang but not executable" if shebang
                                 else "executable without a shebang"))


def check_markers(text):
    for s in ("Gate 1", "Gate 2", "IQR", "Verdict"):
        check(s in text, "SKILL.md mentions %r" % s)
    rows = [ln for ln in text.splitlines() if ln.startswith("|") and "cards-" in ln]
    good = 0
    headings = {}
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        files = [f.strip() for f in cells[-1].split(",")]
        if files and all(os.path.exists(os.path.join(REFS, f)) for f in files):
            good += 1
        else:
            fail("symptom row names a missing card file: %s" % cells[-1])
            continue
        for f in files:
            if f not in headings:
                headings[f] = card_headings(os.path.join(REFS, f))
        for name in [c.strip() for c in cells[-2].split(",")]:
            found = any(h.startswith(name) for f in files for h in headings[f])
            check(found, "symptom index card %r is a heading in %s" % (name, ", ".join(files)))
    check(good >= 10, "symptom index has %d rows naming existing card files (>= 10)" % good)


def check_domain_facts():
    sched = read(os.path.join(REFS, "cards-scheduling.md"))
    check("4/3" in sched, "cards-scheduling.md states the 4/3 bound")
    check("m >= 2" in sched or "m>=2" in sched, "cards-scheduling.md requires m >= 2")
    check("one machine" not in sched, "cards-scheduling.md does not say 'one machine'")
    check("binds" in sched, "cards-scheduling.md says which lower-bound term binds")
    ci = read(os.path.join(REFS, "cards-ci-tests.md"))
    check("18" in ci, "cards-ci-tests.md states the exact-search cutoff 18")
    check("knapsack" in ci, "cards-ci-tests.md mentions knapsack")
    meas = read(os.path.join(REFS, "measurement.md"))
    check("Expensive runs" in meas, "measurement.md covers expensive runs (CI history, replay, reruns)")
    examples = read(os.path.join(REFS, "worked-example.md"))
    for ex in ("## Example C", "## Example D"):
        check(ex in examples, "worked-example.md has %r (stop answers in plain words)" % ex)


def check_stop_answer(text):
    check("plain words" in text, "SKILL.md tells the model to answer stops in plain words")
    check("Offer to measure" in text, "SKILL.md: a stop answer offers to measure")
    check("open them before writing anything" in text,
          "SKILL.md step 0 reads supplied data before writing")


def check_code():
    with tempfile.TemporaryDirectory() as tmp:
        for path in skill_files({".py"}):
            rel = os.path.relpath(path, ROOT)
            try:
                py_compile.compile(path, cfile=os.path.join(tmp, os.path.basename(path) + "c"),
                                   doraise=True)
                ok("py_compile %s" % rel)
            except py_compile.PyCompileError as e:
                fail("py_compile %s: %s" % (rel, e))
    for path in skill_files({".sh"}):
        rel = os.path.relpath(path, ROOT)
        p = subprocess.run(["bash", "-n", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        check(p.returncode == 0, "bash -n %s" % rel)
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "bench.py"), "--help"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    check(p.returncode == 0, "scripts/bench.py --help exits 0")
    dev = os.path.join(SCRIPTS, "dev.py")
    for args, code in ((["--help"], 0), (["help"], 0), (["catalog", "--list"], 0),
                       (["nosuchcmd"], 2)):
        p = subprocess.run([sys.executable, "-I", dev] + args,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        check(p.returncode == code, "scripts/dev.py %s exits %d" % (" ".join(args), code))
    check(os.access(dev, os.X_OK), "scripts/dev.py is executable")


SCENARIO_KEYS = ["id", "title", "situation", "user_message", "fixture", "musts", "failure_modes",
                 "probes", "guards_phase1"]
PROBE_SCOPES = ["text", "final", "actions", "edits", "write_paths", "all"]


def check_evals():
    import json
    try:
        with open(os.path.join(EVALS, "scenarios.json"), encoding="utf-8") as f:
            scenarios = json.load(f)
    except (OSError, ValueError) as e:
        fail("evals/scenarios.json loads: %s" % e)
        return
    check(isinstance(scenarios, list) and len(scenarios) == 12,
          "evals/scenarios.json has 12 scenarios (%d)" % len(scenarios))
    ids = [s.get("id") for s in scenarios]
    check(ids == ["S-%s" % c for c in "ABCDEFGHIJKL"], "scenario ids are S-A..S-L in order")
    fixtures = set()
    mf = os.path.join(EVALS, "fixtures", "make_fixtures.py")
    if os.path.isfile(mf):
        fixtures = set(re.findall(r'^\s+\("([a-z-]+)", [a-z_]+, "', read(mf), re.M))
    for s in scenarios:
        sid = s.get("id", "?")
        missing = [k for k in SCENARIO_KEYS if k not in s]
        check(not missing, "%s has every key%s" % (sid, (": missing " + ", ".join(missing)) if missing else ""))
        if missing:
            continue
        check(s["musts"] and s["failure_modes"], "%s has musts and failure modes" % sid)
        check(isinstance(s["guards_phase1"], bool), "%s guards_phase1 is a boolean" % sid)
        check(s["fixture"] in fixtures, "%s fixture %r is written by make_fixtures.py" % (sid, s["fixture"]))
        pids = [p.get("id") for p in s["probes"]]
        check(len(pids) == len(set(pids)) and all(pids), "%s probe ids are unique" % sid)
        check(any(p.get("must") is True for p in s["probes"]) and any(p.get("must") is False for p in s["probes"]),
              "%s has must and failure probes" % sid)
        for p in s["probes"]:
            where = "%s/%s" % (sid, p.get("id"))
            if p.get("kind") == "regex":
                try:
                    re.compile(p.get("pattern", ""))
                    good = bool(p.get("pattern")) and p.get("scope", "all") in PROBE_SCOPES
                except re.error as e:
                    good = False
                    fail("%s regex compiles: %s" % (where, e))
                check(good and isinstance(p.get("must"), bool), "%s is a valid regex probe" % where)
            elif p.get("kind") == "command":
                check(bool(p.get("command")) and isinstance(p.get("must"), bool), "%s is a valid command probe" % where)
            else:
                fail("%s kind is regex or command (%r)" % (where, p.get("kind")))
    rubric = read(os.path.join(EVALS, "rubric.md")) if os.path.isfile(os.path.join(EVALS, "rubric.md")) else ""
    for sid in ids:
        check(("## %s " % sid) in rubric, "evals/rubric.md covers %s" % sid)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rubric.md")
        p = subprocess.run([sys.executable, "-I", os.path.join(EVALS, "grade.py"), "--rubric", "--out", out],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        check(p.returncode == 0 and os.path.isfile(out) and read(out) == rubric,
              "evals/rubric.md equals `grade.py --rubric` output (regenerate it after editing scenarios.json)")
    try:
        with open(os.path.join(EVALS, "trigger.json"), encoding="utf-8") as f:
            trig = json.load(f)
        check(len(trig.get("should_fire", [])) >= 8, "trigger.json has >= 8 should_fire prompts")
        check(len(trig.get("should_not_fire", [])) >= 8, "trigger.json has >= 8 should_not_fire prompts")
        check(len(trig.get("decoys", [])) == 3 and all(d.get("name") and d.get("description") for d in trig["decoys"]),
              "trigger.json has 3 decoy skills with names and descriptions")
    except (OSError, ValueError) as e:
        fail("evals/trigger.json loads: %s" % e)
    for name in ("README.md", "results.md"):
        check(os.path.isfile(os.path.join(EVALS, name)), "evals/%s exists" % name)
    if os.path.isfile(os.path.join(EVALS, "results.md")):
        res = read(os.path.join(EVALS, "results.md"))
        for marker in ("## Method", "## Limits", "Grader", "n per cell", "Arms"):
            check(marker in res, "evals/results.md contains %r" % marker)
    for dirpath, _, names in os.walk(EVALS):
        for n in sorted(names):
            if n.endswith(".py"):
                path = os.path.join(dirpath, n)
                with open(path, "rb") as f:
                    shebang = f.read(2) == b"#!"
                check(shebang and os.access(path, os.X_OK),
                      "%s has a shebang and is executable" % os.path.relpath(path, ROOT))


def check_tooling_commands():
    """references/tooling.md documents exactly the commands of `dev.py --help`, in its order."""
    p = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "dev.py"), "--help"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    listed = re.findall(r"^  ([a-z][a-z-]+)\s{2,}", p.stdout, re.M)
    documented = []
    for head in re.findall(r"^### (.+)$", read(os.path.join(REFS, "tooling.md")), re.M):
        documented += [c.strip() for c in head.split(" and ")]
    check(listed and documented == listed, "references/tooling.md commands equal dev.py --help in order%s" % (
        "" if documented == listed else ": doc %s vs help %s" % (documented, listed)))


def main():
    if not check(os.path.isfile(SKILL), "SKILL.md exists"):
        return 1
    text = read(SKILL)
    check_frontmatter(text)
    check_paths(text)
    for name in sorted(os.listdir(REFS)):
        if name.startswith("cards-") and name.endswith(".md"):
            check_card(os.path.join(REFS, name))
    check_banned()
    check_glossary()
    check_template()
    check_catalog()
    check_executables()
    check_markers(text)
    check_stop_answer(text)
    check_domain_facts()
    check_code()
    check_evals()
    check_tooling_commands()
    if failures:
        print("%d check(s) failed" % len(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
