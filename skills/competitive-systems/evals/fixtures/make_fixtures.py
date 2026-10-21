#!/usr/bin/env python3
"""Write the eval fixtures for the competitive-systems scenarios. Deterministic and offline.

usage: make_fixtures.py [--out DIR] [--only NAME[,NAME...]] [--list]

Every random draw is seeded, so two runs write byte-identical files; in `monorepo` the
commits are identical too (fixed dates, empty author email) and only .git/index differs,
because it records file timestamps. The default output directory is
eval-runs/competitive-systems/phase2-runs/fixtures when run inside the skills repository and
./eval-runs/fixtures elsewhere. Each fixture is a directory; evals/scenarios.json names the
one a scenario copies into its throwaway work directory. Standard library only, Python 3.8+.
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(HERE))


def default_out():
    """eval-runs/competitive-systems/phase2-runs/fixtures inside the skills repo, else ./eval-runs."""
    repo = os.path.dirname(os.path.dirname(SKILL))
    if os.path.isdir(os.path.join(repo, "eval-runs")) or os.path.isfile(os.path.join(repo, "scripts", "validate.py")):
        return os.path.join(repo, "eval-runs", "competitive-systems", "phase2-runs", "fixtures")
    return os.path.join(os.getcwd(), "eval-runs", "fixtures")


def write(root, rel, text, dedent=True):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if dedent:
        text = textwrap.dedent(text).lstrip("\n")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_json(root, rel, obj, indent=1):
    write(root, rel, json.dumps(obj, indent=indent, sort_keys=True), dedent=False)


# ------------------------------------------------------------------ S-A: TypeScript repo
def ts_repo(root):
    write_json(root, "package.json", {
        "name": "paging-lib", "version": "1.4.0", "private": True,
        "scripts": {"lint": "echo lint ok", "typecheck": "echo typecheck ok",
                    "test": "echo test ok"},
        "devDependencies": {"typescript": "5.4.5", "vitest": "1.6.0"}}, indent=2)
    write(root, "pnpm-lock.yaml", """
        lockfileVersion: '9.0'
        importers:
          .:
            devDependencies:
              typescript:
                specifier: 5.4.5
                version: 5.4.5
              vitest:
                specifier: 1.6.0
                version: 1.6.0
        """)
    write_json(root, "tsconfig.json", {"compilerOptions": {
        "target": "ES2020", "module": "ESNext", "strict": True, "baseUrl": ".",
        "paths": {"@/*": ["src/*"]}}, "include": ["src"]}, indent=2)
    write(root, "src/paginate.ts", """
        export function paginate<T>(items: T[], size: number): T[][] {
          const pages: T[][] = [];
          for (let start = 0; start < items.length; start += size) {
            pages.push(items.slice(start, start + size));
          }
          return pages;
        }
        """)
    write(root, "src/format.ts", """
        export function formatCount(n: number): string {
          return n.toLocaleString("en-US");
        }
        """)
    write(root, "src/index.ts", """
        export { paginate } from "./paginate";
        export { formatCount } from "./format";
        """)
    write(root, "src/paginate.test.ts", """
        import { describe, expect, it } from "vitest";
        import { paginate } from "@/index";

        describe("paginate", () => {
          it("keeps the last short page", () => {
            expect(paginate([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
          });
        });
        """)
    write(root, "src/format.test.ts", """
        import { expect, it } from "vitest";
        import { formatCount } from "./format";

        it("groups thousands", () => {
          expect(formatCount(12345)).toBe("12,345");
        });
        """)


# ------------------------------------------------------------------ S-B: Python monorepo
def git(root, *args, **env):
    full = dict(os.environ, GIT_AUTHOR_DATE=env.get("date", "2026-01-05T12:00:00+00:00"),
                GIT_COMMITTER_DATE=env.get("date", "2026-01-05T12:00:00+00:00"))
    for k in [k for k in full if k.startswith("GIT_") and k not in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")]:
        del full[k]
    cmd = ["git", "-c", "user.name=fixture", "-c", "user.email=", "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"] + list(args)
    subprocess.run(cmd, cwd=root, env=full, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def monorepo(root):
    write(root, "pyproject.toml", """
        [project]
        name = "mono"
        version = "0.1.0"

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """)
    write(root, "requirements.lock", "requests==2.32.3\npydantic==2.9.2\n")
    write(root, "conftest.py", """
        import pytest


        @pytest.fixture
        def sample_rows():
            return [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        """)
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/core.py", """
        def slug(s):
            return s.strip().lower().replace(" ", "-")


        class User:
            def __init__(self, id, name):
                self.id, self.name = id, name
        """)
    write(root, "pkg/api.py", """
        from pkg.core import User, slug


        def create_user(payload):
            return User(payload["id"], slug(payload["name"]))
        """)
    write(root, "pkg/billing.py", """
        def invoice_total(amounts):
            return round(sum(amounts), 2)
        """)
    write(root, "pkg/plugins.py", """
        import importlib


        def load(name):                  # dynamic import: invisible to a static import graph
            return importlib.import_module("pkg." + name)
        """)
    write(root, "tests/test_core.py", """
        from pkg.core import User, slug


        def test_slug():
            assert slug(" Hello World ") == "hello-world"


        def test_user():
            assert User(1, "a").name == "a"
        """)
    write(root, "tests/test_api.py", """
        from pkg.api import create_user


        def test_create_user(sample_rows):
            assert create_user(sample_rows[0]).name == "a"
        """)
    write(root, "tests/test_billing.py", """
        from pkg.billing import invoice_total


        def test_total(sample_rows):
            assert invoice_total([r["id"] for r in sample_rows]) == 3
        """)
    write(root, "tests/test_plugins.py", """
        from pkg.plugins import load


        def test_load():
            assert load("billing").invoice_total([1]) == 1
        """)
    write(root, "README.txt", """
        Python monorepo. The full pytest suite takes about 40 minutes in CI. The open PR is the
        branch `pr` against `main` (checked out). It changes pkg/core.py and conftest.py.
        """)
    if shutil.which("git") is None:
        sys.stderr.write("make_fixtures: git not found; monorepo written without history\n")
        return
    git(root, "init", "-q", "--template=")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "pr")
    write(root, "pkg/core.py", """
        def slug(s):
            return "-".join(s.strip().lower().split())


        class User:
            def __init__(self, id, name):
                self.id, self.name = id, name
        """)
    write(root, "conftest.py", """
        import pytest


        @pytest.fixture
        def sample_rows():
            return [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "c c"}]
        """)
    git(root, "commit", "-q", "-a", "-m", "slug: collapse runs of whitespace", date="2026-01-06T12:00:00+00:00")


# ------------------------------------------------------------------ S-C: noisy 8% claim
NOISY = """
    import hashlib
    import random


    def report(rows):
        total = 0
        for r in rows:
            %s
        return total


    if __name__ == "__main__":
        rng = random.Random(8)
        rows = [rng.random() for _ in range(1_200_000)]
        print(report(rows))
    """


def noisy_pair(root):
    write(root, "before.py", NOISY % "total += int(hashlib.sha256(str(r).encode()).hexdigest()[:6], 16) % 97")
    write(root, "after.py", NOISY % "total = total + int(hashlib.sha256(str(r).encode()).hexdigest()[:6], 16) % 97")
    write(root, "README.txt", """
        PR description: "report step 8% faster: 3.2 s before, 2.95 s after" (one run each on a
        laptop). before.py is main; after.py is the PR. Both print the same number.
        """)


# ------------------------------------------------------------------ S-D: 400-service registry
def registry(root):
    rng = random.Random(9)
    names = ["svc-%03d" % i for i in range(400)]
    for i, name in enumerate(names):
        deps = sorted(set(rng.sample(names[:i], min(i, rng.randint(0, 4))))) if i else []
        manifest = {"name": name, "owner": "team-%d" % (i % 12), "depends_on": deps}
        if i in (57, 233):
            manifest["depends_on"] = deps + ["legacy-billing"]    # not in the registry
        write_json(root, "registry/%s.json" % name, manifest)
    write(root, "which-services-affected.py", """
        \"\"\"Print the services affected by a change to the given services (transitive dependents).\"\"\"
        import json
        import os
        import sys

        REG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry")


        def load_all():
            out = {}
            for name in sorted(os.listdir(REG)):
                with open(os.path.join(REG, name)) as f:
                    m = json.load(f)
                out[m["name"]] = m
            return out


        def depends_on(service, target):
            \"\"\"True if service depends on target directly or transitively.\"\"\"
            manifests = load_all()                 # re-read every manifest on each call
            stack, seen = [service], set()
            while stack:
                s = stack.pop()
                for d in manifests.get(s, {}).get("depends_on", []):
                    if d == target:
                        return True
                    if d not in seen:
                        seen.add(d)
                        stack.append(d)
            return False


        def affected(changed):
            names = sorted(load_all())
            return [s for s in names if any(s == c or depends_on(s, c) for c in changed)]


        if __name__ == "__main__":
            print("\\n".join(affected(sys.argv[1:] or ["svc-010"])))
        """)
    write(root, "README.txt", """
        which-services-affected.py takes about 10 minutes in CI for the 400 services in
        registry/ (the CI runner reads manifests from a network share). Usage:
        python3 which-services-affected.py svc-010 svc-200
        """)


# ------------------------------------------------------------------ S-E: CI durations
def ci_durations(root):
    rng = random.Random(2)
    names = ["test_%s_%03d" % (rng.choice(["api", "auth", "billing", "core", "db", "ui", "worker"]), i)
             for i in range(310)]
    names = sorted(set(names))
    names = sorted(names)[:310]
    remaining = 160 * 60 - 2 * 14 * 60
    weights = [rng.lognormvariate(0, 0.8) for _ in names[:-2]]
    scale = remaining / sum(weights)
    jobs = {}
    for n, w in zip(names[:-2], weights):
        jobs[n] = max(5, int(round(w * scale)))           # clamp: no job under 5 s
    jobs[names[-2]] = 14 * 60
    jobs[names[-1]] = 14 * 60
    diff = 160 * 60 - sum(jobs.values())                  # spread the rounding remainder
    for n in names[:-2]:
        if diff == 0:
            break
        step = max(-(jobs[n] - 5), min(diff, 60)) if diff > 0 else max(diff, -(jobs[n] - 5))
        jobs[n] += step
        diff -= step
    # One invalid record (a timing bug); its two neighbours absorb the change so the raw sum
    # stays 160 minutes.
    jobs["test_api_002"], jobs["test_api_003"], jobs["test_api_004"] = -12, 17, 22
    assert len(jobs) == 310 and sum(jobs.values()) == 160 * 60
    write_json(root, "durations.json", {
        "runners": 8, "jobs": jobs,
        "note": "p50 seconds per job over the last 10 runs; current split is alphabetical"})
    write(root, "README.txt", """
        durations.json: p50 duration in seconds for each of the 310 CI jobs (last 10 runs).
        8 identical runners. Jobs are currently assigned alphabetically in contiguous slices.
        Current wall time is about 40 minutes.
        """)


# ------------------------------------------------------------------ S-F: identity-keyed cache
def identity_cache(root):
    write(root, "report.py", """
        \"\"\"Nightly report: load rows (about 70% of the time), then compute (about 30%).\"\"\"
        import random
        import time


        def load(n, seed=1):
            time.sleep(0.16)                       # stands in for the database read
            rng = random.Random(seed)
            return [{"id": i, "amount": rng.randint(1, 500), "region": rng.choice("NESW")}
                    for i in range(n)]


        def compute(rows):
            totals = {}
            for r in rows:
                totals[r["region"]] = totals.get(r["region"], 0) + r["amount"]
            for _ in range(400):                   # the real step does several passes
                for r in rows:
                    r["amount"] % 7
            return dict(sorted(totals.items()))


        def main(n=10_000):
            rows = load(n)
            return compute(rows)


        if __name__ == "__main__":
            print(main())
        """)
    write(root, "report_fast.py", """
        \"\"\"Candidate: a faster compute().\"\"\"
        from report import compute as _slow_compute

        _cache = {}


        def compute(rows):
            key = id(rows)
            if key not in _cache:
                _cache[key] = _slow_compute(rows)
            return _cache[key]
        """)
    write(root, "bench_compute.py", """
        \"\"\"Benchmark compute() old vs new: 40 calls each on one list.\"\"\"
        import time

        import report
        import report_fast

        rows = report.load(10_000)


        def timed(fn):
            t = time.perf_counter()
            for _ in range(40):
                fn(rows)
            return time.perf_counter() - t


        old = timed(report.compute)
        new = timed(report_fast.compute)
        print("old %.3f s  new %.3f s  speedup %.0fx" % (old, new, old / new))
        """)
    write(root, "README.txt", """
        The nightly report runs `python3 report.py`. The PR swaps report.compute for
        report_fast.compute. `python3 bench_compute.py` reports about 40x.
        """)


# ------------------------------------------------------------------ S-G: tiny pattern match
def tiny_patterns(root):
    rng = random.Random(7)
    pats = ["ERROR_%02d" % i for i in range(25)] + [
        "timeout", "refused", "OOMKilled", "segfault", "panic:", "Traceback", "deadlock",
        "corrupt", "retry limit", "disk full", "permission denied", "unreachable", "stale lock",
        "checksum", "ECONNRESET"]
    assert len(pats) == 40
    write(root, "patterns.txt", "\n".join(pats) + "\n", dedent=False)
    lines = []
    for i in range(2000):
        if rng.random() < 0.05:
            lines.append("2026-10-%02d host%d %s in job %d" % (
                rng.randint(1, 28), rng.randint(1, 9), rng.choice(pats), i))
        else:
            lines.append("2026-10-%02d host%d ok job %d finished in %d ms" % (
                rng.randint(1, 28), rng.randint(1, 9), i, rng.randint(1, 900)))
    write(root, "nightly.log", "\n".join(lines) + "\n", dedent=False)
    write(root, "check.py", """
        \"\"\"Nightly: flag log lines containing any known failure pattern.\"\"\"
        patterns = [p.strip() for p in open("patterns.txt") if p.strip()]
        hits = []
        for line in open("nightly.log"):
            for p in patterns:
                if p in line:
                    hits.append((p, line.rstrip()))
        print(len(hits), "hits")
        """)


# ------------------------------------------------------------------ S-H: context packer
def packer(root):
    write(root, "packer.py", """
        \"\"\"Assemble context for a coding model from a repo under a token budget.\"\"\"
        import hashlib
        import os
        import sys

        BUDGET = 60_000


        def tokens(text):
            return len(text) // 4


        def fake_embedding_score(task, text):
            # stand-in for an embedding similarity: stable but meaningless
            h = hashlib.sha256((task[:50] + text[:200]).encode()).hexdigest()
            return int(h[:8], 16) / 0xFFFFFFFF


        def chunks(root):
            for dirpath, _, names in sorted(os.walk(root)):
                for n in sorted(names):
                    p = os.path.join(dirpath, n)
                    if n.endswith((".py", ".md", ".txt", ".toml")):
                        lines = open(p).read().splitlines()
                        for i in range(0, len(lines), 120):
                            yield p, i + 1, "\\n".join(lines[i:i + 120])


        def pack(task, root):
            ranked = sorted(chunks(root), key=lambda c: -fake_embedding_score(task, c[2]))
            out = []
            for path, start, text in ranked:
                out.append("# %s:%d\\n%s" % (path, start, text))
            out.append(task)              # instructions last, next to the question
            blob = "\\n\\n".join(out)
            return blob[: BUDGET * 4]     # truncate from the end to fit the budget


        if __name__ == "__main__":
            task = open(sys.argv[1]).read()
            print(pack(task, sys.argv[2]))
        """)
    write(root, "task.txt", """
        Fix the off-by-one in `paginate()` in repo/svc/pages.py so the last page is included; keep
        repo/svc/api.py's response shape unchanged.
        Constraint: do not change the public signature of paginate().
        Run the tests with: python -m pytest repo/tests -q
        """)
    for i in range(120):
        body = "\n".join("def helper_%d_%d(x):\n    return x * %d\n" % (i, j, j) for j in range(60))
        write(root, "repo/svc/mod_%03d.py" % i, body + "\n", dedent=False)
    # paginate() spans lines 113 to 121, so it straddles the 120-line chunk
    # boundary: the must-keep range overlaps two candidate chunks of the same file.
    filler = "".join("def page_helper_%d(x):\n    return x + %d\n\n\n" % (j, j) for j in range(28))
    write(root, "repo/svc/pages.py", filler + textwrap.dedent("""
        def paginate(items, size):
            \"\"\"Split items into pages of at most `size` items.\"\"\"
            if size <= 0:
                raise ValueError("size must be positive")
            pages = []
            # bug: the range end drops the last page when len(items) % size == 1
            for start in range(0, len(items) - 1, size):
                pages.append(items[start:start + size])
            return pages


        def page_count(items, size):
            return len(paginate(items, size))
        """).lstrip("\n"), dedent=False)
    write(root, "repo/svc/api.py", """
        from .pages import paginate


        def list_items(items, size=20):
            return {"pages": paginate(items, size), "count": len(items)}
        """)
    write(root, "repo/tests/test_pages.py", """
        from svc.pages import paginate


        def test_last_page():
            assert paginate(list(range(5)), 2) == [[0, 1], [2, 3], [4]]
        """)
    write(root, "README.txt", """
        packer.py builds the prompt for a coding model from repo/ under a 60k-token budget.
        It ranks 120-line chunks by an embedding-like score and truncates the final blob from the
        end. In practice the test command and the stated constraint from task.txt get cut, and the
        two files the task names are not reliably included.
        Usage: python3 packer.py task.txt repo
        """)


# ------------------------------------------------------------------ S-I: perf history
def perf_history(root):
    rng = random.Random(37)
    rows = ["commit,median_ms"]
    for i in range(1, 61):
        v = 1000.0 * (1 + rng.gauss(0, 0.015))
        if i >= 37:
            v *= 1.12
        if i == 12:
            v *= 1.35
        rows.append("c%02d,%.1f" % (i, v))
    write(root, "perf.csv", "\n".join(rows) + "\n", dedent=False)
    write(root, "README.txt", """
        perf.csv: median wall time (ms) of the nightly report benchmark at each of the last 60
        commits on main, oldest first (c01 .. c60). Each value is the median of 10 runs.
        """)


# ------------------------------------------------------------------ S-J: I/O-bound import
def io_import(root):
    write(root, "import.py", """
        \"\"\"Import records: one API call per record.\"\"\"
        import json
        import time


        class Client:
            \"\"\"Stub of the HTTP client: each call takes about 15 ms of network time.\"\"\"

            def get_record(self, record_id):
                time.sleep(0.015)
                return {"id": record_id, "value": record_id * 7 % 13}

            def get_records(self, ids):      # the real API also accepts up to 100 ids per call
                time.sleep(0.015 + 0.0002 * len(ids))
                return [{"id": i, "value": i * 7 % 13} for i in ids]


        def run(ids):
            client = Client()
            seen = []
            out = []
            for i in ids:
                if i in seen:                # dedup of ids that occasionally repeat
                    continue
                seen.append(i)
                out.append(client.get_record(i))
            return out


        if __name__ == "__main__":
            ids = [i % 2900 for i in range(3_000)]
            t = time.time()
            rows = run(ids)
            print(json.dumps({"rows": len(rows), "seconds": round(time.time() - t, 1)}))
        """)
    write(root, "README.txt", """
        $ time python3 import.py
        {"rows": 2900, "seconds": 45.1}
        real    0m45.2s
        user    0m1.9s
        sys     0m0.4s
        """)


# ------------------------------------------------------------------ S-K: hot function at 12%
def hot_function(root):
    write(root, "go.mod", "module example.com/indexer\n\ngo 1.21\n", dedent=False)
    write(root, "keys.go", """
        package indexer

        import (
        	"sort"
        	"strings"
        )

        // normalizeKeys lower-cases, trims and sorts the keys of one record.
        func normalizeKeys(keys []string) []string {
        	out := make([]string, 0, len(keys))
        	for _, k := range keys {
        		out = append(out, strings.ToLower(strings.TrimSpace(k)))
        	}
        	sort.Strings(out)
        	return out
        }
        """)
    write(root, "index.go", """
        package indexer

        // Record is one input document.
        type Record struct {
        	ID   int
        	Keys []string
        	Body string
        }

        // BuildIndex maps each normalized key to the ids that carry it.
        func BuildIndex(records []Record) map[string][]int {
        	idx := make(map[string][]int)
        	for _, r := range records {
        		for _, k := range normalizeKeys(r.Keys) {
        			idx[k] = append(idx[k], r.ID)
        		}
        		tokenizeBody(r.Body, idx, r.ID)
        	}
        	return idx
        }

        func tokenizeBody(body string, idx map[string][]int, id int) {
        	start := -1
        	for i := 0; i <= len(body); i++ {
        		if i == len(body) || body[i] == ' ' {
        			if start >= 0 {
        				w := body[start:i]
        				idx[w] = append(idx[w], id)
        				start = -1
        			}
        		} else if start < 0 {
        			start = i
        		}
        	}
        }
        """)
    write(root, "keys_test.go", """
        package indexer

        import (
        	"reflect"
        	"testing"
        )

        func TestNormalizeKeys(t *testing.T) {
        	got := normalizeKeys([]string{" B", "a ", "C"})
        	want := []string{"a", "b", "c"}
        	if !reflect.DeepEqual(got, want) {
        		t.Fatalf("got %v want %v", got, want)
        	}
        }
        """)
    write(root, "profile.txt", """
        $ go tool pprof -top -cum cpu.prof
        File: indexer.test
        Type: cpu
        Duration: 30.12s, Total samples = 29.80s (98.94%)
              flat  flat%   sum%        cum   cum%
             0.02s 0.067% 0.067%     29.80s   100%  testing.tRunner
             0.04s  0.13%   0.2%     29.70s 99.66%  example.com/indexer.BenchmarkPipeline
             0.31s  1.04%  1.24%     17.88s 60.00%  example.com/indexer.BuildIndex
             6.20s 20.81% 22.05%     11.32s 37.99%  example.com/indexer.tokenizeBody
             4.05s 13.59% 35.64%      9.10s 30.54%  runtime.mapassign_faststr
             0.62s  2.08% 37.72%      3.58s 12.01%  example.com/indexer.normalizeKeys
             1.10s  3.69% 41.41%      2.21s  7.42%  strings.ToLower
             0.48s  1.61% 43.02%      0.91s  3.05%  sort.Strings
             8.90s 29.87% 72.89%     11.62s 38.99%  example.com/indexer.loadRecords
        """)
    write(root, "README.txt", """
        BenchmarkPipeline loads 200,000 records (loadRecords) and builds the index (BuildIndex).
        profile.txt is the CPU profile of one benchmark run.
        """)


# ------------------------------------------------------------------ S-L: exact budget
def suites(root):
    rng = random.Random(12)
    optional = []
    for i in range(40):
        optional.append({"name": "suite_%02d" % i, "p50_seconds": rng.randint(30, 420),
                         "failure_rate_90d": round(rng.random() ** 2 * 0.3, 3)})
    mandatory = [{"name": n, "p50_seconds": s} for n, s in
                 (("smoke", 120), ("contracts", 300), ("migrations", 240), ("security", 180))]
    write_json(root, "suites.json", {
        "pipeline_budget_seconds": 2340, "mandatory": mandatory,
        "values_independent": True, "optional": optional})
    write(root, "README.txt", """
        suites.json: the 4 mandatory suites (always run) and the 40 optional suites with measured
        p50 duration (integer seconds) and a value score (failure rate over 90 days). Values are
        independent (no shared coverage). The pipeline budget is 2340 s in total.
        """)


# ------------------------------------------------------------------ worked examples A and B
def example_a(root):
    for name, text in sorted(EXAMPLE_A.items()):
        write(root, name, text, dedent=False)


def example_b(root):
    ci_durations(root)
    os.remove(os.path.join(root, "README.txt"))
    nulls = lambda n: [None] * n          # noqa: E731
    write(root, "ci-runs.json", json.dumps({
        "schema": "devkit-bench/1",
        "arms": {
            "alphabetical": {"command": "CI history, last 10 runs",
                             "wall": [2410, 2290, 2500, 2350, 2610, 2438, 2330, 2520, 2390, 2460],
                             "cpu": nulls(10), "rss": nulls(10), "exit": [0] * 10,
                             "stdout_sha256": []},
            "lpt": {"command": "reruns of the same commit under the LPT plan",
                    "wall": [1236, 1250, 1212, 1262, 1230, 1241, 1225],
                    "cpu": nulls(7), "rss": nulls(7), "exit": [0] * 7, "stdout_sha256": []}},
        "meta": {}, "warnings": []}, indent=1), dedent=False)


# Example A's files, verbatim: the outputs in references/worked-example.md came from these.
EXAMPLE_A = {
    'events.py': r'''"""Pinned event generator for Example A (seed 7): about 1.7% distinct events, a few NaN ts."""
import random
from collections import namedtuple

Event = namedtuple("Event", "user_id type ts")


def events(n, seed=7):
    rng = random.Random(seed)
    d = max(1, int(0.017 * n))
    base = [(rng.randint(1, 10 ** 6), rng.choice("abcde"), float(rng.randint(-10 ** 5, 10 ** 9)))
            for _ in range(d)]
    out = []
    for i in range(n):
        u, t, ts = base[rng.randrange(d)]
        if i % 5000 == 4999:
            ts = float("nan")
        else:
            ts = ts + rng.random() * 0.99
        out.append(Event(u, t, ts))
    return out
''',
    'gen_events.py': r'''"""Random event lists for the oracle: case, whitespace, 1.0 vs 1, negative and NaN timestamps."""
from events import Event


def case(rng):
    return [Event(rng.choice([1, 2, 1.0]), rng.choice(["a", "A", " a", "b"]),
                  rng.choice([0.0, 0.5, 1, 1.0, -0.5, -1.0, -1.5, 2.9, float("nan")]))
            for _ in range(rng.randint(0, 60))]
''',
    'dedup_before.py': r'''import hashlib
import math
import sys

from events import events


def same(a, b):
    return (a.user_id == b.user_id and a.type == b.type
            and math.floor(a.ts) == math.floor(b.ts) if a.ts == a.ts and b.ts == b.ts else False)


def dedup(evs):
    kept = []
    for e in evs:
        if not any(same(e, k) for k in kept):
            kept.append(e)
    return kept


if __name__ == "__main__":
    out = dedup(events(int(sys.argv[1])))
    print(len(out), hashlib.sha256(repr(out).encode()).hexdigest()[:16])
''',
    'dedup_after.py': r'''import hashlib
import math
import sys

from events import events


def key(e):
    ts = e.ts
    if ts != ts:                       # NaN never matched anything in the old code
        return (e.user_id, e.type, "nan", id(e))
    return (e.user_id, e.type, math.floor(ts))


def dedup(evs):
    seen, out = set(), []
    for e in evs:
        k = key(e)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


if __name__ == "__main__":
    out = dedup(events(int(sys.argv[1])))
    print(len(out), hashlib.sha256(repr(out).encode()).hexdigest()[:16])
''',
    'precondition.py': r'''"""Precondition for Hash-key dedup: key(a) == key(b) exactly when same(a, b), 10,000 pairs."""
import random

from dedup_after import key
from dedup_before import same
from gen_events import case

rng = random.Random(7)
pool = [e for _ in range(400) for e in case(rng)]
checked = 0
for _ in range(10000):
    a, b = rng.sample(pool, 2)            # distinct rows: the loop never compares a row with itself
    if rng.random() < 0.3:
        b = a._replace(ts=a.ts + 0.25 if a.ts == a.ts else a.ts)   # same second, different ts
    assert same(a, b) == (key(a) == key(b)), (a, b)
    checked += 1
print("precondition: key(a) == key(b) iff same(a, b) on %d random pairs (incl. NaN): pass" % checked)
''',
    'precondition_sentinel.py': r'''"""First attempt: NaN mapped to a sentinel. Expected to fail the precondition."""
import math
import random

from dedup_before import same
from gen_events import case


def key(e):
    return (e.user_id, e.type, "nan" if e.ts != e.ts else math.floor(e.ts))


rng = random.Random(7)
pool = [e for _ in range(400) for e in case(rng)]
for i in range(10000):
    a, b = rng.sample(pool, 2)
    if same(a, b) != (key(a) == key(b)):
        print("precondition fails at pair %d: %r vs %r" % (i, a, b))
        break
else:
    print("pass")
''',
}

FIXTURES = [
    ("ts-repo", ts_repo, "S-A: TypeScript repo with a path alias and a re-export"),
    ("monorepo", monorepo, "S-B: Python monorepo, PR branch touches pkg/core.py and conftest.py"),
    ("noisy-pair", noisy_pair, "S-C: equivalent before.py and after.py"),
    ("registry", registry, "S-D: 400 service manifests and the slow impact script"),
    ("ci-durations", ci_durations, "S-E: 310 job durations, one invalid"),
    ("identity-cache", identity_cache, "S-F: candidate that caches by id(rows), harness reuses the list"),
    ("tiny-patterns", tiny_patterns, "S-G: 40 patterns, 2,000 log lines"),
    ("packer", packer, "S-H: context packer, must-keep range straddles a chunk boundary"),
    ("perf-history", perf_history, "S-I: 60 benchmark medians, +12% step at c37, outlier at c12"),
    ("io-import", io_import, "S-J: import loop over a 15 ms stub client"),
    ("hot-function", hot_function, "S-K: Go module, profile shows normalizeKeys at 12%"),
    ("suites", suites, "S-L: 4 mandatory and 40 optional suites"),
    ("example-a", example_a, "worked example A: quadratic dedup, pinned generator"),
    ("example-b", example_b, "worked example B: durations.json and CI history"),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=None, help="output directory (default: %s)" % default_out())
    ap.add_argument("--only", default="", help="comma-separated fixture names")
    ap.add_argument("--list", action="store_true", help="list fixture names and exit")
    a = ap.parse_args(argv)
    if a.list:
        for name, _, what in FIXTURES:
            print("%-15s %s" % (name, what))
        return 0
    known = [n for n, _, _ in FIXTURES]
    only = [s for s in a.only.split(",") if s]
    bad = [s for s in only if s not in known]
    if bad:
        sys.stderr.write("make_fixtures: unknown fixture(s) %s; known: %s\n" % (", ".join(bad), ", ".join(known)))
        return 2
    out = os.path.abspath(a.out or default_out())
    for name, fn, _ in FIXTURES:
        if only and name not in only:
            continue
        root = os.path.join(out, name)
        if os.path.exists(root):
            shutil.rmtree(root)
        os.makedirs(root)
        fn(root)
    print("fixtures written under %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
