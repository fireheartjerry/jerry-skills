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

FIELDS = ["**Problem shape:**", "**Precondition test:**", "**Complexity:**", "**Sketch:**",
          "**Oracle:**", "**Traps:**", "**Not when:**"]
GLOSSARY = ["fail-closed", "direct mapping", "sensitive file", "heavy job", "makespan",
            "must-keep set", "oracle", "property test", "precondition"]
# Assembled from fragments so this file never contains the strings it forbids.
BANNED_DESCRIPTION = ["Code" + "forces", "pres" + "tige"]
# The four-digit form of three thousand is banned everywhere (write it with a comma), so the
# description check and the repo-wide grep in the acceptance checklist agree.
BANNED_EVERYWHERE = ["tools/" + "dev.py", "inven" + "tion-loop", "/Us" + "ers/",
                     "/ho" + "me/", "9to" + "5", "TO" + "DO", "T" + "BD", "30" + "00"]
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
    size = os.path.getsize(SKILL)
    check(size <= 12000, "SKILL.md size %d bytes <= 12000" % size)


def check_paths(text):
    mentioned = re.findall(r"(?:references|scripts|tests)/[A-Za-z0-9_.*-]+", text)
    for rel in sorted(set(mentioned)):
        if "*" in rel:
            continue
        check(os.path.exists(os.path.join(ROOT, rel)), "mentioned path exists: %s" % rel)
    for sub in ("references", "scripts"):
        d = os.path.join(ROOT, sub)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name.startswith(".") or name == "__pycache__":
                continue
            check(name in text, "%s/%s is mentioned in SKILL.md" % (sub, name))


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
    for path in skill_files({".md", ".py", ".sh"}):
        text = read(path)
        rel = os.path.relpath(path, ROOT)
        check(EM_DASH not in text, "%s has no em dash" % rel)
        hits = [s for s in BANNED_EVERYWHERE if s in text]
        check(not hits, "%s has no banned strings%s" % (rel, (": " + ", ".join(hits)) if hits else ""))


def check_glossary(text):
    for term in GLOSSARY:
        pat = r"^(?:- |\*\*)" + re.escape(term)
        check(re.search(pat, text, re.M) is not None, "glossary defines %r" % term)


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
    check_glossary(text)
    check_markers(text)
    check_stop_answer(text)
    check_domain_facts()
    check_code()
    if failures:
        print("%d check(s) failed" % len(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
